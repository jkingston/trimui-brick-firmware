"""Wi-Fi service must give atomic profile saves a persistent destination."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

SERVICE = (Path(__file__).resolve().parents[1] /
           'board/trimui/brick/rootfs-overlay/etc/init.d/S45wifi')


class ProfilePathTests(unittest.TestCase):
    def test_pre_monitor_configuration_does_not_deadlock_supplicant(self):
        text = SERVICE.read_text()
        self.assertNotIn('wpa_supplicant -B -W ', text)
        self.assertLess(text.index('set ignore_old_scan_res 1'),
                        text.index('wpa_cli -B -i wlan0'))

    def test_start_resolves_profile_before_atomic_save(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = root / 'bin'
            commands.mkdir()
            (root / 'wlan0').touch()
            profile = root / 'persistent.conf'
            profile.write_text('update_config=1\n')
            config = root / 'config.conf'
            config.symlink_to(profile)
            deconfig = root / 'deconfig'
            deconfig.write_text('#!/bin/sh\nexit 0\n')
            deconfig.chmod(0o755)
            text = SERVICE.read_text().replace('/run/brick-wifi', str(root / 'state'))
            text = text.replace('/sys/class/net/wlan0', str(root / 'wlan0'))
            text = text.replace('/usr/share/udhcpc/default.script', str(deconfig))
            helper = root / 'stop-daemon'
            helper.write_text('#!/bin/sh\nexit 0\n')
            helper.chmod(0o755)
            text = text.replace('/usr/libexec/brick-stop-daemon', str(helper))
            service = root / 'service'
            service.write_text(text)
            mocks = {
                'start-stop-daemon': '#!/bin/sh\ncase " $* " in *" -t "*) exit 1;; esac\nexit 0\n',
                'wpa_cli': ('#!/bin/sh\ncase "$*" in '
                            '*"set ignore_old_scan_res 1"*) echo OK;; esac\nexit 0\n'),
                # Model config_file.c's temp-file + rename at the path received.
                # No radio/process is started and only synthetic state is used.
                'wpa_supplicant': '''#!/usr/bin/env python3
from pathlib import Path
import os, sys
args = sys.argv[1:]
path = Path(args[args.index('-c') + 1])
Path(os.environ['TEST_CONFIG_PATH']).write_text(str(path))
tmp = path.with_name(path.name + '.tmp-test')
tmp.write_text('update_config=1\\n# saved\\n')
tmp.replace(path)
''',
            }
            for name, contents in mocks.items():
                (commands / name).write_text(contents)
                (commands / name).chmod(0o755)
            env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       WIFI_CONFIG=str(config), WIFI_CTRL_DIR=str(root / 'ctrl'),
                       WIFI_RFKILL_ROOT=str(root / 'rfkill'),
                       TEST_CONFIG_PATH=str(root / 'received-path'))
            result = subprocess.run(['sh', str(service), 'start'], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / 'received-path').read_text(), str(profile))
            self.assertTrue(config.is_symlink())
            self.assertIn('# saved', profile.read_text())


class StopFailureTests(unittest.TestCase):
    def test_failed_stop_prevents_restart_and_preserves_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            state.mkdir()
            pid = state / 'events.pid'
            pid.write_text('12345\n')
            (state / 'enabled').write_text('old-generation\n')
            helper = root / 'stop-daemon'
            helper.write_text('#!/bin/sh\necho stop >> "$TEST_CALLS"\nexit 1\n')
            helper.chmod(0o755)
            commands = root / 'bin'
            commands.mkdir()
            for name in ('wpa_supplicant', 'wpa_cli'):
                mock = commands / name
                mock.write_text('#!/bin/sh\necho started >> "$TEST_CALLS"\n')
                mock.chmod(0o755)
            service = root / 'service'
            service.write_text(SERVICE.read_text()
                               .replace('/run/brick-wifi', str(state))
                               .replace('/usr/libexec/brick-stop-daemon', str(helper)))
            service.chmod(0o755)
            calls = root / 'calls'
            env = dict(os.environ, PATH=str(commands) + ':' + os.environ['PATH'],
                       TEST_CALLS=str(calls))
            result = subprocess.run([str(service), 'restart'], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls.read_text(), 'stop\n')
            self.assertEqual(pid.read_text(), '12345\n')
            self.assertFalse((state / 'enabled').exists())


class RoamingLeaseTests(unittest.TestCase):
    def make_service(self, root):
        state = root / 'state'
        state.mkdir()
        (state / 'enabled').write_text('test-generation\n')
        (state / 'dhcp').write_text('address=192.0.2.10\n')
        wlan = root / 'wlan0'
        wlan.touch()
        calls = root / 'calls'
        commands = root / 'bin'
        commands.mkdir()
        start_stop = commands / 'start-stop-daemon'
        start_stop.write_text('''#!/bin/sh
printf 'daemon %s\n' "$*" >> "$TEST_CALLS"
case " $* " in
*' -K -t '*'/sbin/udhcpc'*) exit "${TEST_DHCP_RUNNING:-1}" ;;
esac
exit 0
''')
        start_stop.chmod(0o755)
        helper = root / 'stop-daemon'
        helper.write_text('#!/bin/sh\nprintf "stop %s\\n" "$*" >> "$TEST_CALLS"\n')
        helper.chmod(0o755)
        deconfig = root / 'deconfig'
        deconfig.write_text('#!/bin/sh\nprintf "deconfig\\n" >> "$TEST_CALLS"\n')
        deconfig.chmod(0o755)
        private = root / 'private-mac'
        private.write_text('#!/bin/sh\nprintf "private %s\\n" "$*" >> "$TEST_CALLS"\n')
        private.chmod(0o755)
        arping = root / 'arping'
        arping.write_text('#!/bin/sh\nprintf "arping %s\\n" "$*" >> "$TEST_CALLS"\n')
        arping.chmod(0o755)
        service = root / 'service'
        service.write_text(SERVICE.read_text()
                           .replace('/run/brick-wifi', str(state))
                           .replace('/sys/class/net/wlan0', str(wlan))
                           .replace('/usr/libexec/brick-stop-daemon', str(helper))
                           .replace('/usr/share/udhcpc/default.script', str(deconfig)))
        service.chmod(0o755)
        env = dict(os.environ,
                   PATH=str(commands) + os.pathsep + os.environ['PATH'],
                   WIFI_GENERATION='test-generation',
                   WIFI_PRIVATE_MAC=str(private), WIFI_ARPING=str(arping),
                   WIFI_ROAM_GRACE='2', TEST_CALLS=str(calls),
                   TEST_DHCP_RUNNING='0', WPA_ID='7')
        return service, state, calls, env

    def run_event(self, service, env, event):
        result = subprocess.run([str(service), 'wlan0', event], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_same_profile_roam_replaces_unqualified_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            service, state, calls, env = self.make_service(Path(directory))
            self.run_event(service, env, 'DISCONNECTED')
            self.assertTrue((state / 'disconnect-pending').exists())
            self.run_event(service, env, 'CONNECTED')
            text = calls.read_text()
            self.assertNotIn('arping ', text)
            self.assertIn('stop udhcpc ', text)
            self.assertIn('deconfig', text)
            self.assertIn('daemon -S', text)
            self.assertFalse((state / 'disconnect-pending').exists())

    def test_different_profile_replaces_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            service, state, calls, env = self.make_service(Path(directory))
            self.run_event(service, env, 'DISCONNECTED')
            self.run_event(service, dict(env, WPA_ID='8'), 'CONNECTED')
            text = calls.read_text()
            self.assertIn('stop udhcpc ', text)
            self.assertIn(' /sbin/udhcpc', text)
            self.assertIn('deconfig', text)
            self.assertIn('daemon -S', text)
            self.assertFalse((state / 'disconnect-pending').exists())

    def test_direct_connected_roam_replaces_live_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            service, state, calls, env = self.make_service(Path(directory))
            self.run_event(service, env, 'CONNECTED')
            text = calls.read_text()
            self.assertIn('daemon -K -t ', text)
            self.assertIn('stop udhcpc ', text)
            self.assertIn('deconfig', text)
            self.assertIn('daemon -S', text)


class RadioControlTests(unittest.TestCase):
    def make_service(self, root):
        state = root / 'state'
        state.mkdir()
        wlan = root / 'wlan0'
        wlan.touch()
        rfkill = root / 'rfkill' / 'rfkill7'
        rfkill.mkdir(parents=True)
        (rfkill / 'type').write_text('wlan\n')
        (rfkill / 'state').write_text('0\n')
        helper = root / 'stop-daemon'
        helper.write_text('#!/bin/sh\nexit 0\n')
        helper.chmod(0o755)
        deconfig = root / 'deconfig'
        deconfig.write_text('#!/bin/sh\nexit 0\n')
        deconfig.chmod(0o755)
        service = root / 'service'
        service.write_text(SERVICE.read_text()
                           .replace('/run/brick-wifi', str(state))
                           .replace('/sys/class/net/wlan0', str(wlan))
                           .replace('/usr/libexec/brick-stop-daemon', str(helper))
                           .replace('/usr/share/udhcpc/default.script', str(deconfig)))
        service.chmod(0o755)
        return service, rfkill

    def test_start_unblocks_wlan_radio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, rfkill = self.make_service(root)
            commands = root / 'bin'
            commands.mkdir()
            for name in ('start-stop-daemon', 'wpa_supplicant', 'wpa_cli'):
                mock = commands / name
                if name == 'start-stop-daemon':
                    mock.write_text('#!/bin/sh\ncase " $* " in *" -t "*) exit 1;; esac\nexit 0\n')
                else:
                    mock.write_text(
                        '#!/bin/sh\ncase "$*" in '
                        '*"set ignore_old_scan_res 1"*) echo OK;; esac\nexit 0\n')
                mock.chmod(0o755)
            env = dict(os.environ,
                       PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       WIFI_RFKILL_ROOT=str(root / 'rfkill'),
                       WIFI_CONFIG=str(root / 'profile'),
                       WIFI_CTRL_DIR=str(root / 'ctrl'))
            (root / 'profile').write_text('update_config=1\n')
            result = subprocess.run([str(service), 'start'], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((rfkill / 'state').read_text(), '1\n')

    def test_stop_blocks_wlan_radio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, rfkill = self.make_service(root)
            (rfkill / 'state').write_text('1\n')
            env = dict(os.environ, WIFI_RFKILL_ROOT=str(root / 'rfkill'))
            result = subprocess.run([str(service), 'stop'], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((rfkill / 'state').read_text(), '0\n')

    def test_persisted_off_state_does_not_start_supplicant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, rfkill = self.make_service(root)
            policy = root / 'enabled'
            policy.write_text('0\n')
            marker = root / 'started'
            commands = root / 'bin'
            commands.mkdir()
            supplicant = commands / 'wpa_supplicant'
            supplicant.write_text('#!/bin/sh\ntouch "$TEST_STARTED"\n')
            supplicant.chmod(0o755)
            env = dict(os.environ,
                       PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       WIFI_RFKILL_ROOT=str(root / 'rfkill'),
                       WIFI_ENABLED_POLICY=str(policy), TEST_STARTED=str(marker))
            result = subprocess.run([str(service), 'start'], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            self.assertEqual((rfkill / 'state').read_text(), '0\n')

    def test_stale_scan_filter_failure_keeps_radio_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, rfkill = self.make_service(root)
            commands = root / 'bin'
            commands.mkdir()
            for name, text in {
                'start-stop-daemon': '#!/bin/sh\ncase " $* " in *" -t "*) exit 1;; esac\nexit 0\n',
                'wpa_supplicant': '#!/bin/sh\nexit 0\n',
                'wpa_cli': '#!/bin/sh\necho FAIL\n',
            }.items():
                command = commands / name
                command.write_text(text)
                command.chmod(0o755)
            profile = root / 'profile'
            profile.write_text('update_config=1\n')
            env = dict(os.environ,
                       PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       WIFI_RFKILL_ROOT=str(root / 'rfkill'),
                       WIFI_CONFIG=str(profile), WIFI_CTRL_DIR=str(root / 'ctrl'))
            result = subprocess.run([str(service), 'start'], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((rfkill / 'state').read_text(), '0\n')

    def test_private_mac_is_prepared_while_radio_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, rfkill = self.make_service(root)
            commands = root / 'bin'
            commands.mkdir()
            start_stop = commands / 'start-stop-daemon'
            start_stop.write_text(
                '#!/bin/sh\ncase " $* " in *" -t "*) exit 1;; esac\nexit 0\n')
            start_stop.chmod(0o755)
            supplicant = commands / 'wpa_supplicant'
            supplicant.write_text('#!/bin/sh\nexit 0\n')
            supplicant.chmod(0o755)
            cli = commands / 'wpa_cli'
            cli.write_text('''#!/bin/sh
case "$*" in
*'set ignore_old_scan_res 1'*) echo OK ;;
*list_networks*) printf 'network id / ssid / bssid / flags\\n7\\ttest\\tany\\t\\n' ;;
*'get_network 7 disabled'*) echo 0 ;;
esac
exit 0
''')
            cli.chmod(0o755)
            private = root / 'private-mac'
            private.write_text(
                '#!/bin/sh\nprintf "%s state=%s\\n" "$*" "$(cat "$TEST_RFKILL")" > "$TEST_PREPARE"\n')
            private.chmod(0o755)
            profile = root / 'profile'
            profile.write_text('update_config=1\n')
            prepared = root / 'prepared'
            env = dict(os.environ,
                       PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       WIFI_RFKILL_ROOT=str(root / 'rfkill'),
                       WIFI_CONFIG=str(profile),
                       WIFI_CTRL_DIR=str(root / 'ctrl'),
                       WIFI_PRIVATE_MAC=str(private),
                       TEST_RFKILL=str(rfkill / 'state'),
                       TEST_PREPARE=str(prepared))
            result = subprocess.run([str(service), 'start'], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(prepared.read_text(), 'prepare 7 state=0\n')
            self.assertEqual((rfkill / 'state').read_text(), '1\n')

    def test_failed_private_mac_prepare_leaves_radio_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, rfkill = self.make_service(root)
            commands = root / 'bin'
            commands.mkdir()
            for name in ('start-stop-daemon', 'wpa_supplicant'):
                mock = commands / name
                if name == 'start-stop-daemon':
                    mock.write_text(
                        '#!/bin/sh\ncase " $* " in *" -t "*) exit 1;; esac\nexit 0\n')
                else:
                    mock.write_text('#!/bin/sh\nexit 0\n')
                mock.chmod(0o755)
            cli = commands / 'wpa_cli'
            cli.write_text('''#!/bin/sh
case "$*" in
*'set ignore_old_scan_res 1'*) echo OK ;;
*list_networks*) printf 'header\\n3\\ttest\\tany\\t\\n' ;;
*'get_network 3 disabled'*) echo 0 ;;
esac
exit 0
''')
            cli.chmod(0o755)
            private = root / 'private-mac'
            private.write_text('#!/bin/sh\nexit 1\n')
            private.chmod(0o755)
            profile = root / 'profile'
            profile.write_text('update_config=1\n')
            env = dict(os.environ,
                       PATH=str(commands) + os.pathsep + os.environ['PATH'],
                       WIFI_RFKILL_ROOT=str(root / 'rfkill'),
                       WIFI_CONFIG=str(profile),
                       WIFI_CTRL_DIR=str(root / 'ctrl'),
                       WIFI_PRIVATE_MAC=str(private))
            result = subprocess.run([str(service), 'start'], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((rfkill / 'state').read_text(), '0\n')

    def test_dhcp_requests_captive_portal_option(self):
        text = SERVICE.read_text()
        self.assertIn('-t 3 -T 1 -O 114 -p "$state/udhcpc.pid"', text)


if __name__ == '__main__':
    unittest.main()
