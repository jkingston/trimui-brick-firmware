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

def package(images, directory):
    manifest = json.loads((images / 'manifest.json').read_text())
    checksums = []
    for source, target in PAYLOAD.items():
        path = images / source
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != manifest['files'][source]['sha256']:
            raise ValueError(f'Artifact checksum mismatch: {source}')
        (directory / target).write_bytes(data)
        checksums.append(f'{digest}  {target}\n')
    (directory / 'SHA256SUMS').write_text(''.join(checksums))
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    archive = directory / 'update.tar.gz'
    with tarfile.open(archive, 'w:gz') as tar:
        for name in (*PAYLOAD.values(), 'SHA256SUMS', 'manifest.json'):
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
[ "$($bb cat /sys/class/block/mmcblk0/device/type)" = SD ]
$bb grep -q 'trimui,brick' /sys/firmware/devicetree/base/compatible
$bb mkdir /tmp/.brick-update-lock
mounted=0
staged=0
cleanup() {{
 if [ "$staged" = 1 ]; then $bb rm -r "$stage"; fi
 if [ "$mounted" = 1 ]; then $bb umount "$mountpoint"; fi
 $bb rmdir /tmp/.brick-update-lock
}}
trap cleanup EXIT
$bb mkdir -p "$work/payload" "$mountpoint"
$bb gzip -dc /tmp/{token}.tar.gz > "$work/payload.tar"
$bb tar -xf "$work/payload.tar" -C "$work/payload"
$bb rm "$work/payload.tar"
(cd "$work/payload" && $bb sha256sum -c SHA256SUMS)
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
$bb cp "$work/payload/"* "$stage/"
(cd "$stage" && $bb sha256sum -c SHA256SUMS)
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
    args = parser.parse_args()
    token = 'brick-update-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='brick-update-') as temporary:
        directory = Path(temporary)
        archive, manifest = package(args.images, directory)
        script = directory / 'install.sh'
        script.write_text(install_script(token))
        with Console(args.port) as console:
            console.upload(archive, '/tmp/' + token + '.tar.gz', raw=True, timeout=args.timeout)
            console.upload(script, '/tmp/' + token + '.sh')
            print(console.checked('/bin/sh ' + shlex.quote('/tmp/' + token + '.sh'), timeout=args.timeout).decode(), end='')
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
