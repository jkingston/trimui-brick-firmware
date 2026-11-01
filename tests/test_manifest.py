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
import combined_accelerator_profile
import cpu_idle_profile
import scp_profile
import usb_profile


class ManifestProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.images = self.root / 'images'
        self.images.mkdir()
        self.config = self.root / 'config'
        self.config.write_bytes((ROOT / 'configs/brick_defconfig').read_bytes())
        scp_profile.configure(self.config, 'disabled')
        usb_profile.configure(self.config, 'peripheral', 'disabled')
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
        self.assertNotIn('vp9_qualification', manifest)
        expected = {
            'g2d_qualification': False,
            've_qualification': False,
            'ce_trng_qualification': False,
            'combined_accelerator_qualification': False,
            'cpu_idle_qualification': False,
        }
        if selected:
            expected[selected] = True
        self.assertEqual({key: manifest[key] for key in expected}, expected)

    def test_normal_manifest_disables_all_accelerator_qualification(self):
        self.assert_qualification(None)

    def test_ce_trng_manifest_has_exact_profile_identity(self):
        ce_trng_profile.configure(self.config, 'qualification')
        self.assert_qualification('ce_trng_qualification')

    def test_combined_manifest_has_exact_profile_identity(self):
        combined_accelerator_profile.configure(self.config, 'qualification')
        self.assert_qualification('combined_accelerator_qualification')

    def test_cpu_idle_manifest_has_exact_profile_identity(self):
        cpu_idle_profile.configure(self.config, 'qualification')
        self.assert_qualification('cpu_idle_qualification')

    def test_manifest_records_composed_product_revision(self):
        product = self.root / 'product'
        product.mkdir()
        subprocess.run(['git', 'init', '-q', str(product)], check=True)
        subprocess.run(['git', '-C', str(product), 'config', 'user.email',
                        'test@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(product), 'config', 'user.name',
                        'Test'], check=True)
        (product / 'source').write_text('product\n')
        subprocess.run(['git', '-C', str(product), 'add', 'source'], check=True)
        subprocess.run(['git', '-C', str(product), 'commit', '-q', '-m',
                        'product'], check=True)
        with self.config.open('a') as config:
            config.write('BR2_EXTERNAL_NAMES="BRICK_UI"\n')
            config.write(f'BR2_EXTERNAL_BRICK_UI_PATH="{product}"\n')
        expected = subprocess.check_output(
            ['git', '-C', str(product), 'rev-parse', 'HEAD'], text=True).strip()
        manifest = self.manifest()
        self.assertEqual(manifest['externals']['brick_ui'], {
            'commit': expected,
            'dirty': False,
            'path_name': 'BRICK_UI',
        })


if __name__ == '__main__':
    unittest.main()
