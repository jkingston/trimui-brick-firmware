"""Exercise the stock-wired combined A133 accelerator qualification profile."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import combined_accelerator_profile as combined
import emmc_recovery
import usb_profile


class CombinedAcceleratorProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / '.config'
        self.config.write_text(
            'BR2_LINUX_KERNEL_INTREE_DTS_NAME="allwinner/sun50i-a133-trimui-brick"\n'
            'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=""\n'
            '# BR2_PACKAGE_BRICK_EMMC_RECOVERY is not set\n'
            '# BR2_PACKAGE_BRICK_SCP is not set\n'
            '# BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND is not set\n'
            '# BR2_BRICK_G2D_QUALIFICATION is not set\n'
            '# BR2_BRICK_VE_QUALIFICATION is not set\n'
            '# BR2_BRICK_VP9_QUALIFICATION is not set\n'
            '# BR2_BRICK_CE_TRNG_QUALIFICATION is not set\n'
            '# BR2_BRICK_COMBINED_ACCELERATOR_QUALIFICATION is not set\n'
            'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV=y\n'
            '# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV is not set\n')

    def tools(self):
        bindir = ROOT / 'output/build-default/host/bin'
        tools = tuple(shutil.which(name) or bindir / name
                      for name in ('dtc', 'fdtget', 'fdtput'))
        if not all(Path(tool).is_file() for tool in tools):
            self.skipTest('dtc, fdtget and fdtput are required')
        return tuple(map(str, tools))

    def compile_dtb(self, *, g2d_master=5):
        dtc, _, _ = self.tools()
        source = f'''/dts-v1/;
/ {{ #address-cells = <1>; #size-cells = <1>; chosen {{ }};
 ve_domain: video-engine-domain {{
  compatible = "allwinner,sun50i-a133-video-engine-domain";
  clocks = <&ccu 0x29>, <&ccu 0x28>, <&ccu 0x34>;
  clock-names = "ahb", "mod", "ram"; resets = <&ccu 5>;
 }};
 soc {{ #address-cells = <1>; #size-cells = <1>; ranges;
  ccu: clock@3001000 {{ compatible = "allwinner,sun50i-a100-ccu";
   reg = <0x03001000 0x1000>; #clock-cells = <1>; #reset-cells = <1>; }};
  iommu: iommu@30f0000 {{ compatible = "allwinner,sun50i-a133-iommu";
   reg = <0x030f0000 0x1000>; interrupts = <0 66 4>; clocks = <&ccu 0x32>;
   #iommu-cells = <1>; status = "disabled"; }};
  g2d@6480000 {{ compatible = "allwinner,sun50i-a133-g2d";
   reg = <0x06480000 0x40000>; interrupts = <0 91 4>;
   clocks = <&ccu 0x23>, <&ccu 0x22>, <&ccu 0x39>;
   clock-names = "bus", "mod", "ram"; resets = <&ccu 2>;
   assigned-clocks = <&ccu 0x22>; assigned-clock-parents = <&ccu 4>;
   assigned-clock-rates = <300000000>; iommus = <&iommu {g2d_master}>;
   status = "disabled"; }};
  video-codec@1c0e000 {{ compatible = "allwinner,sun50i-a133-video-engine";
   reg = <0x01c0e000 0x1000>; interrupts = <0 94 4>;
   allwinner,video-engine-domain = <&ve_domain>;
   allwinner,sram = <&mm_sram 1>, <&ve_sram 1>; iommus = <&iommu 2>;
   status = "disabled"; }};
  video-codec@1c00000 {{ compatible = "allwinner,sun50i-a133-vpu-g2";
   reg = <0x01c00000 0x1000>; interrupts = <0 90 4>;
   allwinner,video-engine-domain = <&ve_domain>; status = "disabled"; }};
  crypto@1904000 {{ compatible = "allwinner,sun50i-a133-crypto";
   reg = <0x01904000 0xa0>; interrupts = <0 92 1>;
   clocks = <&ccu 39>, <&ccu 38>, <&ccu 53>;
   clock-names = "bus", "mod", "ram"; resets = <&ccu 4>;
   status = "disabled"; }};
  syscon@3000000 {{ compatible = "syscon"; reg = <0x03000000 0x1000>;
   #address-cells = <1>; #size-cells = <1>; ranges;
   sram@24000 {{ compatible = "mmio-sram"; reg = <0x24000 0x21000>;
    #address-cells = <1>; #size-cells = <1>; ranges = <0 0x24000 0x21000>;
    mm_sram: sram-section@0 {{ compatible = "allwinner,sun50i-a133-sram-c";
     reg = <0 0x21000>; status = "disabled"; }}; }};
   sram@1a00000 {{ compatible = "mmio-sram"; reg = <0x01a00000 0x200000>;
    #address-cells = <1>; #size-cells = <1>; ranges = <0 0x01a00000 0x200000>;
    status = "disabled";
    ve_sram: sram-section@0 {{ compatible = "allwinner,sun50i-a133-ve-sram";
     reg = <0 0x200000>; status = "disabled"; }}; }};
  }};
 }};
}};
'''
        output = self.root / combined.DTB
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', str(output)],
                       input=source, text=True, check=True, capture_output=True)
        return output

    def test_configuration_is_reversible_and_uses_exact_fragment(self):
        before = self.config.read_text()
        combined.configure(self.config, 'qualification')
        self.assertTrue(combined.enabled(self.config))
        self.assertEqual(combined._fragment(self.config), combined.FRAGMENT)
        self.assertIn('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y',
                      self.config.read_text())
        combined.check_output(self.config, 'qualification')
        combined.configure(self.config, 'disabled')
        self.assertEqual(self.config.read_text(), before)

    def test_disabled_combined_profile_preserves_video_device_manager(self):
        self.config.write_text(self.config.read_text().replace(
            '# BR2_BRICK_VE_QUALIFICATION is not set',
            'BR2_BRICK_VE_QUALIFICATION=y'))
        combined.configure(self.config, 'disabled')
        self.assertIn('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y',
                      self.config.read_text())
        combined.check_output(self.config, 'disabled')

    def test_install_enables_all_reviewed_contracts_atomically(self):
        dtb = self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        combined.configure(self.config, 'qualification')
        combined.install(self.root, self.config, fdtget, fdtput)
        combined.validate_active(fdtget, dtb)

    def test_wrong_contract_is_rejected_without_mutation(self):
        dtb = self.compile_dtb(g2d_master=4)
        before = dtb.read_bytes()
        _, fdtget, fdtput = self.tools()
        combined.configure(self.config, 'qualification')
        with self.assertRaisesRegex(ValueError, 'master 5'):
            combined.install(self.root, self.config, fdtget, fdtput)
        self.assertEqual(dtb.read_bytes(), before)

    def test_install_requires_peripheral_usb_and_no_emmc_scp_or_isolated_peer(self):
        self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        combined.configure(self.config, 'qualification')
        cases = []
        usb_profile.configure(self.config, 'host')
        cases.append(('USB gadget', self.config.read_text()))
        usb_profile.configure(self.config, 'peripheral')
        emmc_recovery.configure(self.config, '1')
        cases.append(('eMMC', self.config.read_text()))
        emmc_recovery.configure(self.config, '0')
        cases.append(('SCP', self.config.read_text().replace(
            '# BR2_PACKAGE_BRICK_SCP is not set', 'BR2_PACKAGE_BRICK_SCP=y')))
        cases.append(('isolated', self.config.read_text().replace(
            '# BR2_BRICK_G2D_QUALIFICATION is not set',
            'BR2_BRICK_G2D_QUALIFICATION=y')))
        for message, text in cases:
            with self.subTest(message=message):
                self.config.write_text(text)
                with self.assertRaisesRegex(ValueError, message):
                    combined.install(self.root, self.config, fdtget, fdtput)


class CombinedAcceleratorWiringTests(unittest.TestCase):
    def test_fragment_enables_every_qualification_only_kernel_feature(self):
        fragment = (ROOT / 'board/trimui/brick/'
                    'combined-accelerators.fragment').read_text().splitlines()
        for option in (
                'CONFIG_VIDEO_SUNXI_CEDRUS_ENCODER_CORE=y',
                'CONFIG_VIDEO_SUNXI_CEDRUS_A133_ENCODER_QUALIFICATION=y'):
            self.assertIn(option, fragment)

    def test_all_bounded_runners_accept_only_the_versioned_combined_marker(self):
        paths = (
            ROOT / 'package/brick-accelerator-qualification/brick-qualify-g2d',
            ROOT / 'package/brick-accelerator-qualification/brick-qualify-ve',
            ROOT / 'package/brick-accelerator-qualification/brick-qualify-vp9',
            ROOT / 'package/brick-ce-trng-qualification/brick-qualify-ce-trng',
            ROOT / 'package/brick-encoder-qualification/run-encoder-qualification',
        )
        for path in paths:
            with self.subTest(path=path.name):
                subprocess.run(['/bin/sh', '-n', path], check=True)
                text = path.read_text()
                self.assertIn('trimui,accelerator-qualification', text)
                self.assertIn('combined-v1', text)

    def test_combined_runner_covers_every_engine_and_shared_coexistence(self):
        path = (ROOT / 'package/brick-accelerator-qualification/'
                'brick-qualify-accelerators')
        subprocess.run(['/bin/sh', '-n', path], check=True)
        text = path.read_text()
        for contract in (
                'brick-qualify-g2d', 'brick-qualify-ve probe',
                'mpeg2-ipb h264-ip hevc-ip hevc-tiles hevc-wpp vp8-ip',
                'brick-qualify-vp9 probe', 'brick-qualify-vp9 decode',
                'run-encoder-qualification', 'brick-qualify-ce-trng core',
                'brick-qualify-ce-trng lifecycle', 'coexist-g2d',
                'coexist-ve', 'coexist-vp9', 'coexist-ce',
                'iommu.*fault', 'video-engine-domain'):
            self.assertIn(contract, text)


if __name__ == '__main__':
    unittest.main()
