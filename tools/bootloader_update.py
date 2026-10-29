#!/usr/bin/env python3
"""Safely update or restore the Brick SD card's raw bootloader region."""

import argparse
import base64
import binascii
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

from serial_console import Console, DEFAULT_PORT as DEFAULT_CONSOLE_PORT, ProtocolError


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
FDT_MAGIC = 0xD00DFEED
ADB_COMMAND_TIMEOUT = 30
ADB_TRANSFER_TIMEOUT = 120
SERIAL_READ_CHUNK = 256 * 1024
ADB_LOCK = "/tmp/.brick-bootloader-update-lock"
ADB_PAYLOAD_RECORD = ADB_LOCK + "/payload"
ADB_STATE_RECORD = ADB_LOCK + "/state"


class UpgradeError(RuntimeError):
    pass


class RestoreError(UpgradeError):
    pass


class WriteUncertainError(UpgradeError):
    """A raw ADB write may still be active; another write must not be started."""


@dataclass(frozen=True)
class CandidateIdentity:
    sha256: str
    bytes: int
    manifest_sha256: str
    firmware_commit: str
    kernel_commit: str


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def normalized_sha256(value, name="SHA-256"):
    if (not isinstance(value, str) or len(value) != 64 or
            any(character not in "0123456789abcdef" for character in value.lower())):
        raise UpgradeError(f"{name} must be exactly 64 hexadecimal digits")
    return value.lower()


def validate_fit(fit, name):
    if len(fit) < 40:
        raise UpgradeError(f"{name} has a truncated FIT header")
    (magic, total, off_struct, off_strings, off_reserve, version,
     last_compatible, _boot_cpu, strings_size, struct_size) = struct.unpack_from(
         ">10I", fit)
    if magic != FDT_MAGIC or total != len(fit):
        raise UpgradeError(f"{name} has an invalid FIT extent")
    if version < 17 or last_compatible > version:
        raise UpgradeError(f"{name} has an unsupported FIT version")
    if off_reserve < 40 or off_reserve % 8:
        raise UpgradeError(f"{name} has an invalid FIT reserve map")
    struct_end = off_struct + struct_size
    strings_end = off_strings + strings_size
    if (off_struct % 4 or off_struct < 40 or struct_end > total or
            off_strings < 40 or strings_end > total):
        raise UpgradeError(f"{name} has invalid FIT block bounds")
    intervals = sorted(((off_struct, struct_end, "structure"),
                        (off_strings, strings_end, "strings")))
    if intervals[0][1] > intervals[1][0]:
        raise UpgradeError(f"{name} has overlapping FIT blocks")

    reserve = off_reserve
    while True:
        if reserve + 16 > total or reserve + 16 > min(off_struct, off_strings):
            raise UpgradeError(f"{name} has an unterminated FIT reserve map")
        address, size = struct.unpack_from(">QQ", fit, reserve)
        reserve += 16
        if address == 0 and size == 0:
            break

    strings = fit[off_strings:strings_end]
    position = off_struct
    depth = 0
    paths = set()
    stack = []
    saw_root = False
    saw_end = False
    while position < struct_end:
        token = struct.unpack_from(">I", fit, position)[0]
        position += 4
        if token == 1:  # FDT_BEGIN_NODE
            terminator = fit.find(b"\0", position, struct_end)
            if terminator < 0:
                raise UpgradeError(f"{name} has an unterminated FIT node name")
            node = fit[position:terminator].decode("ascii", errors="strict")
            position = (terminator + 4) & ~3
            if position > struct_end or (depth == 0 and (saw_root or node)):
                raise UpgradeError(f"{name} has an invalid FIT root node")
            saw_root = True
            stack.append(node)
            depth += 1
            paths.add("/" + "/".join(part for part in stack if part))
        elif token == 2:  # FDT_END_NODE
            if depth == 0:
                raise UpgradeError(f"{name} has unbalanced FIT nodes")
            depth -= 1
            stack.pop()
        elif token == 3:  # FDT_PROP
            if depth == 0 or position + 8 > struct_end:
                raise UpgradeError(f"{name} has an invalid FIT property")
            length, name_offset = struct.unpack_from(">II", fit, position)
            position += 8
            if name_offset >= strings_size or strings.find(b"\0", name_offset) < 0:
                raise UpgradeError(f"{name} has an invalid FIT property name")
            position = (position + length + 3) & ~3
            if position > struct_end:
                raise UpgradeError(f"{name} has a truncated FIT property")
        elif token == 4:  # FDT_NOP
            continue
        elif token == 9:  # FDT_END
            if depth or not saw_root:
                raise UpgradeError(f"{name} has an incomplete FIT structure")
            saw_end = True
            if any(fit[position:struct_end]):
                raise UpgradeError(f"{name} has data after the FIT end token")
            break
        else:
            raise UpgradeError(f"{name} has an unknown FIT structure token")
    if not saw_end or "/images" not in paths or "/configurations" not in paths:
        raise UpgradeError(f"{name} is not a complete boot FIT")


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
    if fit_offset + 40 > len(data):
        raise UpgradeError(f"{name} has no appended FIT")
    validate_fit(data[fit_offset:], name)


def authorize_candidate(candidate, manifest_bytes, expected_manifest_sha256):
    expected = normalized_sha256(expected_manifest_sha256, "manifest SHA-256")
    if sha256(manifest_bytes) != expected:
        raise UpgradeError("candidate manifest SHA-256 does not match the reviewed manifest")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UpgradeError("candidate manifest is not valid JSON") from error
    if not isinstance(manifest, dict):
        raise UpgradeError("candidate manifest is not an object")
    source_commits = {}
    for source in ("firmware", "kernel"):
        identity = manifest.get(source)
        if not isinstance(identity, dict) or identity.get("dirty") is not False:
            raise UpgradeError(f"candidate manifest does not identify a clean {source} source")
        commit = identity.get("commit")
        if (not isinstance(commit, str) or len(commit) != 40 or
                any(character not in "0123456789abcdef" for character in commit.lower())):
            raise UpgradeError(f"candidate manifest has an invalid {source} commit")
        source_commits[source] = commit.lower()
    files = manifest.get("files")
    record = files.get("u-boot-sunxi-with-spl.bin") if isinstance(files, dict) else None
    if not isinstance(record, dict):
        raise UpgradeError("candidate manifest has no U-Boot artifact record")
    recorded_hash = normalized_sha256(record.get("sha256"), "candidate artifact SHA-256")
    recorded_bytes = record.get("bytes")
    if (not isinstance(recorded_bytes, int) or isinstance(recorded_bytes, bool) or
            recorded_bytes <= 0):
        raise UpgradeError("candidate manifest has an invalid U-Boot artifact size")
    if recorded_bytes != len(candidate) or recorded_hash != sha256(candidate):
        raise UpgradeError("candidate does not match the reviewed manifest")
    validate_bootloader(candidate, "candidate")
    return CandidateIdentity(recorded_hash, recorded_bytes, expected,
                             source_commits["firmware"], source_commits["kernel"])


def parse_genimage_mbr(sector):
    if len(sector) != 512 or sector[510:512] != b"\x55\xaa":
        raise UpgradeError("card has no valid DOS partition table")
    raw_entries = []
    for index in range(4):
        entry = sector[446 + index * 16:462 + index * 16]
        boot, ptype = entry[0], entry[4]
        start, sectors = struct.unpack_from("<II", entry, 8)
        raw_entries.append((entry, (boot, ptype, start, sectors)))
    boot = raw_entries[0][1]
    if (boot[0] != 0x80 or boot[1] != 0x0C or
            boot[2] != BOOT_PARTITION_START or
            boot[3] != BOOT_PARTITION_SECTORS):
        raise UpgradeError("card boot partition does not match the Brick genimage layout")
    entries = [boot]
    second_raw, data = raw_entries[1]
    if any(second_raw):
        if (data[0] != 0 or data[1] != 0x83 or
                data[2] != DATA_PARTITION_START or
                data[3] != DATA_PARTITION_SECTORS):
            raise UpgradeError("card data partition does not match the Brick genimage layout")
        if boot[2] + boot[3] > data[2]:
            raise UpgradeError("card boot and data partitions overlap")
        entries.append(data)
    if any(raw_entries[index][0] != b"\0" * 16 for index in (2, 3)):
        raise UpgradeError("card has an unexpected partition outside the Brick genimage slots")
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
        self.uncertain_write = False

    def _run(self, arguments, timeout=ADB_COMMAND_TIMEOUT):
        try:
            result = subprocess.run(self.prefix + arguments, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise UpgradeError(f"ADB command timed out after {timeout} seconds") from error
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip()
            raise UpgradeError(f"ADB command failed: {message or result.returncode}")
        return result.stdout

    def shell(self, command, timeout=ADB_COMMAND_TIMEOUT):
        return self._run(["shell", command], timeout=timeout).replace(b"\r\n", b"\n")

    def _require_root(self):
        if self.shell("id -u").strip() != b"0":
            raise UpgradeError("stock ADB shell is not root")

    def __enter__(self):
        self._require_root()
        # These are stock's established no-sleep sentinels. They do not touch SD.
        self.shell("printf '%s\\n' brick-bootloader-update > /tmp/stay_awake; "
                   "printf '%s\\n' brick-bootloader-update > /tmp/stay_alive")
        if self.shell(f"if test -e {ADB_LOCK}; then echo locked; fi").strip():
            raise UpgradeError(
                "remote bootloader lock exists; recover the interrupted transport "
                "before any update or restore"
            )
        self.shell(f"mkdir {ADB_LOCK}")
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

    def __exit__(self, exception_type=None, exception=None, _traceback=None):
        if self.uncertain_write:
            # The lock and payload are recovery evidence. Removing either could
            # let another raw writer overlap an operation still alive remotely.
            return False
        cleanup_errors = []
        for path in self.remote_files:
            try:
                self.shell(f"rm -f {path}")
            except UpgradeError as error:
                cleanup_errors.append(error)
        if self.locked:
            try:
                self.shell(f"rm -f {ADB_PAYLOAD_RECORD} {ADB_STATE_RECORD}; "
                           f"rmdir {ADB_LOCK}")
            except UpgradeError as error:
                cleanup_errors.append(error)
            self.locked = False
        if cleanup_errors:
            detail = "; ".join(str(error) for error in cleanup_errors)
            if exception is not None and hasattr(exception, "add_note"):
                exception.add_note(f"ADB cleanup also failed: {detail}")
            elif exception_type is None:
                raise UpgradeError(f"ADB cleanup failed: {detail}")
        return False

    def recover_uncertain_write(self):
        """Clear a retained ADB lock only after proving its writer has exited."""
        self._require_root()
        payload = self.shell(
            f"test -d {ADB_LOCK}; test -f {ADB_PAYLOAD_RECORD}; "
            f"test -f {ADB_STATE_RECORD}; cat {ADB_PAYLOAD_RECORD}; "
            f"cat {ADB_STATE_RECORD}"
        ).decode().split()
        if len(payload) != 2:
            raise UpgradeError("remote bootloader lock has incomplete recovery state")
        payload, state = payload
        prefix = "/tmp/.brick-bootloader-"
        token = payload.removeprefix(prefix)
        if (not payload.startswith(prefix) or len(token) != 32 or
                any(character not in "0123456789abcdef" for character in token)):
            raise UpgradeError("remote bootloader lock has an invalid payload record")
        if state not in ("writing", "complete"):
            raise UpgradeError("remote raw write was not recorded as started; lock preserved")
        command = (
            f"set -e; test \"$(cat {ADB_PAYLOAD_RECORD})\" = {payload}; "
            f"state=$(cat {ADB_STATE_RECORD}); "
            "test \"$state\" = writing -o \"$state\" = complete; "
            "for file in /proc/[0-9]*/cmdline; do "
            "test \"$file\" = \"/proc/$$/cmdline\" && continue; "
            f"if /bin/busybox grep -Fq -f {ADB_PAYLOAD_RECORD} \"$file\" 2>/dev/null; "
            "then echo writer-active; exit 7; fi; done; "
            f"rm -f {payload} {ADB_PAYLOAD_RECORD} {ADB_STATE_RECORD}; "
            f"rmdir {ADB_LOCK}; "
            "echo recovered"
        )
        try:
            output = self.shell(command, timeout=ADB_COMMAND_TIMEOUT)
        except UpgradeError as error:
            raise UpgradeError(
                "uncertain remote raw writer is still active or could not be disproved; "
                "lock preserved"
            ) from error
        if output.strip() != b"recovered":
            raise UpgradeError("remote recovery did not return its exact completion marker")

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
        expected = count * block
        arguments = ["/bin/busybox", "dd", f"if={self.path}",
                     f"bs={block}", f"skip={first}", f"count={count}"]
        try:
            data = self._run(["exec-out", *arguments], timeout=ADB_TRANSFER_TIMEOUT)
        except UpgradeError as exec_error:
            # The Brick's stock adbd closes exec-out and only offers a legacy
            # PTY shell, which performs CR/LF conversion.  Encode remotely so
            # the PTY carries text, then strictly decode and size-check it.
            command = (" ".join(arguments) +
                       " 2>/dev/null | /bin/busybox base64")
            try:
                encoded = self._run(["shell", command], timeout=ADB_TRANSFER_TIMEOUT)
            except UpgradeError as shell_error:
                if hasattr(shell_error, "add_note"):
                    shell_error.add_note(f"exec-out also failed: {exec_error}")
                raise
            compact = b"".join(encoded.split())
            encoded_size = 4 * ((expected + 2) // 3)
            if len(compact) != encoded_size:
                raise UpgradeError(
                    f"invalid base64 ADB card read length: expected {encoded_size}, "
                    f"received {len(compact)}")
            try:
                data = base64.b64decode(compact, validate=True)
            except binascii.Error as error:
                raise UpgradeError("invalid base64 ADB card read") from error
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
            self._run(["push", local, remote], timeout=ADB_TRANSFER_TIMEOUT)
        finally:
            os.unlink(local)
        digest = sha256(data)
        self.shell(f"printf '%s\\n' {remote} > {ADB_PAYLOAD_RECORD}; "
                   f"printf '%s\\n' armed > {ADB_STATE_RECORD}")
        # Keep validation outside the uncertain interval: it cannot write the
        # card, so a validation failure needs no retained writer lock.
        self.shell(f"test \"$(/bin/busybox wc -c < {remote})\" = {len(data)}; "
                   f"test \"$(/bin/busybox sha256sum {remote} | "
                   f"/bin/busybox awk '{{print $1}}')\" = {digest}",
                   timeout=ADB_TRANSFER_TIMEOUT)
        try:
            self.shell(f"set -e; printf '%s\\n' writing > {ADB_STATE_RECORD}; "
                       f"/bin/busybox dd if={remote} of={self.path} bs=4096 "
                       f"seek={offset // 4096} conv=notrunc; /bin/busybox sync; "
                       f"printf '%s\\n' complete > {ADB_STATE_RECORD}",
                       timeout=ADB_TRANSFER_TIMEOUT)
        except BaseException as error:
            # ADB cannot prove that a remote dd stopped after a timeout,
            # disconnect, or interruption. Starting an automatic restore could
            # overlap that writer and make recovery worse.
            self.uncertain_write = True
            raise WriteUncertainError(
                "ADB raw-write completion is uncertain; reconnect, run "
                "recover-adb-lock, then use the saved backup with restore"
            ) from error

    def sync(self):
        self.shell("/bin/busybox sync")

    def validate_backup_destination(self, _path):
        # The path is on the ADB host, not on the remote SD block device.
        return


class SerialCard(AdbCard):
    """Operate on the running custom firmware's SD through its USB console."""

    def __init__(self, device, writable, port=DEFAULT_CONSOLE_PORT,
                 timeout=ADB_COMMAND_TIMEOUT):
        super().__init__(device, writable)
        self.console = Console(port, timeout=timeout)

    def __enter__(self):
        self.console.__enter__()
        # AdbCard's entry checks are transport-independent. On failure it calls
        # this class's __exit__, which also closes the console.
        return super().__enter__()

    def __exit__(self, exception_type=None, exception=None, traceback=None):
        try:
            return super().__exit__(exception_type, exception, traceback)
        finally:
            self.console.__exit__(exception_type, exception, traceback)

    def shell(self, command, timeout=ADB_COMMAND_TIMEOUT):
        try:
            return self.console.checked(command, timeout=timeout)
        except (OSError, TimeoutError, ProtocolError) as error:
            raise UpgradeError(f"USB-console command failed: {error}") from error

    def recover_uncertain_write(self):
        # Recovery cannot use normal __enter__: the retained lock is precisely
        # what that path must reject. Open only the transport around the common
        # proof that no remote writer remains.
        self.console.__enter__()
        try:
            return super().recover_uncertain_write()
        finally:
            self.console.__exit__(None, None, None)

    def read(self, offset, size):
        card_size = self.size()
        if offset < 0 or size < 0 or offset + size > card_size:
            raise UpgradeError("read is outside the card")
        block = 4096
        first = offset // block
        inner = offset % block
        total_blocks = (inner + size + block - 1) // block
        blocks_per_chunk = SERIAL_READ_CHUNK // block
        chunks = []
        for consumed in range(0, total_blocks, blocks_per_chunk):
            count = min(blocks_per_chunk, total_blocks - consumed)
            expected = count * block
            command = (f"/bin/busybox dd if={self.path} bs={block} "
                       f"skip={first + consumed} count={count} 2>/dev/null | "
                       f"/bin/busybox base64")
            compact = b"".join(
                self.shell(command, timeout=ADB_TRANSFER_TIMEOUT).split())
            encoded_size = 4 * ((expected + 2) // 3)
            if len(compact) != encoded_size:
                raise UpgradeError(
                    "invalid base64 USB-console card read length: "
                    f"expected {encoded_size}, received {len(compact)}")
            try:
                chunk = base64.b64decode(compact, validate=True)
            except binascii.Error as error:
                raise UpgradeError("invalid base64 USB-console card read") from error
            if len(chunk) != expected:
                raise UpgradeError(
                    "short USB-console card read: "
                    f"expected {expected}, received {len(chunk)}")
            chunks.append(chunk)
        data = b"".join(chunks)
        return data[inner:inner + size]

    def write(self, offset, data):
        if not self.writable:
            raise UpgradeError("card was opened read-only")
        if offset < BOOT_START or offset + len(data) > BOOT_END:
            raise UpgradeError("refusing a write outside the reserved boot region")
        if offset % 4096:
            raise UpgradeError("USB-console writes must begin on a 4096-byte boundary")
        remote = "/tmp/.brick-bootloader-" + uuid.uuid4().hex
        self.remote_files.append(remote)
        with tempfile.NamedTemporaryFile(prefix="brick-bootloader-write-", delete=False) as temporary:
            temporary.write(data)
            local = temporary.name
        try:
            try:
                self.console.upload(local, remote, timeout=ADB_TRANSFER_TIMEOUT, raw=True)
            except (OSError, TimeoutError, ProtocolError, ValueError) as error:
                raise UpgradeError(f"USB-console upload failed: {error}") from error
        finally:
            os.unlink(local)
        digest = sha256(data)
        self.shell(f"printf '%s\\n' {remote} > {ADB_PAYLOAD_RECORD}; "
                   f"printf '%s\\n' armed > {ADB_STATE_RECORD}")
        # Split validation from the writer so every gadget-TTY command remains
        # below the console's bounded input-line limit. A validation failure is
        # known to precede the raw write and needs no retained writer lock.
        self.shell(f"test \"$(/bin/busybox wc -c < {remote})\" = {len(data)}; "
                   f"test \"$(/bin/busybox sha256sum {remote} | "
                   f"/bin/busybox awk '{{print $1}}')\" = {digest}",
                   timeout=ADB_TRANSFER_TIMEOUT)
        try:
            self.shell(f"set -e; printf '%s\\n' writing > {ADB_STATE_RECORD}; "
                       f"/bin/busybox dd if={remote} of={self.path} bs=4096 "
                       f"seek={offset // 4096} conv=notrunc; /bin/busybox sync; "
                       f"printf '%s\\n' complete > {ADB_STATE_RECORD}",
                       timeout=ADB_TRANSFER_TIMEOUT)
        except BaseException as error:
            self.uncertain_write = True
            raise WriteUncertainError(
                "USB-console raw-write completion is uncertain; reconnect, run "
                "recover-console-lock, then use the saved backup with restore"
            ) from error


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


def backup_metadata(expected_cid, current, candidate, candidate_identity, region, card_layout):
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
        "candidate": {
            "bytes": len(candidate),
            "sha256": sha256(candidate),
            "manifest_sha256": candidate_identity.manifest_sha256,
            "firmware_commit": candidate_identity.firmware_commit,
            "kernel_commit": candidate_identity.kernel_commit,
        },
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


def upgrade(card, candidate, candidate_identity, allowed, expected_cid, backup_path,
            dry_run=False):
    if not isinstance(candidate_identity, CandidateIdentity):
        raise UpgradeError("candidate has no authenticated manifest identity")
    if (candidate_identity.bytes != len(candidate) or
            candidate_identity.sha256 != sha256(candidate)):
        raise UpgradeError("candidate changed after manifest authentication")
    validate_bootloader(candidate, "candidate")
    for name, content in allowed:
        validate_bootloader(content, f"authorized current image {name}")
    if not allowed:
        raise UpgradeError("at least one current bootloader image must be authorized")
    card.validate_backup_destination(backup_path)
    card_layout = authenticate(card, expected_cid)
    current = match_current(card, allowed)
    original = card.read(0, BACKUP_SIZE)
    metadata = backup_metadata(expected_cid, current, candidate, candidate_identity,
                               original, card_layout)
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
        if isinstance(update_error, WriteUncertainError):
            raise RestoreError(
                f"upgrade write state is uncertain; no automatic restore was started; "
                f"reconnect, recover the retained transport lock, then restore from "
                f"{backup_path}"
            ) from update_error
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
    update.add_argument("--manifest", required=True, type=Path)
    update.add_argument("--manifest-sha256", required=True,
                        type=lambda value: normalized_sha256(value, "manifest SHA-256"))
    update.add_argument("--cid", required=True, type=normalized_cid)
    update.add_argument("--allow-current", required=True, action="append", type=Path)
    update.add_argument("--backup", required=True, type=Path)
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--adb", metavar="PATH", help="operate through root stock ADB")
    update.add_argument("--serial", help="ADB device serial")
    update.add_argument("--console-port", metavar="PATH",
                        help="operate through running custom firmware USB console")
    recovery = subparsers.add_parser("restore", help="restore and verify a saved raw boot region")
    recovery.add_argument("device", type=Path)
    recovery.add_argument("backup", type=Path)
    recovery.add_argument("--cid", required=True, type=normalized_cid)
    recovery.add_argument("--dry-run", action="store_true")
    recovery.add_argument("--adb", metavar="PATH", help="operate through root stock ADB")
    recovery.add_argument("--serial", help="ADB device serial")
    recovery.add_argument("--console-port", metavar="PATH",
                          help="operate through running custom firmware USB console")
    adb_recovery = subparsers.add_parser(
        "recover-adb-lock",
        help="prove an interrupted remote writer exited, then clear its retained lock",
    )
    adb_recovery.add_argument("device", type=Path)
    adb_recovery.add_argument("--adb", required=True, metavar="PATH")
    adb_recovery.add_argument("--serial", help="ADB device serial")
    console_recovery = subparsers.add_parser(
        "recover-console-lock",
        help="prove an interrupted USB-console writer exited, then clear its retained lock",
    )
    console_recovery.add_argument("device", type=Path)
    console_recovery.add_argument("--console-port", default=DEFAULT_CONSOLE_PORT,
                                  metavar="PATH")
    args = parser.parse_args(argv)

    if args.action == "recover-adb-lock":
        AdbCard(str(args.device), writable=False, adb=args.adb,
                serial=args.serial).recover_uncertain_write()
        print("ADB_WRITE_LOCK_RECOVERED")
        return
    if args.action == "recover-console-lock":
        SerialCard(str(args.device), writable=False,
                   port=args.console_port).recover_uncertain_write()
        print("CONSOLE_WRITE_LOCK_RECOVERED")
        return

    writable = not args.dry_run
    if args.adb and args.console_port:
        parser.error("--adb and --console-port are mutually exclusive")
    if args.adb:
        card_context = AdbCard(str(args.device), writable=writable,
                               adb=args.adb, serial=args.serial)
    elif args.console_port:
        if args.serial:
            parser.error("--serial requires --adb")
        card_context = SerialCard(str(args.device), writable=writable,
                                  port=args.console_port)
    else:
        if args.serial:
            parser.error("--serial requires --adb")
        card_context = Card(args.device, writable=writable)
    with card_context as card:
        if args.action == "upgrade":
            candidate = args.candidate.read_bytes()
            identity = authorize_candidate(candidate, args.manifest.read_bytes(),
                                           args.manifest_sha256)
            allowed = [(str(path), path.read_bytes()) for path in args.allow_current]
            metadata = upgrade(card, candidate, identity, allowed, args.cid,
                               args.backup, args.dry_run)
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
