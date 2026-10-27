#!/usr/bin/env python3
"""Build the isolated, fail-closed Brick CE/TRNG qualification profile."""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from emmc_recovery import enabled as emmc_recovery_enabled
from usb_profile import profile as usb_profile


KEY = 'BR2_BRICK_CE_TRNG_QUALIFICATION'
G2D_KEY = 'BR2_BRICK_G2D_QUALIFICATION'
VE_KEY = 'BR2_BRICK_VE_QUALIFICATION'
VP9_KEY = 'BR2_BRICK_VP9_QUALIFICATION'
FRAGMENT_KEY = 'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES'
FRAGMENT = '$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/ce-trng.fragment'
DTB = 'sun50i-a133-trimui-brick.dtb'
MARKER_NODE = '/chosen'
MARKER = 'trimui,ce-trng-qualification'
CE = '/soc/crypto@1904000'
IOMMU = '/soc/iommu@30f0000'
G2D = '/soc/g2d@6480000'
VE = '/soc/video-codec@1c0e000'
VP9 = '/soc/video-codec@1c00000'
CCU = '/soc/clock@3001000'


def _one(config, pattern, description):
    matches = re.findall(pattern, config.read_text(), re.M)
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one {description}')
    return matches[0]


def enabled(config):
    line = _one(
        config,
        rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$',
        'CE/TRNG profile configuration')
    return line == f'{KEY}=y'


def _fragment(config):
    return _one(config, rf'^{re.escape(FRAGMENT_KEY)}="([^"]*)"$',
                'kernel fragment selection')


def check_output(config, requested):
    requested = requested == 'qualification'
    if not config.exists():
        return
    if enabled(config) != requested:
        raise ValueError('Use a fresh output directory when changing CE/TRNG profile')
    selected = _fragment(config)
    if requested and selected != FRAGMENT:
        raise ValueError('CE/TRNG qualification requires its exact kernel fragment')
    if not requested and selected == FRAGMENT:
        raise ValueError('Disabled CE/TRNG profile retains its kernel fragment')


def configure(config, requested):
    requested = requested == 'qualification'
    text = config.read_text()
    old = rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$'
    new = f'{KEY}=y' if requested else f'# {KEY} is not set'
    text, count = re.subn(old, new, text, flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate CE/TRNG profile configuration')
    fragment = re.search(rf'^{re.escape(FRAGMENT_KEY)}="([^"]*)"$', text, re.M)
    if not fragment:
        raise ValueError('Missing or duplicate kernel fragment selection')
    current = fragment.group(1)
    if requested:
        if current:
            raise ValueError('CE/TRNG qualification cannot share a kernel fragment')
        replacement = f'{FRAGMENT_KEY}="{FRAGMENT}"'
    elif current == FRAGMENT:
        replacement = f'{FRAGMENT_KEY}=""'
    else:
        replacement = fragment.group(0)
    text = text[:fragment.start()] + replacement + text[fragment.end():]
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
    props = _properties(fdtget, dtb, CE)
    required = {'compatible', 'reg', 'interrupts', 'clocks', 'clock-names',
                'resets', 'status'}
    if not required <= props:
        raise ValueError('CE lacks its reviewed hardware contract')
    if _get(fdtget, dtb, CE, 'compatible') != 'allwinner,sun50i-a133-crypto':
        raise ValueError('CE has an unexpected compatible')
    if _cells(fdtget, dtb, CE, 'reg') != [0x01904000, 0xa0]:
        raise ValueError('CE has an unexpected register window')
    if _cells(fdtget, dtb, CE, 'interrupts') != [0, 92, 1]:
        raise ValueError('CE interrupt is not stock SPI 92 edge-rising')
    if _get(fdtget, dtb, CE, 'clock-names').split() != ['bus', 'mod', 'ram']:
        raise ValueError('CE clock roles do not match the A133 contract')
    ccu_phandle = _cells(fdtget, dtb, CCU, 'phandle')[0]
    if _cells(fdtget, dtb, CE, 'clocks') != [
            ccu_phandle, 39, ccu_phandle, 38, ccu_phandle, 53]:
        raise ValueError('CE clocks do not match the A133 contract')
    if _cells(fdtget, dtb, CE, 'resets') != [ccu_phandle, 4]:
        raise ValueError('CE reset does not match the A133 contract')
    if 'iommus' in props:
        raise ValueError('CE must not acquire an IOMMU mapping')
    expected = 'okay' if active else 'disabled'
    if _status(fdtget, dtb, CE) != expected:
        raise ValueError('CE has unexpected status')
    for node in (IOMMU, G2D, VE, VP9):
        if _status(fdtget, dtb, node) != 'disabled':
            raise ValueError('CE/TRNG qualification must isolate peer accelerators')
    actual_marker = _get(fdtget, dtb, MARKER_NODE, MARKER, default='absent')
    expected_marker = 'isolated-zero-credit' if marker else 'absent'
    if actual_marker != expected_marker:
        raise ValueError('Unexpected CE/TRNG qualification marker')


def _config_value(config, key):
    yes = re.findall(rf'^{re.escape(key)}=y$', config.read_text(), re.M)
    no = re.findall(rf'^# {re.escape(key)} is not set$', config.read_text(), re.M)
    if len(yes) + len(no) != 1:
        raise ValueError(f'Expected exactly one {key} kernel configuration')
    return bool(yes)


def verify_kernel(config, kernel):
    if not enabled(config):
        raise ValueError('kernel verification requires CE/TRNG qualification')
    kernel_config = kernel / '.config'
    if not kernel_config.is_file():
        raise FileNotFoundError(kernel_config)
    required = ('CONFIG_CRYPTO_DEV_SUN50I_A133_CE',
                'CONFIG_CRYPTO_DEV_SUN50I_A133_CE_TRNG',
                'CONFIG_HW_RANDOM', 'CONFIG_CRYPTO_USER_API_HASH',
                'CONFIG_CRYPTO_USER_API_SKCIPHER')
    for key in required:
        if not _config_value(kernel_config, key):
            raise ValueError(f'{key} is not built into the qualification kernel')
    for relative in ('drivers/crypto/allwinner/sun50i-a133-ce/core.o',
                     'drivers/char/hw_random/core.o'):
        obj = kernel / relative
        if not obj.is_file():
            raise FileNotFoundError(obj)
    source = (kernel / 'drivers/crypto/allwinner/sun50i-a133-ce/core.c').read_text()
    iommu = source.find('if (device_iommu_mapped(dev))')
    dma = source.find('dma_set_mask_and_coherent(dev, DMA_BIT_MASK(32))')
    if iommu < 0 or dma < 0 or iommu > dma or 'return -EOPNOTSUPP;' not in source[iommu:dma]:
        raise ValueError('A133 CE no longer rejects IOMMU mappings before DMA setup')
    for contract in ('ce->trng.no_entropy_credit = true;',
                     'memchr_inv(s->output + req->bytes, 0xa5,',
                     'max = min_t(size_t, max, 8192);',
                     'max = round_down(max, 32);',
                     '#define CE_TIMEOUT_MS 1000',
                     'ce_recovery_fault(&ce->recovery, flow->index, cause,'):
        if contract not in source:
            raise ValueError(f'A133 CE lacks reviewed TRNG contract: {contract}')

    hwrng = (kernel / 'drivers/char/hw_random/core.c').read_text()
    helper = (kernel / 'drivers/char/hw_random/core-internal.h').read_text()
    store_start = hwrng.find('static ssize_t rng_quality_store(')
    store_end = hwrng.find('static DEVICE_ATTR_RW(rng_current);', store_start)
    if store_start < 0 or store_end < 0:
        raise ValueError('hwrng core lacks the reviewed rng_quality store')
    store = hwrng[store_start:store_end]
    if not re.search(
            r'ret = hwrng_validate_quality\(rng, quality\);\s*'
            r'if \(ret\)\s*goto out;\s*rng->quality = quality;', store):
        raise ValueError('hwrng core does not reject invalid quality before assignment')
    for contract in (
            'if (quality > 1024)\n\t\treturn -EINVAL;',
            'if (rng->no_entropy_credit && quality)\n\t\treturn -EPERM;'):
        if contract not in helper:
            raise ValueError('hwrng quality validator lacks the zero-credit contract')
    for contract in (
            'if (!rng->no_entropy_credit && current_quality != rng->quality)',
            'quality = rng->no_entropy_credit ? 0 : rng->quality;',
            'if (rng->no_entropy_credit)\n\t\trng->quality = 0;'):
        if contract not in hwrng:
            raise ValueError('hwrng fill path can override zero-credit quality')
    support = {
        'drivers/char/hw_random/Kconfig': (
            'config HW_RANDOM_KUNIT_TEST',),
        'drivers/char/hw_random/Makefile': (
            'obj-$(CONFIG_HW_RANDOM_KUNIT_TEST) += core-test.o',),
        'drivers/char/hw_random/core-test.c': (
            'hwrng_quality_normal_source_test',
            'hwrng_quality_zero_credit_source_test',
            'hwrng_validate_quality(&rng, 0)',
            'hwrng_validate_quality(&rng, 1)',
            'hwrng_validate_quality(&rng, 1024)',
            'hwrng_validate_quality(&rng, 1025)'),
        'Documentation/admin-guide/hw_random.rst': (
            'The core rejects attempts to assign\n'
            'such a source a nonzero ``rng_quality`` value.',),
    }
    for relative, contracts in support.items():
        text = (kernel / relative).read_text()
        if any(contract not in text for contract in contracts):
            raise ValueError(f'hwrng zero-credit support is incomplete in {relative}')


def _peer_enabled(config, key):
    return bool(re.search(rf'^{re.escape(key)}=y$', config.read_text(), re.M))


def install(images, config, fdtget, fdtput):
    if not enabled(config):
        raise ValueError('CE/TRNG installation requires the qualification configuration')
    if usb_profile(config) != 'peripheral':
        raise ValueError('CE/TRNG qualification requires the USB gadget profile')
    if emmc_recovery_enabled(config):
        raise ValueError('CE/TRNG and eMMC qualification must remain separate')
    if _peer_enabled(config, 'BR2_PACKAGE_BRICK_SCP'):
        raise ValueError('CE/TRNG and SCP qualification must remain separate')
    if any(_peer_enabled(config, key) for key in (G2D_KEY, VE_KEY, VP9_KEY)):
        raise ValueError('CE/TRNG and peer accelerator qualification must remain separate')
    destination = images / DTB
    if not destination.is_file():
        raise FileNotFoundError(destination)
    validate(fdtget, destination, False)
    with tempfile.NamedTemporaryFile(dir=images, delete=False) as temporary:
        staged = Path(temporary.name)
    try:
        shutil.copyfile(destination, staged)
        subprocess.run([str(fdtput), '-t', 's', str(staged), CE,
                        'status', 'okay'], check=True)
        subprocess.run([str(fdtput), '-t', 's', str(staged), MARKER_NODE,
                        MARKER, 'isolated-zero-credit'], check=True)
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
