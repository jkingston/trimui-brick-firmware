#!/usr/bin/env python3
"""Update the three boot payloads on an existing Brick development SD card."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys
import tarfile
import tempfile
import time
import uuid

from serial_console import Console, DEFAULT_PORT, ProtocolError

PAYLOAD = {'Image': 'Image', 'sun50i-a133-trimui-brick.dtb': 'brick-usb.dtb',
           'rootfs.cpio.gz': 'initramfs.cpio.gz'}
BOOTLOADER = 'u-boot-sunxi-with-spl.bin'
BOOTLOADER_OFFSET = 128 * 1024

def package(images, directory):
    manifest = json.loads((images / 'manifest.json').read_text())
    checksums = []
    archive_checksums = []
    for source, target in (*PAYLOAD.items(), (BOOTLOADER, BOOTLOADER)):
        path = images / source
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != manifest['files'][source]['sha256']:
            raise ValueError(f'Artifact checksum mismatch: {source}')
        if len(data) != manifest['files'][source]['bytes']:
            raise ValueError(f'Artifact size mismatch: {source}')
        (directory / target).write_bytes(data)
        archive_checksums.append(f'{digest}  {target}\n')
        if source in PAYLOAD:
            checksums.append(f'{digest}  {target}\n')
    with (images / 'sdcard.img').open('rb') as card:
        card.seek(BOOTLOADER_OFFSET)
        if card.read(manifest['files'][BOOTLOADER]['bytes']) != (images / BOOTLOADER).read_bytes():
            raise ValueError('Bootloader does not match the SD image at 128 KiB')
    (directory / 'SHA256SUMS').write_text(''.join(checksums))
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    for name in ('SHA256SUMS', 'manifest.json'):
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        archive_checksums.append(f'{digest}  {name}\n')
    (directory / 'ARCHIVE-SHA256SUMS').write_text(''.join(archive_checksums))
    archive = directory / 'update.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name in (*PAYLOAD.values(), BOOTLOADER, 'SHA256SUMS',
                     'manifest.json', 'ARCHIVE-SHA256SUMS'):
            tar.add(directory / name, arcname=name)
    return archive, manifest


def install_script(token):
    # Names are generated UUIDs. No user input is interpolated into shell code.
    return f'''#!/bin/sh
set -eu
bb=/bin/busybox
work=/tmp/{token}
mountpoint=$work/sd
stage=$mountpoint/brick-next
previous=$mountpoint/brick-previous
current=$mountpoint/brick-diag
bootstate_tmp=$mountpoint/.brick-bootcount.pending.{token}
[ "$($bb cat /sys/class/block/mmcblk0/device/type)" = SD ]
$bb grep -q 'trimui,brick' /sys/firmware/devicetree/base/compatible
$bb mkdir /tmp/.brick-update-lock
mounted=0
staged=0
cleanup() {{
 $bb rm -f "$bootstate_tmp"
 if [ "$staged" = 1 ]; then $bb rm -r "$stage"; fi
 if [ "$mounted" = 1 ]; then $bb umount "$mountpoint"; fi
 $bb rmdir /tmp/.brick-update-lock
}}
trap cleanup EXIT
$bb mkdir -p "$work/payload" "$mountpoint"
$bb gzip -dc /tmp/{token}.tar.gz > "$work/payload.tar"
$bb tar -xf "$work/payload.tar" -C "$work/payload"
$bb rm "$work/payload.tar"
(cd "$work/payload" && $bb sha256sum -c ARCHIVE-SHA256SUMS)
# Rollback is implemented by the exact U-Boot build in this update. Refuse to
# arm bootcount unless the raw SPL/U-Boot region already contains that build.
bootloader="$work/payload/u-boot-sunxi-with-spl.bin"
bootloader_size=$($bb wc -c < "$bootloader")
[ "$bootloader_size" -gt 0 ]
[ "$bootloader_size" -le 4063232 ]
bootloader_sectors=$(( (bootloader_size + 511) / 512 ))
expected_bootloader=$($bb sha256sum "$bootloader" | $bb awk '{{ print $1 }}')
installed_bootloader=$($bb dd if=/dev/mmcblk0 bs=512 skip=256 count="$bootloader_sectors" 2>/dev/null | $bb head -c "$bootloader_size" | $bb sha256sum | $bb awk '{{ print $1 }}')
[ "$installed_bootloader" = "$expected_bootloader" ]
# Match the device number so mounts through UUID/label aliases also count.
device=$($bb cat /sys/class/block/mmcblk0p1/dev)
mounted_device=$($bb awk -v device="$device" '$3 == device {{ print $3 }}' /proc/self/mountinfo)
if [ -n "$mounted_device" ]; then
 echo "SD partition is already mounted" >&2
 exit 1
fi
$bb mount /dev/mmcblk0p1 "$mountpoint"
mounted=1
[ -f "$current/Image" ]
[ -f "$current/brick-usb.dtb" ]
[ -f "$current/initramfs.cpio.gz" ]
[ -f "$current/manifest.json" ]
[ -f "$current/SHA256SUMS" ]
(cd "$current" && $bb sha256sum -c SHA256SUMS)
[ ! -e "$stage" ]
# Retire only a prior deployment's verified rollback directory.
if [ -d "$previous" ]; then
 [ -f "$previous/manifest.json" ]
 [ -f "$previous/SHA256SUMS" ]
 (cd "$previous" && $bb sha256sum -c SHA256SUMS)
 $bb rm -r "$previous"
fi
$bb mkdir "$stage"
staged=1
$bb cp "$work/payload/Image" "$stage/Image"
$bb cp "$work/payload/brick-usb.dtb" "$stage/brick-usb.dtb"
$bb cp "$work/payload/initramfs.cpio.gz" "$stage/initramfs.cpio.gz"
$bb cp "$work/payload/SHA256SUMS" "$stage/SHA256SUMS"
$bb cp "$work/payload/manifest.json" "$stage/manifest.json"
(cd "$stage" && $bb sha256sum -c SHA256SUMS)
# Arm rollback before publishing. This makes a power loss during either rename
# recoverable even when the newly staged payload never starts.
$bb printf '\\275\\001\\000\\001' > "$bootstate_tmp"
$bb sync
$bb mv "$bootstate_tmp" "$mountpoint/brick-bootcount"
$bb sync
$bb mv "$current" "$previous"
if ! $bb mv "$stage" "$current"; then
 if ! $bb mv "$previous" "$current"; then
  # Preserve both complete copies if restoring the directory also fails.
  staged=0
  echo "Restore failed; retained brick-previous and brick-next" >&2
 fi
 exit 1
fi
staged=0
(cd "$current" && $bb sha256sum -c SHA256SUMS)
$bb sync
$bb umount "$mountpoint"
mounted=0
$bb rmdir /tmp/.brick-update-lock
trap - EXIT
$bb rm -r "$work"
$bb rm /tmp/{token}.tar.gz
echo UPDATE_VERIFIED
'''


def verify_boot_manifest(actual, expected):
    if actual.get('usb_role', 'peripheral') != expected.get('usb_role', 'peripheral'):
        raise ProtocolError('Boot USB role does not match expected image')
    if actual.get('emmc_recovery', False) != expected.get('emmc_recovery', False):
        raise ProtocolError('Boot eMMC recovery profile does not match expected image')
    if actual.get('scp_profile', 'disabled') != expected.get('scp_profile', 'disabled'):
        raise ProtocolError('Boot SCP profile does not match expected image')
    for field in ('firmware', 'kernel', 'config_sha256'):
        if actual[field] != expected[field]:
            raise ProtocolError(f'Running rootfs has different {field} metadata')
    for name in ('Image', 'sun50i-a133-trimui-brick.dtb'):
        if actual['files'][name]['sha256'] != expected['files'][name]['sha256']:
            raise ProtocolError(f'Running rootfs describes a different {name}')


def wait_for_boot(port, expected, timeout, previous_boot):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with Console(port, timeout=5) as console:
                boot = uuid.UUID(console.checked('/bin/busybox cat /proc/sys/kernel/random/boot_id').decode().strip())
                if boot == previous_boot:
                    raise ProtocolError('Device has not rebooted yet')
                result = json.loads(console.checked('/bin/busybox cat /etc/brick-build.json'))
                verify_boot_manifest(result, expected)
                release = console.checked('/bin/busybox uname -r').decode().strip()
                if release != result['kernel']['release']:
                    raise ProtocolError('Running kernel release does not match the rootfs')
                print(console.checked('/bin/busybox uname -r; /bin/busybox cat /sys/kernel/debug/pvr/status 2>/dev/null || true').decode(), end='')
                return
        except (OSError, TimeoutError, ProtocolError, ValueError, KeyError) as error:
            last = error
        time.sleep(1)
    raise TimeoutError(f'Device did not return with the expected build: {last}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', type=Path)
    parser.add_argument('--port', default=DEFAULT_PORT)
    parser.add_argument('--no-reboot', action='store_true')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--emmc-recovery', action='store_true',
                        help='explicitly permit deploying an eMMC recovery profile')
    args = parser.parse_args()
    token = 'brick-update-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='brick-update-') as temporary:
        directory = Path(temporary)
        archive, manifest = package(args.images, directory)
        if manifest.get('emmc_recovery', False) and not args.emmc_recovery:
            raise ValueError('Use --emmc-recovery for the dedicated recovery profile')
        script = directory / 'install.sh'
        script.write_text(install_script(token))
        with Console(args.port) as console:
            console.upload(archive, '/tmp/' + token + '.tar.gz', raw=True, timeout=args.timeout)
            console.upload(script, '/tmp/' + token + '.sh')
            output = console.checked('/bin/sh ' + shlex.quote('/tmp/' + token + '.sh'),
                                     timeout=args.timeout).decode()
            print(output, end='')
            if not output.splitlines() or output.splitlines()[-1] != 'UPDATE_VERIFIED':
                raise ProtocolError('Installer did not return its completion token')
            console.checked('/bin/busybox rm ' + shlex.quote('/tmp/' + token + '.sh'))
            if args.no_reboot:
                return
            previous_boot = uuid.UUID(console.checked('/bin/busybox cat /proc/sys/kernel/random/boot_id').decode().strip())
            # Let PID 1 stop services and unmount persistent data.
            console.checked('(/bin/busybox sleep 2; /bin/busybox reboot) </dev/null >/dev/null 2>&1 &')
        if manifest.get('usb_role') == 'host':
            print('Host-profile reboot requested; boot is not verified because USB serial is disabled.')
            return
        time.sleep(3)
        wait_for_boot(args.port, manifest, args.timeout, previous_boot)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TimeoutError, ProtocolError) as error:
        sys.exit(f'Deployment failed: {error}')
