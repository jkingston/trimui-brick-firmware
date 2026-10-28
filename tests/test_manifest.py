"""Verify that build manifests identify isolated qualification profiles."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import ce_trng_profile
import vp9_profile
import combined_accelerator_profile
import cpu_idle_profile


class ManifestProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.images = self.root / 'images'
        self.images.mkdir()
        self.config = self.root / 'config'
        self.config.write_bytes((ROOT / 'configs/brick_defconfig').read_bytes())
        with self.config.open('a') as config:
            config.write('# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV is not set\n')
        (self.images / 'Image').write_bytes(b'kernel')
        (self.images / 'sun50i-a133-trimui-brick.dtb').write_bytes(b'dtb')
        (self.images / 'kernel.release').write_text('test-kernel\n')
        self.rootfs = self.root / 'rootfs'
        (self.rootfs / 'etc').mkdir(parents=True)

    def manifest(self):
        subprocess.run([
            sys.executable, str(ROOT / 'tools/manifest.py'),
            '--rootfs', str(self.rootfs), str(self.images),
            str(self.config), 'test-revision'], check=True)
        return json.loads((self.rootfs / 'etc/brick-build.json').read_text())

    def assert_qualification(self, selected):
        manifest = self.manifest()
        expected = {
            'g2d_qualification': False,
            've_qualification': False,
            'vp9_qualification': False,
            'ce_trng_qualification': False,
            'combined_accelerator_qualification': False,
            'cpu_idle_qualification': False,
        }
        if selected:
            expected[selected] = True
        self.assertEqual({key: manifest[key] for key in expected}, expected)

    def test_normal_manifest_disables_all_accelerator_qualification(self):
        self.assert_qualification(None)

    def test_vp9_manifest_has_exact_profile_identity(self):
        vp9_profile.configure(self.config, 'qualification')
        self.assert_qualification('vp9_qualification')

    def test_ce_trng_manifest_has_exact_profile_identity(self):
        ce_trng_profile.configure(self.config, 'qualification')
        self.assert_qualification('ce_trng_qualification')

    def test_combined_manifest_has_exact_profile_identity(self):
        combined_accelerator_profile.configure(self.config, 'qualification')
        self.assert_qualification('combined_accelerator_qualification')

    def test_cpu_idle_manifest_has_exact_profile_identity(self):
        cpu_idle_profile.configure(self.config, 'qualification')
        self.assert_qualification('cpu_idle_qualification')


if __name__ == '__main__':
    unittest.main()
