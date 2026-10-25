"""Exercise profile selection, actual DT payloads and gadget startup gating."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import usb_profile
import deploy


class ProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'config'
        self.config.write_bytes((ROOT / 'configs/brick_defconfig').read_bytes())

    def test_default_and_host_selection_preserve_kernel_pin(self):
        before = self.config.read_text()
        self.assertEqual(usb_profile.profile(self.config), 'peripheral')
        usb_profile.configure(self.config, 'host')
        self.assertEqual(usb_profile.profile(self.config), 'host')
        usb_profile.configure(self.config, 'peripheral')
        self.assertEqual(self.config.read_text(), before)

    def test_profile_change_requires_new_output(self):
        usb_profile.check_output(self.config, 'peripheral')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            usb_profile.check_output(self.config, 'host')
        usb_profile.check_output(self.root / 'new-config', 'host')

    def test_ambiguous_or_unknown_dtb_is_rejected(self):
        self.config.write_text(usb_profile.KEY + '="other-board allwinner/sun50i-a133-trimui-brick"\n')
        with self.assertRaises(ValueError):
            usb_profile.profile(self.config)
        value = usb_profile.KEY + '="allwinner/sun50i-a133-trimui-brick"\n'
        self.config.write_text(value * 2)
        with self.assertRaises(ValueError):
            usb_profile.profile(self.config)

    def compile_dtb(self, role, gadget_status=None):
        dtc, fdtget = shutil.which('dtc'), shutil.which('fdtget')
        if not dtc or not fdtget:
            self.skipTest('dtc and fdtget are required for real DT payload tests')
        gadget = gadget_status or ('okay' if role == 'peripheral' else 'disabled')
        host = 'okay' if role == 'host' else 'disabled'
        source = f'''/dts-v1/;
/ {{ #address-cells = <1>; #size-cells = <1>;
 soc {{ #address-cells = <1>; #size-cells = <1>; ranges;
 usb@5100000 {{ reg = <0x5100000 0x400>; status = "{gadget}"; dr_mode = "peripheral"; }};
 usb@5101000 {{ reg = <0x5101000 0x100>; status = "{host}"; dr_mode = "host"; }};
 usb@5101400 {{ reg = <0x5101400 0x100>; status = "{host}"; dr_mode = "host"; }};
 }};
}};
'''
        output = self.root / (usb_profile.DTBS[role] + '.dtb')
        subprocess.run([dtc, '-I', 'dts', '-O', 'dtb', '-o', str(output)],
                       input=source, text=True, check=True, capture_output=True)
        return output, fdtget

    def test_host_payload_manifest_and_archive_are_consistent(self):
        usb_profile.configure(self.config, 'host')
        source, fdtget = self.compile_dtb('host')
        expected = source.read_bytes()
        canonical = self.root / (usb_profile.DTBS['peripheral'] + '.dtb')
        canonical.write_bytes(b'stale peripheral DTB')
        usb_profile.install(self.root, self.config, fdtget)
        self.assertEqual(canonical.read_bytes(), expected)
        for name in ['Image', 'rootfs.cpio.gz', 'u-boot-sunxi-with-spl.bin']:
            (self.root / name).write_bytes(name.encode())
        (self.root / 'kernel.release').write_text('test-kernel\n')
        subprocess.run([sys.executable, str(ROOT / 'tools/manifest.py'), '--payload',
                        str(self.root), str(self.config), 'test-revision'], check=True)
        manifest = json.loads((self.root / 'payload-manifest.json').read_text())
        self.assertEqual(manifest['usb_role'], 'host')
        self.assertEqual(manifest['files'][canonical.name]['sha256'], hashlib.sha256(expected).hexdigest())
        (self.root / 'manifest.json').write_text(json.dumps(manifest))
        stage = self.root / 'stage'
        stage.mkdir()
        archive, _ = deploy.package(self.root, stage)
        with tarfile.open(archive) as contents:
            self.assertEqual(contents.extractfile('brick-usb.dtb').read(), expected)
        with self.assertRaises(deploy.ProtocolError):
            deploy.verify_boot_manifest(dict(manifest, usb_role='peripheral'), manifest)

    def test_default_dtb_is_unchanged(self):
        source, fdtget = self.compile_dtb('peripheral')
        before = source.read_bytes()
        usb_profile.install(self.root, self.config, fdtget)
        self.assertEqual(source.read_bytes(), before)

    def test_host_profile_rejects_enabled_gadget_before_overwriting_payload(self):
        usb_profile.configure(self.config, 'host')
        _, fdtget = self.compile_dtb('host', gadget_status='okay')
        canonical = self.root / (usb_profile.DTBS['peripheral'] + '.dtb')
        canonical.write_bytes(b'previous')
        with self.assertRaises(ValueError):
            usb_profile.install(self.root, self.config, fdtget)
        self.assertEqual(canonical.read_bytes(), b'previous')

    def test_missing_host_dtb_cannot_reuse_default_dtb(self):
        usb_profile.configure(self.config, 'host')
        with self.assertRaises(FileNotFoundError):
            usb_profile.install(self.root, self.config, '/not-invoked')

    def test_gadget_service_uses_live_dt_status(self):
        script = (ROOT / 'board/trimui/brick/rootfs-overlay/etc/init.d/S30usb').read_text()
        status = self.root / 'status'
        configfs = self.root / 'configfs'
        (configfs / 'usb_gadget/brick').mkdir(parents=True)
        marker = self.root / 'called'
        command = self.root / 'mountpoint'
        command.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 0\n')
        command.chmod(0o755)
        script = script.replace('/sys/firmware/devicetree/base/soc/usb@5100000/status', str(status))
        script = script.replace('/sys/kernel/config', str(configfs))
        env = dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH'])
        for value, expected, called in [(b'disabled\0', 0, False), (b'bad\0', 1, False), (b'okay\0', 0, True)]:
            with self.subTest(status=value):
                status.write_bytes(value)
                result = subprocess.run(['sh', '-c', script, 'S30usb', 'start'], env=env)
                self.assertEqual(result.returncode, expected)
                self.assertEqual(marker.exists(), called)
