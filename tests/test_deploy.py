"""Artifact checks before deployment can open a serial device."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

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
        for name in deploy.PAYLOAD:
            data = ('test ' + name).encode()
            (self.images / name).write_bytes(data)
            files[name] = {'sha256': hashlib.sha256(data).hexdigest()}
        (self.images / 'manifest.json').write_text(json.dumps({'files': files}))

    def test_archive_names_and_checksums_match_sd_layout(self):
        archive, _ = deploy.package(self.images, self.stage)
        with tarfile.open(archive) as contents:
            self.assertEqual(set(contents.getnames()),
                             {*deploy.PAYLOAD.values(), 'SHA256SUMS', 'manifest.json'})
            checksums = contents.extractfile('SHA256SUMS').read().decode()
            for line in checksums.splitlines():
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
