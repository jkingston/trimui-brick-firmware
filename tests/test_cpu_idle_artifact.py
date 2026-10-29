"""Verify produced TF-A artifacts match the selected suspend profile."""
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import cpu_idle_artifact
import cpu_idle_profile
import scp_profile
import usb_profile


class CpuIdleArtifactTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / '.config'
        self.config.write_bytes((ROOT / 'configs/brick_defconfig').read_bytes())

    def select_idle(self):
        cpu_idle_profile.configure(self.config, 'qualification')

    def select_deep(self):
        scp_profile.configure(self.config, 'deep')
        usb_profile.configure(self.config, 'peripheral', 'deep')

    def select_combined(self):
        self.select_deep()
        cpu_idle_profile.configure(self.config, 'qualification')

    def test_default_requires_both_features_absent(self):
        cpu_idle_artifact.verify_symbols(self.config, set())
        with self.assertRaisesRegex(ValueError, 'all-or-none'):
            cpu_idle_artifact.verify_symbols(
                self.config, cpu_idle_artifact.NATIVE_SYMBOLS)
        with self.assertRaisesRegex(ValueError, 'system suspend'):
            cpu_idle_artifact.verify_symbols(
                self.config, cpu_idle_artifact.SYSTEM_SYMBOLS)

    def test_isolated_idle_requires_complete_native_feature(self):
        self.select_idle()
        cpu_idle_artifact.verify_symbols(
            self.config, cpu_idle_artifact.NATIVE_SYMBOLS |
            cpu_idle_artifact.SUSPEND_SYMBOLS)
        with self.assertRaisesRegex(ValueError, 'suspend callbacks'):
            cpu_idle_artifact.verify_symbols(self.config, set())
        partial = set(cpu_idle_artifact.SUSPEND_SYMBOLS)
        partial.pop()
        with self.assertRaisesRegex(ValueError, 'partial A133 suspend'):
            cpu_idle_artifact.verify_symbols(
                self.config, cpu_idle_artifact.NATIVE_SYMBOLS | partial)

    def test_deep_only_requires_shared_callbacks_without_cpu_idle(self):
        self.select_deep()
        deep = (cpu_idle_artifact.SUSPEND_SYMBOLS |
                cpu_idle_artifact.SYSTEM_SYMBOLS)
        cpu_idle_artifact.verify_symbols(self.config, deep)
        with self.assertRaisesRegex(ValueError, 'CPU-suspend all-or-none'):
            cpu_idle_artifact.verify_symbols(
                self.config, deep | cpu_idle_artifact.NATIVE_SYMBOLS)
        with self.assertRaisesRegex(ValueError, 'suspend callbacks'):
            cpu_idle_artifact.verify_symbols(
                self.config, cpu_idle_artifact.SYSTEM_SYMBOLS)

    def test_combined_requires_native_and_system_features(self):
        self.select_combined()
        both = (cpu_idle_artifact.NATIVE_SYMBOLS |
                cpu_idle_artifact.SUSPEND_SYMBOLS |
                cpu_idle_artifact.SYSTEM_SYMBOLS)
        cpu_idle_artifact.verify_symbols(self.config, both)
        with self.assertRaisesRegex(ValueError, 'system suspend'):
            cpu_idle_artifact.verify_symbols(
                self.config, cpu_idle_artifact.NATIVE_SYMBOLS |
                cpu_idle_artifact.SUSPEND_SYMBOLS)
        partial = both - {next(iter(cpu_idle_artifact.SYSTEM_SYMBOLS))}
        with self.assertRaisesRegex(ValueError, 'partial system'):
            cpu_idle_artifact.verify_symbols(self.config, partial)

    def test_build_hook_verifies_every_produced_bl31(self):
        external = (ROOT / 'external.mk').read_text()
        self.assertIn('ARM_TRUSTED_FIRMWARE_POST_INSTALL_IMAGES_HOOKS += '
                      'BRICK_CHECK_CPU_IDLE_TFA_PROFILE', external)
        self.assertIn('$(ARM_TRUSTED_FIRMWARE_IMG_DIR)/bl31/bl31.elf',
                      external)


if __name__ == '__main__':
    unittest.main()
