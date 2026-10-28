#!/usr/bin/env python3
"""Select and validate the board's USB role profile at build time."""
import argparse
from pathlib import Path
import re
import subprocess

from scp_profile import profile as scp_profile

DTBS = {'peripheral': 'sun50i-a133-trimui-brick',
        'host': 'sun50i-a133-trimui-brick-host',
        'auto': 'sun50i-a133-trimui-brick-auto-usb'}
NATIVE_WAKE_DTB = 'sun50i-a133-trimui-brick-native-wake'
NATIVE_DEEP_DTB = 'sun50i-a133-trimui-brick-native-deep'
NATIVE_IDLE_DTB = 'sun50i-a133-trimui-brick-native-idle'
NATIVE_DEEP_IDLE_DTB = 'sun50i-a133-trimui-brick-native-deep-idle'
WAKE_DTBS = {'startup': NATIVE_WAKE_DTB, 'deep': NATIVE_DEEP_DTB}
KEY = 'BR2_LINUX_KERNEL_INTREE_DTS_NAME'


def expected_dtb(role, wake_profile):
    if wake_profile in WAKE_DTBS:
        if role != 'peripheral':
            raise ValueError('SCP firmware requires the USB peripheral profile')
        return WAKE_DTBS[wake_profile]
    return DTBS[role]


def selected_dtb(config):
    matches = re.findall(r'^' + KEY + r'="allwinner/([^"]+)"$',
                         config.read_text(), re.M)
    allowed = set(DTBS.values()) | set(WAKE_DTBS.values()) | {
        NATIVE_IDLE_DTB, NATIVE_DEEP_IDLE_DTB}
    if len(matches) != 1 or matches[0] not in allowed:
        raise ValueError('Expected exactly one supported Brick kernel DTB')
    return matches[0]


def profile(config):
    selected = selected_dtb(config)
    for role, name in DTBS.items():
        if selected == name or (role == 'peripheral' and
                                selected in set(WAKE_DTBS.values()) |
                                {NATIVE_IDLE_DTB, NATIVE_DEEP_IDLE_DTB}):
            return role
    raise ValueError('Expected exactly one supported Brick USB profile DTB')


def check_output(config, role, wake_profile='disabled'):
    if not config.exists():
        return
    selected = selected_dtb(config)
    accepted = {expected_dtb(role, wake_profile)}
    if role == 'peripheral' and wake_profile == 'disabled':
        accepted.add(NATIVE_IDLE_DTB)
    if role == 'peripheral' and wake_profile == 'deep':
        accepted.add(NATIVE_DEEP_IDLE_DTB)
    if selected not in accepted:
        raise ValueError('Use a fresh output directory when changing USB or SCP profile')


def configure(config, role, wake_profile='disabled'):
    text, count = re.subn(r'^' + KEY + r'="[^"]*"$',
                         KEY + '="allwinner/' + expected_dtb(role, wake_profile) + '"',
                         config.read_text(), flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate kernel DTB configuration')
    config.write_text(text)


def install(images, config, fdtget):
    role = profile(config)
    wake_profile = scp_profile(config)
    selected = selected_dtb(config)
    accepted = {expected_dtb(role, wake_profile)}
    if role == 'peripheral' and wake_profile == 'disabled':
        accepted.add(NATIVE_IDLE_DTB)
    if role == 'peripheral' and wake_profile == 'deep':
        accepted.add(NATIVE_DEEP_IDLE_DTB)
    if selected not in accepted:
        raise ValueError('Kernel DTB does not match the combined USB and SCP profile')
    source = images / (selected + '.dtb')
    if not source.is_file():
        raise FileNotFoundError(f'Selected kernel does not provide {source.name}')

    def get(node, prop, kind='s', default=None):
        path = node if node.startswith('/') else '/soc/' + node
        args = [str(fdtget), '-t', kind]
        if default is not None:
            args += ['-d', default]
        return subprocess.check_output(args + [str(source), path, prop], text=True).strip()

    def properties(node):
        path = node if node.startswith('/') else '/soc/' + node
        return set(subprocess.check_output(
            [str(fdtget), '-p', str(source), path], text=True).split())

    if wake_profile in WAKE_DTBS:
        wakeupgen = '/interrupt-controller'
        gic = '/soc/interrupt-controller@3021000'
        r_intc = '/soc/interrupt-controller@7010320'
        direct = (gic, '/timer')
        gic_phandle = get(gic, 'phandle', 'x')
        wake_phandle = get(wakeupgen, 'phandle', 'x')
        if (get('/', 'interrupt-parent', 'x') != wake_phandle or
                get(wakeupgen, 'compatible') !=
                'allwinner,sun50i-a133-native-wakeupgen' or
                'interrupt-controller' not in properties(wakeupgen) or
                get(wakeupgen, '#interrupt-cells', 'u') != '3' or
                get(wakeupgen, 'interrupt-parent', 'x') != gic_phandle):
            raise ValueError('Native wake profile has an invalid root interrupt hierarchy')
        if any(get(node, 'interrupt-parent', 'x') != gic_phandle
               for node in direct):
            raise ValueError('Native wake profile does not preserve direct-GIC exceptions')
        expected_r_parent = wake_phandle if wake_profile == 'deep' else gic_phandle
        if get(r_intc, 'interrupt-parent', 'x') != expected_r_parent:
            raise ValueError('Native wake profile has an invalid NMI wake parent')
        if get(gic, 'interrupts', 'x').split()[0] != '1':
            raise ValueError('GIC maintenance interrupt is not a PPI')
        timer = get('/timer', 'interrupts', 'x').split()
        if len(timer) != 12 or timer[0::3] != ['1'] * 4:
            raise ValueError('Architectural timer interrupts are not direct GIC PPIs')
        nmi_irq = get(r_intc, 'interrupts', 'x').split()
        if nmi_irq[:2] != ['0', '67']:
            raise ValueError('NMI cascade input is not a GIC SPI')
        context_nodes = ('/soc/clock@3001000', '/soc/clock@7010000',
                         '/soc/pinctrl@300b000', '/soc/pinctrl@7022000')
        context_enabled = [
            'allwinner,stock-suspend' in properties(node)
            for node in context_nodes
        ]
        if any(context_enabled) != (wake_profile == 'deep') or \
                len(set(context_enabled)) != 1:
            raise ValueError('Native wake profile has an incomplete deep-context contract')

    for node, enabled in [('usb@5100000', role == 'peripheral'),
                          ('usb@5101000', role == 'host'),
                          ('usb@5101400', role == 'host')]:
        if get(node, 'status') != ('okay' if enabled else 'disabled'):
            raise ValueError(f'{role} profile has inconsistent {node} status')
        if enabled and get(node, 'dr_mode') != role:
            raise ValueError(f'{role} profile has inconsistent {node} dr_mode')
    if role == 'auto':
        owner = '/usb-role-switch'
        phy = '/soc/phy@5100400'
        pio = '/soc/pinctrl@300b000'
        if (get(owner, 'status', default='okay') != 'okay' or
                get(owner, 'compatible') != 'trimui,brick-usb-role-switch'):
            raise ValueError('auto profile lacks its role coordinator')
        owner_properties = properties(owner)
        if 'id-gpios' not in owner_properties:
            raise ValueError('auto profile does not own the stock PH8 ID input')
        if not {'phys', 'phy-names', 'vbus-power-supply'}.issubset(owner_properties):
            raise ValueError('auto profile lacks required PHY or VBUS ownership')
        if (get(owner, 'phys', 'x').split() !=
                [get(phy, 'phandle', 'x'), '0'] or
                get(owner, 'phy-names') != 'usb'):
            raise ValueError('auto profile has inconsistent PHY ownership')
        if (get(owner, 'id-gpios', 'x').split() !=
                [get(pio, 'phandle', 'x'), '7', '8', '0']):
            raise ValueError('auto profile does not own the stock PH8 ID input')
        if not get(owner, 'vbus-power-supply', 'x'):
            raise ValueError('auto profile lacks VBUS supply observation')
        phy_properties = properties(phy)
        if ('usb-role-switch' not in phy_properties or
                'usb0_id_det-gpios' in phy_properties):
            raise ValueError('auto profile has conflicting PHY role ownership')
        for prop, node, mode in [('peripheral', 'usb@5100000', 'peripheral'),
                                 ('ehci', 'usb@5101000', 'host'),
                                 ('ohci', 'usb@5101400', 'host')]:
            if (get(owner, prop, 'x') != get(node, 'phandle', 'x') or
                    get(node, 'dr_mode') != mode):
                raise ValueError('auto profile has inconsistent controller ownership')
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
    guard.add_argument('wake_profile', choices=('disabled', 'startup', 'deep'), nargs='?', default='disabled')
    setup = commands.add_parser('configure')
    setup.add_argument('config', type=Path)
    setup.add_argument('role', choices=DTBS)
    setup.add_argument('wake_profile', choices=('disabled', 'startup', 'deep'), nargs='?', default='disabled')
    build = commands.add_parser('install')
    build.add_argument('images', type=Path)
    build.add_argument('config', type=Path)
    build.add_argument('fdtget', type=Path)
    args = parser.parse_args()
    if args.command == 'check-output':
        check_output(args.config, args.role, args.wake_profile)
    elif args.command == 'configure':
        configure(args.config, args.role, args.wake_profile)
    else:
        install(args.images, args.config, args.fdtget)
