"""Image preparation must preserve package scripts and avoid duplicate services."""
from pathlib import Path
import subprocess
import tempfile
import unittest

PREPARE = Path(__file__).resolve().parents[1] / 'board/trimui/brick/prepare-state.sh'
SERVICES = {
    'S01seedrng': 'S13seedrng',
    'S30dbus-daemon': 'S30dbus-daemon',
    'S40bluetoothd': 'S40bluetoothd',
    'S45wifi': 'S45wifi',
    'S46bluealsa': 'S46bluealsa',
    'S50dropbear': 'S50dropbear',
}


class StatePreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.init = self.root / 'etc/init.d'
        self.init.mkdir(parents=True)
        self.originals = {}
        for old, new in SERVICES.items():
            source = '#!/bin/sh\n# package ' + old + '\nexit 0\n'
            (self.init / old).write_text(source)
            (self.init / old).chmod(0o755)
            self.originals[new] = source

    def prepare(self):
        return subprocess.run(['sh', str(PREPARE), str(self.root)],
                              capture_output=True, text=True)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def test_repeat_preserves_originals_and_one_seed_service(self):
        self.assertEqual(self.prepare().returncode, 0)
        before = self.snapshot()
        self.assertEqual(self.prepare().returncode, 0)
        self.assertEqual(self.snapshot(), before)
        for name, text in self.originals.items():
            self.assertEqual((self.root / 'usr/libexec/brick-init' / name).read_text(), text)
        self.assertFalse((self.init / 'S01seedrng').exists())
        self.assertTrue((self.init / 'S13seedrng').exists())

    def test_package_reinstall_refreshes_originals_without_nesting(self):
        self.assertEqual(self.prepare().returncode, 0)
        for old, new in SERVICES.items():
            (self.init / old).write_text(self.originals[new] + '# upgraded\n')
            (self.init / old).chmod(0o755)
        self.assertEqual(self.prepare().returncode, 0)
        self.assertEqual(self.prepare().returncode, 0)
        for name, text in self.originals.items():
            self.assertEqual((self.root / 'usr/libexec/brick-init' / name).read_text(),
                             text + '# upgraded\n')

    def test_missing_saved_original_fails(self):
        self.assertEqual(self.prepare().returncode, 0)
        (self.root / 'usr/libexec/brick-init/S13seedrng').unlink()
        self.assertNotEqual(self.prepare().returncode, 0)

    def test_only_persistent_profile_allows_saving(self):
        self.assertEqual(self.prepare().returncode, 0)
        share = self.root / 'usr/share/brick'
        self.assertIn('update_config=0\n', (share / 'wpa-volatile.conf').read_text())
        self.assertIn('update_config=1\n', (share / 'wpa_supplicant.conf').read_text())


if __name__ == '__main__':
    unittest.main()
