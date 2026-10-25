#!/usr/bin/env python3
"""Select and validate the board's fixed USB role at build time."""
import argparse
from pathlib import Path
import re
import subprocess

DTBS = {'peripheral': 'sun50i-a133-trimui-brick',
        'host': 'sun50i-a133-trimui-brick-host'}
KEY = 'BR2_LINUX_KERNEL_INTREE_DTS_NAME'


def profile(config):
    matches = re.findall(r'^' + KEY + r'="([^"]+)"$', config.read_text(), re.M)
    for role, name in DTBS.items():
        if matches == ['allwinner/' + name]:
            return role
    raise ValueError('Expected exactly one supported Brick USB profile DTB')


def check_output(config, role):
    if config.exists() and profile(config) != role:
        raise ValueError('Use a fresh output directory when changing USB role')


def configure(config, role):
    text, count = re.subn(r'^' + KEY + r'="[^"]*"$',
                         KEY + '="allwinner/' + DTBS[role] + '"',
                         config.read_text(), flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate kernel DTB configuration')
    config.write_text(text)


def install(images, config, fdtget):
    role = profile(config)
    source = images / (DTBS[role] + '.dtb')
    if not source.is_file():
        raise FileNotFoundError(f'Selected kernel does not provide {source.name}')

    def get(node, prop):
        return subprocess.check_output([str(fdtget), '-t', 's', str(source),
                                        '/soc/' + node, prop], text=True).strip()

    for node, enabled in [('usb@5100000', role == 'peripheral'),
                          ('usb@5101000', role == 'host'),
                          ('usb@5101400', role == 'host')]:
        if get(node, 'status') != ('okay' if enabled else 'disabled'):
            raise ValueError(f'{role} profile has inconsistent {node} status')
        if enabled and get(node, 'dr_mode') != role:
            raise ValueError(f'{role} profile has inconsistent {node} dr_mode')
    # Keep the bootloader/updater payload ABI independent of the build profile.
    destination = images / (DTBS['peripheral'] + '.dtb')
    if source != destination:
        destination.write_bytes(source.read_bytes())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    guard = commands.add_parser('check-output')
    guard.add_argument('config', type=Path)
    guard.add_argument('role', choices=DTBS)
    setup = commands.add_parser('configure')
    setup.add_argument('config', type=Path)
    setup.add_argument('role', choices=DTBS)
    build = commands.add_parser('install')
    build.add_argument('images', type=Path)
    build.add_argument('config', type=Path)
    build.add_argument('fdtget', type=Path)
    args = parser.parse_args()
    if args.command == 'check-output':
        check_output(args.config, args.role)
    elif args.command == 'configure':
        configure(args.config, args.role)
    else:
        install(args.images, args.config, args.fdtget)
