#!/usr/bin/env python3
"""Select and validate the Brick SCP startup profile at build time."""
import argparse
from pathlib import Path
import re


KEY = 'BR2_PACKAGE_BRICK_SCP'
LINES = {
    'disabled': f'# {KEY} is not set',
    'startup': f'{KEY}=y',
}


def profile(config):
    matches = re.findall(
        rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$',
        config.read_text(), re.M)
    for name, line in LINES.items():
        if matches == [line]:
            return name
    raise ValueError('Expected exactly one supported Brick SCP profile')


def check_output(config, expected):
    if config.exists() and profile(config) != expected:
        raise ValueError('Use a fresh output directory when changing SCP profile')


def configure(config, selected):
    text, count = re.subn(
        rf'^(?:{re.escape(KEY)}=y|# {re.escape(KEY)} is not set)$',
        LINES[selected], config.read_text(), flags=re.M)
    if count != 1:
        raise ValueError('Missing or duplicate SCP profile configuration')
    config.write_text(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    guard = commands.add_parser('check-output')
    guard.add_argument('config', type=Path)
    guard.add_argument('profile', choices=LINES)
    setup = commands.add_parser('configure')
    setup.add_argument('config', type=Path)
    setup.add_argument('profile', choices=LINES)
    args = parser.parse_args()
    if args.command == 'check-output':
        check_output(args.config, args.profile)
    else:
        configure(args.config, args.profile)
