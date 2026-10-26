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
        self.config.write_text(
            'BR2_OTHER=y\n# BR2_PACKAGE_BRICK_SCP is not set\n'
            '# BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND is not set\n'
            'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=""\n')

    def test_selection_preserves_unrelated_config(self):
        self.assertEqual(scp_profile.profile(self.config), 'disabled')
        scp_profile.configure(self.config, 'startup')
        self.assertEqual(self.config.read_text(),
                         'BR2_OTHER=y\nBR2_PACKAGE_BRICK_SCP=y\n'
                         '# BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND is not set\n'
                         f'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES="{scp_profile.FRAGMENT}"\n')
        scp_profile.configure(self.config, 'deep')
        self.assertEqual(scp_profile.profile(self.config), 'deep')
        scp_profile.configure(self.config, 'disabled')
        self.assertEqual(scp_profile.profile(self.config), 'disabled')

    def test_same_output_rejects_both_profile_changes(self):
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            scp_profile.check_output(self.config, 'startup')
        scp_profile.configure(self.config, 'startup')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            scp_profile.check_output(self.config, 'deep')
        with self.assertRaisesRegex(ValueError, 'fresh output'):
            scp_profile.check_output(self.config, 'disabled')

    def test_missing_or_duplicate_selection_is_rejected(self):
        for text in ('BR2_OTHER=y\n',
                     'BR2_PACKAGE_BRICK_SCP=y\n'
                     '# BR2_PACKAGE_BRICK_SCP is not set\n'):
            with self.subTest(text=text):
                self.config.write_text(
                    text + '# BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND is not set\n'
                    'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=""\n')
                with self.assertRaises(ValueError):
                    scp_profile.profile(self.config)

    def test_mixed_package_and_fragment_profiles_are_rejected(self):
        self.config.write_text(
            'BR2_PACKAGE_BRICK_SCP=y\n'
            '# BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND is not set\n'
            'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=""\n')
        with self.assertRaisesRegex(ValueError, 'do not form'):
            scp_profile.profile(self.config)

    def test_kernel_verification_requires_config_and_object_as_a_pair(self):
        kernel = self.config.parent / 'linux'
        obj = kernel / 'drivers/irqchip/irq-sun50i-a133-wakeupgen.o'
        obj.parent.mkdir(parents=True)
        (kernel / '.config').write_text(
            '# CONFIG_SUN50I_A133_WAKEUPGEN is not set\n')
        scp_profile.verify_kernel(self.config, kernel)
        scp_profile.configure(self.config, 'startup')
        (kernel / '.config').write_text('CONFIG_SUN50I_A133_WAKEUPGEN=y\n')
        with self.assertRaisesRegex(ValueError, 'object'):
            scp_profile.verify_kernel(self.config, kernel)
        obj.write_bytes(b'object')
        scp_profile.verify_kernel(self.config, kernel)
        scp_profile.configure(self.config, 'deep')
        scp_profile.verify_kernel(self.config, kernel)

    def test_system_suspend_requires_complete_scp_profile(self):
        self.config.write_text(
            '# BR2_PACKAGE_BRICK_SCP is not set\n'
            'BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND=y\n'
            'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES=""\n')
        with self.assertRaisesRegex(ValueError, 'do not form'):
            scp_profile.profile(self.config)

    def test_new_output_is_accepted(self):
        scp_profile.check_output(self.config.with_name('new-config'), 'startup')


if __name__ == '__main__':
    unittest.main()
