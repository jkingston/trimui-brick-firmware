import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import verify_release


class VerifyReleaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.images = Path(directory.name)
        files = {}
        sums = []
        for name in sorted(verify_release.REQUIRED):
            path = self.images / name
            path.write_bytes(('release:' + name).encode())
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            files[name] = {'sha256': sha256, 'bytes': path.stat().st_size}
            sums.append(f'{sha256}  {name}\n')
        manifest = {
            'firmware': {'commit': 'f' * 40, 'dirty': False},
            'kernel': {'commit': 'k' * 40, 'dirty': False},
            'externals': {'product': {'commit': 'p' * 40, 'dirty': False}},
            'files': files,
        }
        (self.images / 'manifest.json').write_text(json.dumps(manifest))
        (self.images / 'SHA256SUMS').write_text(''.join(sums))

    def test_accepts_complete_clean_release(self):
        verify_release.verify(self.images)

    def test_rejects_modified_payload(self):
        (self.images / 'Image').write_bytes(b'modified')
        with self.assertRaisesRegex(ValueError, 'Image'):
            verify_release.verify(self.images)

    def test_rejects_dirty_product_external(self):
        path = self.images / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['externals']['product']['dirty'] = True
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'product'):
            verify_release.verify(self.images)


if __name__ == '__main__':
    unittest.main()
