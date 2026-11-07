"""Connection-boundary private MAC policy tests."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POLICY = (ROOT / 'board/trimui/brick/rootfs-overlay/usr/libexec/'
          'brick-wifi-private-mac')


class PrivateMacPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'calls'
        self.status = self.root / 'status'
        self.status.write_text('wpa_state=DISCONNECTED\n')
        mock = self.bin / 'wpa_cli'
        mock.write_text('''#!/bin/sh
while [ "$#" -gt 0 ]; do
 case "$1" in -i|-p) shift 2;; *) break;; esac
done
case "$1" in
 get_network)
  case "$3" in ssid) echo '"Test"';; key_mgmt) echo "${TEST_KEY_MGMT:-NONE}";; esac;;
 status) cat "$TEST_STATUS";;
 set_network) echo "$*" >> "$TEST_CALLS"; echo OK;;
 save_config) echo save_config >> "$TEST_CALLS"; echo OK;;
 *) echo FAIL;;
esac
''')
        mock.chmod(0o755)
        self.env = dict(os.environ,
                        PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        WIFI_MAC_STATE_DIR=str(self.root / 'state'),
                        WIFI_RANDOM_HEX='001122334455',
                        WIFI_SAVE_CONFIG='0',
                        WIFI_NOW='100000',
                        TEST_CALLS=str(self.calls),
                        TEST_STATUS=str(self.status),
                        TEST_KEY_MGMT='NONE')

    def tearDown(self):
        self.temp.cleanup()

    def run_policy(self, *args, ok=True, **updates):
        env = dict(self.env, **{key: str(value) for key, value in updates.items()})
        result = subprocess.run([str(POLICY), *map(str, args)], env=env,
                                capture_output=True, text=True, timeout=5)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def state(self):
        files = [path for path in (self.root / 'state').iterdir()
                 if path.name != 'lock']
        self.assertEqual(len(files), 1)
        return dict(line.split('=', 1) for line in
                    files[0].read_text().splitlines())

    def test_private_default_is_stable_and_applied(self):
        first = self.run_policy('prepare', 7)
        second = self.run_policy('prepare', 7, WIFI_NOW=200000)
        self.assertEqual(first.stdout, 'private 02:11:22:33:44:55\n')
        self.assertEqual(second.stdout, first.stdout)
        calls = self.calls.read_text()
        self.assertIn('set_network 7 mac_value 02:11:22:33:44:55', calls)
        self.assertIn('set_network 7 mac_addr 3', calls)

    def test_enhanced_rotates_after_twenty_four_hours(self):
        self.run_policy('set-mode', 7, 'enhanced')
        self.run_policy('prepare', 7)
        result = self.run_policy('prepare', 7, WIFI_NOW=186400,
                                 WIFI_RANDOM_HEX='aabbccddeeff')
        self.assertEqual(result.stdout, 'enhanced aa:bb:cc:dd:ee:ff\n')
        self.assertEqual(self.state()['created'], '186400')

    def test_enhanced_rotates_after_expired_lease_and_four_hours_unused(self):
        self.run_policy('set-mode', 7, 'enhanced')
        self.run_policy('prepare', 7)
        self.run_policy('lease', 7, 60)
        self.run_policy('disconnected', 7, WIFI_NOW=100100)
        unchanged = self.run_policy('prepare', 7, WIFI_NOW=114499,
                                    WIFI_RANDOM_HEX='aabbccddeeff')
        rotated = self.run_policy('prepare', 7, WIFI_NOW=114500,
                                  WIFI_RANDOM_HEX='aabbccddeeff')
        self.assertIn('02:11:22:33:44:55', unchanged.stdout)
        self.assertIn('aa:bb:cc:dd:ee:ff', rotated.stdout)

    def test_enhanced_is_suppressed_for_secured_or_captive_network(self):
        self.run_policy('set-mode', 7, 'enhanced')
        secured = self.run_policy('prepare', 7, TEST_KEY_MGMT='WPA-PSK')
        self.assertTrue(secured.stdout.startswith('private '))
        self.run_policy('mark-captive', 7)
        captive = self.run_policy('prepare', 7, WIFI_NOW=200000,
                                  WIFI_RANDOM_HEX='aabbccddeeff')
        self.assertEqual(captive.stdout, 'private 02:11:22:33:44:55\n')

    def test_device_mode_selects_factory_address(self):
        self.run_policy('set-mode', 7, 'device')
        result = self.run_policy('prepare', 7)
        self.assertTrue(result.stdout.startswith('device '))
        self.assertIn('set_network 7 mac_addr 0', self.calls.read_text())

    def test_generated_address_is_local_unicast(self):
        result = self.run_policy('prepare', 7, WIFI_RANDOM_HEX='ff1122334455')
        self.assertEqual(result.stdout, 'private fe:11:22:33:44:55\n')

    def test_connected_network_is_never_changed(self):
        self.status.write_text('id=7\nwpa_state=COMPLETED\n')
        result = self.run_policy('prepare', 7, ok=False)
        self.assertIn('refusing to change MAC', result.stderr)

    def test_forget_removes_metadata(self):
        self.run_policy('prepare', 7)
        self.run_policy('forget', 7)
        files = [path for path in (self.root / 'state').iterdir()
                 if path.name != 'lock']
        self.assertEqual(files, [])


if __name__ == '__main__':
    unittest.main()
