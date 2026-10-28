#!/usr/bin/env python3
"""Build the isolated, fail-closed Brick VP9 qualification profile."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from emmc_recovery import enabled as emmc_recovery_enabled
from usb_profile import profile as usb_profile


KEY = 'BR2_BRICK_VP9_QUALIFICATION'
G2D_KEY = 'BR2_BRICK_G2D_QUALIFICATION'
VE_KEY = 'BR2_BRICK_VE_QUALIFICATION'
CE_KEY = 'BR2_BRICK_CE_TRNG_QUALIFICATION'
COMBINED_KEY = 'BR2_BRICK_COMBINED_ACCELERATOR_QUALIFICATION'
FRAGMENT_KEY = 'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES'
FRAGMENT = '$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/vp9.fragment'
DTB = 'sun50i-a133-trimui-brick.dtb'
MARKER_NODE = '/chosen'
MARKER = 'trimui,vp9-qualification'
VP9 = '/soc/video-codec@1c00000'
VE_DOMAIN = '/video-engine-domain'
DOMAIN_PROPERTY = 'allwinner,video-engine-domain'
IOMMU = '/soc/iommu@30f0000'
VE = '/soc/video-codec@1c0e000'
G2D = '/soc/g2d@6480000'
CE = '/soc/crypto@1904000'
MM_SRAM = '/soc/syscon@3000000/sram@24000/sram-section@0'
VE_SRAM_PARENT = '/soc/syscon@3000000/sram@1a00000'
VE_SRAM = VE_SRAM_PARENT + '/sram-section@0'
CCU = '/soc/clock@3001000'


def _one(config, pattern, description):
    matches = re.findall(pattern, config.read_text(), re.M)
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one {description}')
    return matches[0]


def enabled(config):
    line = _one(config, rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$',
                'VP9 profile configuration')
    return line == f'{KEY}=y'


def _selected(config, key):
    return bool(re.search(rf'^{re.escape(key)}=y$', config.read_text(), re.M))


def _fragment(config):
    return _one(config, rf'^{re.escape(FRAGMENT_KEY)}="([^"]*)"$',
                'kernel fragment selection')


def _uses_eudev(config):
    return enabled(config) or any(_selected(config, key)
                                  for key in (VE_KEY, COMBINED_KEY))


def check_output(config, requested):
    requested = requested == 'qualification'
    if not config.exists():
        return
    if enabled(config) != requested:
        raise ValueError('Use a fresh output directory when changing VP9 profile')
    selected = _fragment(config)
    if requested and selected != FRAGMENT:
        raise ValueError('VP9 qualification requires its exact kernel fragment')
    if not requested and selected == FRAGMENT:
        raise ValueError('Disabled VP9 profile retains its kernel fragment')
    text = config.read_text().splitlines()
    expected = (
        ('# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV is not set',
         'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV=y') if _uses_eudev(config) else
        ('BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV=y',
         '# BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV is not set'))
    if any(value not in text for value in expected):
        raise ValueError('VP9 profile has the wrong device manager')


def configure(config, requested):
    requested = requested == 'qualification'
    text = config.read_text()
    old = rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$'
    new = f'{KEY}=y' if requested else f'# {KEY} is not set'
    text, count = re.subn(old, new, text, flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate VP9 profile configuration')
    fragment = re.search(rf'^{re.escape(FRAGMENT_KEY)}="([^"]*)"$', text, re.M)
    if not fragment:
        raise ValueError('Missing or duplicate kernel fragment selection')
    current = fragment.group(1)
    if requested:
        if current:
            raise ValueError('VP9 qualification cannot share a kernel fragment')
        replacement = f'{FRAGMENT_KEY}="{FRAGMENT}"'
    elif current == FRAGMENT:
        replacement = f'{FRAGMENT_KEY}=""'
    else:
        replacement = fragment.group(0)
    text = text[:fragment.start()] + replacement + text[fragment.end():]
    use_eudev = requested or any(re.search(
        rf'^{re.escape(key)}=y$', text, re.M)
        for key in (VE_KEY, COMBINED_KEY))
    settings = {
        'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV': not use_eudev,
        'BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_EUDEV': use_eudev,
    }
    for key, value in settings.items():
        pattern = rf'^(?:{re.escape(key)}=y|# {re.escape(key)} is not set)$'
        replacement = f'{key}=y' if value else f'# {key} is not set'
        text, count = re.subn(pattern, replacement, text, flags=re.M)
        if count != 1:
            raise ValueError(f'Missing or duplicate {key} configuration')
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
    props = _properties(fdtget, dtb, VP9)
    if not {'compatible', 'reg', 'interrupts', DOMAIN_PROPERTY, 'status'} <= props:
        raise ValueError('VP9 lacks its reviewed hardware contract')
    if _get(fdtget, dtb, VP9, 'compatible') != 'allwinner,sun50i-a133-vpu-g2':
        raise ValueError('VP9 has an unexpected compatible')
    if _cells(fdtget, dtb, VP9, 'reg') != [0x01c00000, 0x1000]:
        raise ValueError('VP9 has an unexpected register window')
    if _cells(fdtget, dtb, VP9, 'interrupts') != [0, 90, 4]:
        raise ValueError('VP9 interrupt is not stock SPI 90 level-high')
    if {'clocks', 'clock-names', 'resets', 'iommus'} & props:
        raise ValueError('VP9 must use the shared domain without an IOMMU mapping')
    expected = 'okay' if active else 'disabled'
    if _status(fdtget, dtb, VP9) != expected:
        raise ValueError('VP9 has unexpected status')

    domain_props = _properties(fdtget, dtb, VE_DOMAIN)
    required = {'compatible', 'clocks', 'clock-names', 'resets', 'phandle'}
    if not required <= domain_props:
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
    domain_phandle = _cells(fdtget, dtb, VE_DOMAIN, 'phandle')[0]
    if _cells(fdtget, dtb, VP9, DOMAIN_PROPERTY) != [domain_phandle]:
        raise ValueError('VP9 references the wrong shared domain')

    for node in (IOMMU, VE, G2D, CE, MM_SRAM, VE_SRAM_PARENT, VE_SRAM):
        if _status(fdtget, dtb, node) != 'disabled':
            raise ValueError('VP9 qualification must isolate peer accelerators')
    actual_marker = _get(fdtget, dtb, MARKER_NODE, MARKER, default='absent')
    expected_marker = 'isolated-no-iommu' if marker else 'absent'
    if actual_marker != expected_marker:
        raise ValueError('Unexpected VP9 qualification marker')


def _config_value(config, key):
    yes = re.findall(rf'^{re.escape(key)}=y$', config.read_text(), re.M)
    no = re.findall(rf'^# {re.escape(key)} is not set$', config.read_text(), re.M)
    if len(yes) + len(no) != 1:
        raise ValueError(f'Expected exactly one {key} kernel configuration')
    return bool(yes)


def verify_kernel(config, kernel):
    if not enabled(config):
        raise ValueError('kernel verification requires VP9 qualification')
    kernel_config = kernel / '.config'
    if not kernel_config.is_file():
        raise FileNotFoundError(kernel_config)
    for key in ('CONFIG_VIDEO_HANTRO', 'CONFIG_VIDEO_HANTRO_SUNXI',
                'CONFIG_V4L2_VP9', 'CONFIG_SUN50I_A133_VE_DOMAIN'):
        if not _config_value(kernel_config, key):
            raise ValueError(f'{key} is not built into the qualification kernel')
    for relative in (
            'drivers/media/platform/verisilicon/hantro_drv.o',
            'drivers/media/platform/verisilicon/sunxi_vpu_hw.o',
            'drivers/media/platform/verisilicon/hantro_g2_vp9_dec.o',
            'drivers/soc/sunxi/sun50i-a133-ve-domain.o'):
        if not (kernel / relative).is_file():
            raise FileNotFoundError(kernel / relative)
    hantro = (kernel / 'drivers/media/platform/verisilicon/hantro_drv.c').read_text()
    backend = (kernel / 'drivers/media/platform/verisilicon/sunxi_vpu_hw.c').read_text()
    domain = (kernel / 'drivers/soc/sunxi/sun50i-a133-ve-domain.c').read_text()
    contracts = (
        ('allwinner,sun50i-a133-vpu-g2', hantro),
        ('.job_acquire = sunxi_a133_vpu_job_acquire,', backend),
        ('.job_release = sunxi_a133_vpu_job_release,', backend),
        ('sun50i_a133_ve_domain_reset(vpu->variant_priv, vpu->dev);', backend),
        ('#define A133_VE_RATE 432000000', domain),
        ('clk_set_rate(domain->clocks[1].clk, A133_VE_RATE)', domain),
    )
    for contract, source in contracts:
        if contract not in source:
            raise ValueError(f'VP9 kernel lacks reviewed contract: {contract}')


def install(images, config, fdtget, fdtput):
    if not enabled(config):
        raise ValueError('VP9 installation requires the qualification configuration')
    if usb_profile(config) != 'peripheral':
        raise ValueError('VP9 qualification requires the USB gadget profile')
    if emmc_recovery_enabled(config):
        raise ValueError('VP9 and eMMC qualification must remain separate')
    if _selected(config, 'BR2_PACKAGE_BRICK_SCP'):
        raise ValueError('VP9 and SCP qualification must remain separate')
    if any(_selected(config, key)
           for key in (G2D_KEY, VE_KEY, CE_KEY, COMBINED_KEY)):
        raise ValueError('VP9 and peer accelerator qualification must remain separate')
    destination = images / DTB
    if not destination.is_file():
        raise FileNotFoundError(destination)
    validate(fdtget, destination, False)
    with tempfile.NamedTemporaryFile(dir=images, delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(destination, staged)
        subprocess.run([str(fdtput), '-t', 's', str(staged), VP9,
                        'status', 'okay'], check=True)
        subprocess.run([str(fdtput), '-t', 's', str(staged), MARKER_NODE,
                        MARKER, 'isolated-no-iommu'], check=True)
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
