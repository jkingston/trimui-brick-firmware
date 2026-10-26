"""Artifact checks before deployment can open a serial device."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location('deploy', TOOLS / 'deploy.py')
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.images, self.stage = root / 'images', root / 'stage'
        self.images.mkdir()
        self.stage.mkdir()
        files = {}
        for name in (*deploy.PAYLOAD, deploy.BOOTLOADER):
            data = ('test ' + name).encode()
            (self.images / name).write_bytes(data)
            files[name] = {'sha256': hashlib.sha256(data).hexdigest(),
                           'bytes': len(data)}
        bootloader = (self.images / deploy.BOOTLOADER).read_bytes()
        (self.images / 'sdcard.img').write_bytes(bytes(deploy.BOOTLOADER_OFFSET) + bootloader)
        (self.images / 'manifest.json').write_text(json.dumps({'files': files}))

    def test_archive_names_and_checksums_match_sd_layout(self):
        archive, _ = deploy.package(self.images, self.stage)
        with tarfile.open(archive) as contents:
            self.assertEqual(set(contents.getnames()),
                             {*deploy.PAYLOAD.values(), deploy.BOOTLOADER,
                              'SHA256SUMS', 'manifest.json', 'ARCHIVE-SHA256SUMS'})
            checksums = contents.extractfile('SHA256SUMS').read().decode()
            for line in checksums.splitlines():
                digest, name = line.split()
                self.assertEqual(hashlib.sha256(contents.extractfile(name).read()).hexdigest(), digest)
            archive_checksums = contents.extractfile('ARCHIVE-SHA256SUMS').read().decode()
            for line in archive_checksums.splitlines():
                digest, name = line.split()
                self.assertEqual(hashlib.sha256(contents.extractfile(name).read()).hexdigest(), digest)

    def test_changed_kernel_is_rejected_before_upload(self):
        (self.images / 'Image').write_bytes(b'changed kernel')
        with self.assertRaisesRegex(ValueError, 'Artifact checksum mismatch: Image'):
            deploy.package(self.images, self.stage)
        self.assertFalse((self.stage / 'update.tar.gz').exists())

    def test_missing_rootfs_is_rejected_before_upload(self):
        (self.images / 'rootfs.cpio.gz').unlink()
        with self.assertRaises(FileNotFoundError):
            deploy.package(self.images, self.stage)
        self.assertFalse((self.stage / 'update.tar.gz').exists())

    def test_bootloader_at_wrong_sd_offset_is_rejected_before_upload(self):
        (self.images / 'sdcard.img').write_bytes(b'wrong image')
        with self.assertRaisesRegex(ValueError, '128 KiB'):
            deploy.package(self.images, self.stage)
        self.assertFalse((self.stage / 'update.tar.gz').exists())


class InstallTests(unittest.TestCase):
    """Run the generated installer against real files, with simulated mounts."""
    def setUp(self):
        import os
        import shutil
        self.env = os.environ.copy()
        self.busybox = shutil.which('busybox')
        if not self.busybox:
            self.skipTest('busybox is required for installer integration tests')
        self.directory = tempfile.TemporaryDirectory(prefix='brick-installer-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sd = self.root / 'card'
        self.sd.mkdir()
        self.tmp = self.root / 'tmp'
        self.tmp.mkdir()
        (self.root / 'type').write_text('SD\n')
        (self.root / 'compatible').write_bytes(b'trimui,brick\0allwinner,sun50i-a133\0')
        (self.root / 'mounts').write_text('')
        (self.root / 'device').write_text('179:1\n')
        self.current = self.sd / 'brick-diag'
        self.previous = self.sd / 'brick-previous'
        self.write_payload(self.current, b'current')
        self.write_payload(self.previous, b'previous')
        payload = self.root / 'payload'
        self.write_payload(payload, b'new')
        bootloader = b'test bootloader'
        (payload / deploy.BOOTLOADER).write_bytes(bootloader)
        self.pack_source_payload(payload)
        self.raw = self.root / 'mmcblk0'
        self.raw.write_bytes(bytes(128 * 1024) + bootloader)
        self.env.update(TEST_SD=str(self.sd), REAL_BB=self.busybox)
        shim = self.root / 'busybox'
        shim.write_text('''#!/bin/sh
command=$1
shift
case "$command" in
 mount) rmdir "$2" && ln -s "$TEST_SD" "$2"; exit $? ;;
 umount) rm "$1" && mkdir "$1"; exit $? ;;
 dd)
  case " $*:${FAIL_ACTION:-}" in
   *" of="*:copy) : > "$DD_FAILURE_MARKER"; exit 1 ;;
  esac ;;
 mv)
  case "$1:${FAIL_ACTION:-}" in
   */brick-next:publish|*/brick-next:publish-and-restore|*/brick-previous:publish-and-restore) echo INJECTED-rename >&2; exit 1 ;;
  esac ;;
esac
exec "$REAL_BB" "$command" "$@"
''')
        shim.chmod(0o755)
        script = deploy.install_script('test')
        replacements = {'/tmp/': str(self.tmp) + '/', '/bin/busybox': str(shim),
                        '/sys/class/block/mmcblk0/device/type': str(self.root / 'type'),
                        '/sys/firmware/devicetree/base/compatible': str(self.root / 'compatible'),
                        '/proc/self/mountinfo': str(self.root / 'mounts'),
                        '/sys/class/block/mmcblk0p1/dev': str(self.root / 'device'),
                        '/dev/mmcblk0': str(self.raw)}
        for old, new in replacements.items():
            script = script.replace(old, new)
        self.script = self.root / 'install.sh'
        self.script.write_text(script)

    def pack_source_payload(self, payload):
        archive_lines = []
        for file in payload.iterdir():
            if file.name == 'ARCHIVE-SHA256SUMS':
                continue
            archive_lines.append(f'{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}\n')
        (payload / 'ARCHIVE-SHA256SUMS').write_text(''.join(archive_lines))
        with tarfile.open(self.tmp / 'test.tar.gz', 'w:gz') as archive:
            for file in payload.iterdir():
                archive.add(file, arcname=file.name)

    def write_payload(self, directory, content):
        directory.mkdir()
        lines = []
        for name in deploy.PAYLOAD.values():
            (directory / name).write_bytes(content)
            lines.append(f'{hashlib.sha256(content).hexdigest()}  {name}\n')
        (directory / 'SHA256SUMS').write_text(''.join(lines))
        (directory / 'manifest.json').write_text('{}\n')

    def run_installer(self, failure=''):
        import subprocess
        self.dd_failure_marker = self.root / 'dd-failure'
        self.env['DD_FAILURE_MARKER'] = str(self.dd_failure_marker)
        self.env['FAIL_ACTION'] = failure
        return subprocess.run([self.busybox, 'sh', str(self.script)], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def assert_clean(self):
        self.assertFalse((self.tmp / '.brick-update-lock').exists())
        self.assertFalse((self.sd / 'brick-next').exists())

    def test_success_retains_current_as_previous(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('UPDATE_VERIFIED', result.stdout)
        self.assertEqual((self.current / 'Image').read_bytes(), b'new')
        self.assertEqual((self.previous / 'Image').read_bytes(), b'current')
        self.assertEqual((self.sd / 'brick-bootcount').read_bytes(),
                         bytes((0xbd, 1, 0, 1)))
        self.assertFalse((self.current / deploy.BOOTLOADER).exists())
        self.assert_clean()

    def test_missing_current_manifest_preserves_existing_payloads(self):
        (self.current / 'manifest.json').unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('UPDATE_VERIFIED', result.stdout)
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assertEqual((self.previous / 'Image').read_bytes(), b'previous')
        self.assertTrue((self.previous / 'manifest.json').exists())
        self.assert_clean()

    def test_incompatible_bootloader_refuses_before_sd_mutation(self):
        self.raw.write_bytes(bytes(128 * 1024) + b'old bootloader')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('UPDATE_VERIFIED', result.stdout)
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assertEqual((self.previous / 'Image').read_bytes(), b'previous')
        self.assertFalse((self.sd / 'brick-bootcount').exists())
        self.assert_clean()

    def test_missing_previous_manifest_does_not_delete_rollback(self):
        (self.previous / 'manifest.json').unlink()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('UPDATE_VERIFIED', result.stdout)
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assertEqual((self.previous / 'Image').read_bytes(), b'previous')
        self.assertTrue((self.current / 'manifest.json').exists())
        self.assert_clean()

    def test_truncated_archive_preserves_both_existing_payloads(self):
        archive = self.tmp / 'test.tar.gz'
        archive.write_bytes(archive.read_bytes()[:-12])
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assertEqual((self.previous / 'Image').read_bytes(), b'previous')
        self.assert_clean()

    def test_corrupt_current_preserves_good_previous(self):
        (self.current / 'Image').write_bytes(b'corrupt')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.previous / 'Image').read_bytes(), b'previous')
        self.assert_clean()

    def test_copy_failure_preserves_current(self):
        result = self.run_installer('copy')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.dd_failure_marker.exists())
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assert_clean()

    def test_payload_copy_uses_large_writes_without_remounting(self):
        script = deploy.install_script('test')
        self.assertIn('bs=1048576', script)
        self.assertIn('wc -c < "$destination"', script)
        self.assertNotIn('$bb cp "$work/payload/', script)
        self.assertNotIn('remount', script)

    def test_large_unaligned_payload_roundtrips_exactly(self):
        payload = self.root / 'payload'
        content = b'large-payload-block-' * 140000 + b'tail'
        (payload / 'initramfs.cpio.gz').write_bytes(content)
        lines = []
        for name in deploy.PAYLOAD.values():
            checksum = hashlib.sha256((payload / name).read_bytes()).hexdigest()
            lines.append(f'{checksum}  {name}\n')
        (payload / 'SHA256SUMS').write_text(''.join(lines))
        self.pack_source_payload(payload)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.current / 'initramfs.cpio.gz').read_bytes(), content)

    def test_publish_failure_restores_current(self):
        result = self.run_installer('publish')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('INJECTED-rename', result.stderr)
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assert_clean()

    def test_restore_failure_keeps_both_complete_payloads(self):
        result = self.run_installer('publish-and-restore')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Restore failed', result.stderr)
        self.assertEqual((self.previous / 'Image').read_bytes(), b'current')
        self.assertEqual((self.sd / 'brick-next' / 'Image').read_bytes(), b'new')
        self.assertFalse((self.tmp / '.brick-update-lock').exists())

    def test_already_mounted_card_is_refused(self):
        (self.root / 'mounts').write_text('36 25 179:1 / /other rw - vfat /dev/disk/by-label/BOOT rw\n')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assertEqual((self.previous / 'Image').read_bytes(), b'previous')
        self.assert_clean()

    def test_corrupt_previous_is_not_deleted(self):
        (self.previous / 'Image').write_bytes(b'corrupt')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assertEqual((self.previous / 'Image').read_bytes(), b'corrupt')
        self.assert_clean()

    def test_non_sd_storage_is_refused(self):
        (self.root / 'type').write_text('MMC\n')
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertEqual((self.current / 'Image').read_bytes(), b'current')
        self.assertEqual((self.previous / 'Image').read_bytes(), b'previous')
        self.assert_clean()


class BootIdentityTests(unittest.TestCase):
    def test_changed_dtb_or_rootfs_is_not_accepted_as_same_kernel(self):
        import copy
        expected = {'firmware': {'commit': 'firmware-a', 'dirty': False},
                    'kernel': {'commit': 'kernel-a', 'release': '7.3.0'},
                    'config_sha256': 'config-a',
                    'files': {'Image': {'sha256': 'image-a'},
                              'sun50i-a133-trimui-brick.dtb': {'sha256': 'dtb-a'}}}
        deploy.verify_boot_manifest(copy.deepcopy(expected), expected)
        for field in ('firmware', 'kernel', 'config_sha256', 'files'):
            actual = copy.deepcopy(expected)
            if field == 'files':
                actual[field]['sun50i-a133-trimui-brick.dtb']['sha256'] = 'old-dtb'
            else:
                actual[field] = 'old-build'
            with self.subTest(field=field), self.assertRaises(deploy.ProtocolError):
                deploy.verify_boot_manifest(actual, expected)

    def test_recovery_profile_cannot_be_mistaken_for_normal_image(self):
        expected = {'firmware': {'commit': 'firmware-a'},
                    'kernel': {'commit': 'kernel-a', 'release': '7.3.0'},
                    'config_sha256': 'config-a', 'emmc_recovery': True,
                    'files': {'Image': {'sha256': 'image-a'},
                              'sun50i-a133-trimui-brick.dtb': {'sha256': 'dtb-a'}}}
        actual = dict(expected, emmc_recovery=False)
        with self.assertRaisesRegex(deploy.ProtocolError, 'eMMC recovery'):
            deploy.verify_boot_manifest(actual, expected)

    def test_ve_profile_cannot_be_mistaken_for_normal_image(self):
        expected = {'firmware': {'commit': 'firmware-a'},
                    'kernel': {'commit': 'kernel-a', 'release': '7.3.0'},
                    'config_sha256': 'config-a', 've_qualification': True,
                    'files': {'Image': {'sha256': 'image-a'},
                              'sun50i-a133-trimui-brick.dtb': {'sha256': 'dtb-a'}}}
        actual = dict(expected, ve_qualification=False)
        with self.assertRaisesRegex(deploy.ProtocolError, 'video-engine qualification'):
            deploy.verify_boot_manifest(actual, expected)

    def test_g2d_profile_cannot_be_mistaken_for_normal_image(self):
        expected = {'firmware': {'commit': 'firmware-a'},
                    'kernel': {'commit': 'kernel-a', 'release': '7.3.0'},
                    'config_sha256': 'config-a', 'g2d_qualification': True,
                    'files': {'Image': {'sha256': 'image-a'},
                              'sun50i-a133-trimui-brick.dtb': {'sha256': 'dtb-a'}}}
        actual = dict(expected, g2d_qualification=False)
        with self.assertRaisesRegex(deploy.ProtocolError, 'G2D qualification'):
            deploy.verify_boot_manifest(actual, expected)


class DeploymentLifecycleTests(unittest.TestCase):
    def run_deployment(self, no_reboot=False, install_failure=False, usb_role=None,
                       install_output=b'UPDATE_VERIFIED\n', token_failure=False):
        console = MagicMock()
        console.__enter__.return_value = console
        console.checked.side_effect = lambda command, **kwargs: (
            b'00000000-0000-0000-0000-000000000001\n' if 'boot_id' in command
            else install_output)
        if install_failure:
            console.checked.side_effect = deploy.ProtocolError('installer failed')
        args = ['deploy.py', 'images'] + (['--no-reboot'] if no_reboot else [])
        with patch.object(sys, 'argv', args), \
             patch.object(deploy, 'Console', return_value=console), \
             patch.object(deploy, 'package', return_value=(Path('archive'), {'build': 'new', 'usb_role': usb_role})), \
             patch.object(deploy, 'wait_for_boot') as wait, \
             patch.object(deploy.time, 'sleep'):
            if install_failure or token_failure:
                with self.assertRaises(deploy.ProtocolError):
                    deploy.main()
            else:
                deploy.main()
        return console, wait

    def test_success_requests_init_managed_reboot(self):
        import os
        import subprocess
        import time
        console, wait = self.run_deployment()
        commands = [call.args[0] for call in console.checked.call_args_list]
        reboot = [command for command in commands if 'reboot' in command]
        self.assertEqual(len(reboot), 1)
        # Execute the actual shell command with an inert BusyBox substitute:
        # record reboot arguments; never reboot the host or open a device.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = root / 'busybox'
            record = root / 'reboot-args'
            shim.write_text('#!/bin/sh\n[ "$1" = sleep ] && exit 0\nprintf "%s\\n" "$@" > "$REBOOT_RECORD.tmp"\nmv "$REBOOT_RECORD.tmp" "$REBOOT_RECORD"\n')
            shim.chmod(0o755)
            subprocess.run(['/bin/sh', '-c', reboot[0].replace('/bin/busybox', str(shim))],
                           env={**os.environ, 'REBOOT_RECORD': str(record)}, check=True)
            deadline = time.monotonic() + 2
            while not record.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(record.read_text().splitlines(), ['reboot'])
        wait.assert_called_once()
        self.assertLess(commands.index(next(c for c in commands if c.startswith('/bin/sh '))),
                        commands.index(reboot[0]))

    def test_host_reboot_does_not_wait_for_unavailable_usb_serial(self):
        console, wait = self.run_deployment(usb_role='host')
        self.assertEqual(sum('reboot' in call.args[0] for call in console.checked.call_args_list), 1)
        wait.assert_not_called()

    def test_no_reboot_leaves_device_running_without_waiting_for_boot(self):
        console, wait = self.run_deployment(no_reboot=True)
        self.assertFalse(any('reboot' in call.args[0] for call in console.checked.call_args_list))
        wait.assert_not_called()

    def test_install_failure_does_not_reboot_or_wait_for_boot(self):
        console, wait = self.run_deployment(install_failure=True)
        self.assertFalse(any('reboot' in call.args[0] for call in console.checked.call_args_list))
        wait.assert_not_called()

    def test_missing_completion_token_does_not_reboot(self):
        console, wait = self.run_deployment(install_output=b'', token_failure=True)
        self.assertFalse(any('reboot' in call.args[0] for call in console.checked.call_args_list))
        wait.assert_not_called()

    def test_emmc_recovery_requires_explicit_deployment_flag(self):
        args = ['deploy.py', 'images']
        manifest = {'emmc_recovery': True}
        with patch.object(sys, 'argv', args), \
             patch.object(deploy, 'package', return_value=(Path('archive'), manifest)), \
             patch.object(deploy, 'Console') as console:
            with self.assertRaisesRegex(ValueError, '--emmc-recovery'):
                deploy.main()
        console.assert_not_called()

    def test_ve_qualification_requires_explicit_deployment_flag(self):
        args = ['deploy.py', 'images']
        manifest = {'ve_qualification': True}
        with patch.object(sys, 'argv', args), \
             patch.object(deploy, 'package', return_value=(Path('archive'), manifest)), \
             patch.object(deploy, 'Console') as console:
            with self.assertRaisesRegex(ValueError, '--ve-qualification'):
                deploy.main()
        console.assert_not_called()

    def test_g2d_qualification_requires_explicit_deployment_flag(self):
        args = ['deploy.py', 'images']
        manifest = {'g2d_qualification': True}
        with patch.object(sys, 'argv', args), \
             patch.object(deploy, 'package', return_value=(Path('archive'), manifest)), \
             patch.object(deploy, 'Console') as console:
            with self.assertRaisesRegex(ValueError, '--g2d-qualification'):
                deploy.main()
        console.assert_not_called()


class BootTransitionTests(unittest.TestCase):
    def test_same_image_on_old_boot_is_not_accepted(self):
        old = deploy.uuid.UUID('00000000-0000-0000-0000-000000000001')
        new = deploy.uuid.UUID('00000000-0000-0000-0000-000000000002')
        expected = {'firmware': {}, 'kernel': {'release': 'test'},
                    'config_sha256': 'config',
                    'files': {name: {'sha256': name} for name in
                              ('Image', 'sun50i-a133-trimui-brick.dtb')}}
        clients = []
        for boot in (old, new):
            console = MagicMock()
            console.__enter__.return_value = console
            def checked(command, boot=boot):
                if 'boot_id' in command:
                    return str(boot).encode()
                if 'brick-build.json' in command:
                    return json.dumps(expected).encode()
                return b'test\n'
            console.checked.side_effect = checked
            clients.append(console)
        with patch.object(deploy, 'Console', side_effect=clients) as factory, \
             patch.object(deploy.time, 'sleep'):
            deploy.wait_for_boot('unused', expected, 5, old)
        self.assertEqual(factory.call_count, 2)
        # The first boot is rejected even though its image is identical.
        self.assertEqual(clients[0].checked.call_count, 1)
        self.assertGreater(clients[1].checked.call_count, 1)
