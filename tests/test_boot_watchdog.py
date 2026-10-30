from pathlib import Path
import subprocess
import tempfile
import time
import unittest


WATCHDOG = (Path(__file__).resolve().parents[1]
            / "board/trimui/brick/rootfs-overlay/usr/libexec/brick-boot-watchdog")


class BootWatchdogTests(unittest.TestCase):
    def test_lease_services_watchdog_until_verified_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            device = root / "watchdog"
            script = root / "lease"
            script.write_text(
                WATCHDOG.read_text()
                .replace("/run/brick-boot-watchdog.release", str(release))
                .replace("/dev/watchdog", str(device))
                .replace("sleep 2", "sleep 0.01")
            )
            process = subprocess.Popen(["sh", script])
            for _ in range(100):
                if device.exists() and device.stat().st_size:
                    break
                time.sleep(0.01)
            else:
                process.kill()
                self.fail("watchdog lease did not service the device")
            release.touch()
            self.assertEqual(process.wait(timeout=2), 0)
            self.assertTrue(device.read_bytes().startswith(b"x"))
            self.assertTrue(device.read_bytes().endswith(b"V"))


if __name__ == "__main__":
    unittest.main()
