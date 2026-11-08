#!/usr/bin/env python3
"""Update a Brick product root from its retained recovery slot."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from serial_console import Console, DEFAULT_PORT, ProtocolError

PAYLOAD = {'Image': 'Image', 'sun50i-a133-trimui-brick.dtb': 'brick-usb.dtb',
           'rootfs.ext4': 'rootfs.ext4'}
BOOTLOADER = 'u-boot-sunxi-with-spl.bin'
BOOTLOADER_OFFSET = 128 * 1024
COMPATIBLE_BOOTLOADER = 'COMPATIBLE-BOOTLOADER-SHA256'


def upload_archive(console, archive, destination, timeout, ssh_host=None):
    """Upload a verified archive over serial or an explicitly selected SSH host."""
    if ssh_host is None:
        return console.upload(archive, destination, raw=True, timeout=timeout)
    if not ssh_host or ssh_host.startswith('-') or any(
            character not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._:@[]'
            for character in ssh_host):
        raise ValueError('Invalid SSH host')
    try:
        subprocess.run(
            ['scp', '-O', '-q', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
             '--', str(archive), f'{ssh_host}:{destination}'],
            check=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ProtocolError(f'SSH upload failed: {error}') from error
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    try:
        actual = subprocess.check_output(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
             ssh_host, '/bin/busybox', 'sha256sum', destination],
            text=True, timeout=timeout).split()[0]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ProtocolError(f'SSH verification failed: {error}') from error
    if actual != expected:
        subprocess.run(
            ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
             ssh_host, '/bin/busybox', 'rm', '-f', destination],
            check=False, timeout=timeout)
        raise ProtocolError('SSH upload SHA256 mismatch')
    return actual

def package(images, directory, compatible_bootloader=None):
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
    metadata_names = ['SHA256SUMS', 'manifest.json']
    archive_names = [*PAYLOAD.values(), BOOTLOADER, *metadata_names]
    if compatible_bootloader is not None:
        compatible = compatible_bootloader.read_bytes()
        candidate_size = (images / BOOTLOADER).stat().st_size
        if len(compatible) != candidate_size:
            raise ValueError('Compatible bootloader must match candidate size')
        (directory / COMPATIBLE_BOOTLOADER).write_text(
            hashlib.sha256(compatible).hexdigest() + '\n')
        archive_names.append(COMPATIBLE_BOOTLOADER)
        metadata_names.append(COMPATIBLE_BOOTLOADER)
    for name in metadata_names:
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        archive_checksums.append(f'{digest}  {name}\n')
    (directory / 'ARCHIVE-SHA256SUMS').write_text(''.join(archive_checksums))
    archive = directory / 'update.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name in (*archive_names, 'ARCHIVE-SHA256SUMS'):
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
case " $($bb cat /proc/cmdline) " in
 *' brick.bootdir=brick-previous '*) ;;
 *) echo "Storage-root updater requires brick-previous recovery" >&2; exit 1 ;;
esac
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
if [ "$installed_bootloader" != "$expected_bootloader" ]; then
 [ -f "$work/payload/{COMPATIBLE_BOOTLOADER}" ]
 compatible_bootloader=$($bb cat "$work/payload/{COMPATIBLE_BOOTLOADER}")
 case "$compatible_bootloader" in *[!0-9a-f]*|'') exit 1 ;; esac
 [ "${{#compatible_bootloader}}" = 64 ]
 [ "$installed_bootloader" = "$compatible_bootloader" ]
fi
# Match the device number so mounts through UUID/label aliases also count.
device=$($bb cat /sys/class/block/mmcblk0p1/dev)
mounted_device=$($bb awk -v device="$device" '$3 == device {{ print $3 }}' /proc/self/mountinfo)
if [ -n "$mounted_device" ]; then
 echo "SD partition is already mounted" >&2
 exit 1
fi
root_device=$($bb cat /sys/class/block/mmcblk0p2/dev)
mounted_root=$($bb awk -v device="$root_device" '$3 == device {{ print $3 }}' /proc/self/mountinfo)
if [ -n "$mounted_root" ]; then
 echo "Product root partition is mounted" >&2
 exit 1
fi
$bb mount /dev/mmcblk0p1 "$mountpoint"
mounted=1
[ -f "$current/Image" ]
[ -f "$current/brick-usb.dtb" ]
[ -f "$current/manifest.json" ]
[ -f "$current/SHA256SUMS" ]
(cd "$current" && $bb sha256sum -c SHA256SUMS)
[ -f "$previous/Image" ]
[ -f "$previous/brick-usb.dtb" ]
[ -f "$previous/manifest.json" ]
[ -f "$previous/SHA256SUMS" ]
(cd "$previous" && $bb sha256sum -c SHA256SUMS)
[ ! -e "$stage" ]
# Account for the verified retired directory before deleting it.  Leave a
# bounded margin for FAT allocation and directory updates so ENOSPC is rejected
# before any slot or boot state is changed.
payload_bytes=0
for name in Image brick-usb.dtb SHA256SUMS manifest.json; do
 size=$($bb wc -c < "$work/payload/$name")
 [ "$size" -gt 0 ]
 payload_bytes=$((payload_bytes + size))
done
required_kib=$(( (payload_bytes + 1023) / 1024 + 1024 ))
available_kib=$($bb df -Pk "$mountpoint" | $bb awk 'END {{ print $(NF - 2) }}')
case "$available_kib" in *[!0-9]*|'') echo "Cannot determine SD free space" >&2; exit 1 ;; esac
if [ "$available_kib" -lt "$required_kib" ]; then
 echo "Insufficient SD space: need ${{required_kib}} KiB, have ${{available_kib}} KiB" >&2
 exit 1
fi
$bb mkdir "$stage"
staged=1
# Stock mounts this FAT filesystem with `sync`. BusyBox cp uses a small copy
# buffer, so every small write waits for the card. Keep the crash-safe mount
# policy and reduce those waits by issuing bounded 1 MiB writes instead.
copy_payload_file() {{
 source=$1
 destination=$2
 expected_size=$($bb wc -c < "$source")
 [ "$expected_size" -gt 0 ]
 $bb dd if="$source" of="$destination" bs=1048576 2>/dev/null
 [ "$($bb wc -c < "$destination")" = "$expected_size" ]
}}
for name in Image brick-usb.dtb manifest.json; do
 copy_payload_file "$work/payload/$name" "$stage/$name"
done
$bb grep -v '  rootfs.ext4$' "$work/payload/SHA256SUMS" > "$stage/SHA256SUMS"
(cd "$stage" && $bb sha256sum -c SHA256SUMS)
# p2 is safe to replace only from the retained initramfs recovery. Recovery
# remains selected until the root image and candidate boot files both verify.
rootfs="$work/payload/rootfs.ext4"
rootfs_bytes=$($bb wc -c < "$rootfs")
case "$rootfs_bytes" in *[!0-9]*|'') exit 1 ;; esac
[ $((rootfs_bytes % 1048576)) -eq 0 ]
partition_sectors=$($bb cat /sys/class/block/mmcblk0p2/size)
[ "$partition_sectors" = "$((rootfs_bytes / 512))" ] || {{
 echo "Product root image does not exactly match p2" >&2; exit 1;
}}
$bb dd if="$rootfs" of=/dev/mmcblk0p2 bs=1048576 2>/dev/null
$bb sync
expected_root=$($bb sha256sum "$rootfs" | $bb awk '{{ print $1 }}')
installed_root=$($bb dd if=/dev/mmcblk0p2 bs=1048576 count=$((rootfs_bytes / 1048576)) 2>/dev/null | $bb sha256sum | $bb awk '{{ print $1 }}')
[ "$installed_root" = "$expected_root" ]
$bb rm -r "$current"
$bb mv "$stage" "$current"
staged=0
(cd "$current" && $bb sha256sum -c SHA256SUMS)
$bb sync
$bb printf '\\275\\001\\000\\001' > "$bootstate_tmp"
$bb sync
$bb mv "$bootstate_tmp" "$mountpoint/brick-bootcount"
$bb sync
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
    if actual.get('g2d_qualification', False) != expected.get('g2d_qualification', False):
        raise ProtocolError('Boot G2D qualification profile does not match expected image')
    if actual.get('scp_profile', 'disabled') != expected.get('scp_profile', 'disabled'):
        raise ProtocolError('Boot SCP profile does not match expected image')
    if actual.get('ve_qualification', False) != expected.get('ve_qualification', False):
        raise ProtocolError('Boot video-engine qualification profile does not match expected image')
    if actual.get('ce_trng_qualification', False) != expected.get('ce_trng_qualification', False):
        raise ProtocolError('Boot CE/TRNG qualification profile does not match expected image')
    if actual.get('combined_accelerator_qualification', False) != expected.get('combined_accelerator_qualification', False):
        raise ProtocolError('Boot combined accelerator profile does not match expected image')
    if actual.get('cpu_idle_qualification', False) != expected.get('cpu_idle_qualification', False):
        raise ProtocolError('Boot CPU-idle qualification profile does not match expected image')
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
    parser.add_argument('--ssh-host',
                        help='copy the large archive over SSH; retain serial control and verification')
    parser.add_argument('--compatible-bootloader', type=Path,
                        help='explicitly admit this same-size installed bootloader without writing it')
    parser.add_argument('--no-reboot', action='store_true')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--emmc-recovery', action='store_true',
                        help='explicitly permit deploying an eMMC recovery profile')
    parser.add_argument('--ve-qualification', action='store_true',
                        help='explicitly permit deploying a video-engine qualification profile')
    parser.add_argument('--g2d-qualification', action='store_true',
                        help='explicitly permit deploying a G2D qualification profile')
    parser.add_argument('--ce-trng-qualification', action='store_true',
                        help='explicitly permit deploying a CE/TRNG qualification profile')
    parser.add_argument('--combined-accelerator-qualification', action='store_true',
                        help='explicitly permit deploying the combined accelerator profile')
    parser.add_argument('--cpu-idle-qualification', action='store_true',
                        help='explicitly permit deploying the CPU-idle qualification profile')
    args = parser.parse_args()
    token = 'brick-update-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='brick-update-') as temporary:
        directory = Path(temporary)
        archive, manifest = package(args.images, directory,
                                    args.compatible_bootloader)
        if manifest.get('emmc_recovery', False) and not args.emmc_recovery:
            raise ValueError('Use --emmc-recovery for the dedicated recovery profile')
        if manifest.get('ve_qualification', False) and not args.ve_qualification:
            raise ValueError('Use --ve-qualification for the dedicated video-engine profile')
        if manifest.get('g2d_qualification', False) and not args.g2d_qualification:
            raise ValueError('Use --g2d-qualification for the dedicated G2D profile')
        if manifest.get('ce_trng_qualification', False) and not args.ce_trng_qualification:
            raise ValueError('Use --ce-trng-qualification for the dedicated CE/TRNG profile')
        if (manifest.get('combined_accelerator_qualification', False) and
                not args.combined_accelerator_qualification):
            raise ValueError('Use --combined-accelerator-qualification for the combined accelerator profile')
        if manifest.get('cpu_idle_qualification', False) and not args.cpu_idle_qualification:
            raise ValueError('Use --cpu-idle-qualification for the CPU-idle qualification profile')
        script = directory / 'install.sh'
        script.write_text(install_script(token))
        archive_destination = '/tmp/' + token + '.tar.gz'
        if args.ssh_host:
            upload_archive(None, archive, archive_destination,
                           args.timeout, args.ssh_host)
        with Console(args.port) as console:
            if not args.ssh_host:
                upload_archive(console, archive, archive_destination,
                               args.timeout)
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
