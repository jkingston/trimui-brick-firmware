#!/usr/bin/env python3
"""Safely update or restore the Brick SD card's raw bootloader region."""

import argparse
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid


BOOT_START = 128 * 1024
BOOT_END = 4 * 1024 * 1024
BOOT_SIZE = BOOT_END - BOOT_START
BACKUP_SIZE = BOOT_END
BOOT_PARTITION_START = 4 * 1024 * 1024 // 512
BOOT_PARTITION_SECTORS = 124 * 1024 * 1024 // 512
DATA_PARTITION_START = 128 * 1024 * 1024 // 512
DATA_PARTITION_SECTORS = 64 * 1024 * 1024 // 512
SUNXI_CHECKSUM_STAMP = 0x5F0A6C39
BLKGETSIZE64 = 0x80081272


class UpgradeError(RuntimeError):
    pass


class RestoreError(UpgradeError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def validate_bootloader(data, name="bootloader"):
    if not 32 <= len(data) <= BOOT_SIZE:
        raise UpgradeError(f"{name} length is outside the reserved boot region")
    if data[4:12] != b"eGON.BT0":
        raise UpgradeError(f"{name} has no Allwinner SPL signature")
    checksum, spl_size = struct.unpack_from("<II", data, 12)
    if not 0 < spl_size <= len(data) or spl_size % 4:
        raise UpgradeError(f"{name} has an invalid SPL extent")
    words = list(struct.unpack_from(f"<{spl_size // 4}I", data))
    words[3] = SUNXI_CHECKSUM_STAMP
    if sum(words) & 0xFFFFFFFF != checksum:
        raise UpgradeError(f"{name} has an invalid SPL checksum")
    fit_offset = max(32768, spl_size)
    if fit_offset + 8 > len(data):
        raise UpgradeError(f"{name} has no appended FIT")
    magic, fit_size = struct.unpack_from(">II", data, fit_offset)
    if magic != 0xD00DFEED or fit_size != len(data) - fit_offset:
        raise UpgradeError(f"{name} has an invalid FIT extent")


def parse_genimage_mbr(sector):
    if len(sector) != 512 or sector[510:512] != b"\x55\xaa":
        raise UpgradeError("card has no valid DOS partition table")
    entries = []
    for index in range(4):
        entry = sector[446 + index * 16:462 + index * 16]
        boot, ptype = entry[0], entry[4]
        start, sectors = struct.unpack_from("<II", entry, 8)
        if ptype or start or sectors:
            entries.append((boot, ptype, start, sectors))
    if len(entries) not in (1, 2):
        raise UpgradeError("card does not use a known Brick partition layout")
    boot = entries[0]
    if (boot[0] != 0x80 or boot[1] != 0x0C or
            boot[2] != BOOT_PARTITION_START or
            boot[3] != BOOT_PARTITION_SECTORS):
        raise UpgradeError("card boot partition does not match the Brick genimage layout")
    if len(entries) == 2:
        data = entries[1]
        if (data[0] != 0 or data[1] != 0x83 or
                data[2] != DATA_PARTITION_START or
                data[3] != DATA_PARTITION_SECTORS):
            raise UpgradeError("card data partition does not match the Brick genimage layout")
        if boot[2] + boot[3] > data[2]:
            raise UpgradeError("card boot and data partitions overlap")
    return entries


def write_new_file(path, content):
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("backup write made no progress")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass(frozen=True)
class Identity:
    cid: str
    kind: str = "SD"
    logical_block_size: int = 512


class Card:
    """Bounded random-access card API; tests use the same upgrade engine."""

    def __init__(self, path, writable, identity=None):
        self.path = Path(path)
        self.writable = writable
        self._identity = identity
        self._sysfs = None
        flags = os.O_RDWR if writable else os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        self.fd = os.open(self.path, flags)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._check_node()
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)

    def _check_node(self):
        mode = os.fstat(self.fd).st_mode
        if self._identity is not None:
            if not stat.S_ISREG(mode):
                raise UpgradeError("test cards must be regular files")
            return
        if not stat.S_ISBLK(mode):
            raise UpgradeError("device is not a block device")
        st = os.fstat(self.fd)
        node = (Path("/sys/dev/block") /
                f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}").resolve()
        if (node / "partition").exists():
            raise UpgradeError("device is a partition, not a whole SD card")
        self._sysfs = node
        self._identity = self._read_identity()

    def _read_identity(self):
        try:
            kind = (self._sysfs / "device/type").read_text().strip()
            cid = (self._sysfs / "device/cid").read_text().strip().lower()
            logical = int((self._sysfs / "queue/logical_block_size").read_text())
        except (OSError, ValueError) as error:
            raise UpgradeError("device has no readable native MMC identity") from error
        return Identity(cid, kind, logical)

    @property
    def identity(self):
        return self._identity if self._sysfs is None else self._read_identity()

    def size(self):
        if stat.S_ISREG(os.fstat(self.fd).st_mode):
            return os.fstat(self.fd).st_size
        return struct.unpack("Q", fcntl.ioctl(self.fd, BLKGETSIZE64, b"\0" * 8))[0]

    def read(self, offset, size):
        if offset < 0 or size < 0 or offset + size > self.size():
            raise UpgradeError("read is outside the card")
        chunks = []
        position = offset
        remaining = size
        while remaining:
            chunk = os.pread(self.fd, min(remaining, 1024 * 1024), position)
            if not chunk:
                raise UpgradeError("short card read")
            chunks.append(chunk)
            position += len(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def write(self, offset, data):
        if not self.writable:
            raise UpgradeError("card was opened read-only")
        if offset < BOOT_START or offset + len(data) > BOOT_END:
            raise UpgradeError("refusing a write outside the reserved boot region")
        position = offset
        view = memoryview(data)
        while view:
            count = os.pwrite(self.fd, view[:1024 * 1024], position)
            if count <= 0:
                raise OSError("card write made no progress")
            position += count
            view = view[count:]

    def sync(self):
        os.fsync(self.fd)

    def validate_backup_destination(self, path):
        if self._sysfs is None:
            return
        parent = Path(path).parent
        st = parent.stat()
        backing = Path("/sys/dev/block") / f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
        if backing.exists():
            backing = backing.resolve()
            if backing == self._sysfs or self._sysfs in backing.parents:
                raise UpgradeError("backup destination is on the SD card being updated")


class AdbCard:
    """Operate on the SD through the root ADB service in awake stock firmware."""

    def __init__(self, device, writable, adb="adb", serial=None):
        if not isinstance(device, str) or not device.startswith("/dev/mmcblk"):
            raise UpgradeError("ADB device must be a whole /dev/mmcblkN device")
        suffix = device.removeprefix("/dev/mmcblk")
        if not suffix.isdigit():
            raise UpgradeError("ADB device must be a whole /dev/mmcblkN device")
        self.path = device
        self.name = "mmcblk" + suffix
        self.writable = writable
        self.prefix = [str(adb), "-P", "5038"]
        if serial:
            self.prefix += ["-s", serial]
        self.remote_files = []
        self.locked = False

    def _run(self, arguments):
        result = subprocess.run(self.prefix + arguments, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip()
            raise UpgradeError(f"ADB command failed: {message or result.returncode}")
        return result.stdout

    def shell(self, command):
        return self._run(["shell", command]).replace(b"\r\n", b"\n")

    def __enter__(self):
        if self.shell("id -u").strip() != b"0":
            raise UpgradeError("stock ADB shell is not root")
        # These are stock's established no-sleep sentinels. They do not touch SD.
        self.shell("printf '%s\\n' brick-bootloader-update > /tmp/stay_awake; "
                   "printf '%s\\n' brick-bootloader-update > /tmp/stay_alive")
        self.shell("mkdir /tmp/.brick-bootloader-update-lock")
        self.locked = True
        try:
            output = self.shell(f"test -b {self.path} || exit 1; "
                                f"if test -e /sys/block/{self.name}/partition; then echo partition; fi")
            if output.strip():
                raise UpgradeError("ADB device is a partition, not a whole SD card")
            # Read now so a missing MMC identity fails before any backup work.
            self.identity
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        for path in self.remote_files:
            try:
                self.shell(f"rm -f {path}")
            except UpgradeError:
                pass
        if self.locked:
            try:
                self.shell("rmdir /tmp/.brick-bootloader-update-lock")
            except UpgradeError:
                pass
            self.locked = False

    @property
    def identity(self):
        output = self.shell(
            f"cat /sys/block/{self.name}/device/type; "
            f"cat /sys/block/{self.name}/device/cid; "
            f"cat /sys/block/{self.name}/queue/logical_block_size"
        ).decode().split()
        if len(output) != 3:
            raise UpgradeError("ADB device has no readable native MMC identity")
        try:
            return Identity(output[1].lower(), output[0], int(output[2]))
        except ValueError as error:
            raise UpgradeError("ADB device reports an invalid block size") from error

    def size(self):
        output = self.shell(f"cat /sys/block/{self.name}/size").strip()
        try:
            return int(output) * 512
        except ValueError as error:
            raise UpgradeError("ADB device reports an invalid size") from error

    def read(self, offset, size):
        card_size = self.size()
        if offset < 0 or size < 0 or offset + size > card_size:
            raise UpgradeError("read is outside the card")
        block = 4096
        first = offset // block
        inner = offset % block
        count = (inner + size + block - 1) // block
        data = self._run(["exec-out", "/bin/busybox", "dd", f"if={self.path}",
                          f"bs={block}", f"skip={first}", f"count={count}"])
        expected = count * block
        # A final device block can be short, but every current use is far from EOF.
        if len(data) != expected:
            raise UpgradeError(f"short ADB card read: expected {expected}, received {len(data)}")
        return data[inner:inner + size]

    def write(self, offset, data):
        if not self.writable:
            raise UpgradeError("card was opened read-only")
        if offset < BOOT_START or offset + len(data) > BOOT_END:
            raise UpgradeError("refusing a write outside the reserved boot region")
        if offset % 4096:
            raise UpgradeError("ADB writes must begin on a 4096-byte boundary")
        remote = "/tmp/.brick-bootloader-" + uuid.uuid4().hex
        self.remote_files.append(remote)
        with tempfile.NamedTemporaryFile(prefix="brick-bootloader-write-", delete=False) as temporary:
            temporary.write(data)
            local = temporary.name
        try:
            self._run(["push", local, remote])
        finally:
            os.unlink(local)
        digest = sha256(data)
        self.shell(f"set -e; test \"$(/bin/busybox wc -c < {remote})\" = {len(data)}; "
                   f"test \"$(/bin/busybox sha256sum {remote} | "
                   f"/bin/busybox awk '{{print $1}}')\" = {digest}; "
                   f"/bin/busybox dd if={remote} of={self.path} bs=4096 "
                   f"seek={offset // 4096} conv=notrunc; /bin/busybox sync")

    def sync(self):
        self.shell("/bin/busybox sync")

    def validate_backup_destination(self, _path):
        # The path is on the ADB host, not on the remote SD block device.
        return


def authenticate(card, expected_cid):
    identity = card.identity
    if identity.kind != "SD":
        raise UpgradeError(f"device type is {identity.kind!r}, not 'SD'")
    if identity.logical_block_size != 512:
        raise UpgradeError("device does not use the expected 512-byte logical blocks")
    if identity.cid.lower() != expected_cid.lower():
        raise UpgradeError("SD CID does not match the authorized card")
    card_size = card.size()
    if card_size < BOOT_END:
        raise UpgradeError("card is too small for the Brick genimage layout")
    sector = card.read(0, 512)
    entries = parse_genimage_mbr(sector)
    card_sectors = card_size // 512
    if any(start + sectors > card_sectors for _boot, _type, start, sectors in entries):
        raise UpgradeError("partition table extends beyond the card")
    return {"mbr_sha256": sha256(sector), "partitions": [list(entry) for entry in entries]}


def match_current(card, allowed):
    for name, content in allowed:
        if card.read(BOOT_START, len(content)) == content:
            return {"name": name, "bytes": len(content), "sha256": sha256(content)}
    raise UpgradeError("current raw bootloader is not in the authorized allowlist")


def backup_metadata(expected_cid, current, candidate, region, card_layout):
    return {
        "format": 1,
        "card_cid": expected_cid.lower(),
        "created_unix": int(time.time()),
        "layout": {
            "backup_start": 0,
            "boot_start": BOOT_START,
            "boot_end": BOOT_END,
            "boot_partition_lba": BOOT_PARTITION_START,
            "data_partition_lba": DATA_PARTITION_START,
        },
        "card_layout": card_layout,
        "backup": {"bytes": len(region), "sha256": sha256(region)},
        "current": current,
        "candidate": {"bytes": len(candidate), "sha256": sha256(candidate)},
    }


def preserve_backup(path, region, metadata):
    path = Path(path)
    metadata_path = Path(str(path) + ".json")
    write_new_file(path, region)
    write_new_file(metadata_path, (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode())


def checked_layout(card, expected_cid, expected_layout):
    actual = authenticate(card, expected_cid)
    if actual != expected_layout:
        raise RestoreError("card partition table changed after the backup")


def restore_region(card, original, expected_cid, expected_layout):
    # A removable card may have been exchanged after a failed write. Never
    # apply a saved region until the target CID and partition layout re-match.
    checked_layout(card, expected_cid, expected_layout)
    card.write(BOOT_START, original)
    card.sync()
    checked_layout(card, expected_cid, expected_layout)
    if card.read(BOOT_START, BOOT_SIZE) != original:
        raise RestoreError("restored boot region did not verify")


def upgrade(card, candidate, allowed, expected_cid, backup_path, dry_run=False):
    validate_bootloader(candidate, "candidate")
    for name, content in allowed:
        validate_bootloader(content, f"authorized current image {name}")
    if not allowed:
        raise UpgradeError("at least one current bootloader image must be authorized")
    card.validate_backup_destination(backup_path)
    card_layout = authenticate(card, expected_cid)
    current = match_current(card, allowed)
    original = card.read(0, BACKUP_SIZE)
    metadata = backup_metadata(expected_cid, current, candidate, original, card_layout)
    preserve_backup(backup_path, original, metadata)
    if dry_run:
        return metadata

    # Close the check-to-write gap as far as one process can: repeat identity,
    # layout, current-image and full-region checks after the durable backup.
    checked_layout(card, expected_cid, card_layout)
    match_current(card, allowed)
    if card.read(0, BACKUP_SIZE) != original:
        raise UpgradeError("raw boot area changed after backup; no write performed")
    expected = (original[:BOOT_START] + candidate +
                original[BOOT_START + len(candidate):])
    try:
        card.write(BOOT_START, candidate)
        card.sync()
        checked_layout(card, expected_cid, card_layout)
        if card.read(0, BACKUP_SIZE) != expected:
            raise UpgradeError("updated raw boot area did not match the expected image")
    except BaseException as update_error:
        try:
            restore_region(card, original[BOOT_START:BOOT_END], expected_cid, card_layout)
        except BaseException as restore_error:
            raise RestoreError(
                f"upgrade failed ({update_error}); automatic restore also failed "
                f"({restore_error}); use {backup_path} for physical recovery"
            ) from restore_error
        raise UpgradeError(f"upgrade failed; original boot region restored: {update_error}") from update_error
    return metadata


def load_backup(path):
    path = Path(path)
    region = path.read_bytes()
    metadata = json.loads(Path(str(path) + ".json").read_text())
    if not isinstance(metadata, dict):
        raise UpgradeError("backup metadata is not an object")
    if metadata.get("format") != 1:
        raise UpgradeError("unsupported backup metadata format")
    layout = metadata.get("layout", {})
    expected_layout = {"backup_start": 0, "boot_start": BOOT_START, "boot_end": BOOT_END,
                       "boot_partition_lba": BOOT_PARTITION_START,
                       "data_partition_lba": DATA_PARTITION_START}
    if layout != expected_layout:
        raise UpgradeError("backup layout does not match this tool")
    description = metadata.get("backup", {})
    if len(region) != BACKUP_SIZE or description.get("bytes") != BACKUP_SIZE:
        raise UpgradeError("backup has the wrong length")
    if description.get("sha256") != sha256(region):
        raise UpgradeError("backup checksum mismatch")
    card_layout = metadata.get("card_layout", {})
    if (not isinstance(card_layout.get("mbr_sha256"), str) or
            not isinstance(card_layout.get("partitions"), list)):
        raise UpgradeError("backup has no valid card-layout identity")
    return region, metadata


def restore(card, backup_path, expected_cid, dry_run=False):
    region, metadata = load_backup(backup_path)
    if metadata.get("card_cid", "").lower() != expected_cid.lower():
        raise UpgradeError("backup belongs to a different SD CID")
    checked_layout(card, expected_cid, metadata["card_layout"])
    if not dry_run:
        restore_region(card, region[BOOT_START:BOOT_END], expected_cid,
                       metadata["card_layout"])
    return metadata


def normalized_cid(value):
    value = value.strip().lower()
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("CID must be exactly 32 hexadecimal digits")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    update = subparsers.add_parser("upgrade", help="back up, update and verify a bootloader")
    update.add_argument("device", type=Path)
    update.add_argument("candidate", type=Path)
    update.add_argument("--cid", required=True, type=normalized_cid)
    update.add_argument("--allow-current", required=True, action="append", type=Path)
    update.add_argument("--backup", required=True, type=Path)
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--adb", metavar="PATH", help="operate through root stock ADB")
    update.add_argument("--serial", help="ADB device serial")
    recovery = subparsers.add_parser("restore", help="restore and verify a saved raw boot region")
    recovery.add_argument("device", type=Path)
    recovery.add_argument("backup", type=Path)
    recovery.add_argument("--cid", required=True, type=normalized_cid)
    recovery.add_argument("--dry-run", action="store_true")
    recovery.add_argument("--adb", metavar="PATH", help="operate through root stock ADB")
    recovery.add_argument("--serial", help="ADB device serial")
    args = parser.parse_args(argv)

    writable = not args.dry_run
    if args.adb:
        card_context = AdbCard(str(args.device), writable=writable,
                               adb=args.adb, serial=args.serial)
    else:
        if args.serial:
            parser.error("--serial requires --adb")
        card_context = Card(args.device, writable=writable)
    with card_context as card:
        if args.action == "upgrade":
            candidate = args.candidate.read_bytes()
            allowed = [(str(path), path.read_bytes()) for path in args.allow_current]
            metadata = upgrade(card, candidate, allowed, args.cid, args.backup, args.dry_run)
            verb = "DRY_RUN_VERIFIED" if args.dry_run else "UPGRADE_VERIFIED"
        else:
            metadata = restore(card, args.backup, args.cid, args.dry_run)
            verb = "DRY_RUN_VERIFIED" if args.dry_run else "RESTORE_VERIFIED"
    print(f"{verb} cid={metadata['card_cid']} backup_sha256={metadata['backup']['sha256']}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, UpgradeError, json.JSONDecodeError) as error:
        sys.exit(f"Bootloader operation failed: {error}")
