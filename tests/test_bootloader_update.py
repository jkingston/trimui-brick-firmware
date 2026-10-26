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


def image(length, fill):
    spl_size = 1024
    data = bytearray([fill] * length)
    data[4:12] = b"eGON.BT0"
    struct.pack_into("<I", data, 16, spl_size)
    words = list(struct.unpack_from(f"<{spl_size // 4}I", data))
    words[3] = boot.SUNXI_CHECKSUM_STAMP
    struct.pack_into("<I", data, 12, sum(words) & 0xFFFFFFFF)
    fit_offset = max(32768, spl_size)
    struct.pack_into(">II", data, fit_offset, 0xD00DFEED, length - fit_offset)
    return bytes(data)


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
            return boot.upgrade(card, self.candidate, [("known.bin", self.current)],
                                CID, self.backup, dry_run)

    def test_success_writes_only_exact_image_extent_and_preserves_partitions(self):
        metadata = self.update()
        expected = self.candidate + self.original_region[len(self.candidate):]
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), expected)
        self.assertEqual(self.raw(boot.BOOT_END, 18), b"PARTITION-SENTINEL")
        self.assertEqual(self.raw(128 * 1024 * 1024, 13), b"DATA-SENTINEL")
        self.assertEqual(self.backup.read_bytes(), self.original_backup)
        self.assertEqual(metadata["backup"]["sha256"], hashlib.sha256(self.original_backup).hexdigest())
        self.assertEqual(json.loads(Path(str(self.backup) + ".json").read_text())["card_cid"], CID)

    def test_dry_run_authenticates_and_preserves_backup_without_card_write(self):
        self.update(dry_run=True)
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)
        self.assertEqual(self.backup.read_bytes(), self.original_backup)

    def test_wrong_cid_refuses_before_backup_or_write(self):
        with self.open() as card, self.assertRaisesRegex(boot.UpgradeError, "CID"):
            boot.upgrade(card, self.candidate, [("known.bin", self.current)],
                         "00" * 16, self.backup)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)

    def test_unknown_current_bootloader_refuses_before_backup_or_write(self):
        other = image(43001, 0x43)
        with self.open() as card, self.assertRaisesRegex(boot.UpgradeError, "allowlist"):
            boot.upgrade(card, self.candidate, [("other.bin", other)], CID, self.backup)
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
            boot.upgrade(card, self.candidate, [("known.bin", self.current)], CID, self.backup)
        self.assertEqual(self.backup.read_bytes(), b"keep")
        self.assertEqual(self.raw(boot.BOOT_START, boot.BOOT_SIZE), self.original_region)

    def test_bad_partition_layout_is_rejected(self):
        with self.card_path.open("r+b") as card:
            card.seek(446 + 8)
            card.write(struct.pack("<I", 4096))
        with self.open() as card, self.assertRaisesRegex(boot.UpgradeError, "genimage"):
            boot.upgrade(card, self.candidate, [("known.bin", self.current)], CID, self.backup)
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


class AdbBackendTests(unittest.TestCase):
    def test_stock_adb_upgrade_uses_host_backup_and_bounded_raw_write(self):
        with tempfile.TemporaryDirectory(prefix="brick-adb-bootloader-") as directory:
            root = Path(directory)
            backup = root / "backup.bin"
            current = image(42001, 0x21)
            candidate = image(56003, 0x37)
            original = current + bytes([0xA5] * (boot.BOOT_SIZE - len(current)))
            disk = bytearray(boot.BOOT_END)
            disk[:512] = mbr()
            disk[boot.BOOT_START:boot.BOOT_END] = original
            remote = {}
            calls = []

            def completed(argv, stdout=b"", returncode=0, stderr=b""):
                return boot.subprocess.CompletedProcess(argv, returncode, stdout, stderr)

            def run(argv, **_kwargs):
                calls.append(argv)
                action = next(name for name in ("shell", "exec-out", "push") if name in argv)
                index = argv.index(action)
                if action == "push":
                    remote[argv[index + 2]] = Path(argv[index + 1]).read_bytes()
                    return completed(argv)
                if action == "exec-out":
                    arguments = argv[index + 1:]
                    values = {item.split("=", 1)[0]: int(item.split("=", 1)[1])
                              for item in arguments if item.startswith(("bs=", "skip=", "count="))}
                    start = values["bs"] * values["skip"]
                    end = start + values["bs"] * values["count"]
                    return completed(argv, bytes(disk[start:end]))
                command = argv[index + 1]
                if command == "id -u":
                    return completed(argv, b"0\n")
                if "device/type" in command:
                    return completed(argv, f"SD\n{CID}\n512\n".encode())
                if command == "cat /sys/block/mmcblk1/size":
                    return completed(argv, b"524288\n")
                if "dd if=/tmp/.brick-bootloader-" in command:
                    import re
                    match = re.search(r"dd if=(/tmp/[^ ]+) of=/dev/mmcblk1 bs=4096 seek=([0-9]+)", command)
                    self.assertIsNotNone(match)
                    payload = remote[match.group(1)]
                    start = int(match.group(2)) * 4096
                    disk[start:start + len(payload)] = payload
                return completed(argv)

            with patch.object(boot.subprocess, "run", side_effect=run):
                with boot.AdbCard("/dev/mmcblk1", writable=True,
                                  adb="mock-adb", serial="brick") as card:
                    boot.upgrade(card, candidate, [("known.bin", current)], CID, backup)

            self.assertEqual(backup.read_bytes(), bytes(disk[:boot.BOOT_START]) + original)
            self.assertEqual(disk[boot.BOOT_START:boot.BOOT_END],
                             candidate + original[len(candidate):])
            writes = [" ".join(call) for call in calls
                      if "shell" in call and " of=/dev/mmcblk1 " in call[-1]]
            self.assertEqual(len(writes), 1)
            self.assertIn("bs=4096 seek=32 conv=notrunc", writes[0])
            self.assertTrue(all("of=/dev/mmcblk1" not in " ".join(call)
                                for call in calls if "exec-out" in call))


if __name__ == "__main__":
    unittest.main()
