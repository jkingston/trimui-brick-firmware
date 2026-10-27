"""Exercise the isolated A133 Hantro G2 VP9 qualification profile."""
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
import vp9_profile


class Vp9ProfileTests(unittest.TestCase):
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
            'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV=y\n'
            '# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV is not set\n')

    def tools(self):
        bindir = ROOT / 'output/build-default/host/bin'
        tools = tuple(shutil.which(name) or bindir / name
                      for name in ('dtc', 'fdtget', 'fdtput'))
        if not all(Path(tool).is_file() for tool in tools):
            self.skipTest('dtc, fdtget and fdtput are required')
        return tuple(map(str, tools))

    def compile_dtb(self, *, irq=90, domain=True, iommus=False,
                    ve='disabled', iommu='disabled', g2d='disabled',
                    ce='disabled', mm_sram='disabled', ve_sram='disabled'):
        dtc, _, _ = self.tools()
        owner = 'allwinner,video-engine-domain = <&ve_domain>;' if domain else ''
        iommu_property = 'iommus = <&iommu 7>;' if iommus else ''
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
   reg = <0x030f0000 0x1000>; #iommu-cells = <1>; status = "{iommu}"; }};
  g2d@6480000 {{ compatible = "allwinner,sun50i-a133-g2d";
   reg = <0x06480000 0x40000>; status = "{g2d}"; }};
  video-codec@1c0e000 {{ compatible = "allwinner,sun50i-a133-video-engine";
   reg = <0x01c0e000 0x1000>; status = "{ve}"; }};
  video-codec@1c00000 {{ compatible = "allwinner,sun50i-a133-vpu-g2";
   reg = <0x01c00000 0x1000>; interrupts = <0 {irq} 4>;
   {owner} {iommu_property} status = "disabled"; }};
  crypto@1904000 {{ compatible = "allwinner,sun50i-a133-crypto";
   reg = <0x01904000 0xa0>; status = "{ce}"; }};
  syscon@3000000 {{ compatible = "syscon"; reg = <0x03000000 0x1000>;
   #address-cells = <1>; #size-cells = <1>; ranges;
   sram@24000 {{ compatible = "mmio-sram"; reg = <0x24000 0x21000>;
    #address-cells = <1>; #size-cells = <1>; ranges = <0 0x24000 0x21000>;
    mm_sram_node: sram-section@0 {{ compatible = "allwinner,sun50i-a133-sram-c";
     reg = <0 0x21000>; status = "{mm_sram}"; }}; }};
   sram@1a00000 {{ compatible = "mmio-sram"; reg = <0x01a00000 0x200000>;
    #address-cells = <1>; #size-cells = <1>; ranges = <0 0x01a00000 0x200000>;
    status = "{ve_sram}";
    ve_sram_node: sram-section@0 {{ compatible = "allwinner,sun50i-a133-ve-sram";
     reg = <0 0x200000>; status = "{ve_sram}"; }}; }};
  }};
 }};
}};
'''
        output = self.root / vp9_profile.DTB
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', str(output)],
                       input=source, text=True, check=True, capture_output=True)
        return output

    def test_default_is_disabled_and_configuration_is_reversible(self):
        before = self.config.read_text()
        self.assertFalse(vp9_profile.enabled(self.config))
        vp9_profile.configure(self.config, 'qualification')
        self.assertTrue(vp9_profile.enabled(self.config))
        self.assertEqual(vp9_profile._fragment(self.config), vp9_profile.FRAGMENT)
        self.assertIn('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y',
                      self.config.read_text())
        vp9_profile.configure(self.config, 'disabled')
        self.assertEqual(self.config.read_text(), before)

    def test_disabled_vp9_preserves_ve_device_manager(self):
        self.config.write_text(self.config.read_text().replace(
            '# BR2_BRICK_VE_QUALIFICATION is not set',
            'BR2_BRICK_VE_QUALIFICATION=y'))
        vp9_profile.configure(self.config, 'disabled')
        self.assertIn('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y',
                      self.config.read_text())
        vp9_profile.check_output(self.config, 'disabled')

    def test_profile_change_requires_fresh_output(self):
        vp9_profile.check_output(self.config, 'disabled')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            vp9_profile.check_output(self.config, 'qualification')
        vp9_profile.check_output(self.root / 'new-config', 'qualification')

    def test_install_enables_only_vp9_and_marks_no_iommu(self):
        dtb = self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        vp9_profile.configure(self.config, 'qualification')
        vp9_profile.install(self.root, self.config, fdtget, fdtput)
        vp9_profile.validate(fdtget, dtb, True, marker=True)

    def test_wrong_contract_is_rejected_without_mutation(self):
        _, fdtget, fdtput = self.tools()
        vp9_profile.configure(self.config, 'qualification')
        for arguments, message in (({'irq': 91}, 'SPI 90'),
                                   ({'domain': False}, 'hardware contract'),
                                   ({'iommus': True}, 'without an IOMMU')):
            with self.subTest(message=message):
                dtb = self.compile_dtb(**arguments)
                before = dtb.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    vp9_profile.install(self.root, self.config, fdtget, fdtput)
                self.assertEqual(dtb.read_bytes(), before)

    def test_enabled_peer_is_rejected_without_mutation(self):
        _, fdtget, fdtput = self.tools()
        vp9_profile.configure(self.config, 'qualification')
        for arguments in ({'ve': 'okay'}, {'iommu': 'okay'}, {'g2d': 'okay'},
                          {'ce': 'okay'}, {'mm_sram': 'okay'}, {'ve_sram': 'okay'}):
            with self.subTest(arguments=arguments):
                dtb = self.compile_dtb(**arguments)
                before = dtb.read_bytes()
                with self.assertRaisesRegex(ValueError, 'isolate'):
                    vp9_profile.install(self.root, self.config, fdtget, fdtput)
                self.assertEqual(dtb.read_bytes(), before)

    def test_profile_requires_peripheral_usb_and_no_other_qualification(self):
        self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        vp9_profile.configure(self.config, 'qualification')
        cases = []
        usb_profile.configure(self.config, 'host')
        cases.append(('USB gadget', self.config.read_text()))
        usb_profile.configure(self.config, 'peripheral')
        emmc_recovery.configure(self.config, '1')
        cases.append(('eMMC', self.config.read_text()))
        emmc_recovery.configure(self.config, '0')
        text = self.config.read_text().replace(
            '# BR2_PACKAGE_BRICK_SCP is not set', 'BR2_PACKAGE_BRICK_SCP=y')
        cases.append(('SCP', text))
        text = self.config.read_text().replace(
            '# BR2_BRICK_G2D_QUALIFICATION is not set',
            'BR2_BRICK_G2D_QUALIFICATION=y')
        cases.append(('peer accelerator', text))
        for message, text in cases:
            with self.subTest(message=message):
                self.config.write_text(text)
                with self.assertRaisesRegex(ValueError, message):
                    vp9_profile.install(self.root, self.config, fdtget, fdtput)

    def test_kernel_verifier_requires_builtins_and_shared_domain_contract(self):
        vp9_profile.configure(self.config, 'qualification')
        kernel = self.root / 'kernel'
        objects = (
            'drivers/media/platform/verisilicon/hantro_drv.o',
            'drivers/media/platform/verisilicon/sunxi_vpu_hw.o',
            'drivers/media/platform/verisilicon/hantro_g2_vp9_dec.o',
            'drivers/soc/sunxi/sun50i-a133-ve-domain.o')
        for relative in objects:
            path = kernel / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'object')
        (kernel / '.config').write_text(''.join(
            f'{key}=y\n' for key in (
                'CONFIG_VIDEO_HANTRO', 'CONFIG_VIDEO_HANTRO_SUNXI',
                'CONFIG_V4L2_VP9', 'CONFIG_SUN50I_A133_VE_DOMAIN')))
        (kernel / 'drivers/media/platform/verisilicon/hantro_drv.c').write_text(
            'allwinner,sun50i-a133-vpu-g2\n')
        (kernel / 'drivers/media/platform/verisilicon/sunxi_vpu_hw.c').write_text(
            '.job_acquire = sunxi_a133_vpu_job_acquire,\n'
            '.job_release = sunxi_a133_vpu_job_release,\n'
            'sun50i_a133_ve_domain_reset(vpu->variant_priv, vpu->dev);\n')
        domain = kernel / 'drivers/soc/sunxi/sun50i-a133-ve-domain.c'
        domain.write_text('#define A133_VE_RATE 432000000\n'
                          'clk_set_rate(domain->clocks[1].clk, A133_VE_RATE)\n')
        vp9_profile.verify_kernel(self.config, kernel)
        domain.write_text(domain.read_text().replace('432000000', '300000000'))
        with self.assertRaisesRegex(ValueError, '432000000'):
            vp9_profile.verify_kernel(self.config, kernel)


class Vp9TargetTests(unittest.TestCase):
    def test_fixture_and_bounded_runner(self):
        package = ROOT / 'package/brick-accelerator-qualification'
        fixture = package / 'fixtures/vp9/vp9-ip.ivf'
        fixture_manifest = package / 'fixtures/vp9/FIXTURE-SHA256'
        reference_manifest = package / 'fixtures/vp9/REFERENCE-SHA256'
        import hashlib
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(),
                         '6f9849001939af34e3d867a109289890c9585ca3eae802bd7ffb2143242ed70d')
        self.assertEqual(
            fixture_manifest.read_text(),
            '6f9849001939af34e3d867a109289890c9585ca3eae802bd7ffb2143242ed70d  '
            'vp9-ip.ivf\n')
        self.assertEqual(
            reference_manifest.read_text(),
            '53298844f62b9b0a64dc210626dbb89f0c6c421bd9d5b8fa7050cf5e954cf08f  '
            'vp9-ip.reference.yuv\n')
        script = (package / 'brick-qualify-vp9').read_text()
        subprocess.run(['/bin/sh', '-n', package / 'brick-qualify-vp9'], check=True)
        for required in ('isolated-no-iommu', '432000000', 'pll-ve',
                         'hantro-vpu', 'sun50i-a133-ve-domain',
                         'timeout 120 v4l2-compliance',
                         'timeout 30 gst-launch-1.0', 'v4l2slvp9dec',
                         'VP9 unexpectedly acquired an IOMMU mapping'):
            self.assertIn(required, script)


if __name__ == '__main__':
    unittest.main()
