#!/usr/bin/env python3
"""Build the isolated, fail-closed Brick video-engine qualification profile."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from emmc_recovery import enabled as emmc_recovery_enabled
from scp_profile import profile as scp_profile
from usb_profile import profile as usb_profile


KEY = 'BR2_BRICK_VE_QUALIFICATION'
DTB = 'sun50i-a133-trimui-brick.dtb'
MARKER_NODE = '/chosen'
MARKER = 'trimui,ve-qualification'
IOMMU = '/soc/iommu@30f0000'
VE = '/soc/video-codec@1c0e000'
MM_SRAM = '/soc/syscon@3000000/sram@24000/sram-section@0'
VE_SRAM_PARENT = '/soc/syscon@3000000/sram@1a00000'
VE_SRAM = VE_SRAM_PARENT + '/sram-section@0'
G2D = '/soc/g2d@6480000'
CE = '/soc/crypto@1904000'
CCU = '/soc/clock@3001000'
ENABLED = (IOMMU, VE, MM_SRAM, VE_SRAM_PARENT, VE_SRAM)


def enabled(config):
    text = config.read_text()
    yes = len(re.findall(r'^' + KEY + r'=y$', text, re.M))
    no = len(re.findall(r'^# ' + KEY + r' is not set$', text, re.M))
    if yes + no != 1:
        raise ValueError('Expected exactly one video-engine profile configuration')
    return bool(yes)


def check_output(config, requested):
    requested = requested == 'qualification'
    if config.exists() and enabled(config) != requested:
        raise ValueError('Use a fresh output directory when changing video-engine profile')


def configure(config, requested):
    requested = requested == 'qualification'
    old = r'^(?:' + KEY + r'=y|# ' + KEY + r' is not set)$'
    new = KEY + '=y' if requested else '# ' + KEY + ' is not set'
    text, count = re.subn(old, new, config.read_text(), flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate video-engine profile configuration')
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
        VE: ('allwinner,sun50i-a133-video-engine', [0x01c0e000, 0x1000], [0, 94, 4]),
        MM_SRAM: ('allwinner,sun50i-a133-sram-c', [0, 0x21000], None),
        VE_SRAM_PARENT: ('mmio-sram', [0x01a00000, 0x200000], None),
        VE_SRAM: ('allwinner,sun50i-a133-ve-sram', [0, 0x200000], None),
    }
    for node, (compatible, reg, interrupts) in contracts.items():
        props = _properties(fdtget, dtb, node)
        if not {'compatible', 'reg', 'status'} <= props:
            raise ValueError(f'{node} lacks its reviewed hardware contract')
        if _get(fdtget, dtb, node, 'compatible') != compatible:
            raise ValueError(f'{node} has an unexpected compatible')
        if _cells(fdtget, dtb, node, 'reg') != reg:
            raise ValueError(f'{node} has an unexpected register window')
        if interrupts is not None and _cells(fdtget, dtb, node, 'interrupts') != interrupts:
            raise ValueError(f'{node} has an unexpected interrupt')
        expected = 'okay' if active else 'disabled'
        if _status(fdtget, dtb, node) != expected:
            raise ValueError(f'{node} has unexpected status')

    required = {'clocks', 'clock-names', 'resets', 'allwinner,sram', 'iommus'}
    if not required <= _properties(fdtget, dtb, VE):
        raise ValueError('video engine lacks clocks, reset, SRAM or IOMMU ownership')
    if _get(fdtget, dtb, VE, 'clock-names').split() != ['ahb', 'mod', 'ram']:
        raise ValueError('video engine clock roles do not match the A133 contract')

    ccu_phandle = _cells(fdtget, dtb, CCU, 'phandle')[0]
    if _cells(fdtget, dtb, VE, 'clocks') != [
            ccu_phandle, 0x29, ccu_phandle, 0x28, ccu_phandle, 0x34]:
        raise ValueError('video engine clocks do not match the A133 contract')
    if _cells(fdtget, dtb, VE, 'resets') != [ccu_phandle, 5]:
        raise ValueError('video engine reset does not match the A133 contract')
    if _cells(fdtget, dtb, IOMMU, 'clocks') != [ccu_phandle, 0x32]:
        raise ValueError('IOMMU clock does not match the A133 contract')
    if _cells(fdtget, dtb, IOMMU, '#iommu-cells') != [1]:
        raise ValueError('IOMMU provider must use one-cell master IDs')

    iommu_phandle = _cells(fdtget, dtb, IOMMU, 'phandle')[0]
    mm_sram_phandle = _cells(fdtget, dtb, MM_SRAM, 'phandle')[0]
    ve_sram_phandle = _cells(fdtget, dtb, VE_SRAM, 'phandle')[0]
    if _cells(fdtget, dtb, VE, 'iommus') != [iommu_phandle, 2]:
        raise ValueError('video engine must use exact IOMMU master 2')
    if _cells(fdtget, dtb, VE, 'allwinner,sram') != [
            mm_sram_phandle, 1, ve_sram_phandle, 1]:
        raise ValueError('video engine SRAM selectors do not match stock')

    for node in (G2D, CE):
        if _status(fdtget, dtb, node) != 'disabled':
            raise ValueError('qualification image must isolate the video engine')

    actual_marker = _get(fdtget, dtb, MARKER_NODE, MARKER, default='absent')
    expected_marker = 'isolated' if marker else 'absent'
    if actual_marker != expected_marker:
        raise ValueError('Unexpected video-engine qualification marker')


def install(images, config, fdtget, fdtput):
    if not enabled(config):
        raise ValueError('video-engine installation requires the qualification configuration')
    if usb_profile(config) != 'peripheral':
        raise ValueError('video-engine qualification requires the USB gadget profile')
    if emmc_recovery_enabled(config):
        raise ValueError('video-engine and eMMC qualification must remain separate')
    if scp_profile(config) != 'disabled':
        raise ValueError('video-engine and SCP qualification must remain separate')
    destination = images / DTB
    if not destination.is_file():
        raise FileNotFoundError(destination)
    validate(fdtget, destination, False)
    with tempfile.NamedTemporaryFile(dir=images, delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(destination, staged)
        for node in ENABLED:
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
