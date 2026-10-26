#!/usr/bin/env python3
"""Provision a preserved Bluetooth identity over the Brick USB console."""
import argparse
from pathlib import Path
import re
import shlex
import sys
import tempfile
import uuid

from serial_console import Console, DEFAULT_PORT, ProtocolError


def canonical_address(contents):
    text = contents.decode("ascii").strip()
    colon = re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", text)
    spaced = re.fullmatch(r"[0-9a-fA-F]{2}(?:\s+[0-9a-fA-F]{2}){5}", text)
    if not colon and not spaced:
        raise ValueError("Bluetooth address must use canonical or stock format")
    octets = bytes.fromhex(text.replace(":", " "))
    if octets in (bytes(6), bytes((0xff,)) * 6):
        raise ValueError("Bluetooth address is not a usable device identity")
    if octets[3] == 0x9e and octets[4] == 0x8b and octets[5] <= 0x3f:
        raise ValueError("Bluetooth inquiry address is not a device identity")
    return ":".join(f"{octet:02X}" for octet in octets)


def provision(console, source, expected):
    remote = "/tmp/brick-bluetooth-address-" + uuid.uuid4().hex
    created = False
    console.upload(source, remote, mode="0600")
    try:
        output = console.checked(
            "/usr/sbin/brick-bluetooth-identity provision " + shlex.quote(remote)
        ).decode().strip()
        if output not in ("CREATED " + expected, "EXISTING " + expected):
            raise ProtocolError("Target returned a different Bluetooth identity")
        created = output.startswith("CREATED ")
        try:
            console.checked(
                "/etc/init.d/S46bluealsa stop && "
                "/etc/init.d/S40bluetoothd stop && "
                "/etc/init.d/S39bluetooth-identity restart && "
                "/etc/init.d/S40bluetoothd start && "
                "/etc/init.d/S46bluealsa start"
            )
            applied = console.checked(
                "test -f /run/brick-bluetooth-identity/ready && "
                "cat /run/brick-bluetooth-identity/address"
            ).decode().strip()
            if applied != expected:
                raise ProtocolError(
                    "Controller identity verification did not persist"
                )
        except (OSError, UnicodeError, TimeoutError, ProtocolError) as error:
            try:
                console.checked(
                    "status=0; "
                    "/etc/init.d/S46bluealsa stop || status=1; "
                    "/etc/init.d/S40bluetoothd stop || status=1; "
                    "/etc/init.d/S39bluetooth-identity stop || status=1; "
                    "exit \"$status\""
                )
                if created:
                    rolled_back = console.checked(
                        "/usr/sbin/brick-bluetooth-identity unprovision "
                        + shlex.quote(remote)
                    ).decode().strip()
                    if rolled_back != expected:
                        raise ProtocolError(
                            "Target returned a different rollback identity"
                        )
            except (OSError, UnicodeError, TimeoutError, ProtocolError) as rollback:
                raise ProtocolError(f"{error}; rollback failed: {rollback}") from error
            raise
    finally:
        try:
            console.checked("/bin/busybox rm -f " + shlex.quote(remote))
        except (OSError, TimeoutError, ProtocolError):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address_file", type=Path,
                        help="preserved stock address file")
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args(argv)
    expected = canonical_address(args.address_file.read_bytes())
    with tempfile.NamedTemporaryFile("w") as canonical:
        canonical.write(expected + "\n")
        canonical.flush()
        with Console(args.port, args.timeout) as console:
            provision(console, canonical.name, expected)
    print("BLUETOOTH_IDENTITY_VERIFIED " + expected)


if __name__ == "__main__":
    try:
        main()
    except (OSError, UnicodeError, ValueError, TimeoutError, ProtocolError) as error:
        sys.exit(f"Bluetooth identity provisioning failed: {error}")
