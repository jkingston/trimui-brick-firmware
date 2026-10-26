"""Exercise fail-closed SCP startup profile selection."""
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import scp_profile


class ScpProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / '.config'
        self.config.write_text('BR2_OTHER=y\n# BR2_PACKAGE_BRICK_SCP is not set\n')

    def test_selection_preserves_unrelated_config(self):
        self.assertEqual(scp_profile.profile(self.config), 'disabled')
        scp_profile.configure(self.config, 'startup')
        self.assertEqual(self.config.read_text(),
                         'BR2_OTHER=y\nBR2_PACKAGE_BRICK_SCP=y\n')
        scp_profile.configure(self.config, 'disabled')
        self.assertEqual(scp_profile.profile(self.config), 'disabled')

    def test_same_output_rejects_both_profile_changes(self):
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            scp_profile.check_output(self.config, 'startup')
        scp_profile.configure(self.config, 'startup')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            scp_profile.check_output(self.config, 'disabled')

    def test_missing_or_duplicate_selection_is_rejected(self):
        for text in ('BR2_OTHER=y\n',
                     'BR2_PACKAGE_BRICK_SCP=y\n'
                     '# BR2_PACKAGE_BRICK_SCP is not set\n'):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(ValueError):
                    scp_profile.profile(self.config)

    def test_new_output_is_accepted(self):
        scp_profile.check_output(self.config.with_name('new-config'), 'startup')


if __name__ == '__main__':
    unittest.main()
