#!/usr/bin/env python3
"""Build the isolated, fail-closed Brick G2D qualification profile."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from emmc_recovery import enabled as emmc_recovery_enabled
from scp_profile import profile as scp_profile
from usb_profile import profile as usb_profile
KEY = 'BR2_BRICK_G2D_QUALIFICATION'
VE_KEY = 'BR2_BRICK_VE_QUALIFICATION'
VP9_KEY = 'BR2_BRICK_VP9_QUALIFICATION'
CE_KEY = 'BR2_BRICK_CE_TRNG_QUALIFICATION'
COMBINED_KEY = 'BR2_BRICK_COMBINED_ACCELERATOR_QUALIFICATION'
DTB = 'sun50i-a133-trimui-brick.dtb'
MARKER_NODE = '/chosen'
MARKER = 'trimui,g2d-qualification'
IOMMU = '/soc/iommu@30f0000'
G2D = '/soc/g2d@6480000'
VE = '/soc/video-codec@1c0e000'
VP9 = '/soc/video-codec@1c00000'
CE = '/soc/crypto@1904000'
CCU = '/soc/clock@3001000'


def enabled(config):
    text = config.read_text()
    yes = len(re.findall(r'^' + KEY + r'=y$', text, re.M))
    no = len(re.findall(r'^# ' + KEY + r' is not set$', text, re.M))
    if yes + no != 1:
        raise ValueError('Expected exactly one G2D profile configuration')
    return bool(yes)


def check_output(config, requested):
    requested = requested == 'qualification'
    if config.exists() and enabled(config) != requested:
        raise ValueError('Use a fresh output directory when changing G2D profile')


def configure(config, requested):
    requested = requested == 'qualification'
    old = r'^(?:' + KEY + r'=y|# ' + KEY + r' is not set)$'
    new = KEY + '=y' if requested else '# ' + KEY + ' is not set'
    text, count = re.subn(old, new, config.read_text(), flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate G2D profile configuration')
    config.write_text(text)


def _get(fdtget, dtb, node, prop, kind='s', default=None):
    args = [str(fdtget), '-t', kind]
    if default is not None:
        args += ['-d', default]
    return subprocess.check_output(args + [str(dtb), node, prop], text=True).strip()


def _cells(fdtget, dtb, node, prop):
    return [int(cell, 16) for cell in _get(fdtget, dtb, node, prop, 'x').split()]


def _properties(fdtget, dtb, node):
    return set(subprocess.check_output(
        [str(fdtget), '-p', str(dtb), node], text=True).split())


def _status(fdtget, dtb, node):
    return _get(fdtget, dtb, node, 'status')


def validate(fdtget, dtb, active, marker=False):
    contracts = {
        IOMMU: ('allwinner,sun50i-a133-iommu', [0x030f0000, 0x1000], [0, 66, 4]),
        G2D: ('allwinner,sun50i-a133-g2d', [0x06480000, 0x40000], [0, 91, 4]),
    }
    for node, (compatible, reg, interrupts) in contracts.items():
        props = _properties(fdtget, dtb, node)
        if not {'compatible', 'reg', 'interrupts', 'status'} <= props:
            raise ValueError(f'{node} lacks its reviewed hardware contract')
        if _get(fdtget, dtb, node, 'compatible') != compatible:
            raise ValueError(f'{node} has an unexpected compatible')
        if _cells(fdtget, dtb, node, 'reg') != reg:
            raise ValueError(f'{node} has an unexpected register window')
        if _cells(fdtget, dtb, node, 'interrupts') != interrupts:
            raise ValueError(f'{node} has an unexpected interrupt')
        expected = 'okay' if active else 'disabled'
        if _status(fdtget, dtb, node) != expected:
            raise ValueError(f'{node} has unexpected status')

    required = {'clocks', 'clock-names', 'resets', 'assigned-clocks',
                'assigned-clock-parents', 'assigned-clock-rates', 'iommus'}
    if not required <= _properties(fdtget, dtb, G2D):
        raise ValueError('G2D lacks clocks, reset, assigned rate or IOMMU ownership')
    if _get(fdtget, dtb, G2D, 'clock-names').split() != ['bus', 'mod', 'ram']:
        raise ValueError('G2D clock roles do not match the A133 contract')

    ccu_phandle = _cells(fdtget, dtb, CCU, 'phandle')[0]
    if _cells(fdtget, dtb, G2D, 'clocks') != [
            ccu_phandle, 0x23, ccu_phandle, 0x22, ccu_phandle, 0x39]:
        raise ValueError('G2D clocks do not match the A133 contract')
    if _cells(fdtget, dtb, G2D, 'resets') != [ccu_phandle, 2]:
        raise ValueError('G2D reset does not match the A133 contract')
    if _cells(fdtget, dtb, G2D, 'assigned-clocks') != [ccu_phandle, 0x22]:
        raise ValueError('G2D assigned clock does not match stock')
    if _cells(fdtget, dtb, G2D, 'assigned-clock-parents') != [ccu_phandle, 4]:
        raise ValueError('G2D clock parent does not match stock PLL_PERIPH0_2X')
    if _cells(fdtget, dtb, G2D, 'assigned-clock-rates') != [300000000]:
        raise ValueError('G2D clock rate does not match stock 300 MHz')
    if _cells(fdtget, dtb, IOMMU, 'clocks') != [ccu_phandle, 0x32]:
        raise ValueError('IOMMU clock does not match the A133 contract')
    if _cells(fdtget, dtb, IOMMU, '#iommu-cells') != [1]:
        raise ValueError('IOMMU provider must use one-cell master IDs')

    iommu_phandle = _cells(fdtget, dtb, IOMMU, 'phandle')[0]
    if _cells(fdtget, dtb, G2D, 'iommus') != [iommu_phandle, 5]:
        raise ValueError('G2D must use exact IOMMU master 5')
    for node in (VE, VP9, CE):
        if _status(fdtget, dtb, node) != 'disabled':
            raise ValueError('qualification image must isolate G2D')

    actual_marker = _get(fdtget, dtb, MARKER_NODE, MARKER, default='absent')
    expected_marker = 'isolated' if marker else 'absent'
    if actual_marker != expected_marker:
        raise ValueError('Unexpected G2D qualification marker')


def install(images, config, fdtget, fdtput):
    if not enabled(config):
        raise ValueError('G2D installation requires the qualification configuration')
    text = config.read_text()
    if any(re.search(rf'^{re.escape(key)}=y$', text, re.M)
           for key in (VE_KEY, VP9_KEY, CE_KEY, COMBINED_KEY)):
        raise ValueError('G2D and peer accelerator qualification must remain separate')
    if usb_profile(config) != 'peripheral':
        raise ValueError('G2D qualification requires the USB gadget profile')
    if emmc_recovery_enabled(config):
        raise ValueError('G2D and eMMC qualification must remain separate')
    if scp_profile(config) != 'disabled':
        raise ValueError('G2D and SCP qualification must remain separate')
    destination = images / DTB
    if not destination.is_file():
        raise FileNotFoundError(destination)
    validate(fdtget, destination, False)
    with tempfile.NamedTemporaryFile(dir=images, delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(destination, staged)
        for node in (IOMMU, G2D):
            subprocess.run([str(fdtput), '-t', 's', str(staged), node,
                            'status', 'okay'], check=True)
        subprocess.run([str(fdtput), '-t', 's', str(staged), MARKER_NODE,
                        MARKER, 'isolated'], check=True)
        validate(fdtget, staged, True, marker=True)
        staged.replace(destination)
    finally:
        staged.unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    guard = commands.add_parser('check-output')
    guard.add_argument('config', type=Path)
    guard.add_argument('requested', choices=('disabled', 'qualification'))
    setup = commands.add_parser('configure')
    setup.add_argument('config', type=Path)
    setup.add_argument('requested', choices=('disabled', 'qualification'))
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
