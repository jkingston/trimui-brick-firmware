#!/usr/bin/env python3
"""Verify the produced BL31 has exactly the selected A133 suspend profile."""
import argparse
from pathlib import Path
import re
import subprocess

from cpu_idle_profile import (DEEP_DTB, DTB, enabled as idle_enabled,
                              selected_dtb)
from scp_profile import profile as scp_profile


NATIVE_SYMBOLS = {
    'sunxi_a133_cpuidle_power_off_self',
    'sunxi_a133_pwr_domain_suspend',
    'sunxi_a133_pwr_domain_suspend_finish',
    'sunxi_a133_pwr_domain_suspend_pwrdown_early',
    'sunxi_a133_validate_power_state',
}
SYSTEM_SYMBOLS = {
    'sunxi_a133_system_suspend_psci_ops',
    'sunxi_a133_system_suspend_runtime_available',
}


def _symbols(nm, elf):
    if not elf.is_file():
        raise FileNotFoundError(elf)
    output = subprocess.check_output([str(nm), '-a', str(elf)], text=True)
    return {match.group(1) for line in output.splitlines()
            if (match := re.search(r'\b([A-Za-z_][A-Za-z0-9_]*)$', line))}


def _complete(symbols, required, label):
    present = required & symbols
    if present and present != required:
        missing = ', '.join(sorted(required - present))
        raise ValueError(f'Produced BL31 has partial {label}: missing {missing}')
    return present == required


def verify_symbols(config, symbols):
    native = _complete(symbols, NATIVE_SYMBOLS, 'native CPU-suspend support')
    system = _complete(symbols, SYSTEM_SYMBOLS, 'system-suspend support')
    idle = idle_enabled(config)
    dtb = selected_dtb(config)
    idle_dtb = dtb in {DTB, DEEP_DTB}
    deep = scp_profile(config) == 'deep'

    if native != idle or idle_dtb != idle or native != idle_dtb:
        raise ValueError('Produced BL31 and selected DTB violate the native '
                         'CPU-suspend all-or-none contract')
    if system != deep:
        raise ValueError('Produced BL31 does not match the selected system '
                         'suspend profile')
    if idle and deep and dtb != DEEP_DTB:
        raise ValueError('Combined CPU and system suspend require the combined DTB')
    if idle and not deep and dtb != DTB:
        raise ValueError('Isolated CPU suspend requires the isolated idle DTB')


def verify(config, elf, nm):
    verify_symbols(config, _symbols(nm, elf))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('elf', type=Path)
    parser.add_argument('nm', type=Path)
    args = parser.parse_args()
    verify(args.config, args.elf, args.nm)
