#!/usr/bin/env python3
"""Verify a complete Brick release directory before publishing it."""
import argparse
import hashlib
import json
from pathlib import Path


REQUIRED = {
    'Image',
    'sun50i-a133-trimui-brick.dtb',
    'rootfs.ext4',
    'u-boot-sunxi-with-spl.bin',
    'sdcard.img',
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def parse_sums(path):
    entries = {}
    for line in path.read_text().splitlines():
        value, name = line.split(None, 1)
        entries[name.lstrip('*')] = value
    return entries


def verify(images):
    manifest_path = images / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest['firmware']['dirty'] is not False:
        raise ValueError('firmware source is not a clean Git revision')
    if manifest['kernel']['dirty'] is not False:
        raise ValueError('kernel source is not a clean Git revision')
    for name, source in manifest.get('externals', {}).items():
        if source['dirty'] is not False:
            raise ValueError(f'{name} external source is not a clean Git revision')

    files = manifest['files']
    if set(files) != REQUIRED:
        raise ValueError('manifest does not describe the complete release payload')
    for name, expected in files.items():
        path = images / name
        if path.stat().st_size != expected['bytes'] or digest(path) != expected['sha256']:
            raise ValueError(f'{name} does not match the manifest')

    sums = parse_sums(images / 'SHA256SUMS')
    if set(sums) != REQUIRED:
        raise ValueError('SHA256SUMS does not describe the complete release payload')
    for name, expected in sums.items():
        if digest(images / name) != expected:
            raise ValueError(f'{name} does not match SHA256SUMS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images', type=Path)
    verify(parser.parse_args().images)
