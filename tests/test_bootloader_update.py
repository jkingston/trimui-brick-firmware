"""File-image lifecycle tests for controlled raw bootloader updates."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("bootloader_update", TOOLS / "bootloader_update.py")
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)

CID = "fe3432534400000020000000ef01a779"


def fit_blob(total_length):
    def token(value):
        return struct.pack(">I", value)

    def begin(name):
        value = token(1) + name.encode() + b"\0"
        return value + b"\0" * (-len(value) % 4)

    structure = (begin("") + begin("images") + begin("uboot") + token(2) +
                 begin("atf") + token(2) + begin("fdt-1") + token(2) + token(2) +
                 begin("configurations") + begin("conf-1") + token(2) + token(2) +
                 token(2) + token(9))
    reserve = b"\0" * 16
    off_reserve = 40
    off_struct = off_reserve + len(reserve)
    off_strings = off_struct + len(structure)
    if total_length < off_strings:
        raise ValueError("FIT test image is too short")
    header = struct.pack(">10I", boot.FDT_MAGIC, total_length, off_struct, off_strings,
                         off_reserve, 17, 16, 0, 0, len(structure))
    return header + reserve + structure + b"\0" * (total_length - off_strings)


def image(length, fill):
    spl_size = 1024
    data = bytearray([fill] * length)
    data[4:12] = b"eGON.BT0"
    struct.pack_into("<I", data, 16, spl_size)
    words = list(struct.unpack_from(f"<{spl_size // 4}I", data))
    words[3] = boot.SUNXI_CHECKSUM_STAMP
    struct.pack_into("<I", data, 12, sum(words) & 0xFFFFFFFF)
    fit_offset = max(32768, spl_size)
    data[fit_offset:] = fit_blob(length - fit_offset)
    return bytes(data)


def authorized(candidate):
    manifest = {
        "firmware": {"commit": "1" * 40, "dirty": False},
        "kernel": {"commit": "2" * 40, "dirty": False},
        "files": {"u-boot-sunxi-with-spl.bin": {
            "sha256": boot.sha256(candidate), "bytes": len(candidate),
        }},
    }
    raw = json.dumps(manifest, sort_keys=True).encode()
    return boot.authorize_candidate(candidate, raw, boot.sha256(raw)), raw


def mbr(include_data=True):
    sector = bytearray(512)
    boot_entry = bytearray(16)
    boot_entry[0] = 0x80
    boot_entry[4] = 0x0C
    struct.pack_into("<II", boot_entry, 8, boot.BOOT_PARTITION_START,
                     boot.BOOT_PARTITION_SECTORS)
    data_entry = bytearray(16)
    data_entry[4] = 0x83
    struct.pack_into("<II", data_entry, 8, boot.DATA_PARTITION_START,
                     boot.DATA_PARTITION_SECTORS)
    sector[446:462] = boot_entry
    if include_data:
        sector[462:478] = data_entry
    sector[510:512] = b"\x55\xaa"
    return sector


class CorruptAfterUpdateCard(boot.Card):
    def write(self, offset, data):
        super().write(offset, data)
        if len(data) != boot.BOOT_SIZE:
            os.pwrite(self.fd, b"X", boot.BOOT_END - 1)


class PartialFailureCard(boot.Card):
    failed = False

    def write(self, offset, data):
        if not self.failed and len(data) != boot.BOOT_SIZE:
            self.failed = True
            super().write(offset, data[:137])
            raise OSError("injected partial write")
        super().write(offset, data)


class UnrecoverableCard(boot.Card):
    def write(self, offset, data):
        super().write(offset, data[:137])
        raise OSError("injected persistent failure")


class BootloaderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="brick-bootloader-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.card_path = self.root / "card.img"
        with self.card_path.open("wb") as card:
            card.truncate(192 * 1024 * 1024)
            card.seek(0)
            card.write(mbr())
        self.current = image(42001, 0x21)
        self.candidate = image(56003, 0x37)
        self.candidate_identity, self.candidate_manifest = authorized(self.candidate)
        self.original_region = self.current + bytes(
            [0xA5] * (boot.BOOT_SIZE - len(self.current)))
        with self.card_path.open("r+b") as card:
            card.seek(boot.BOOT_START)
            card.write(self.original_region)
            card.seek(boot.BOOT_END)
            card.write(b"PARTITION-SENTINEL")
            card.seek(128 * 1024 * 1024)
            card.write(b"DATA-SENTINEL")
        self.original_backup = self.raw(0, boot.BACKUP_SIZE)
        self.identity = boot.Identity(CID)
        self.backup = self.root / "boot-region.bin"

    def open(self, cls=boot.Card, writable=True):
        return cls(self.card_path, writable=writable, identity=self.identity)

    def raw(self, offset, size):
        with self.card_path.open("rb") as card:
            card.seek(offset)
            return card.read(size)

    def update(self, cls=boot.Card, dry_run=False):
        with self.open(cls, writable=not dry_run) as card:
            return boot.upgrade(card, self.candidate, self.candidate_identity,
                                [("known.bin", self.current)], CID, self.backup, dry_run)

    def test_success_writes_only_exact_image_extent_and_preserves_partitions(self):
        metadata = self.update()
        expected = self.candidate + self.original_region[len(self.candidate):]
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), expected)
        self.assertEqual(self.raw(boot.BOOT_END, 18), b"PARTITION-SENTINEL")
        self.assertEqual(self.raw(128 * 1024 * 1024, 13), b"DATA-SENTINEL")
        self.assertEqual(self.backup.read_bytes(), self.original_backup)
        self.assertEqual(metadata["backup"]["sha256"], hashlib.sha256(self.original_backup).hexdigest())
        self.assertEqual(metadata["candidate"]["manifest_sha256"],
                         boot.sha256(self.candidate_manifest))
        self.assertEqual(metadata["candidate"]["firmware_commit"], "1" * 40)
        self.assertEqual(json.loads(Path(str(self.backup) + ".json").read_text())["card_cid"], CID)

    def test_dry_run_authenticates_and_preserves_backup_without_card_write(self):
        self.update(dry_run=True)
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)
        self.assertEqual(self.backup.read_bytes(), self.original_backup)

    def test_wrong_cid_refuses_before_backup_or_write(self):
        with self.open() as card, self.assertRaisesRegex(boot.UpgradeError, "CID"):
            boot.upgrade(card, self.candidate, self.candidate_identity,
                         [("known.bin", self.current)], "00" * 16, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)

    def test_unknown_current_bootloader_refuses_before_backup_or_write(self):
        other = image(43001, 0x43)
        with self.open() as card, self.assertRaisesRegex(boot.UpgradeError, "allowlist"):
            boot.upgrade(card, self.candidate, self.candidate_identity,
                         [("other.bin", other)], CID, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)

    def test_partial_write_is_restored_and_verified(self):
        with self.assertRaisesRegex(boot.UpgradeError, "original boot region restored"):
            self.update(PartialFailureCard)
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)
        self.assertEqual(self.backup.read_bytes(), self.original_backup)

    def test_full_region_verification_mismatch_is_restored(self):
        with self.assertRaisesRegex(boot.UpgradeError, "original boot region restored"):
            self.update(CorruptAfterUpdateCard)
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)

    def test_failed_automatic_restore_reports_physical_recovery_path(self):
        with self.assertRaisesRegex(boot.RestoreError, "automatic restore also failed"):
            self.update(UnrecoverableCard)
        self.assertEqual(self.backup.read_bytes(), self.original_backup)

    def test_restore_rejects_other_card_and_restores_full_region(self):
        self.update()
        with self.card_path.open("r+b") as card:
            card.seek(boot.BOOT_START)
            card.write(b"CORRUPT")
        with self.open() as card, self.assertRaisesRegex(boot.UpgradeError, "different SD"):
            boot.restore(card, self.backup, "11" * 16)
        with self.open() as card:
            boot.restore(card, self.backup, CID)
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)

    def test_corrupt_backup_is_never_written(self):
        self.update(dry_run=True)
        with self.backup.open("r+b") as saved:
            saved.seek(7)
            saved.write(b"bad")
        before = self.raw(boot.BOOT_START, boot.BOOT_SIZE)
        with self.open() as card, self.assertRaisesRegex(boot.UpgradeError, "checksum"):
            boot.restore(card, self.backup, CID)
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), before)

    def test_existing_backup_is_never_overwritten(self):
        self.backup.write_bytes(b"keep")
        with self.open() as card, self.assertRaises(FileExistsError):
            boot.upgrade(card, self.candidate, self.candidate_identity,
                         [("known.bin", self.current)], CID, self.backup)
        self.assertEqual(self.backup.read_bytes(), b"keep")
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)

    def test_bad_partition_layout_is_rejected(self):
        with self.card_path.open("r+b") as card:
            card.seek(446 + 8)
            card.write(struct.pack("<I", 4096))
        with self.open() as card, self.assertRaisesRegex(boot.UpgradeError, "genimage"):
            boot.upgrade(card, self.candidate, self.candidate_identity,
                         [("known.bin", self.current)], CID, self.backup)
        self.assertFalse(self.backup.exists())

    def test_deployed_boot_only_partition_generation_is_accepted(self):
        with self.card_path.open("r+b") as card:
            card.seek(0)
            card.write(mbr(include_data=False))
        self.update(dry_run=True)
        metadata = json.loads(Path(str(self.backup) + ".json").read_text())
        self.assertEqual(len(metadata["card_layout"]["partitions"]), 1)

    def test_restore_refuses_changed_partition_table_even_with_same_cid(self):
        self.update(dry_run=True)
        with self.card_path.open("r+b") as card:
            card.seek(440)
            card.write(b"CHNG")
        before = self.raw(boot.BOOT_START, boot.BOOT_SIZE)
        with self.open() as card, self.assertRaisesRegex(boot.RestoreError, "partition table changed"):
            boot.restore(card, self.backup, CID)
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), before)

    def test_write_api_cannot_cross_reserved_region(self):
        with self.open() as card:
            for offset, data in ((boot.BOOT_START - 1, b"x"),
                                 (boot.BOOT_END, b"x"),
                                 (boot.BOOT_END - 1, b"xx")):
                with self.subTest(offset=offset), self.assertRaisesRegex(boot.UpgradeError, "outside"):
                    card.write(offset, data)


class ImageValidationTests(unittest.TestCase):
    def test_valid_generated_image(self):
        boot.validate_bootloader(image(40001, 0x10))

    def test_bad_spl_checksum_and_fit_extent_are_rejected(self):
        candidate = bytearray(image(40001, 0x10))
        candidate[100] ^= 1
        with self.assertRaisesRegex(boot.UpgradeError, "checksum"):
            boot.validate_bootloader(candidate)
        candidate = bytearray(image(40001, 0x10))
        candidate[32775] ^= 1
        with self.assertRaisesRegex(boot.UpgradeError, "FIT extent"):
            boot.validate_bootloader(candidate)

    def test_trivial_fit_header_is_rejected(self):
        candidate = bytearray(image(40001, 0x10))
        fit_offset = 32768
        candidate[fit_offset:] = b"\0" * (len(candidate) - fit_offset)
        struct.pack_into(">II", candidate, fit_offset, boot.FDT_MAGIC,
                         len(candidate) - fit_offset)
        with self.assertRaisesRegex(boot.UpgradeError, "FIT"):
            boot.validate_bootloader(candidate)

    def test_malformed_fit_structure_is_rejected(self):
        candidate = bytearray(image(40001, 0x10))
        off_struct = struct.unpack_from(">I", candidate, 32768 + 8)[0]
        struct.pack_into(">I", candidate, 32768 + off_struct, 0x12345678)
        with self.assertRaisesRegex(boot.UpgradeError, "token"):
            boot.validate_bootloader(candidate)

    def test_candidate_requires_exact_clean_reviewed_manifest(self):
        candidate = image(40001, 0x10)
        identity, manifest = authorized(candidate)
        self.assertEqual(identity.sha256, boot.sha256(candidate))
        self.assertEqual(identity.manifest_sha256, boot.sha256(manifest))
        with self.assertRaisesRegex(boot.UpgradeError, "reviewed manifest"):
            boot.authorize_candidate(candidate, manifest, "0" * 64)
        dirty_object = json.loads(manifest)
        dirty_object["firmware"]["dirty"] = True
        dirty_manifest = json.dumps(dirty_object, sort_keys=True).encode()
        with self.assertRaisesRegex(boot.UpgradeError, "clean firmware"):
            boot.authorize_candidate(candidate, dirty_manifest, boot.sha256(dirty_manifest))
        changed = bytearray(candidate)
        changed[-1] ^= 1
        with self.assertRaisesRegex(boot.UpgradeError, "reviewed manifest"):
            boot.authorize_candidate(bytes(changed), manifest, boot.sha256(manifest))


class StrictMbrTests(unittest.TestCase):
    def test_partition_entries_must_use_exact_genimage_slots(self):
        shifted_boot = mbr()
        shifted_boot[478:494] = shifted_boot[446:462]
        shifted_boot[446:462] = b"\0" * 16
        with self.assertRaises(boot.UpgradeError):
            boot.parse_genimage_mbr(shifted_boot)

        shifted_data = mbr()
        shifted_data[494:510] = shifted_data[462:478]
        shifted_data[462:478] = b"\0" * 16
        with self.assertRaisesRegex(boot.UpgradeError, "unexpected partition"):
            boot.parse_genimage_mbr(shifted_data)

    def test_unused_entry_must_be_fully_zero(self):
        sector = mbr(include_data=False)
        sector[462 + 1] = 1
        with self.assertRaises(boot.UpgradeError):
            boot.parse_genimage_mbr(sector)


class FakeAdb:
    def __init__(self, disk):
        self.disk = disk
        self.remote = {}
        self.calls = []
        self.destructive_calls = 0
        self.fail_write = None
        self.corrupt_first_write = False
        self.locked = False
        self.payload_record = None
        self.write_state = None
        self.writer_active = False
        self.fail_cleanup = False
        self.fail_exec_out = False

    @staticmethod
    def completed(argv, stdout=b"", returncode=0, stderr=b""):
        return boot.subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        action = next(name for name in ("shell", "exec-out", "push") if name in argv)
        index = argv.index(action)
        if action == "push":
            self.remote[argv[index + 2]] = Path(argv[index + 1]).read_bytes()
            return self.completed(argv)
        if action == "exec-out":
            if self.fail_exec_out:
                return self.completed(argv, returncode=1, stderr=b"error: closed")
            arguments = argv[index + 1:]
            values = {item.split("=", 1)[0]: int(item.split("=", 1)[1])
                      for item in arguments if item.startswith(("bs=", "skip=", "count="))}
            start = values["bs"] * values["skip"]
            end = start + values["bs"] * values["count"]
            return self.completed(argv, bytes(self.disk[start:end]))
        command = argv[index + 1]
        if command.startswith("/bin/busybox dd if=/dev/mmcblk1 bs=4096 "):
            values = {item.split("=", 1)[0]: int(item.split("=", 1)[1])
                      for item in command.split()
                      if item.startswith(("bs=", "skip=", "count="))}
            start = values["bs"] * values["skip"]
            end = start + values["bs"] * values["count"]
            encoded = boot.base64.encodebytes(bytes(self.disk[start:end]))
            return self.completed(argv, encoded.replace(b"\n", b"\r\n"))
        if command == "id -u":
            return self.completed(argv, b"0\n")
        if command == f"if test -e {boot.ADB_LOCK}; then echo locked; fi":
            return self.completed(argv, b"locked\n" if self.locked else b"")
        if command == f"mkdir {boot.ADB_LOCK}":
            if self.locked:
                return self.completed(argv, returncode=1, stderr=b"File exists")
            self.locked = True
            return self.completed(argv)
        if command.startswith("printf '%s\\n' /tmp/.brick-bootloader-"):
            self.payload_record = command.split(" ", 2)[2].split(" >", 1)[0]
            self.write_state = "armed"
            return self.completed(argv)
        if command == (f"test -d {boot.ADB_LOCK}; test -f {boot.ADB_PAYLOAD_RECORD}; "
                       f"test -f {boot.ADB_STATE_RECORD}; cat {boot.ADB_PAYLOAD_RECORD}; "
                       f"cat {boot.ADB_STATE_RECORD}"):
            if not self.locked or not self.payload_record or not self.write_state:
                return self.completed(argv, returncode=1, stderr=b"missing recovery state")
            return self.completed(
                argv, (self.payload_record + "\n" + self.write_state + "\n").encode())
        if "echo recovered" in command and boot.ADB_PAYLOAD_RECORD in command:
            if self.writer_active:
                return self.completed(argv, returncode=7, stderr=b"writer active")
            self.remote.pop(self.payload_record, None)
            self.payload_record = None
            self.write_state = None
            self.locked = False
            return self.completed(argv, b"recovered\n")
        if command.startswith("rm -f /tmp/.brick-bootloader-"):
            if self.fail_cleanup:
                return self.completed(argv, returncode=1, stderr=b"cleanup failed")
            path = command.split()[2].rstrip(";")
            self.remote.pop(path, None)
            if f"rmdir {boot.ADB_LOCK}" in command:
                self.payload_record = None
                self.write_state = None
                self.locked = False
            return self.completed(argv)
        if "device/type" in command:
            return self.completed(argv, f"SD\n{CID}\n512\n".encode())
        if command == "cat /sys/block/mmcblk1/size":
            return self.completed(argv, b"524288\n")
        if "dd if=/tmp/.brick-bootloader-" in command:
            import re
            match = re.search(r"dd if=(/tmp/[^ ]+) of=/dev/mmcblk1 bs=4096 seek=([0-9]+)",
                              command)
            if match:
                self.write_state = "writing"
                payload = self.remote[match.group(1)]
                start = int(match.group(2)) * 4096
                self.destructive_calls += 1
                if self.fail_write and self.destructive_calls == 1:
                    self.disk[start:start + 137] = payload[:137]
                    self.writer_active = True
                    if self.fail_write == "timeout":
                        raise boot.subprocess.TimeoutExpired(argv, kwargs["timeout"])
                    return self.completed(argv, returncode=1, stderr=b"injected dd failure")
                self.disk[start:start + len(payload)] = payload
                if self.corrupt_first_write and self.destructive_calls == 1:
                    self.disk[start + len(payload) - 1] ^= 1
                self.write_state = "complete"
        if command == (f"rm -f {boot.ADB_PAYLOAD_RECORD} {boot.ADB_STATE_RECORD}; "
                       f"rmdir {boot.ADB_LOCK}"):
            if self.fail_cleanup:
                return self.completed(argv, returncode=1, stderr=b"cleanup failed")
            self.payload_record = None
            self.write_state = None
            self.locked = False
        return self.completed(argv)


class AdbBackendTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory(prefix="brick-adb-bootloader-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        current = image(42001, 0x21)
        candidate = image(56003, 0x37)
        identity, _manifest = authorized(candidate)
        original = current + bytes([0xA5] * (boot.BOOT_SIZE - len(current)))
        disk = bytearray(boot.BOOT_END)
        disk[:512] = mbr()
        disk[boot.BOOT_START:boot.BOOT_END] = original
        return root, current, candidate, identity, original, disk, FakeAdb(disk)

    def test_stock_adb_upgrade_uses_host_backup_and_bounded_raw_write(self):
        root, current, candidate, identity, original, disk, fake = self.fixture()
        backup = root / "backup.bin"
        initial_prefix = bytes(disk[:boot.BOOT_START])
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with boot.AdbCard("/dev/mmcblk1", writable=True,
                              adb="mock-adb", serial="brick") as card:
                boot.upgrade(card, candidate, identity, [("known.bin", current)],
                             CID, backup)

        self.assertEqual(backup.read_bytes(), initial_prefix + original)
        self.assertEqual(disk[boot.BOOT_START:boot.BOOT_END],
                         candidate + original[len(candidate):])
        writes = [" ".join(argv) for argv, _kwargs in fake.calls
                  if "shell" in argv and " of=/dev/mmcblk1 " in argv[-1]]
        self.assertEqual(len(writes), 1)
        self.assertIn("bs=4096 seek=32 conv=notrunc", writes[0])
        self.assertTrue(all("of=/dev/mmcblk1" not in " ".join(argv)
                            for argv, _kwargs in fake.calls if "exec-out" in argv))

    def test_adb_dry_run_never_pushes_or_writes(self):
        root, current, candidate, identity, original, disk, fake = self.fixture()
        backup = root / "backup.bin"
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with boot.AdbCard("/dev/mmcblk1", writable=False, adb="mock-adb") as card:
                boot.upgrade(card, candidate, identity, [("known.bin", current)],
                             CID, backup, dry_run=True)
        self.assertEqual(disk[boot.BOOT_START:boot.BOOT_END], original)
        self.assertFalse(any("push" in argv for argv, _kwargs in fake.calls))
        self.assertEqual(fake.destructive_calls, 0)

    def test_adb_read_falls_back_to_binary_safe_legacy_shell(self):
        _root, _current, _candidate, _identity, _original, disk, fake = self.fixture()
        fake.fail_exec_out = True
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with boot.AdbCard("/dev/mmcblk1", writable=False, adb="mock-adb") as card:
                self.assertEqual(card.read(boot.BOOT_START, 12345),
                                 bytes(disk[boot.BOOT_START:boot.BOOT_START + 12345]))
        fallback = [argv for argv, _kwargs in fake.calls
                    if "shell" in argv and argv[-1].startswith("/bin/busybox dd ")]
        self.assertEqual(len(fallback), 1)
        self.assertTrue(fallback[0][-1].endswith(
            "2>/dev/null | /bin/busybox base64"))

    def test_corrupt_adb_readback_is_automatically_restored(self):
        root, current, candidate, identity, original, disk, fake = self.fixture()
        fake.corrupt_first_write = True
        backup = root / "backup.bin"
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                with self.assertRaisesRegex(boot.UpgradeError, "original boot region restored"):
                    boot.upgrade(card, candidate, identity, [("known.bin", current)],
                                 CID, backup)
        self.assertEqual(fake.destructive_calls, 2)
        self.assertEqual(disk[boot.BOOT_START:boot.BOOT_END], original)

    def test_failed_adb_dd_does_not_overlap_restore_and_explicit_restore_works(self):
        root, current, candidate, identity, original, disk, fake = self.fixture()
        fake.fail_write = "error"
        backup = root / "backup.bin"
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                with self.assertRaisesRegex(boot.RestoreError, "no automatic restore"):
                    boot.upgrade(card, candidate, identity, [("known.bin", current)],
                                 CID, backup)
            self.assertEqual(fake.destructive_calls, 1)
            recovery = boot.AdbCard("/dev/mmcblk1", writable=False, adb="mock-adb")
            with self.assertRaisesRegex(boot.UpgradeError, "still active"):
                recovery.recover_uncertain_write()
            self.assertTrue(fake.locked)
            fake.writer_active = False
            recovery.recover_uncertain_write()
            fake.fail_write = None
            with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                boot.restore(card, backup, CID)
        self.assertEqual(fake.destructive_calls, 2)
        self.assertEqual(disk[boot.BOOT_START:boot.BOOT_END], original)

    def test_timed_out_adb_dd_is_bounded_and_never_auto_restored(self):
        root, current, candidate, identity, _original, _disk, fake = self.fixture()
        fake.fail_write = "timeout"
        backup = root / "backup.bin"
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                with self.assertRaisesRegex(boot.RestoreError, "no automatic restore"):
                    boot.upgrade(card, candidate, identity, [("known.bin", current)],
                                 CID, backup)
        self.assertEqual(fake.destructive_calls, 1)
        destructive = [(argv, kwargs) for argv, kwargs in fake.calls
                       if "shell" in argv and " of=/dev/mmcblk1 " in argv[-1]]
        self.assertEqual(len(destructive), 1)
        self.assertEqual(destructive[0][1]["timeout"], boot.ADB_TRANSFER_TIMEOUT)
        self.assertTrue(fake.locked)
        self.assertIsNotNone(fake.payload_record)

    def test_uncertain_lock_blocks_second_update_and_restore_without_raw_write(self):
        root, current, candidate, identity, _original, _disk, fake = self.fixture()
        fake.fail_write = "timeout"
        backup = root / "backup.bin"
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                with self.assertRaises(boot.RestoreError):
                    boot.upgrade(card, candidate, identity, [("known.bin", current)],
                                 CID, backup)
            destructive_before = fake.destructive_calls
            with self.assertRaisesRegex(boot.UpgradeError, "lock exists"):
                with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                    boot.upgrade(card, candidate, identity, [("known.bin", current)],
                                 CID, root / "second-backup.bin")
            with self.assertRaisesRegex(boot.UpgradeError, "lock exists"):
                with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                    boot.restore(card, backup, CID)
        self.assertEqual(fake.destructive_calls, destructive_before)

    def test_recovery_cannot_clear_an_armed_prewrite_lock(self):
        _root, _current, _candidate, _identity, _original, _disk, fake = self.fixture()
        fake.locked = True
        fake.payload_record = "/tmp/.brick-bootloader-" + "a" * 32
        fake.write_state = "armed"
        with patch.object(boot.subprocess, "run", side_effect=fake):
            recovery = boot.AdbCard("/dev/mmcblk1", writable=False, adb="mock-adb")
            with self.assertRaisesRegex(boot.UpgradeError, "not recorded as started"):
                recovery.recover_uncertain_write()
        self.assertTrue(fake.locked)
        self.assertEqual(fake.destructive_calls, 0)

    def test_adb_temporary_payload_and_lock_are_cleaned_up(self):
        root, current, candidate, identity, _original, _disk, fake = self.fixture()
        backup = root / "backup.bin"
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                boot.upgrade(card, candidate, identity, [("known.bin", current)],
                             CID, backup)
        commands = [argv[-1] for argv, _kwargs in fake.calls if "shell" in argv]
        self.assertTrue(any(command.startswith("rm -f /tmp/.brick-bootloader-")
                            for command in commands))
        self.assertIn(f"rm -f {boot.ADB_PAYLOAD_RECORD} {boot.ADB_STATE_RECORD}; "
                      f"rmdir {boot.ADB_LOCK}", commands)
        self.assertTrue(all("timeout" in kwargs for _argv, kwargs in fake.calls))

    def test_adb_cleanup_failure_is_reported_and_lock_is_retained(self):
        root, current, candidate, identity, _original, _disk, fake = self.fixture()
        backup = root / "backup.bin"
        with patch.object(boot.subprocess, "run", side_effect=fake):
            with self.assertRaisesRegex(boot.UpgradeError, "cleanup failed"):
                with boot.AdbCard("/dev/mmcblk1", writable=True, adb="mock-adb") as card:
                    boot.upgrade(card, candidate, identity, [("known.bin", current)],
                                 CID, backup)
                    fake.fail_cleanup = True
        self.assertTrue(fake.locked)


if __name__ == "__main__":
    unittest.main()
