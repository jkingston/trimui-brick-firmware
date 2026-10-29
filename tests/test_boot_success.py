import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SERVICE = (Path(__file__).resolve().parents[1]
           / "board/trimui/brick/rootfs-overlay/etc/init.d/S99boot-success")


class BootSuccessTests(unittest.TestCase):
    def run_action(self, action, cmdline='brick.bootdir=unrelated'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            cat = root / "cat"
            cat.write_text('#!/bin/sh\necho "$1" >> "$CALLS"\n'
                           'case "$1" in\n'
                           f'/proc/cmdline) echo {cmdline!r} ;;\n'
                           '*) echo MMC ;;\n'
                           'esac\n')
            cat.chmod(0o755)
            result = subprocess.run(
                ["/bin/sh", str(SERVICE), *action],
                env={**os.environ, "PATH": str(root), "CALLS": str(calls)},
                capture_output=True, text=True, timeout=3,
            )
            inspected = calls.read_text().splitlines() if calls.exists() else []
            return result, inspected

    def test_stop_does_not_repeat_boot_success(self):
        result, inspected = self.run_action(["stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertEqual(inspected, [])

    def test_invalid_action_is_rejected_before_device_access(self):
        for action in ([], ["restart"]):
            with self.subTest(action=action):
                result, inspected = self.run_action(action)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stderr)
                self.assertEqual(inspected, [])

    def test_unrelated_boot_does_not_inspect_devices(self):
        result, inspected = self.run_action(["start"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(inspected, ["/proc/cmdline"])

    def test_all_managed_slots_reach_storage_validation(self):
        for bootdir in ("brick-diag", "brick-previous", "brick-baseline"):
            with self.subTest(bootdir=bootdir):
                result, inspected = self.run_action(
                    ["start"], f"console=ttyS0 brick.bootdir={bootdir}")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(inspected,
                                 ["/proc/cmdline",
                                  "/sys/class/block/mmcblk0/device/type"])


if __name__ == "__main__":
    unittest.main()
