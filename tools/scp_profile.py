#!/usr/bin/env python3
"""Select and validate the Brick SCP firmware profile at build time."""
import argparse
from pathlib import Path
import re


PACKAGE_KEY = 'BR2_PACKAGE_BRICK_SCP'
SYSTEM_SUSPEND_KEY = 'BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND'
FRAGMENT_KEY = 'BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES'
WAKE_FRAGMENT = '$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/native-wake.fragment'
DEEP_FRAGMENT = '$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/native-deep.fragment'
NON_SCP_FRAGMENTS = {
    '$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/vp9.fragment',
    '$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/ce-trng.fragment',
    '$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/combined-accelerators.fragment',
}
LINES = {
    'disabled': (f'# {PACKAGE_KEY} is not set',
                 f'# {SYSTEM_SUSPEND_KEY} is not set', f'{FRAGMENT_KEY}=""'),
    'startup': (f'{PACKAGE_KEY}=y', f'# {SYSTEM_SUSPEND_KEY} is not set',
                f'{FRAGMENT_KEY}="{WAKE_FRAGMENT}"'),
    'deep': (f'{PACKAGE_KEY}=y', f'{SYSTEM_SUSPEND_KEY}=y',
             f'{FRAGMENT_KEY}="{DEEP_FRAGMENT}"'),
}


def _one(config, pattern, description):
    matches = re.findall(pattern, config.read_text(), re.M)
    if len(matches) != 1:
        raise ValueError(f'Expected exactly one {description}')
    return matches[0]


def profile(config):
    package = _one(
        config,
        rf'^(?:{re.escape(PACKAGE_KEY)}=y|# {re.escape(PACKAGE_KEY)} is not set)$',
        'Brick SCP package selection')
    system_suspend = _one(
        config,
        rf'^(?:{re.escape(SYSTEM_SUSPEND_KEY)}=y|# {re.escape(SYSTEM_SUSPEND_KEY)} is not set)$',
        'Brick SCP system-suspend selection')
    fragment = _one(
        config, rf'^{re.escape(FRAGMENT_KEY)}="[^"]*"$',
        'kernel fragment selection')
    for name, lines in LINES.items():
        if (package, system_suspend, fragment) == lines:
            return name
    if (package, system_suspend) == LINES['disabled'][:2] and \
            fragment.removeprefix(f'{FRAGMENT_KEY}="').removesuffix('"') in NON_SCP_FRAGMENTS:
        return 'disabled'
    raise ValueError('SCP package and native wake kernel fragment do not form a supported profile')


def check_output(config, expected):
    if config.exists() and profile(config) != expected:
        raise ValueError('Use a fresh output directory when changing SCP profile')


def configure(config, selected):
    text = config.read_text()
    replacements = (
        (rf'^(?:{re.escape(PACKAGE_KEY)}=y|# {re.escape(PACKAGE_KEY)} is not set)$',
         LINES[selected][0], 'SCP package'),
        (rf'^(?:{re.escape(SYSTEM_SUSPEND_KEY)}=y|# {re.escape(SYSTEM_SUSPEND_KEY)} is not set)$',
         LINES[selected][1], 'SCP system suspend'),
        (rf'^{re.escape(FRAGMENT_KEY)}="[^"]*"$',
         LINES[selected][2], 'kernel fragment'),
    )
    for pattern, replacement, description in replacements:
        text, count = re.subn(pattern, replacement, text, flags=re.M)
        if count != 1:
            raise ValueError(f'Missing or duplicate {description} configuration')
    config.write_text(text)


def verify_kernel(config, kernel):
    selected = profile(config)
    kernel_config = kernel / '.config'
    if not kernel_config.is_file():
        raise FileNotFoundError(kernel_config)
    wake_enabled = _one(
        kernel_config,
        r'^(?:CONFIG_SUN50I_A133_WAKEUPGEN=y|# CONFIG_SUN50I_A133_WAKEUPGEN is not set)$',
        'native wakeupgen kernel configuration') == 'CONFIG_SUN50I_A133_WAKEUPGEN=y'
    wake_expected = selected != 'disabled'
    if wake_enabled != wake_expected:
        raise ValueError('Kernel wakeupgen configuration does not match SCP profile')
    wakeupgen = kernel / 'drivers/irqchip/irq-sun50i-a133-wakeupgen.o'
    if wakeupgen.is_file() != wake_expected:
        raise ValueError('Kernel wakeupgen object does not match SCP profile')
    ccu_enabled = _one(
        kernel_config,
        r'^(?:CONFIG_SUN50I_A133_CCU_SLEEP=y|# CONFIG_SUN50I_A133_CCU_SLEEP is not set)$',
        'A133 deep CCU context configuration') == 'CONFIG_SUN50I_A133_CCU_SLEEP=y'
    ccu_expected = selected == 'deep'
    if ccu_enabled != ccu_expected:
        raise ValueError('Kernel CCU deep-context configuration does not match SCP profile')
    ccu_context = kernel / 'drivers/clk/sunxi-ng/ccu-sun50i-a133-sleep.o'
    if ccu_context.is_file() != ccu_expected:
        raise ValueError('Kernel CCU deep-context object does not match SCP profile')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    guard = commands.add_parser('check-output')
    guard.add_argument('config', type=Path)
    guard.add_argument('profile', choices=LINES)
    setup = commands.add_parser('configure')
    setup.add_argument('config', type=Path)
    setup.add_argument('profile', choices=LINES)
    verify = commands.add_parser('verify-kernel')
    verify.add_argument('config', type=Path)
    verify.add_argument('kernel', type=Path)
    args = parser.parse_args()
    if args.command == 'check-output':
        check_output(args.config, args.profile)
    elif args.command == 'configure':
        configure(args.config, args.profile)
    else:
        verify_kernel(args.config, args.kernel)
