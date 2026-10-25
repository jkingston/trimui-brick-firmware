"""Wi-Fi service must give atomic profile saves a persistent destination."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

SERVICE = (Path(__file__).resolve().parents[1] /
           'board/trimui/brick/rootfs-overlay/etc/init.d/S45wifi')


class ProfilePathTests(unittest.TestCase):
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
                'wpa_cli': '#!/bin/sh\nexit 0\n',
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


if __name__ == '__main__':
    unittest.main()
