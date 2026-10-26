#!/usr/bin/env python3
"""Build the opt-in, fail-closed Brick eMMC recovery profile."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from scp_profile import profile as scp_profile
from usb_profile import profile as usb_profile


KEY = 'BR2_PACKAGE_BRICK_EMMC_RECOVERY'
DTB = 'sun50i-a133-trimui-brick.dtb'
NODE = '/soc/mmc@4022000'
MARKER_NODE = '/chosen'
MARKER = 'trimui,emmc-recovery'
G2D_KEY = 'BR2_BRICK_G2D_QUALIFICATION'
VE_KEY = 'BR2_BRICK_VE_QUALIFICATION'


def enabled(config):
    text = config.read_text()
    yes = len(re.findall(r'^' + KEY + r'=y$', text, re.M))
    no = len(re.findall(r'^# ' + KEY + r' is not set$', text, re.M))
    if yes + no != 1:
        raise ValueError('Expected exactly one eMMC recovery configuration')
    return bool(yes)


def check_output(config, requested):
    requested = requested == '1'
    if config.exists() and enabled(config) != requested:
        raise ValueError('Use a fresh output directory when changing eMMC recovery mode')


def configure(config, requested):
    requested = requested == '1'
    old = r'^(?:' + KEY + r'=y|# ' + KEY + r' is not set)$'
    new = KEY + '=y' if requested else '# ' + KEY + ' is not set'
    text, count = re.subn(old, new, config.read_text(), flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate eMMC recovery configuration')
    config.write_text(text)


def _get(fdtget, dtb, node, prop, kind='s', default=None):
    args = [str(fdtget), '-t', kind]
    if default is not None:
        args += ['-d', default]
    return subprocess.check_output(args + [str(dtb), node, prop], text=True).strip()


def _properties(fdtget, dtb, node):
    return set(subprocess.check_output(
        [str(fdtget), '-p', str(dtb), node], text=True).split())


def validate(fdtget, dtb, expected_status, marker=False):
    props = _properties(fdtget, dtb, NODE)
    required = {'compatible', 'status', 'bus-width', 'max-frequency',
                'pinctrl-0', 'non-removable', 'cap-mmc-highspeed',
                'mmc-ddr-1_8v', 'no-sd', 'no-sdio', 'vmmc-supply',
                'vqmmc-supply'}
    if not required <= props:
        raise ValueError('eMMC node lacks the reviewed stock DDR52 contract')
    if {'mmc-hs200-1_8v', 'mmc-hs400-1_8v', 'no-mmc'} & props:
        raise ValueError('eMMC recovery advertises an incompatible bus mode')
    compatible = _get(fdtget, dtb, NODE, 'compatible').split()
    if compatible != ['allwinner,sun50i-a133-emmc']:
        raise ValueError('Unexpected eMMC controller compatible')
    if _get(fdtget, dtb, NODE, 'status') != expected_status:
        raise ValueError('Unexpected eMMC controller status')
    if int(_get(fdtget, dtb, NODE, 'bus-width', 'u')) != 8:
        raise ValueError('Recovery eMMC must use the stock eight-bit bus')
    if int(_get(fdtget, dtb, NODE, 'max-frequency', 'u')) != 50_000_000:
        raise ValueError('Recovery eMMC must use the measured 50 MHz DDR52 target')
    actual_marker = _get(fdtget, dtb, MARKER_NODE, MARKER,
                         default='absent')
    expected_marker = 'read-only' if marker else 'absent'
    if actual_marker != expected_marker:
        raise ValueError('Unexpected eMMC recovery marker')


def install(images, config, fdtget, fdtput):
    if not enabled(config):
        raise ValueError('eMMC recovery installation requires the opt-in configuration')
    if usb_profile(config) != 'peripheral':
        raise ValueError('eMMC recovery requires the independently verified USB gadget profile')
    if scp_profile(config) != 'disabled':
        raise ValueError('eMMC recovery and SCP qualification must remain separate')
    text = config.read_text()
    if (re.search(r'^' + G2D_KEY + r'=y$', text, re.M) or
            re.search(r'^' + VE_KEY + r'=y$', text, re.M)):
        raise ValueError('eMMC and accelerator qualification must remain separate')
    destination = images / DTB
    if not destination.is_file():
        raise FileNotFoundError(destination)
    validate(fdtget, destination, 'disabled')
    with tempfile.NamedTemporaryFile(dir=images, delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(destination, staged)
        subprocess.run([str(fdtput), '-t', 's', str(staged), NODE,
                        'status', 'okay'], check=True)
        subprocess.run([str(fdtput), '-t', 's', str(staged), MARKER_NODE,
                        MARKER, 'read-only'], check=True)
        validate(fdtget, staged, 'okay', marker=True)
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    guard = commands.add_parser('check-output')
    guard.add_argument('config', type=Path)
    guard.add_argument('requested', choices=('0', '1'))
    setup = commands.add_parser('configure')
    setup.add_argument('config', type=Path)
    setup.add_argument('requested', choices=('0', '1'))
    build = commands.add_parser('install')
    build.add_argument('images', type=Path)
    build.add_argument('config', type=Path)
    build.add_argument('fdtget', type=Path)
    build.add_argument('fdtput', type=Path)
    args = parser.parse_args()
    if args.command == 'check-output':
        check_output(args.config, args.requested)
    elif args.command == 'configure':
        configure(args.config, args.requested)
    else:
        install(args.images, args.config, args.fdtget, args.fdtput)
