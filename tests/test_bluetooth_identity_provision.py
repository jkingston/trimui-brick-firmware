"""Host provisioning imports one preserved identity and verifies activation."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import provision_bluetooth_identity as provision


class BluetoothIdentityProvisionTests(unittest.TestCase):
    def test_stock_and_canonical_formats_normalize_without_byte_reversal(self):
        expected = "22:22:AA:BB:CC:DD"
        self.assertEqual(provision.canonical_address(b"22 22 aa bb cc dd "),
                         expected)
        self.assertEqual(provision.canonical_address(
            b"22:22:aa:bb:cc:dd\n"), expected)

    def test_invalid_identity_is_rejected_before_console_access(self):
        with tempfile.NamedTemporaryFile("wb") as address:
            address.write(b"00:00:00:00:00:00\n")
            address.flush()
            with patch.object(provision, "Console") as console:
                with self.assertRaisesRegex(ValueError, "usable"):
                    provision.main([address.name])
            console.assert_not_called()

    def test_upload_provision_service_order_and_verification(self):
        console = MagicMock()
        console.checked.side_effect = [
            b"CREATED 22:22:AA:BB:CC:DD\n", b"",
            b"22:22:AA:BB:CC:DD\n", b""
        ]
        provision.provision(console, "/host/address", "22:22:AA:BB:CC:DD")
        console.upload.assert_called_once()
        commands = [call.args[0] for call in console.checked.call_args_list]
        self.assertIn(" provision /tmp/brick-bluetooth-address-", commands[0])
        self.assertLess(commands[1].index("S46bluealsa stop"),
                        commands[1].index("S40bluetoothd stop"))
        self.assertLess(commands[1].index("S39bluetooth-identity restart"),
                        commands[1].index("S40bluetoothd start"))
        self.assertIn("/run/brick-bluetooth-identity/ready", commands[2])
        self.assertTrue(commands[3].startswith("/bin/busybox rm -f /tmp/"))

    def test_activation_failure_still_cleans_staged_input(self):
        console = MagicMock()
        console.checked.side_effect = [
            b"CREATED 22:22:AA:BB:CC:DD\n",
            provision.ProtocolError("start failed"), b"", b"22:22:AA:BB:CC:DD\n",
            b""
        ]
        with self.assertRaisesRegex(provision.ProtocolError, "start failed"):
            provision.provision(console, "/host/address", "22:22:AA:BB:CC:DD")
        self.assertTrue(console.checked.call_args_list[-1].args[0].startswith(
            "/bin/busybox rm -f /tmp/"))
        commands = [call.args[0] for call in console.checked.call_args_list]
        self.assertTrue(any("S39bluetooth-identity stop" in command
                            for command in commands))
        self.assertTrue(any(" unprovision " in command for command in commands))

    def test_existing_identity_is_not_removed_on_activation_failure(self):
        console = MagicMock()
        console.checked.side_effect = [
            b"EXISTING 22:22:AA:BB:CC:DD\n",
            provision.ProtocolError("start failed"), b"", b""
        ]
        with self.assertRaisesRegex(provision.ProtocolError, "start failed"):
            provision.provision(console, "/host/address", "22:22:AA:BB:CC:DD")
        commands = [call.args[0] for call in console.checked.call_args_list]
        self.assertFalse(any(" unprovision " in command for command in commands))

    def test_verification_mismatch_rolls_back_new_identity(self):
        console = MagicMock()
        console.checked.side_effect = [
            b"CREATED 22:22:AA:BB:CC:DD\n", b"",
            b"22:22:AA:BB:CC:DE\n", b"", b"22:22:AA:BB:CC:DD\n", b""
        ]
        with self.assertRaisesRegex(provision.ProtocolError,
                                    "verification did not persist"):
            provision.provision(console, "/host/address", "22:22:AA:BB:CC:DD")
        commands = [call.args[0] for call in console.checked.call_args_list]
        self.assertTrue(any(" unprovision " in command for command in commands))


if __name__ == "__main__":
    unittest.main()
