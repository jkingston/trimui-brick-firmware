#!/usr/bin/env python3
"""Select and verify the native A133 CPU-idle qualification profile."""
import argparse
from pathlib import Path
import re
import subprocess

from emmc_recovery import enabled as emmc_recovery_enabled
from scp_profile import profile as scp_profile
from usb_profile import (NATIVE_DEEP_IDLE_DTB, NATIVE_IDLE_DTB,
                         expected_dtb as usb_expected_dtb,
                         profile as usb_profile)


KEY = 'BR2_BRICK_CPU_IDLE_QUALIFICATION'
DTS_KEY = 'BR2_LINUX_KERNEL_INTREE_DTS_NAME'
DEFAULT_DTB = 'sun50i-a133-trimui-brick'
DTB = NATIVE_IDLE_DTB
DEEP_DTB = NATIVE_DEEP_IDLE_DTB
MARKER = 'trimui,cpu-idle-qualification'
PEER_KEYS = (
    'BR2_BRICK_G2D_QUALIFICATION',
    'BR2_BRICK_VE_QUALIFICATION',
    'BR2_BRICK_VP9_QUALIFICATION',
    'BR2_BRICK_CE_TRNG_QUALIFICATION',
    'BR2_BRICK_COMBINED_ACCELERATOR_QUALIFICATION',
)


def _selected(config, key):
    text = config.read_text()
    yes = len(re.findall(rf'^{re.escape(key)}=y$', text, re.M))
    no = len(re.findall(rf'^# {re.escape(key)} is not set$', text, re.M))
    if yes + no != 1:
        raise ValueError(f'Expected exactly one {key} configuration')
    return bool(yes)


def enabled(config):
    return _selected(config, KEY)


def selected_dtb(config):
    matches = re.findall(rf'^{re.escape(DTS_KEY)}="allwinner/([^"]+)"$',
                         config.read_text(), re.M)
    if len(matches) != 1:
        raise ValueError('Expected exactly one kernel DTB selection')
    return matches[0]


def expected_dtb(config):
    selected_scp = scp_profile(config)
    if selected_scp == 'disabled':
        return DTB
    if selected_scp == 'deep':
        return DEEP_DTB
    raise ValueError('CPU-idle qualification requires SCP disabled or deep')


def check_output(config, requested):
    requested = requested == 'qualification'
    if not config.exists():
        return
    if enabled(config) != requested:
        raise ValueError('Use a fresh output directory when changing CPU-idle profile')
    selected = selected_dtb(config)
    idle_dtbs = {DTB, DEEP_DTB}
    if (requested and selected != expected_dtb(config)) or \
            (not requested and selected in idle_dtbs):
        raise ValueError('CPU-idle profile and kernel DTB do not form a complete pair')


def configure(config, requested):
    requested = requested == 'qualification'
    text = config.read_text()
    pattern = rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$'
    replacement = f'{KEY}=y' if requested else f'# {KEY} is not set'
    text, count = re.subn(pattern, replacement, text, flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate CPU-idle profile configuration')
    dts_matches = list(re.finditer(
        rf'^{re.escape(DTS_KEY)}="allwinner/([^"]+)"$', text, re.M))
    if len(dts_matches) != 1:
        raise ValueError('Missing or duplicate kernel DTB configuration')
    dts_match = dts_matches[0]
    current = dts_match.group(1)
    if requested:
        selected = expected_dtb(config)
    elif current in {DTB, DEEP_DTB}:
        selected = usb_expected_dtb('peripheral', scp_profile(config))
    else:
        selected = current
    dts = f'{DTS_KEY}="allwinner/{selected}"'
    text = text[:dts_match.start()] + dts + text[dts_match.end():]
    config.write_text(text)


def _kernel_option(config, key):
    text = config.read_text()
    yes = re.findall(rf'^{re.escape(key)}=y$', text, re.M)
    no = re.findall(rf'^# {re.escape(key)} is not set$', text, re.M)
    if len(yes) > 1 or len(no) > 1 or yes and no:
        raise ValueError(f'Conflicting {key} kernel configuration')
    return bool(yes)


def verify_kernel(config, kernel):
    active = enabled(config)
    selected = selected_dtb(config)
    if (active and selected != expected_dtb(config)) or \
            (not active and selected in {DTB, DEEP_DTB}):
        raise ValueError('CPU-idle profile selected the wrong kernel DTB')
    kernel_config = kernel / '.config'
    if not kernel_config.is_file():
        raise FileNotFoundError(kernel_config)
    for option in ('CONFIG_CPU_IDLE', 'CONFIG_ARM_PSCI_CPUIDLE'):
        if not _kernel_option(kernel_config, option):
            raise ValueError(f'{option} is not built into the selected kernel')

    dts = kernel / 'arch/arm64/boot/dts/allwinner'
    wrapper = (dts / 'sun50i-a133-trimui-brick-native-idle.dts').read_text()
    if wrapper.count('#include "sun50i-a133-trimui-brick.dts"') != 1 or \
            wrapper.count('#include "sun50i-a133-stock-psci-idle.dtsi"') != 1 or \
            MARKER not in wrapper:
        raise ValueError('native idle wrapper lacks its exact base, state or marker')
    deep_wrapper = (dts /
                    'sun50i-a133-trimui-brick-native-deep-idle.dts').read_text()
    if deep_wrapper.count(
            '#include "sun50i-a133-trimui-brick-native-deep.dts"') != 1 or \
            deep_wrapper.count(
                '#include "sun50i-a133-stock-psci-idle.dtsi"') != 1 or \
            MARKER not in deep_wrapper:
        raise ValueError('combined deep-idle wrapper lacks either exact contract')
    idle = (dts / 'sun50i-a133-stock-psci-idle.dtsi').read_text()
    contracts = (
        'arm,psci-suspend-param = <0x00010000>;',
        'entry-latency-us = <46>;', 'exit-latency-us = <59>;',
        'min-residency-us = <3570>;',
        'arm,psci-suspend-param = <0x01010000>;',
        'entry-latency-us = <47>;', 'exit-latency-us = <74>;',
        'min-residency-us = <5000>;',
    )
    if any(idle.count(contract) != 1 for contract in contracts):
        raise ValueError('native idle states do not match the exact stock contract')
    for cpu in range(4):
        if idle.count(f'&cpu{cpu} {{') != 1:
            raise ValueError('native idle states do not cover all four CPUs')
    makefile = (dts / 'Makefile').read_text()
    for name in (DTB, DEEP_DTB):
        if makefile.count(f'{name}.dtb') != 1:
            raise ValueError(f'{name} is not built exactly once')


def _get(fdtget, dtb, node, prop, kind='s'):
    return subprocess.check_output(
        [str(fdtget), '-t', kind, str(dtb), node, prop], text=True).strip()


def _properties(fdtget, dtb, node):
    return set(subprocess.check_output(
        [str(fdtget), '-p', str(dtb), node], text=True).split())


def _has_node(fdtget, dtb, node):
    return subprocess.run([str(fdtget), '-p', str(dtb), node],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def validate(fdtget, dtb):
    if MARKER not in _properties(fdtget, dtb, '/chosen'):
        raise ValueError('native CPU-idle payload lacks its qualification marker')
    if _get(fdtget, dtb, '/cpus/idle-states', 'entry-method') != 'psci':
        raise ValueError('native CPU-idle payload uses the wrong entry method')
    states = (
        ('/cpus/idle-states/cpu-sleep-0', '10000', 46, 59, 3570),
        ('/cpus/idle-states/cluster-sleep-0', '1010000', 47, 74, 5000),
    )
    phandles = []
    for node, parameter, entry, exit_, residency in states:
        props = _properties(fdtget, dtb, node)
        required = {'compatible', 'arm,psci-suspend-param', 'entry-latency-us',
                    'exit-latency-us', 'min-residency-us',
                    'local-timer-stop', 'phandle'}
        if not required <= props or _get(fdtget, dtb, node, 'compatible') != \
                'arm,idle-state':
            raise ValueError(f'{node} lacks its exact idle-state contract')
        if _get(fdtget, dtb, node, 'arm,psci-suspend-param', 'x') != parameter:
            raise ValueError(f'{node} has the wrong PSCI parameter')
        actual = tuple(int(_get(fdtget, dtb, node, prop, 'u')) for prop in
                       ('entry-latency-us', 'exit-latency-us',
                        'min-residency-us'))
        if actual != (entry, exit_, residency):
            raise ValueError(f'{node} has non-stock idle timing')
        phandles.append(_get(fdtget, dtb, node, 'phandle', 'x'))
    for cpu in range(4):
        if _get(fdtget, dtb, f'/cpus/cpu@{cpu}', 'cpu-idle-states', 'x').split() != \
                phandles:
            raise ValueError(f'CPU {cpu} does not reference both stock idle states')


def validate_disabled(fdtget, dtb):
    if _has_node(fdtget, dtb, '/chosen') and \
            MARKER in _properties(fdtget, dtb, '/chosen'):
        raise ValueError('ordinary payload retains the CPU-idle marker')
    if _has_node(fdtget, dtb, '/cpus/idle-states'):
        raise ValueError('ordinary payload advertises PSCI idle states')
    for cpu in range(4):
        if 'cpu-idle-states' in _properties(
                fdtget, dtb, f'/cpus/cpu@{cpu}'):
            raise ValueError(f'ordinary payload gives CPU {cpu} idle states')


def install(images, config, fdtget):
    active = enabled(config)
    selected = selected_dtb(config)
    dtb = images / f'{selected}.dtb'
    if not dtb.is_file():
        raise FileNotFoundError(dtb)
    if not active:
        if selected in {DTB, DEEP_DTB}:
            raise ValueError('disabled CPU-idle profile selected an idle DTB')
        validate_disabled(fdtget, dtb)
        return
    if selected_dtb(config) != expected_dtb(config) or \
            usb_profile(config) != 'peripheral':
        raise ValueError('CPU-idle qualification requires its peripheral DTB')
    if scp_profile(config) not in ('disabled', 'deep'):
        raise ValueError('CPU-idle qualification requires SCP disabled or deep')
    if emmc_recovery_enabled(config):
        raise ValueError('CPU-idle and eMMC qualification must remain separate')
    if any(_selected(config, key) for key in PEER_KEYS):
        raise ValueError('CPU-idle and hardware qualification must remain separate')
    validate(fdtget, dtb)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    guard = commands.add_parser('check-output')
    guard.add_argument('config', type=Path)
    guard.add_argument('requested', choices=('disabled', 'qualification'))
    setup = commands.add_parser('configure')
    setup.add_argument('config', type=Path)
    setup.add_argument('requested', choices=('disabled', 'qualification'))
    kernel = commands.add_parser('verify-kernel')
    kernel.add_argument('config', type=Path)
    kernel.add_argument('kernel', type=Path)
    build = commands.add_parser('install')
    build.add_argument('images', type=Path)
    build.add_argument('config', type=Path)
    build.add_argument('fdtget', type=Path)
    args = parser.parse_args()
    if args.command == 'check-output':
        check_output(args.config, args.requested)
    elif args.command == 'configure':
        configure(args.config, args.requested)
    elif args.command == 'verify-kernel':
        verify_kernel(args.config, args.kernel)
    else:
        install(args.images, args.config, args.fdtget)
