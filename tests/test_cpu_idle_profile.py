"""Exercise the native A133 CPU-idle firmware profiles."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import cpu_idle_profile
import scp_profile
import usb_profile


class CpuIdleProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / '.config'
        self.config.write_bytes((ROOT / 'configs/brick_defconfig').read_bytes())
        scp_profile.configure(self.config, 'disabled')
        usb_profile.configure(self.config, 'peripheral', 'disabled')

    def test_default_is_disabled_and_selection_is_reversible(self):
        before = self.config.read_text()
        self.assertFalse(cpu_idle_profile.enabled(self.config))
        cpu_idle_profile.configure(self.config, 'qualification')
        self.assertTrue(cpu_idle_profile.enabled(self.config))
        self.assertEqual(cpu_idle_profile.selected_dtb(self.config),
                         cpu_idle_profile.DTB)
        self.assertEqual(usb_profile.profile(self.config), 'peripheral')
        cpu_idle_profile.configure(self.config, 'disabled')
        self.assertEqual(self.config.read_text(), before)

    def test_deep_profile_selects_combined_contract_and_is_reversible(self):
        before = self.config.read_text()
        scp_profile.configure(self.config, 'deep')
        usb_profile.configure(self.config, 'peripheral', 'deep')
        cpu_idle_profile.configure(self.config, 'qualification')
        self.assertEqual(cpu_idle_profile.selected_dtb(self.config),
                         cpu_idle_profile.DEEP_DTB)
        cpu_idle_profile.check_output(self.config, 'qualification')
        cpu_idle_profile.configure(self.config, 'disabled')
        self.assertEqual(cpu_idle_profile.selected_dtb(self.config),
                         usb_profile.NATIVE_DEEP_DTB)
        scp_profile.configure(self.config, 'disabled')
        usb_profile.configure(self.config, 'peripheral', 'disabled')
        self.assertEqual(self.config.read_text(), before)

    def test_startup_scp_is_not_a_complete_idle_pair(self):
        scp_profile.configure(self.config, 'startup')
        usb_profile.configure(self.config, 'peripheral', 'startup')
        with self.assertRaisesRegex(ValueError, 'disabled or deep'):
            cpu_idle_profile.configure(self.config, 'qualification')

    def test_disabled_profile_preserves_other_usb_dtb(self):
        usb_profile.configure(self.config, 'host')
        cpu_idle_profile.configure(self.config, 'disabled')
        self.assertEqual(cpu_idle_profile.selected_dtb(self.config),
                         usb_profile.DTBS['host'])
        cpu_idle_profile.check_output(self.config, 'disabled')

    def test_profile_change_requires_fresh_output_and_complete_pair(self):
        cpu_idle_profile.check_output(self.config, 'disabled')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            cpu_idle_profile.check_output(self.config, 'qualification')
        cpu_idle_profile.configure(self.config, 'qualification')
        self.config.write_text(self.config.read_text().replace(
            cpu_idle_profile.DTB, cpu_idle_profile.DEFAULT_DTB))
        with self.assertRaisesRegex(ValueError, 'complete pair'):
            cpu_idle_profile.check_output(self.config, 'qualification')
        cpu_idle_profile.check_output(self.root / 'new-config', 'qualification')

    def test_kernel_verifier_requires_exact_source_and_config(self):
        kernel = self.root / 'kernel'
        dts = kernel / 'arch/arm64/boot/dts/allwinner'
        dts.mkdir(parents=True)
        (kernel / '.config').write_text(
            'CONFIG_CPU_IDLE=y\nCONFIG_ARM_PSCI_CPUIDLE=y\n')
        source_dts = ROOT.parent / 'kernel/arch/arm64/boot/dts/allwinner'
        for name in ('sun50i-a133-trimui-brick-native-idle.dts',
                     'sun50i-a133-trimui-brick-native-deep-idle.dts',
                     'sun50i-a133-stock-psci-idle.dtsi', 'Makefile'):
            (dts / name).write_bytes((source_dts / name).read_bytes())
        cpu_idle_profile.verify_kernel(self.config, kernel)
        cpu_idle_profile.configure(self.config, 'qualification')
        cpu_idle_profile.verify_kernel(self.config, kernel)
        (kernel / '.config').write_text(
            'CONFIG_CPU_IDLE=y\n# CONFIG_ARM_PSCI_CPUIDLE is not set\n')
        with self.assertRaisesRegex(ValueError, 'CONFIG_ARM_PSCI_CPUIDLE'):
            cpu_idle_profile.verify_kernel(self.config, kernel)
        (kernel / '.config').write_text(
            'CONFIG_CPU_IDLE=y\nCONFIG_ARM_PSCI_CPUIDLE=y\n')
        idle = dts / 'sun50i-a133-stock-psci-idle.dtsi'
        idle.write_text(idle.read_text().replace('<5000>', '<4999>'))
        with self.assertRaisesRegex(ValueError, 'stock contract'):
            cpu_idle_profile.verify_kernel(self.config, kernel)

    def compile_dtb(self):
        dtc = shutil.which('dtc')
        fdtget = shutil.which('fdtget')
        if not dtc or not fdtget:
            self.skipTest('dtc and fdtget are required')
        source = '''/dts-v1/;
/ {
 chosen { trimui,cpu-idle-qualification; };
 cpus { #address-cells = <1>; #size-cells = <0>;
  cpu@0 { reg = <0>; cpu-idle-states = <&core &cluster>; };
  cpu@1 { reg = <1>; cpu-idle-states = <&core &cluster>; };
  cpu@2 { reg = <2>; cpu-idle-states = <&core &cluster>; };
  cpu@3 { reg = <3>; cpu-idle-states = <&core &cluster>; };
  idle-states { entry-method = "psci";
   core: cpu-sleep-0 { compatible = "arm,idle-state";
    arm,psci-suspend-param = <0x00010000>; entry-latency-us = <46>;
    exit-latency-us = <59>; min-residency-us = <3570>; local-timer-stop; };
   cluster: cluster-sleep-0 { compatible = "arm,idle-state";
    arm,psci-suspend-param = <0x01010000>; entry-latency-us = <47>;
    exit-latency-us = <74>; min-residency-us = <5000>; local-timer-stop; };
  };
 };
};
'''
        dtb = self.root / f'{cpu_idle_profile.DTB}.dtb'
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', dtb],
                       input=source, text=True, check=True,
                       capture_output=True)
        return dtb, fdtget

    def test_exact_compiled_contract_is_accepted(self):
        dtb, fdtget = self.compile_dtb()
        cpu_idle_profile.validate(fdtget, dtb)
        cpu_idle_profile.configure(self.config, 'qualification')
        cpu_idle_profile.install(self.root, self.config, fdtget)

    def test_combined_deep_compiled_idle_contract_is_accepted(self):
        dtb, fdtget = self.compile_dtb()
        dtb.rename(self.root / f'{cpu_idle_profile.DEEP_DTB}.dtb')
        scp_profile.configure(self.config, 'deep')
        usb_profile.configure(self.config, 'peripheral', 'deep')
        cpu_idle_profile.configure(self.config, 'qualification')
        cpu_idle_profile.install(self.root, self.config, fdtget)

    def test_disabled_profile_verifies_compiled_payload_is_idle_free(self):
        dtc = shutil.which('dtc')
        fdtget = shutil.which('fdtget')
        if not dtc or not fdtget:
            self.skipTest('dtc and fdtget are required')
        source = '''/dts-v1/;
/ { chosen {}; cpus { #address-cells = <1>; #size-cells = <0>;
 cpu@0 { reg = <0>; }; cpu@1 { reg = <1>; };
 cpu@2 { reg = <2>; }; cpu@3 { reg = <3>; };
}; };
'''
        dtb = self.root / f'{cpu_idle_profile.DEFAULT_DTB}.dtb'
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', dtb],
                       input=source, text=True, check=True,
                       capture_output=True)
        cpu_idle_profile.install(self.root, self.config, fdtget)
        fdtput = shutil.which('fdtput')
        if not fdtput:
            self.skipTest('fdtput is required')
        subprocess.run([fdtput, dtb, '/chosen',
                        cpu_idle_profile.MARKER], check=True)
        with self.assertRaisesRegex(ValueError, 'retains.*marker'):
            cpu_idle_profile.install(self.root, self.config, fdtget)

    def test_disabled_profile_rejects_compiled_idle_states(self):
        dtb, fdtget = self.compile_dtb()
        fdtput = shutil.which('fdtput')
        if not fdtput:
            self.skipTest('fdtput is required')
        subprocess.run([fdtput, '-d', dtb, '/chosen',
                        cpu_idle_profile.MARKER], check=True)
        dtb.rename(self.root / f'{cpu_idle_profile.DEFAULT_DTB}.dtb')
        with self.assertRaisesRegex(ValueError, 'advertises PSCI idle states'):
            cpu_idle_profile.install(self.root, self.config, fdtget)

    def test_wrong_state_is_rejected(self):
        dtb, fdtget = self.compile_dtb()
        fdtput = shutil.which('fdtput')
        if not fdtput:
            self.skipTest('fdtput is required')
        subprocess.run([fdtput, '-t', 'x', dtb,
                        '/cpus/idle-states/cluster-sleep-0',
                        'arm,psci-suspend-param', '10000'], check=True)
        with self.assertRaisesRegex(ValueError, 'wrong PSCI'):
            cpu_idle_profile.validate(fdtget, dtb)

    def test_tfa_patch_has_exact_separation_and_host_tests(self):
        patch = (ROOT / 'board/trimui/brick/patches/arm-trusted-firmware/'
                 '0009-feat-allwinner-add-native-A133-CPU-suspend.patch').read_text()
        required = (
            'SUNXI_A133_NATIVE_CPU_SUSPEND',
            'revision == 0U) || (revision == 3U) || (revision == 4U',
            'A133_V2_CORE_CONTROL(core)', '1U << (8U * hardware_level)',
            '0x00010000U', '0x01010000U',
            'SUNXI_A133_SUSPEND_SYSTEM',
            'sunxi_a133_system_suspend_prepare',
            'sunxi_a133_cpuidle_power_off_self',
            'test_sunxi_a133_cpuidle',
            'test_sunxi_a133_pm_dispatch',
            'sunxi_a133_pm_dispatch_early',
            'sunxi_a133_pm_dispatch_suspend',
            'sunxi_a133_pm_dispatch_finish',
        )
        for contract in required:
            self.assertIn(contract, patch)
        system_patch = (ROOT / 'board/trimui/brick/patches/arm-trusted-firmware/'
                        '0007-feat-allwinner-complete-A133-system-suspend.patch').read_text()
        self.assertIn('if (sunxi_a133_system_suspend_runtime_available())',
                      system_patch)
        self.assertIn('*psci_ops = &sunxi_native_psci_ops;', system_patch)
        self.assertIn('.validate_power_state\t\t= sunxi_a133_validate_power_state,',
                      patch)


if __name__ == '__main__':
    unittest.main()
