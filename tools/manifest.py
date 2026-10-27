#!/usr/bin/env python3
"""Record the source revision and hashes of a firmware image."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from usb_profile import profile
from scp_profile import profile as scp_profile
from emmc_recovery import enabled as emmc_recovery_enabled
from g2d_profile import enabled as g2d_qualification_enabled
from ve_profile import enabled as ve_qualification_enabled
from vp9_profile import enabled as vp9_qualification_enabled
from ce_trng_profile import enabled as ce_trng_qualification_enabled

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--rootfs', type=Path)
p.add_argument('--payload', action='store_true')
p.add_argument('images', type=Path)
p.add_argument('config', type=Path)
p.add_argument('kernel_revision')
p.add_argument('kernel_override', nargs='?', default='')
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()
def source(path):
    try:
        revision = git(path, 'rev-parse', 'HEAD')
        changes = git(path, 'status', '--porcelain')
        return {'commit': revision, 'dirty': bool(changes)}
    except subprocess.CalledProcessError:
        return {'commit': None, 'dirty': None}

kernel = source(a.kernel_override) if a.kernel_override else {'commit': a.kernel_revision, 'dirty': False}
kernel['release'] = (a.images / 'kernel.release').read_text().strip()
manifest = {'firmware': source(root), 'kernel': kernel, 'buildroot': '2026.08.1',
            'usb_role': profile(a.config),
            'emmc_recovery': emmc_recovery_enabled(a.config),
            'g2d_qualification': g2d_qualification_enabled(a.config),
            've_qualification': ve_qualification_enabled(a.config),
            'vp9_qualification': vp9_qualification_enabled(a.config),
            'ce_trng_qualification': ce_trng_qualification_enabled(a.config),
            'config_sha256': hashlib.sha256(a.config.read_bytes()).hexdigest(), 'files': {}}
selected_scp_profile = scp_profile(a.config)
if selected_scp_profile != 'disabled':
    manifest['scp_profile'] = selected_scp_profile
names = ['Image', 'sun50i-a133-trimui-brick.dtb']
if not a.rootfs:
    names += ['rootfs.cpio.gz', 'u-boot-sunxi-with-spl.bin']
    if not a.payload:
        names.append('sdcard.img')
for name in names:
    path = a.images / name
    with path.open('rb') as f:
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    manifest['files'][name] = {'sha256': digest, 'bytes': path.stat().st_size}
destination = a.rootfs / 'etc/brick-build.json' if a.rootfs else a.images / ('payload-manifest.json' if a.payload else 'manifest.json')
destination.write_text(json.dumps(manifest, indent=2) + '\n')
