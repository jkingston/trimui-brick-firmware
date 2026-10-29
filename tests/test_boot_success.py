import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SERVICE = (Path(__file__).resolve().parents[1]
           / "board/trimui/brick/rootfs-overlay/etc/init.d/S99boot-success")


class BootSuccessTests(unittest.TestCase):
    def run_action(self, action):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            cat = root / "cat"
            cat.write_text('#!/bin/sh\necho cat >> "$CALLS"\n'
                           "echo 'brick.bootdir=unrelated'\n")
            cat.chmod(0o755)
            result = subprocess.run(
                ["/bin/sh", str(SERVICE), *action],
                env={**os.environ, "PATH": str(root), "CALLS": str(calls)},
                capture_output=True, text=True, timeout=3,
            )
            self.assertFalse(calls.exists(), "action inspected boot/device state")
            return result

    def test_stop_does_not_repeat_boot_success(self):
        result = self.run_action(["stop"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_invalid_action_is_rejected_before_device_access(self):
        for action in ([], ["restart"]):
            with self.subTest(action=action):
                result = self.run_action(action)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stderr)


if __name__ == "__main__":
    unittest.main()
