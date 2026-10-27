"""Exercise the isolated A133 video-engine qualification profile."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import emmc_recovery
import scp_profile
import usb_profile
import ve_profile


class VeProfileTests(unittest.TestCase):
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
            '# BR2_BRICK_VP9_QUALIFICATION is not set\n'
            '# BR2_BRICK_CE_TRNG_QUALIFICATION is not set\n'
            'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV=y\n'
            '# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV is not set\n'
            '# BR2_BRICK_VE_QUALIFICATION is not set\n')

    def tools(self):
        bindir = ROOT / 'output/build-default/host/bin'
        fdtget = shutil.which('fdtget') or bindir / 'fdtget'
        fdtput = shutil.which('fdtput') or bindir / 'fdtput'
        dtc = shutil.which('dtc') or bindir / 'dtc'
        if not all(Path(tool).is_file() for tool in (fdtget, fdtput, dtc)):
            self.skipTest('dtc, fdtget and fdtput are required')
        return str(dtc), str(fdtget), str(fdtput)

    def compile_dtb(self, master=2, iommu_cells=1, g2d='disabled',
                    vp9_domain=True):
        dtc, _, _ = self.tools()
        vp9_owner = 'allwinner,video-engine-domain = <&ve_domain>;' \
            if vp9_domain else ''
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
   #iommu-cells = <{iommu_cells}>; status = "disabled"; }};
  video-codec@1c0e000 {{ compatible = "allwinner,sun50i-a133-video-engine";
   reg = <0x01c0e000 0x1000>; interrupts = <0 94 4>;
   allwinner,video-engine-domain = <&ve_domain>;
   allwinner,sram = <&mm_sram 1>, <&ve_sram 1>; iommus = <&iommu {master}>;
   status = "disabled"; }};
  video-codec@1c00000 {{ compatible = "allwinner,sun50i-a133-vpu-g2";
   reg = <0x01c00000 0x1000>; interrupts = <0 90 4>;
   {vp9_owner} status = "disabled"; }};
  g2d@6480000 {{ compatible = "allwinner,sun50i-a133-g2d";
   reg = <0x06480000 0x40000>; status = "{g2d}"; }};
  crypto@1904000 {{ compatible = "allwinner,sun50i-a133-crypto";
   reg = <0x01904000 0xa0>; status = "disabled"; }};
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
        output = self.root / ve_profile.DTB
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', str(output)],
                       input=source, text=True, check=True, capture_output=True)
        return output

    def test_default_is_disabled_and_configuration_is_reversible(self):
        before = self.config.read_text()
        self.assertFalse(ve_profile.enabled(self.config))
        ve_profile.configure(self.config, 'qualification')
        self.assertTrue(ve_profile.enabled(self.config))
        self.assertIn('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y',
                      self.config.read_text())
        ve_profile.configure(self.config, 'disabled')
        self.assertEqual(self.config.read_text(), before)

    def test_profile_change_requires_fresh_output(self):
        ve_profile.check_output(self.config, 'disabled')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            ve_profile.check_output(self.config, 'qualification')
        ve_profile.check_output(self.root / 'new-config', 'qualification')

    def test_disabled_ve_preserves_vp9_device_manager(self):
        self.config.write_text(self.config.read_text().replace(
            '# BR2_BRICK_VP9_QUALIFICATION is not set',
            'BR2_BRICK_VP9_QUALIFICATION=y'))
        ve_profile.configure(self.config, 'disabled')
        self.assertIn('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y',
                      self.config.read_text())
        ve_profile.check_output(self.config, 'disabled')

    def test_install_enables_exact_ve_chain_and_marker(self):
        dtb = self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        ve_profile.configure(self.config, 'qualification')
        ve_profile.install(self.root, self.config, fdtget, fdtput)
        ve_profile.validate(fdtget, dtb, True, marker=True)
        self.assertEqual(
            ve_profile._get(fdtget, dtb, ve_profile.VE,
                            ve_profile.ENCODER_MARKER),
            ve_profile.ENCODER_CONTRACT)

    def test_kernel_verification_requires_bounded_encoder_and_shared_domain(self):
        kernel = self.root / 'kernel'
        objects = (
            'drivers/staging/media/sunxi/cedrus/cedrus.o',
            'drivers/staging/media/sunxi/cedrus/cedrus_a133_enc_backend.o',
            'drivers/staging/media/sunxi/cedrus/cedrus_enc_video.o',
            'drivers/soc/sunxi/sun50i-a133-ve-domain.o',
        )
        for relative in objects:
            path = kernel / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (kernel / '.config').write_text(
            'CONFIG_VIDEO_SUNXI_CEDRUS=y\n'
            'CONFIG_VIDEO_SUNXI_CEDRUS_ENCODER_CORE=y\n'
            'CONFIG_VIDEO_SUNXI_CEDRUS_A133_ENCODER_QUALIFICATION=y\n'
            'CONFIG_SUN50I_A133_VE_DOMAIN=y\n')
        cedrus = kernel / 'drivers/staging/media/sunxi/cedrus/cedrus.c'
        cedrus.write_text('.enc_ops = CEDRUS_A133_QUALIFICATION_OPS,\n')
        hardware = kernel / 'drivers/staging/media/sunxi/cedrus/cedrus_hw.c'
        hardware.write_text(
            '"allwinner,encoder-qualification"\n'
            '"bounded-320x240-h264-qp30-jpeg-q95"\n')
        ve_profile.configure(self.config, 'qualification')
        ve_profile.verify_kernel(self.config, kernel)
        (kernel / '.config').write_text(
            (kernel / '.config').read_text().replace(
                'CONFIG_VIDEO_SUNXI_CEDRUS_A133_ENCODER_QUALIFICATION=y',
                '# CONFIG_VIDEO_SUNXI_CEDRUS_A133_ENCODER_QUALIFICATION is not set'))
        with self.assertRaisesRegex(ValueError, 'not built'):
            ve_profile.verify_kernel(self.config, kernel)

    def test_wrong_iommu_master_is_rejected_without_mutating_image(self):
        dtb = self.compile_dtb(master=5)
        before = dtb.read_bytes()
        _, fdtget, fdtput = self.tools()
        ve_profile.configure(self.config, 'qualification')
        with self.assertRaisesRegex(ValueError, 'IOMMU master 2'):
            ve_profile.install(self.root, self.config, fdtget, fdtput)
        self.assertEqual(dtb.read_bytes(), before)

    def test_wrong_iommu_cell_count_is_rejected_without_mutating_image(self):
        dtb = self.compile_dtb(iommu_cells=2)
        before = dtb.read_bytes()
        _, fdtget, fdtput = self.tools()
        ve_profile.configure(self.config, 'qualification')
        with self.assertRaisesRegex(ValueError, 'one-cell master IDs'):
            ve_profile.install(self.root, self.config, fdtget, fdtput)
        self.assertEqual(dtb.read_bytes(), before)

    def test_enabled_peer_accelerator_is_rejected(self):
        dtb = self.compile_dtb(g2d='okay')
        before = dtb.read_bytes()
        _, fdtget, fdtput = self.tools()
        ve_profile.configure(self.config, 'qualification')
        with self.assertRaisesRegex(ValueError, 'isolate'):
            ve_profile.install(self.root, self.config, fdtget, fdtput)
        self.assertEqual(dtb.read_bytes(), before)

    def test_missing_vp9_shared_domain_is_rejected_without_mutating_image(self):
        dtb = self.compile_dtb(vp9_domain=False)
        before = dtb.read_bytes()
        _, fdtget, fdtput = self.tools()
        ve_profile.configure(self.config, 'qualification')
        with self.assertRaisesRegex(ValueError, 'VP9 peer lacks'):
            ve_profile.install(self.root, self.config, fdtget, fdtput)
        self.assertEqual(dtb.read_bytes(), before)

    def test_profile_requires_peripheral_usb_and_no_other_qualification(self):
        self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        ve_profile.configure(self.config, 'qualification')
        cases = []
        usb_profile.configure(self.config, 'host')
        cases.append(('USB gadget', self.config.read_text()))
        usb_profile.configure(self.config, 'peripheral')
        emmc_recovery.configure(self.config, '1')
        cases.append(('eMMC', self.config.read_text()))
        emmc_recovery.configure(self.config, '0')
        scp_profile.configure(self.config, 'startup')
        cases.append(('SCP', self.config.read_text()))
        scp_profile.configure(self.config, 'disabled')
        self.config.write_text(self.config.read_text().replace(
            '# BR2_BRICK_G2D_QUALIFICATION is not set',
            'BR2_BRICK_G2D_QUALIFICATION=y'))
        cases.append(('G2D', self.config.read_text()))
        for message, text in cases:
            with self.subTest(message=message):
                self.config.write_text(text)
                with self.assertRaisesRegex(ValueError, message):
                    ve_profile.install(self.root, self.config, fdtget, fdtput)


if __name__ == '__main__':
    unittest.main()
