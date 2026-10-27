"""Exercise the opt-in eMMC DT and early read-only policy."""
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import emmc_recovery


class RecoveryProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'config'
        self.config.write_bytes((ROOT / 'configs/brick_defconfig').read_bytes())

    def compile_dtb(self, status='disabled', hs200=False):
        dtc = shutil.which('dtc')
        if not dtc:
            self.skipTest('dtc is required')
        source = f'''/dts-v1/;
/ {{ compatible = "trimui,brick"; #address-cells = <1>; #size-cells = <1>;
 chosen {{ }};
 soc {{ #address-cells = <1>; #size-cells = <1>; ranges;
 mmc@4022000 {{
  compatible = "allwinner,sun50i-a133-emmc";
  reg = <0x4022000 0x1000>;
  status = "{status}";
  bus-width = <8>;
  max-frequency = <50000000>;
  pinctrl-0 = <3>;
  non-removable;
  cap-mmc-highspeed;
  mmc-ddr-1_8v;
  no-sd;
  no-sdio;
  {'mmc-hs200-1_8v;' if hs200 else ''}
  vmmc-supply = <1>;
  vqmmc-supply = <2>;
 }};
 }};
}};
'''
        output = self.root / emmc_recovery.DTB
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', str(output)],
                       input=source, text=True, check=True, capture_output=True)
        return output

    def tools(self):
        fdtget, fdtput = shutil.which('fdtget'), shutil.which('fdtput')
        if not fdtget or not fdtput:
            self.skipTest('fdtget and fdtput are required')
        return fdtget, fdtput

    def test_default_is_disabled_and_configuration_is_reversible(self):
        self.assertFalse(emmc_recovery.enabled(self.config))
        before = self.config.read_text()
        emmc_recovery.configure(self.config, '1')
        self.assertTrue(emmc_recovery.enabled(self.config))
        emmc_recovery.configure(self.config, '0')
        self.assertEqual(self.config.read_text(), before)

    def test_profile_change_requires_fresh_output(self):
        emmc_recovery.check_output(self.config, '0')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            emmc_recovery.check_output(self.config, '1')
        emmc_recovery.check_output(self.root / 'absent', '1')

    def test_install_enables_only_reviewed_ddr52_node(self):
        dtb = self.compile_dtb()
        fdtget, fdtput = self.tools()
        emmc_recovery.configure(self.config, '1')
        emmc_recovery.install(self.root, self.config, fdtget, fdtput)
        emmc_recovery.validate(fdtget, dtb, 'okay', marker=True)

    def test_install_rejects_hs200_and_preserves_input(self):
        dtb = self.compile_dtb(hs200=True)
        before = dtb.read_bytes()
        fdtget, fdtput = self.tools()
        emmc_recovery.configure(self.config, '1')
        with self.assertRaisesRegex(ValueError, 'incompatible bus mode'):
            emmc_recovery.install(self.root, self.config, fdtget, fdtput)
        self.assertEqual(dtb.read_bytes(), before)

    def test_install_rejects_non_peripheral_usb_profile(self):
        self.compile_dtb()
        fdtget, fdtput = self.tools()
        emmc_recovery.configure(self.config, '1')
        text = self.config.read_text().replace(
            'allwinner/sun50i-a133-trimui-brick"',
            'allwinner/sun50i-a133-trimui-brick-host"')
        self.config.write_text(text)
        with self.assertRaisesRegex(ValueError, 'USB gadget'):
            emmc_recovery.install(self.root, self.config, fdtget, fdtput)

    def test_install_rejects_other_hardware_qualification_profiles(self):
        self.compile_dtb()
        fdtget, fdtput = self.tools()
        emmc_recovery.configure(self.config, '1')
        cases = []
        cases.append(('SCP', self.config.read_text().replace(
            '# BR2_PACKAGE_BRICK_SCP is not set',
            'BR2_PACKAGE_BRICK_SCP=y').replace(
            'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=""',
            'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/native-wake.fragment"')))
        cases.append(('accelerator', self.config.read_text().replace(
            '# BR2_BRICK_G2D_QUALIFICATION is not set',
            'BR2_BRICK_G2D_QUALIFICATION=y')))
        cases.append(('accelerator', self.config.read_text().replace(
            '# BR2_BRICK_VE_QUALIFICATION is not set',
            'BR2_BRICK_VE_QUALIFICATION=y')))
        for message, text in cases:
            with self.subTest(message=message):
                self.config.write_text(text)
                with self.assertRaisesRegex(ValueError, message):
                    emmc_recovery.install(self.root, self.config, fdtget, fdtput)

    def test_boot_success_writer_revalidates_removable_sd(self):
        service = (ROOT / 'board/trimui/brick/rootfs-overlay/etc/init.d/S99boot-success').read_text()
        identity = 'cat /sys/class/block/mmcblk0/device/type'
        self.assertIn(identity, service)
        self.assertIn('= SD ]', service)
        self.assertLess(service.index(identity), service.index('mount -o rw,sync'))

    def test_mdev_rpmb_rule_removes_devtmpfs_node_on_add(self):
        post_build = (ROOT / 'board/trimui/brick/post-build.sh').read_text()
        rule = 'mmcblk[0-9]+rpmb root:root 000 ! @/bin/rm -f /dev/$MDEV'
        old = 'mmcblk[0-9]+rpmb root:root 000 !'
        self.assertIn("rpmb_rule='" + rule + "'", post_build)
        self.assertIn("old_rpmb_rule='" + old + "'", post_build)
        self.assertIn('grep -Fvx "$old_rpmb_rule"', post_build)
        self.assertIn('grep -Fqx "$rpmb_rule"', post_build)
        self.assertIn("printf '%s\\n' \"$rpmb_rule\"", post_build)
        self.assertNotIn("printf '%s\\n' 'mmcblk[0-9]+rpmb root:root 000 !'", post_build)


class RecoveryServiceTests(unittest.TestCase):
    service = ROOT / 'package/brick-emmc-recovery/S03emmc-recovery'

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.busybox = shutil.which('busybox')
        if not self.busybox:
            self.skipTest('busybox is required for recovery service tests')
        self.root = Path(directory.name)
        self.sys = self.root / 'sys'
        self.dev = self.root / 'dev'
        self.run = self.root / 'run'
        self.dev.mkdir()
        marker = self.sys / 'firmware/devicetree/base/chosen/trimui,emmc-recovery'
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b'read-only\0')

        node = self.sys / 'firmware/devicetree/base/soc/mmc@4022000'
        node.mkdir(parents=True)
        controller = self.sys / 'bus/platform/devices/4022000.mmc'
        controller.mkdir(parents=True)
        (controller / 'driver_override').write_text('(null)\n')
        (controller / 'of_node').symlink_to(node)
        host = controller / 'mmc_host/mmc2'
        card = host / 'mmc2:0001'
        (card / 'block').mkdir(parents=True)
        (card / 'type').write_text('MMC\n')
        (card / 'cid').write_text('90014a4838473165050700a5d6c64100\n')
        (card / 'name').write_bytes(b'H8G1e\x05\n')

        class_block = self.sys / 'class/block'
        for area in ('mmcblk2', 'mmcblk2boot0', 'mmcblk2boot1'):
            path = class_block / area
            path.mkdir(parents=True)
            (path / 'force_ro').write_text('0\n')
            (path / 'ro').symlink_to(path / 'force_ro')
            (self.dev / area).write_text('device')
        (class_block / 'mmcblk2/size').write_text('15269888\n')
        part = class_block / 'mmcblk2p1'
        part.mkdir()
        (part / 'ro').symlink_to(class_block / 'mmcblk2/force_ro')
        (self.dev / 'mmcblk2p1').write_text('partition')
        (card / 'block/mmcblk2').symlink_to(class_block / 'mmcblk2')

        (self.sys / 'bus/mmc_rpmb/devices/mmcblk2rpmb').mkdir(parents=True)
        (self.dev / 'mmcblk2rpmb').write_text('rpmb')
        driver = self.sys / 'bus/platform/drivers/sunxi-mmc'
        driver.mkdir(parents=True)
        (driver / 'unbind').write_text('')
        (controller / 'driver').symlink_to(driver)

    def simulate_unbind(self):
        unbind = self.sys / 'bus/platform/drivers/sunxi-mmc/unbind'
        controller = self.sys / 'bus/platform/devices/4022000.mmc'

        def remove():
            for _ in range(1000):
                if unbind.read_text():
                    (controller / 'driver').unlink(missing_ok=True)
                    shutil.rmtree(controller / 'mmc_host')
                    return
                time.sleep(0.001)

        worker = threading.Thread(target=remove)
        worker.start()
        self.addCleanup(worker.join)

    def run_service(self):
        env = dict(os.environ, BRICK_SYSFS_ROOT=str(self.sys),
                   BRICK_DEV_ROOT=str(self.dev), BRICK_RUN_ROOT=str(self.run),
                   BRICK_SLEEP='sleep')
        return subprocess.run([self.busybox, 'sh', str(self.service), 'start'], env=env,
                              text=True, capture_output=True)

    def assert_later_init_blocked(self, reason):
        self.run.mkdir(exist_ok=True)
        after = self.root / 'later-init-ran'
        env = dict(os.environ, BRICK_SYSFS_ROOT=str(self.sys),
                   BRICK_DEV_ROOT=str(self.dev), BRICK_RUN_ROOT=str(self.run),
                   BRICK_SLEEP='true')
        command = f'"{self.busybox}" sh "{self.service}" start; : > "{after}"'
        process = subprocess.Popen([self.busybox, 'sh', '-c', command], env=env,
                                   text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        try:
            for _ in range(100):
                try:
                    state = (self.run / 'brick-emmc-recovery').read_text()
                except FileNotFoundError:
                    state = ''
                if reason in state:
                    break
                time.sleep(0.005)
            self.assertIsNone(process.poll())
            self.assertFalse(after.exists())
            self.assertIn(reason, state)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            process.stdout.close()
            process.stderr.close()

    def test_exact_device_is_forced_read_only(self):
        result = self.run_service()
        self.assertEqual(result.returncode, 0, result.stderr)
        for area in ('mmcblk2', 'mmcblk2boot0', 'mmcblk2boot1'):
            self.assertEqual((self.sys / 'class/block' / area / 'force_ro').read_text(), '1\n')
            self.assertEqual(stat.S_IMODE((self.dev / area).stat().st_mode), 0o400)
        self.assertEqual(stat.S_IMODE((self.dev / 'mmcblk2p1').stat().st_mode), 0o400)
        self.assertFalse((self.dev / 'mmcblk2rpmb').exists())
        state = (self.run / 'brick-emmc-recovery').read_text()
        self.assertIn('cid=90014a4838473165050700a5d6c64100\n', state)
        self.assertIn('name_hex=483847316505\n', state)
        self.assertIn('mode=read-only\n', state)
        self.assertEqual((self.sys / 'bus/platform/drivers/sunxi-mmc/unbind').read_text(), '')

    def test_wrong_identity_unbinds_controller(self):
        (self.sys / 'bus/platform/devices/4022000.mmc/mmc_host/mmc2/mmc2:0001/name').write_text('OTHER\n')
        self.simulate_unbind()
        result = self.run_service()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unexpected eMMC product', result.stderr)
        self.assertEqual((self.sys / 'bus/platform/drivers/sunxi-mmc/unbind').read_text(),
                         '4022000.mmc\n')
        self.assertEqual((self.sys / 'bus/platform/devices/4022000.mmc/driver_override').read_text(),
                         'brick-emmc-rejected\n')

    def test_missing_marker_unbinds_controller(self):
        (self.sys / 'firmware/devicetree/base/chosen/trimui,emmc-recovery').unlink()
        self.simulate_unbind()
        result = self.run_service()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('profile marker is absent', result.stderr)
        self.assertEqual((self.sys / 'bus/platform/drivers/sunxi-mmc/unbind').read_text(),
                         '4022000.mmc\n')

    def test_invalid_marker_unbinds_controller(self):
        marker = self.sys / 'firmware/devicetree/base/chosen/trimui,emmc-recovery'
        marker.write_bytes(b'writable\0')
        self.simulate_unbind()
        result = self.run_service()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('invalid profile marker', result.stderr)
        self.assertEqual((self.sys / 'bus/platform/drivers/sunxi-mmc/unbind').read_text(),
                         '4022000.mmc\n')

    def test_failed_unbind_blocks_later_init(self):
        (self.sys / 'bus/platform/devices/4022000.mmc/mmc_host/mmc2/mmc2:0001/name').write_text('OTHER\n')
        (self.sys / 'bus/platform/drivers/sunxi-mmc/unbind').unlink()
        self.assert_later_init_blocked('unsafe: unexpected eMMC product')

    def test_absent_controller_blocks_later_init(self):
        shutil.rmtree(self.sys / 'bus/platform/devices/4022000.mmc')
        self.assert_later_init_blocked(
            'unsafe: ambiguous or absent controller 0x04022000')

    def test_unbind_write_error_blocks_init(self):
        (self.sys / 'bus/platform/devices/4022000.mmc/mmc_host/mmc2/mmc2:0001/name').write_text('OTHER\n')
        unbind = self.sys / 'bus/platform/drivers/sunxi-mmc/unbind'
        unbind.unlink()
        unbind.symlink_to('/dev/full')
        self.assert_later_init_blocked('unsafe: unexpected eMMC product')

    def test_driver_override_write_error_blocks_init(self):
        (self.sys / 'bus/platform/devices/4022000.mmc/mmc_host/mmc2/mmc2:0001/name').write_text('OTHER\n')
        override = self.sys / 'bus/platform/devices/4022000.mmc/driver_override'
        override.unlink()
        override.symlink_to('/dev/full')
        self.assert_later_init_blocked('unsafe: unexpected eMMC product')

    def test_unbind_without_device_removal_blocks_init(self):
        (self.sys / 'bus/platform/devices/4022000.mmc/mmc_host/mmc2/mmc2:0001/name').write_text('OTHER\n')
        self.assert_later_init_blocked('unsafe: unexpected eMMC product')
        self.assertEqual((self.sys / 'bus/platform/drivers/sunxi-mmc/unbind').read_text(),
                         '4022000.mmc\n')

    def test_force_ro_write_error_blocks_init(self):
        attribute = self.sys / 'class/block/mmcblk2/force_ro'
        attribute.unlink()
        attribute.symlink_to('/dev/full')
        self.assert_later_init_blocked('unsafe: mmcblk2 force_ro write failed')

    def test_rpmb_removal_error_blocks_init(self):
        rpmb = self.dev / 'mmcblk2rpmb'
        rpmb.unlink()
        rpmb.mkdir()
        self.assert_later_init_blocked('unsafe: RPMB device node removal failed')

    def test_unexpected_general_purpose_area_blocks_init(self):
        (self.sys / 'class/block/mmcblk2gp0').mkdir()
        self.assert_later_init_blocked('unsafe: unexpected eMMC area mmcblk2gp0')


if __name__ == '__main__':
    unittest.main()
