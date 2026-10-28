#!/usr/bin/env python3
"""Build the fail-closed Brick combined accelerator qualification profile."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import ce_trng_profile
from emmc_recovery import enabled as emmc_recovery_enabled
import g2d_profile
from scp_profile import profile as scp_profile
from usb_profile import profile as usb_profile
import ve_profile
import vp9_profile


KEY = 'BR2_BRICK_COMBINED_ACCELERATOR_QUALIFICATION'
PEER_KEYS = (
    'BR2_BRICK_G2D_QUALIFICATION',
    'BR2_BRICK_VE_QUALIFICATION',
    'BR2_BRICK_VP9_QUALIFICATION',
    'BR2_BRICK_CE_TRNG_QUALIFICATION',
)
FRAGMENT_KEY = 'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES'
FRAGMENT = ('$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/'
            'combined-accelerators.fragment')
DTB = 'sun50i-a133-trimui-brick.dtb'
MARKER_NODE = '/chosen'
MARKER = 'trimui,accelerator-qualification'
CONTRACT = 'combined-v1'
ENCODER_MARKER = 'allwinner,encoder-qualification'
ENCODER_CONTRACT = 'bounded-320x240-h264-qp30-jpeg-q95'
IOMMU = '/soc/iommu@30f0000'
G2D = '/soc/g2d@6480000'
VE = '/soc/video-codec@1c0e000'
VP9 = '/soc/video-codec@1c00000'
CE = '/soc/crypto@1904000'
MM_SRAM = '/soc/syscon@3000000/sram@24000/sram-section@0'
VE_SRAM_PARENT = '/soc/syscon@3000000/sram@1a00000'
VE_SRAM = VE_SRAM_PARENT + '/sram-section@0'
ENABLED_NODES = (IOMMU, G2D, VE, VP9, CE, MM_SRAM,
                 VE_SRAM_PARENT, VE_SRAM)
ISOLATED_MARKERS = (
    g2d_profile.MARKER,
    ve_profile.MARKER,
    vp9_profile.MARKER,
    ce_trng_profile.MARKER,
)


def _one(config, pattern, description):
    matches = re.findall(pattern, config.read_text(), re.M)
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one {description}')
    return matches[0]


def enabled(config):
    line = _one(config,
                rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$',
                'combined accelerator profile configuration')
    return line == f'{KEY}=y'


def _selected(config, key):
    return bool(re.search(rf'^{re.escape(key)}=y$', config.read_text(), re.M))


def _fragment(config):
    return _one(config, rf'^{re.escape(FRAGMENT_KEY)}="([^"]*)"$',
                'kernel fragment selection')


def check_output(config, requested):
    requested = requested == 'qualification'
    if not config.exists():
        return
    if enabled(config) != requested:
        raise ValueError('Use a fresh output directory when changing the combined profile')
    selected = _fragment(config)
    if requested and selected != FRAGMENT:
        raise ValueError('Combined qualification requires its exact kernel fragment')
    if not requested and selected == FRAGMENT:
        raise ValueError('Disabled combined profile retains its kernel fragment')
    lines = config.read_text().splitlines()
    use_eudev = requested or any(_selected(config, key) for key in (
        'BR2_BRICK_VE_QUALIFICATION', 'BR2_BRICK_VP9_QUALIFICATION'))
    expected = (
        ('# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV is not set',
         'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y') if use_eudev else
        ('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV=y',
         '# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV is not set'))
    if any(value not in lines for value in expected):
        raise ValueError('Combined qualification has the wrong device manager')


def configure(config, requested):
    requested = requested == 'qualification'
    text = config.read_text()
    pattern = rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$'
    replacement = f'{KEY}=y' if requested else f'# {KEY} is not set'
    text, count = re.subn(pattern, replacement, text, flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate combined profile configuration')

    fragment = re.search(rf'^{re.escape(FRAGMENT_KEY)}="([^"]*)"$', text, re.M)
    if not fragment:
        raise ValueError('Missing or duplicate kernel fragment selection')
    current = fragment.group(1)
    if requested:
        if current:
            raise ValueError('Combined qualification cannot share a kernel fragment')
        replacement = f'{FRAGMENT_KEY}="{FRAGMENT}"'
    elif current == FRAGMENT:
        replacement = f'{FRAGMENT_KEY}=""'
    else:
        replacement = fragment.group(0)
    text = text[:fragment.start()] + replacement + text[fragment.end():]

    use_eudev = requested or any(re.search(
        rf'^{re.escape(key)}=y$', text, re.M)
        for key in ('BR2_BRICK_VE_QUALIFICATION',
                    'BR2_BRICK_VP9_QUALIFICATION'))
    settings = {
        'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV': not use_eudev,
        'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV': use_eudev,
    }
    for setting, value in settings.items():
        pattern = rf'^(?:{re.escape(setting)}=y|# {re.escape(setting)} is not set)$'
        replacement = f'{setting}=y' if value else f'# {setting} is not set'
        text, count = re.subn(pattern, replacement, text, flags=re.M)
        if count != 1:
            raise ValueError(f'Missing or duplicate {setting} configuration')
    config.write_text(text)


def _get(fdtget, dtb, node, prop, default=None):
    args = [str(fdtget), '-t', 's']
    if default is not None:
        args += ['-d', default]
    return subprocess.check_output(args + [str(dtb), node, prop], text=True).strip()


def _status(fdtget, dtb, node):
    return _get(fdtget, dtb, node, 'status')


def validate_base(fdtget, dtb):
    """Require every independently reviewed contract before changing status."""
    g2d_profile.validate(fdtget, dtb, False)
    ve_profile.validate(fdtget, dtb, False)
    vp9_profile.validate(fdtget, dtb, False)
    ce_trng_profile.validate(fdtget, dtb, False)
    if _get(fdtget, dtb, MARKER_NODE, MARKER, default='absent') != 'absent':
        raise ValueError('Unexpected combined accelerator marker')


def validate_active(fdtget, dtb):
    for node in ENABLED_NODES:
        if _status(fdtget, dtb, node) != 'okay':
            raise ValueError(f'{node} is not enabled in the combined profile')
    if _get(fdtget, dtb, MARKER_NODE, MARKER, default='absent') != CONTRACT:
        raise ValueError('Unexpected combined accelerator marker')
    for marker in ISOLATED_MARKERS:
        if _get(fdtget, dtb, MARKER_NODE, marker, default='absent') != 'absent':
            raise ValueError('Combined profile must not claim isolated qualification')
    if _get(fdtget, dtb, VE, ENCODER_MARKER, default='absent') != ENCODER_CONTRACT:
        raise ValueError('Combined profile lacks the bounded encoder contract')


def _profile_config(config, key):
    text = config.read_text()
    text = re.sub(rf'^{re.escape(KEY)}=y$', f'# {KEY} is not set', text,
                  flags=re.M)
    text, count = re.subn(rf'^# {re.escape(key)} is not set$', f'{key}=y',
                          text, flags=re.M)
    if count != 1:
        raise ValueError(f'Missing isolated verifier key {key}')
    return text


def _config_value(config, key):
    text = config.read_text()
    yes = re.findall(rf'^{re.escape(key)}=(?:y|m)$', text, re.M)
    no = re.findall(rf'^# {re.escape(key)} is not set$', text, re.M)
    if len(yes) + len(no) != 1:
        raise ValueError(f'Expected exactly one {key} kernel configuration')
    return bool(yes)


def verify_kernel(config, kernel):
    if not enabled(config):
        raise ValueError('kernel verification requires combined qualification')
    for key, verifier in (
            ('BR2_BRICK_VE_QUALIFICATION', ve_profile.verify_kernel),
            ('BR2_BRICK_VP9_QUALIFICATION', vp9_profile.verify_kernel),
            ('BR2_BRICK_CE_TRNG_QUALIFICATION', ce_trng_profile.verify_kernel)):
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as temporary:
            temporary.write(_profile_config(config, key))
            path = Path(temporary.name)
        try:
            verifier(path, kernel)
        finally:
            path.unlink(missing_ok=True)
    kernel_config = kernel / '.config'
    if not _config_value(kernel_config, 'CONFIG_VIDEO_SUN8I_ROTATE'):
        raise ValueError('CONFIG_VIDEO_SUN8I_ROTATE is not built')
    g2d = kernel / 'drivers/media/platform/sunxi/sun8i-rotate/sun8i-rotate.o'
    if not g2d.is_file():
        raise FileNotFoundError(g2d)


def install(images, config, fdtget, fdtput):
    if not enabled(config):
        raise ValueError('combined installation requires qualification configuration')
    if any(_selected(config, key) for key in PEER_KEYS):
        raise ValueError('combined and isolated accelerator profiles are mutually exclusive')
    if usb_profile(config) != 'peripheral':
        raise ValueError('combined qualification requires the USB gadget profile')
    if emmc_recovery_enabled(config):
        raise ValueError('combined and eMMC qualification must remain separate')
    if scp_profile(config) != 'disabled':
        raise ValueError('combined and SCP qualification must remain separate')
    destination = images / DTB
    if not destination.is_file():
        raise FileNotFoundError(destination)
    validate_base(fdtget, destination)
    with tempfile.NamedTemporaryFile(dir=images, delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(destination, staged)
        for node in ENABLED_NODES:
            subprocess.run([str(fdtput), '-t', 's', str(staged), node,
                            'status', 'okay'], check=True)
        subprocess.run([str(fdtput), '-t', 's', str(staged), MARKER_NODE,
                        MARKER, CONTRACT], check=True)
        subprocess.run([str(fdtput), '-t', 's', str(staged), VE,
                        ENCODER_MARKER, ENCODER_CONTRACT], check=True)
        validate_active(fdtget, staged)
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
