"""Crash-consistency tests for Wi-Fi state, credentials and Forget Network."""
from pathlib import Path
import hashlib
import os
import resource
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / 'board/trimui/brick/rootfs-overlay'
PRIVATE = OVERLAY / 'usr/libexec/brick-wifi-private-mac'
FORGET = OVERLAY / 'usr/libexec/brick-wifi-forget'
DHCP = (OVERLAY / 'usr/share/udhcpc/default.script.d/50-brick-wifi')
WPA_PATCH = (ROOT / 'board/trimui/brick/patches/wpa_supplicant/'
             '0002-config-file-make-replacement-durable.patch')
SERVICE = OVERLAY / 'etc/init.d/S45wifi'


def no_file_growth():
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))


class WifiPersistenceFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.calls = self.root / 'calls'
        self.hook = self.root / 'fault-hook'
        self.hook.write_text('''#!/bin/sh
printf '%s\n' "$1" >> "$FAULT_EVENTS"
[ "$1" != "${FAIL_AT:-}" ] || kill -KILL "$PPID"
''')
        self.hook.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def private_env(self):
        mock = self.bin / 'wpa_cli'
        mock.write_text('''#!/bin/sh
while [ "$#" -gt 0 ]; do
 case "$1" in -i|-p) shift 2;; *) break;; esac
done
case "$1" in
 get_network)
  case "$3" in ssid) echo '"Test"';; key_mgmt) echo NONE;; esac;;
 status) echo wpa_state=DISCONNECTED;;
 set_network|save_config) echo OK;;
 *) echo FAIL;;
esac
''')
        mock.chmod(0o755)
        return dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                    WIFI_MAC_STATE_DIR=str(self.root / 'private'),
                    WIFI_RANDOM_HEX='001122334455', WIFI_SAVE_CONFIG='0',
                    WIFI_NOW='100000', WIFI_FAULT_HOOK=str(self.hook),
                    FAULT_EVENTS=str(self.root / 'fault-events'))

    @staticmethod
    def state_file(root):
        return next(path for path in root.iterdir()
                    if path.name != 'lock' and not path.name.startswith('.'))

    def test_private_mac_interrupted_temp_keeps_last_valid_state(self):
        env = self.private_env()
        subprocess.run([str(PRIVATE), 'prepare', '7'], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        state = self.state_file(self.root / 'private')
        before = state.read_bytes()
        failed = subprocess.run([str(PRIVATE), 'mark-captive', '7'],
                                env=dict(env, FAIL_AT='private-after-temp'))
        self.assertEqual(failed.returncode, -9)
        self.assertEqual(state.read_bytes(), before)
        self.assertTrue(list(state.parent.glob('.private-mac.*')))
        subprocess.run([str(PRIVATE), 'mark-captive', '7'], env=env, check=True)
        self.assertIn(b'captive=1\n', state.read_bytes())

    def test_private_mac_after_rename_is_complete_new_state(self):
        env = self.private_env()
        subprocess.run([str(PRIVATE), 'prepare', '7'], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        state = self.state_file(self.root / 'private')
        failed = subprocess.run([str(PRIVATE), 'mark-captive', '7'],
                                env=dict(env, FAIL_AT='private-after-rename'))
        self.assertEqual(failed.returncode, -9)
        lines = dict(line.split('=', 1) for line in state.read_text().splitlines())
        self.assertEqual(lines['captive'], '1')
        self.assertEqual(set(lines), {'mac', 'created', 'last_disconnected',
                                     'lease_expires', 'mode', 'captive'})

    def test_private_mac_full_file_fault_preserves_old_inode(self):
        env = self.private_env()
        subprocess.run([str(PRIVATE), 'prepare', '7'], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        state = self.state_file(self.root / 'private')
        before = state.read_bytes()
        failed = subprocess.run([str(PRIVATE), 'mark-captive', '7'], env=env,
                                preexec_fn=no_file_growth,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(state.read_bytes(), before)

    def dhcp_env(self):
        connectivity = self.root / 'connectivity'
        connectivity.write_text('#!/bin/sh\nexit 0\n')
        connectivity.chmod(0o755)
        private = self.root / 'private-mock'
        private.write_text('#!/bin/sh\nexit 0\n')
        private.chmod(0o755)
        return dict(os.environ, WIFI_STATE_DIR=str(self.root / 'runtime'),
                    WIFI_CONNECTIVITY=str(connectivity),
                    WIFI_PRIVATE_MAC=str(private),
                    WIFI_FAULT_HOOK=str(self.hook),
                    FAULT_EVENTS=str(self.root / 'fault-events'),
                    interface='wlan0', ip='192.0.2.2', lease='60')

    def test_dhcp_partial_temp_and_full_file_keep_previous_lease(self):
        env = self.dhcp_env()
        state = self.root / 'runtime'
        state.mkdir()
        lease = state / 'dhcp'
        lease.write_text('address=192.0.2.1\n')
        before = lease.read_bytes()
        failed = subprocess.run([str(DHCP), 'renew'],
                                env=dict(env, FAIL_AT='dhcp-after-temp'))
        self.assertEqual(failed.returncode, -9)
        self.assertEqual(lease.read_bytes(), before)
        failed = subprocess.run([str(DHCP), 'renew'], env=env,
                                preexec_fn=no_file_growth,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(lease.read_bytes(), before)

    def forget_fixture(self):
        runtime = self.root / 'network-present'
        runtime.write_text('1\n')
        config = self.root / 'wpa.conf'
        config.write_text('network=present\n')
        cli = self.bin / 'wpa_cli'
        cli.write_text('''#!/bin/sh
while [ "$#" -gt 0 ]; do
 case "$1" in -i|-p) shift 2;; *) break;; esac
done
case "$1" in
 get_network)
  [ -f "$MOCK_NETWORK" ] || { echo FAIL; exit; }
  case "$3" in ssid) echo '"Test"';; key_mgmt) echo NONE;; esac;;
 disable_network) echo OK;;
 remove_network) rm -f "$MOCK_NETWORK"; echo OK;;
 save_config)
  if [ -f "$MOCK_NETWORK" ]; then echo network=present > "$MOCK_CONFIG";
  else echo network=absent > "$MOCK_CONFIG"; fi
  echo OK;;
 *) echo FAIL;;
esac
''')
        cli.chmod(0o755)
        canonical = hashlib.sha256(b'ssid="Test"\nkey_mgmt=NONE\n').hexdigest()
        captive_id = hashlib.sha256(canonical.encode()).hexdigest()
        private_root = self.root / 'private'
        private_root.mkdir()
        (private_root / canonical).write_text('mac=02:11:22:33:44:55\n')
        profiles = self.root / 'profiles'
        profile = profiles / captive_id
        (profile / 'data').mkdir(parents=True)
        (profile / 'data/cookie').write_text('secret-cookie')
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                   WIFI_FORGET_STATE_DIR=str(self.root / 'pending'),
                   WIFI_CAPTIVE_PROFILE_ROOT=str(profiles),
                   WIFI_PRIVATE_MAC=str(PRIVATE),
                   WIFI_MAC_STATE_DIR=str(private_root),
                   WIFI_CTRL_DIR=str(self.root / 'ctrl'),
                   WIFI_FAULT_HOOK=str(self.hook),
                   FAULT_EVENTS=str(self.root / 'fault-events'),
                   MOCK_NETWORK=str(runtime), MOCK_CONFIG=str(config))
        return env, runtime, config, private_root, profile, canonical

    def test_forget_recovers_idempotently_at_every_published_boundary(self):
        phases = ('forget-after-intent-rename', 'forget-after-remove-network',
                  'forget-after-save-config', 'forget-after-private-mac',
                  'forget-after-profile-rename',
                  'forget-after-captive-profile')
        for phase in phases:
            with self.subTest(phase=phase):
                shutil.rmtree(self.root, ignore_errors=True)
                self.root.mkdir()
                self.bin = self.root / 'bin'
                self.bin.mkdir()
                self.hook = self.root / 'fault-hook'
                self.hook.write_text('#!/bin/sh\n[ "$1" != "$FAIL_AT" ] || kill -KILL "$PPID"\n')
                self.hook.chmod(0o755)
                env, runtime, config, private_root, profile, key = self.forget_fixture()
                failed = subprocess.run([str(FORGET), 'network', '7'],
                                        env=dict(env, FAIL_AT=phase))
                self.assertEqual(failed.returncode, -9)
                subprocess.run([str(FORGET), 'recover'],
                               env=dict(env, FAIL_AT='never'), check=True)
                subprocess.run([str(FORGET), 'recover'],
                               env=dict(env, FAIL_AT='never'), check=True)
                self.assertFalse(runtime.exists())
                self.assertEqual(config.read_text(), 'network=absent\n')
                self.assertFalse((private_root / key).exists())
                self.assertFalse(profile.exists())
                self.assertEqual(list(profile.parent.glob('.forget-*')), [])
                pending = self.root / 'pending'
                self.assertEqual([p.name for p in pending.iterdir()
                                  if p.name != 'lock'], [])

    def test_forget_before_intent_publish_changes_nothing_and_can_retry(self):
        env, runtime, config, private_root, profile, key = self.forget_fixture()
        failed = subprocess.run([str(FORGET), 'network', '7'],
                                env=dict(env, FAIL_AT='forget-after-intent-temp'))
        self.assertEqual(failed.returncode, -9)
        self.assertTrue(runtime.exists())
        self.assertEqual(config.read_text(), 'network=present\n')
        self.assertTrue((private_root / key).exists())
        self.assertTrue(profile.exists())
        subprocess.run([str(FORGET), 'network', '7'],
                       env=dict(env, FAIL_AT='never'), check=True)
        self.assertFalse(profile.exists())

    def test_forget_rejects_symlink_profile_root_without_changes(self):
        env, runtime, config, private_root, profile, key = self.forget_fixture()
        real_profiles = profile.parent
        linked_profiles = self.root / 'linked-profiles'
        linked_profiles.symlink_to(real_profiles, target_is_directory=True)
        failed = subprocess.run(
            [str(FORGET), 'network', '7'],
            env=dict(env, WIFI_CAPTIVE_PROFILE_ROOT=str(linked_profiles)),
            capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('profile root must not be a symlink', failed.stderr)
        self.assertTrue(runtime.exists())
        self.assertEqual(config.read_text(), 'network=present\n')
        self.assertTrue((private_root / key).exists())
        self.assertTrue(profile.exists())

    def test_forget_full_file_fault_never_starts_destructive_steps(self):
        env, runtime, config, private_root, profile, key = self.forget_fixture()
        failed = subprocess.run([str(FORGET), 'network', '7'], env=env,
                                preexec_fn=no_file_growth,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(runtime.exists())
        self.assertEqual(config.read_text(), 'network=present\n')
        self.assertTrue((private_root / key).exists())
        self.assertTrue(profile.exists())

    def test_wpa_patch_checks_writeback_and_syncs_parent(self):
        text = WPA_PATCH.read_text()
        self.assertIn('if (os_fdatasync(f))', text)
        self.assertIn('if (fclose(f))', text)
        self.assertIn('wpa_config_sync_parent_dir(orig_name)', text)
        self.assertIn('ret = fsync(fd);', text)

    def test_service_recovers_forget_before_private_mac_and_radio_enable(self):
        start = SERVICE.read_text().split('case "${1:-}" in', 1)[1]
        recover = start.index('"$forget" recover')
        prepare = start.index('! prepare_private_macs')
        enable = start.index('radio_set 1', prepare)
        self.assertLess(recover, prepare)
        self.assertLess(prepare, enable)


if __name__ == '__main__':
    unittest.main()
