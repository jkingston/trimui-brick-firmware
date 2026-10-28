"""Hardware selectors reject every unsupported combination early."""
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProfileMatrixTests(unittest.TestCase):
    def make(self, **settings):
        command = ['make', '--no-print-directory', '-n', 'check']
        command.extend(f'{key}={value}' for key, value in settings.items())
        return subprocess.run(command, cwd=ROOT, text=True, capture_output=True)

    def test_supported_profiles_parse(self):
        profiles = (
            {},
            {'USB_ROLE': 'host'},
            {'USB_ROLE': 'auto'},
            {'SCP_PROFILE': 'startup'},
            {'SCP_PROFILE': 'deep'},
            {'EMMC_RECOVERY': '1'},
            {'G2D_PROFILE': 'qualification'},
            {'VE_PROFILE': 'qualification'},
            {'VP9_PROFILE': 'qualification'},
            {'CE_TRNG_PROFILE': 'qualification'},
            {'COMBINED_ACCELERATOR_PROFILE': 'qualification'},
            {'CPU_IDLE_PROFILE': 'qualification'},
            {'CPU_IDLE_PROFILE': 'qualification', 'SCP_PROFILE': 'deep'},
        )
        for settings in profiles:
            with self.subTest(settings=settings):
                result = self.make(**settings)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_qualification_profiles_are_isolated(self):
        cases = (
            ({'SCP_PROFILE': 'startup', 'USB_ROLE': 'host'}, 'requires USB_ROLE'),
            ({'EMMC_RECOVERY': '1', 'USB_ROLE': 'host'}, 'requires USB_ROLE'),
            ({'EMMC_RECOVERY': '1', 'SCP_PROFILE': 'startup'}, 'mutually exclusive'),
            ({'EMMC_RECOVERY': '1', 'G2D_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'EMMC_RECOVERY': '1', 'VE_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'EMMC_RECOVERY': '1', 'VP9_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'EMMC_RECOVERY': '1', 'CE_TRNG_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'EMMC_RECOVERY': '1', 'COMBINED_ACCELERATOR_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'G2D_PROFILE': 'qualification', 'USB_ROLE': 'auto'}, 'requires USB_ROLE'),
            ({'VE_PROFILE': 'qualification', 'USB_ROLE': 'host'}, 'requires USB_ROLE'),
            ({'VP9_PROFILE': 'qualification', 'USB_ROLE': 'host'}, 'requires USB_ROLE'),
            ({'CE_TRNG_PROFILE': 'qualification', 'USB_ROLE': 'auto'}, 'requires USB_ROLE'),
            ({'COMBINED_ACCELERATOR_PROFILE': 'qualification', 'USB_ROLE': 'host'}, 'requires USB_ROLE'),
            ({'G2D_PROFILE': 'qualification', 'SCP_PROFILE': 'deep'}, 'mutually exclusive'),
            ({'VE_PROFILE': 'qualification', 'SCP_PROFILE': 'startup'}, 'mutually exclusive'),
            ({'VP9_PROFILE': 'qualification', 'SCP_PROFILE': 'startup'}, 'mutually exclusive'),
            ({'CE_TRNG_PROFILE': 'qualification', 'SCP_PROFILE': 'deep'}, 'mutually exclusive'),
            ({'COMBINED_ACCELERATOR_PROFILE': 'qualification', 'SCP_PROFILE': 'deep'}, 'mutually exclusive'),
            ({'G2D_PROFILE': 'qualification', 'VE_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'G2D_PROFILE': 'qualification', 'VP9_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'G2D_PROFILE': 'qualification', 'CE_TRNG_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'VE_PROFILE': 'qualification', 'VP9_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'VE_PROFILE': 'qualification', 'CE_TRNG_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'VP9_PROFILE': 'qualification', 'CE_TRNG_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'COMBINED_ACCELERATOR_PROFILE': 'qualification', 'G2D_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'COMBINED_ACCELERATOR_PROFILE': 'qualification', 'VE_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'COMBINED_ACCELERATOR_PROFILE': 'qualification', 'VP9_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'COMBINED_ACCELERATOR_PROFILE': 'qualification', 'CE_TRNG_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'CPU_IDLE_PROFILE': 'qualification', 'USB_ROLE': 'host'}, 'requires USB_ROLE'),
            ({'CPU_IDLE_PROFILE': 'qualification', 'SCP_PROFILE': 'startup'}, 'disabled or deep'),
            ({'CPU_IDLE_PROFILE': 'qualification', 'EMMC_RECOVERY': '1'}, 'mutually exclusive'),
            ({'CPU_IDLE_PROFILE': 'qualification', 'G2D_PROFILE': 'qualification'}, 'mutually exclusive'),
            ({'CPU_IDLE_PROFILE': 'qualification', 'COMBINED_ACCELERATOR_PROFILE': 'qualification'}, 'mutually exclusive'),
        )
        for settings, message in cases:
            with self.subTest(settings=settings):
                result = self.make(**settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)


if __name__ == '__main__':
    unittest.main()
