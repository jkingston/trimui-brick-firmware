"""Validate deterministic accelerator fixtures and target entry points."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'package/brick-accelerator-qualification'


class AcceleratorQualificationTests(unittest.TestCase):
    def test_g2d_fixture_generation_and_comparator(self):
        compiler = shutil.which('cc')
        if not compiler:
            self.skipTest('host C compiler is required')
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            shutil.copy(PACKAGE / 'generate-g2d-fixtures.py', work)
            subprocess.run([sys.executable, work / 'generate-g2d-fixtures.py'],
                           check=True, capture_output=True, text=True)
            cases = (work / 'cases.tsv').read_text().splitlines()
            self.assertEqual(len(cases), 30)
            self.assertEqual(len({line.split('\t', 1)[0] for line in cases}), 30)
            comparator = work / 'compare'
            subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror',
                            PACKAGE / 'g2d-compare.c', '-o', comparator], check=True)
            fixture = work / 'fixtures/g2d/rotate-90.expected'
            mask = work / 'fixtures/g2d/rotate-90.mask'
            subprocess.run([comparator, fixture, fixture, mask, '0'], check=True,
                           capture_output=True, text=True)
            changed = work / 'changed'
            data = bytearray(fixture.read_bytes())
            data[0] ^= 0xff
            changed.write_bytes(data)
            self.assertEqual(subprocess.run(
                [comparator, changed, fixture, mask, '0'], capture_output=True).returncode, 1)

    def test_ve_encoded_fixture_hashes_and_shell_syntax(self):
        fixture_dir = PACKAGE / 'fixtures/ve'
        lines = (fixture_dir / 'FIXTURE-SHA256').read_text().splitlines()
        self.assertEqual(len(lines), 6)
        for line in lines:
            digest, name = line.split()
            self.assertEqual(hashlib.sha256((fixture_dir / name).read_bytes()).hexdigest(),
                             digest)
        self.assertEqual({line.split()[1].split('.', 1)[0] for line in lines},
                         {line.split()[1].split('.', 1)[0] for line in
                          (fixture_dir / 'REFERENCE-SHA256').read_text().splitlines()})
        vp9_fixture = fixture_dir.parent / 'vp9/vp9-ip.ivf'
        self.assertEqual(hashlib.sha256(vp9_fixture.read_bytes()).hexdigest(),
                         '6f9849001939af34e3d867a109289890c9585ca3eae802bd7ffb2143242ed70d')
        for script in ('brick-qualify-g2d', 'brick-qualify-ve', 'brick-qualify-vp9'):
            subprocess.run(['/bin/sh', '-n', PACKAGE / script], check=True)

    def test_target_qualification_is_bounded_and_has_explicit_build_dependency(self):
        package = (PACKAGE / 'brick-accelerator-qualification.mk').read_text()
        config = (PACKAGE / 'Config.in').read_text()
        g2d = (PACKAGE / 'brick-qualify-g2d').read_text()
        ve = (PACKAGE / 'brick-qualify-ve').read_text()
        vp9 = (PACKAGE / 'brick-qualify-vp9').read_text()
        busybox = (ROOT / 'board/trimui/brick/busybox.fragment').read_text()
        self.assertIn('BRICK_ACCELERATOR_QUALIFICATION_DEPENDENCIES += host-python3',
                      package)
        self.assertIn('select BR2_PACKAGE_GST1_PLUGINS_BAD_PLUGIN_IVFPARSE', config)
        self.assertIn('timeout 120 v4l2-compliance', g2d)
        self.assertIn('timeout 15 "$tools/brick-g2d-m2m"', g2d)
        self.assertIn('G2D clock is not at stock 300 MHz', g2d)
        self.assertIn('timeout 120 v4l2-compliance', ve)
        self.assertIn('VE clock is not at stock 432 MHz', ve)
        self.assertIn('v4l2slvp8dec', ve)
        self.assertIn('vp8-ip) suffix=ivf; parser=ivfparse', ve)
        self.assertIn('fixture changed during decode', ve)
        self.assertIn('v4l2slvp9dec', vp9)
        self.assertIn('isolated-no-iommu', vp9)
        self.assertIn('timeout 120 v4l2-compliance', vp9)
        self.assertIn('trap finish EXIT', g2d)
        self.assertIn('trap finish EXIT', ve)
        self.assertIn('CONFIG_TIMEOUT=y', busybox.splitlines())


if __name__ == '__main__':
    unittest.main()
