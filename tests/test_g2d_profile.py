"""Exercise the isolated stock-wired A133 G2D qualification profile."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import g2d_profile
import ve_profile


class G2dProfileTests(unittest.TestCase):
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
            '# BR2_BRICK_VE_QUALIFICATION is not set\n')

    def tools(self):
        bindir = ROOT / 'output/build-default/host/bin'
        tools = tuple(shutil.which(name) or bindir / name
                      for name in ('dtc', 'fdtget', 'fdtput'))
        if not all(Path(tool).is_file() for tool in tools):
            self.skipTest('dtc, fdtget and fdtput are required')
        return tuple(map(str, tools))

    def compile_dtb(self, master=5, rate=300000000, ve='disabled'):
        dtc, _, _ = self.tools()
        source = f'''/dts-v1/;
/ {{ #address-cells = <1>; #size-cells = <1>; chosen {{ }};
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
   assigned-clock-rates = <{rate}>; iommus = <&iommu {master}>;
   status = "disabled"; }};
  video-codec@1c0e000 {{ compatible = "allwinner,sun50i-a133-video-engine";
   reg = <0x01c0e000 0x1000>; status = "{ve}"; }};
  crypto@1904000 {{ compatible = "allwinner,sun50i-a133-crypto";
   reg = <0x01904000 0xa0>; status = "disabled"; }};
 }};
}};
'''
        output = self.root / g2d_profile.DTB
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', str(output)],
                       input=source, text=True, check=True, capture_output=True)
        return output

    def test_default_is_disabled_and_configuration_is_reversible(self):
        before = self.config.read_text()
        self.assertFalse(g2d_profile.enabled(self.config))
        g2d_profile.configure(self.config, 'qualification')
        self.assertTrue(g2d_profile.enabled(self.config))
        g2d_profile.configure(self.config, 'disabled')
        self.assertEqual(self.config.read_text(), before)

    def test_profile_change_requires_fresh_output(self):
        g2d_profile.check_output(self.config, 'disabled')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            g2d_profile.check_output(self.config, 'qualification')
        g2d_profile.check_output(self.root / 'new-config', 'qualification')

    def test_install_enables_only_iommu_and_g2d(self):
        dtb = self.compile_dtb()
        _, fdtget, fdtput = self.tools()
        g2d_profile.configure(self.config, 'qualification')
        g2d_profile.install(self.root, self.config, fdtget, fdtput)
        g2d_profile.validate(fdtget, dtb, True, marker=True)

    def test_wrong_master_or_clock_is_rejected_without_mutation(self):
        _, fdtget, fdtput = self.tools()
        g2d_profile.configure(self.config, 'qualification')
        for master, rate, message in ((2, 300000000, 'master 5'),
                                      (5, 432000000, '300 MHz')):
            with self.subTest(message=message):
                dtb = self.compile_dtb(master=master, rate=rate)
                before = dtb.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    g2d_profile.install(self.root, self.config, fdtget, fdtput)
                self.assertEqual(dtb.read_bytes(), before)

    def test_enabled_peer_or_ve_profile_is_rejected(self):
        _, fdtget, fdtput = self.tools()
        g2d_profile.configure(self.config, 'qualification')
        dtb = self.compile_dtb(ve='okay')
        with self.assertRaisesRegex(ValueError, 'isolate'):
            g2d_profile.install(self.root, self.config, fdtget, fdtput)
        self.compile_dtb()
        text = self.config.read_text().replace(
            '# BR2_BRICK_VE_QUALIFICATION is not set',
            'BR2_BRICK_VE_QUALIFICATION=y')
        self.config.write_text(text)
        with self.assertRaisesRegex(ValueError, 'remain separate'):
            g2d_profile.install(self.root, self.config, fdtget, fdtput)


if __name__ == '__main__':
    unittest.main()
