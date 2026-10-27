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
G2D_KEY = 'BR2_BRICK_G2D_QUALIFICATION'
VP9_KEY = 'BR2_BRICK_VP9_QUALIFICATION'
CE_KEY = 'BR2_BRICK_CE_TRNG_QUALIFICATION'
DTB = 'sun50i-a133-trimui-brick.dtb'
MARKER_NODE = '/chosen'
MARKER = 'trimui,ve-qualification'
ENCODER_MARKER = 'allwinner,encoder-qualification'
ENCODER_CONTRACT = 'bounded-320x240-h264-qp30-jpeg-q95'
IOMMU = '/soc/iommu@30f0000'
VE = '/soc/video-codec@1c0e000'
VP9 = '/soc/video-codec@1c00000'
VE_DOMAIN = '/video-engine-domain'
DOMAIN_PROPERTY = 'allwinner,video-engine-domain'
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
    if not config.exists():
        return
    if enabled(config) != requested:
        raise ValueError('Use a fresh output directory when changing video-engine profile')
    text = config.read_text()
    uses_eudev = requested or bool(re.search(
        rf'^{re.escape(VP9_KEY)}=y$', config.read_text(), re.M))
    expected = (
        ('# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV is not set',
         'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y') if uses_eudev else
        ('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV=y',
         '# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV is not set'))
    if any(value not in text.splitlines() for value in expected):
        raise ValueError('Video-engine profile has the wrong device manager')


def configure(config, requested):
    requested = requested == 'qualification'
    old = r'^(?:' + KEY + r'=y|# ' + KEY + r' is not set)$'
    new = KEY + '=y' if requested else '# ' + KEY + ' is not set'
    text, count = re.subn(old, new, config.read_text(), flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate video-engine profile configuration')
    use_eudev = requested or bool(re.search(
        rf'^{re.escape(VP9_KEY)}=y$', text, re.M))
    settings = {
        'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV': not use_eudev,
        'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV': use_eudev,
    }
    for key, value in settings.items():
        pattern = r'^(?:' + key + r'=y|# ' + key + r' is not set)$'
        replacement = key + '=y' if value else '# ' + key + ' is not set'
        text, count = re.subn(pattern, replacement, text, flags=re.M)
        if count != 1:
            raise ValueError(f'Missing or duplicate {key} configuration')
    config.write_text(text)


def _peer_enabled(config):
    text = config.read_text()
    return any(re.search(rf'^{re.escape(key)}=y$', text, re.M)
               for key in (G2D_KEY, VP9_KEY, CE_KEY))


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


def _config_value(config, key):
    text = config.read_text()
    yes = re.findall(rf'^{re.escape(key)}=y$', text, re.M)
    no = re.findall(rf'^# {re.escape(key)} is not set$', text, re.M)
    if len(yes) + len(no) != 1:
        raise ValueError(f'Expected exactly one {key} kernel configuration')
    return bool(yes)


def verify_kernel(config, kernel):
    if not enabled(config):
        raise ValueError('kernel verification requires video-engine qualification')
    kernel_config = kernel / '.config'
    if not kernel_config.is_file():
        raise FileNotFoundError(kernel_config)
    for key in ('CONFIG_VIDEO_SUNXI_CEDRUS',
                'CONFIG_VIDEO_SUNXI_CEDRUS_ENCODER_CORE',
                'CONFIG_VIDEO_SUNXI_CEDRUS_A133_ENCODER_QUALIFICATION',
                'CONFIG_SUN50I_A133_VE_DOMAIN'):
        if not _config_value(kernel_config, key):
            raise ValueError(f'{key} is not built into the qualification kernel')
    for relative in (
            'drivers/staging/media/sunxi/cedrus/cedrus.o',
            'drivers/staging/media/sunxi/cedrus/cedrus_a133_enc_backend.o',
            'drivers/staging/media/sunxi/cedrus/cedrus_enc_video.o',
            'drivers/soc/sunxi/sun50i-a133-ve-domain.o'):
        if not (kernel / relative).is_file():
            raise FileNotFoundError(kernel / relative)
    cedrus = (kernel / 'drivers/staging/media/sunxi/cedrus/cedrus.c').read_text()
    hardware = (kernel / 'drivers/staging/media/sunxi/cedrus/cedrus_hw.c').read_text()
    if not re.search(
            r'\.enc_ops\s*=\s*CEDRUS_A133_QUALIFICATION_OPS,', cedrus):
        raise ValueError('encoder qualification kernel lacks the A133 backend gate')
    for contract, source in (
            ('"allwinner,encoder-qualification"', hardware),
            ('"bounded-320x240-h264-qp30-jpeg-q95"', hardware)):
        if contract not in source:
            raise ValueError(f'encoder qualification kernel lacks contract: {contract}')


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

    required = {DOMAIN_PROPERTY, 'allwinner,sram', 'iommus'}
    if not required <= _properties(fdtget, dtb, VE):
        raise ValueError('video engine lacks shared-domain, SRAM or IOMMU ownership')
    if {'clocks', 'clock-names', 'resets'} & _properties(fdtget, dtb, VE):
        raise ValueError('video engine duplicates shared clock or reset ownership')

    domain_props = _properties(fdtget, dtb, VE_DOMAIN)
    if not {'compatible', 'clocks', 'clock-names', 'resets', 'phandle'} <= domain_props:
        raise ValueError('shared video-engine domain lacks its reviewed resources')
    if _get(fdtget, dtb, VE_DOMAIN, 'compatible') != \
            'allwinner,sun50i-a133-video-engine-domain':
        raise ValueError('shared video-engine domain has an unexpected compatible')
    if _get(fdtget, dtb, VE_DOMAIN, 'clock-names').split() != ['ahb', 'mod', 'ram']:
        raise ValueError('shared video-engine clock roles do not match A133')
    ccu_phandle = _cells(fdtget, dtb, CCU, 'phandle')[0]
    if _cells(fdtget, dtb, VE_DOMAIN, 'clocks') != [
            ccu_phandle, 0x29, ccu_phandle, 0x28, ccu_phandle, 0x34]:
        raise ValueError('shared video-engine clocks do not match A133')
    if _cells(fdtget, dtb, VE_DOMAIN, 'resets') != [ccu_phandle, 5]:
        raise ValueError('shared video-engine reset does not match A133')
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
    domain_phandle = _cells(fdtget, dtb, VE_DOMAIN, 'phandle')[0]
    if _cells(fdtget, dtb, VE, DOMAIN_PROPERTY) != [domain_phandle]:
        raise ValueError('video engine references the wrong shared domain')

    vp9_props = _properties(fdtget, dtb, VP9)
    if not {'compatible', 'reg', 'interrupts', DOMAIN_PROPERTY, 'status'} <= vp9_props:
        raise ValueError('VP9 peer lacks its reviewed disabled contract')
    if _get(fdtget, dtb, VP9, 'compatible') != 'allwinner,sun50i-a133-vpu-g2':
        raise ValueError('VP9 peer has an unexpected compatible')
    if _cells(fdtget, dtb, VP9, 'reg') != [0x01c00000, 0x1000] or \
            _cells(fdtget, dtb, VP9, 'interrupts') != [0, 90, 4]:
        raise ValueError('VP9 peer has unexpected hardware resources')
    if _cells(fdtget, dtb, VP9, DOMAIN_PROPERTY) != [domain_phandle]:
        raise ValueError('VP9 peer references the wrong shared domain')
    if {'clocks', 'clock-names', 'resets', 'iommus'} & vp9_props:
        raise ValueError('VP9 peer violates the shared-domain no-IOMMU contract')

    for node in (G2D, CE, VP9):
        if _status(fdtget, dtb, node) != 'disabled':
            raise ValueError('qualification image must isolate the video engine')

    actual_marker = _get(fdtget, dtb, MARKER_NODE, MARKER, default='absent')
    expected_marker = 'isolated' if marker else 'absent'
    if actual_marker != expected_marker:
        raise ValueError('Unexpected video-engine qualification marker')
    actual_encoder = _get(fdtget, dtb, VE, ENCODER_MARKER, default='absent')
    expected_encoder = ENCODER_CONTRACT if marker else 'absent'
    if actual_encoder != expected_encoder:
        raise ValueError('Unexpected encoder qualification contract')


def install(images, config, fdtget, fdtput):
    if not enabled(config):
        raise ValueError('video-engine installation requires the qualification configuration')
    if bool(re.search(rf'^{re.escape(G2D_KEY)}=y$', config.read_text(), re.M)):
        raise ValueError('video-engine and G2D qualification must remain separate')
    if _peer_enabled(config):
        raise ValueError('video-engine and peer accelerator qualification must remain separate')
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
        subprocess.run([str(fdtput), '-t', 's', str(staged), VE,
                        ENCODER_MARKER, ENCODER_CONTRACT], check=True)
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
    verify = commands.add_parser('verify-kernel')
    verify.add_argument('config', type=Path)
    verify.add_argument('kernel', type=Path)
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
    elif args.command == 'verify-kernel':
        verify_kernel(args.config, args.kernel)
    else:
        install(args.images, args.config, args.fdtget, args.fdtput)
