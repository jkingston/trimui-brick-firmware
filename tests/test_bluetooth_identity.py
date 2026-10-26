"""Bluetooth identity is explicit, persistent and applied before consumers."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "package/brick-bluetooth-identity/brick-bluetooth-identity.c"
SERVICE = ROOT / "board/trimui/brick/rootfs-overlay/etc/init.d/S39bluetooth-identity"
WRAPPER = ROOT / "board/trimui/brick/rootfs-overlay/usr/libexec/brick-state-service"


class BluetoothIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "brick-bluetooth-identity"
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                        str(SOURCE), "-o", str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def check(self, contents):
        with tempfile.NamedTemporaryFile("wb") as identity:
            identity.write(contents)
            identity.flush()
            return subprocess.run([self.binary, "check", identity.name],
                                  capture_output=True, text=True)

    def test_canonical_and_stock_formats_have_one_result(self):
        expected = "22:22:AA:BB:CC:DD\n"
        self.assertEqual(self.check(b"22:22:aa:bb:cc:dd\n").stdout, expected)
        self.assertEqual(self.check(b"22 22 aa bb cc dd ").stdout, expected)

    def test_rejects_non_addresses_and_reserved_laps(self):
        invalid = [
            b"", b"22:22:aa:bb:cc", b"22:22:aa:bb:cc:dde",
            b"22 22:aa bb cc dd", b"00:00:00:00:00:00",
            b"ff:ff:ff:ff:ff:ff", b"22:22:22:9e:8b:00",
            b"22:22:22:9e:8b:3f", b"\0" * 6,
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertNotEqual(self.check(value).returncode, 0)

    def run_service(self, root, utility):
        script = (SERVICE.read_text()
                  .replace("/run/brick-bluetooth-identity", str(root / "run"))
                  .replace("/data/bluetooth/public-address", str(root / "public-address"))
                  .replace("/run/brick-data/state", str(root / "data-state"))
                  .replace("/usr/sbin/brick-bluetooth-identity", str(utility)))
        path = root / "service"
        path.write_text(script)
        return subprocess.run(["sh", path, "start"], capture_output=True, text=True)

    def test_persistent_identity_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data-state").write_text("persistent\n")
            identity = root / "public-address"
            original = b"22 22 aa bb cc dd "
            identity.write_bytes(original)
            utility = root / "apply"
            utility.write_text("#!/bin/sh\n[ \"$1\" = apply ] || exit 2\n"
                               "[ \"$2\" = \"$IDENTITY\" ] || exit 3\n"
                               "printf '22:22:AA:BB:CC:DD\\n'\n")
            utility.chmod(0o755)
            # Embed the expected path because init deliberately has no environment API.
            utility.write_text(utility.read_text().replace("$IDENTITY", str(identity)))
            self.assertEqual(self.run_service(root, utility).returncode, 0)
            self.assertEqual(identity.read_bytes(), original)
            first = (root / "run/address").read_bytes()
            self.assertEqual(self.run_service(root, utility).returncode, 0)
            self.assertEqual(identity.read_bytes(), original)
            self.assertEqual((root / "run/address").read_bytes(), first)
            self.assertTrue((root / "run/ready").is_file())

    def test_absent_identity_never_generates_collision_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data-state").write_text("persistent\n")
            utility = root / "must-not-run"
            utility.write_text("#!/bin/sh\nexit 99\n")
            utility.chmod(0o755)
            result = self.run_service(root, utility)
            self.assertEqual(result.returncode, 0)
            self.assertFalse((root / "public-address").exists())
            self.assertFalse((root / "run/ready").exists())

    def test_apply_error_leaves_consumers_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data-state").write_text("persistent\n")
            (root / "public-address").write_text("22:22:AA:BB:CC:DD\n")
            utility = root / "fail"
            utility.write_text("#!/bin/sh\nexit 1\n")
            utility.chmod(0o755)
            self.assertNotEqual(self.run_service(root, utility).returncode, 0)
            self.assertFalse((root / "run/ready").exists())

    def test_bluetooth_consumers_require_verified_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data-state").write_text("persistent\n")
            originals = root / "originals"
            originals.mkdir()
            called = root / "called"
            daemon = originals / "S40bluetoothd"
            daemon.write_text(f"#!/bin/sh\n: > {called}\n")
            daemon.chmod(0o755)
            script = (WRAPPER.read_text()
                      .replace("/run/brick-data/state", str(root / "data-state"))
                      .replace("/run/brick-bluetooth-identity/ready", str(root / "ready"))
                      .replace("/usr/libexec/brick-init", str(originals)))
            wrapper = root / "wrapper"
            wrapper.write_text(script)
            result = subprocess.run(["sh", wrapper, "S40bluetoothd", "start"])
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(called.exists())
            (root / "ready").touch()
            result = subprocess.run(["sh", wrapper, "S40bluetoothd", "start"])
            self.assertEqual(result.returncode, 0)
            self.assertTrue(called.exists())

    def test_provision_is_atomic_idempotent_and_collision_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_directory = root / "data/bluetooth"
            state = root / "data-state"
            binary = root / "brick-bluetooth-identity"
            subprocess.run([
                "cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                f'-DDATA_STATE="{state}"',
                f'-DIDENTITY_DIRECTORY="{identity_directory}"',
                str(SOURCE), "-o", str(binary),
            ], check=True)
            script = root / "test.sh"
            script.write_text(f'''#!/bin/sh
set -eu
mkdir -p '{identity_directory}'
chmod 0700 '{identity_directory}'
printf 'persistent\n' > '{state}'
printf '22 22 aa bb cc dd ' > '{root / "source"}'
chmod 0600 '{state}' '{root / "source"}'
'{binary}' provision '{root / "source"}' > '{root / "result"}'
[ "$(cat '{root / "result"}')" = 'CREATED 22:22:AA:BB:CC:DD' ]
'{binary}' provision '{root / "source"}' > '{root / "result"}'
[ "$(cat '{root / "result"}')" = 'EXISTING 22:22:AA:BB:CC:DD' ]
[ "$(cat '{identity_directory / "public-address"}')" = 22:22:AA:BB:CC:DD ]
printf '22:22:AA:BB:CC:DE\n' > '{root / "other"}'
chmod 0600 '{root / "other"}'
! '{binary}' provision '{root / "other"}'
[ "$(cat '{identity_directory / "public-address"}')" = 22:22:AA:BB:CC:DD ]
[ ! -e '{identity_directory / ".public-address.new"}' ]
! '{binary}' unprovision '{root / "other"}'
[ "$(cat '{identity_directory / "public-address"}')" = 22:22:AA:BB:CC:DD ]
'{binary}' unprovision '{root / "source"}'
[ ! -e '{identity_directory / "public-address"}' ]
! '{binary}' unprovision '{root / "source"}'
''')
            result = subprocess.run(["sh", str(script)], capture_output=True,
                                    text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_provision_requires_persistent_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity_directory = root / "data/bluetooth"
            state = root / "data-state"
            binary = root / "brick-bluetooth-identity"
            subprocess.run([
                "cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                f'-DDATA_STATE="{state}"',
                f'-DIDENTITY_DIRECTORY="{identity_directory}"',
                str(SOURCE), "-o", str(binary),
            ], check=True)
            script = root / "test.sh"
            script.write_text(f'''#!/bin/sh
set -eu
mkdir -p '{identity_directory}'
chmod 0700 '{identity_directory}'
printf 'volatile\n' > '{state}'
printf '22:22:AA:BB:CC:DD\n' > '{root / "source"}'
chmod 0600 '{state}' '{root / "source"}'
! '{binary}' provision '{root / "source"}'
[ ! -e '{identity_directory / "public-address"}' ]
''')
            result = subprocess.run(["sh", str(script)], capture_output=True,
                                    text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
