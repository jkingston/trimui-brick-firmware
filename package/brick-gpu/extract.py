#!/usr/bin/env python3
"""Extract and verify the GPU runtime from the pinned stock archive."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from extract_stock import extract_rootfs

archive, output, debugfs = sys.argv[1:]
output = Path(output)
output.mkdir(parents=True, exist_ok=True)
image = output / "rootfs.ext4"
extract_rootfs(archive, image)
files = json.loads(Path(__file__).with_name("files.json").read_text())
for target, record in files.items():
    path = output / "runtime" / target
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([debugfs, "-R", f"dump {record['source']} {path}", str(image)], check=True)
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise RuntimeError(f"GPU runtime checksum mismatch: {target}")
lib = output / "runtime/usr/lib"
(lib / "libEGL.so").symlink_to("libEGL.so.1")
(lib / "libGLESv2.so.2").symlink_to("libGLESv2.so")
image.unlink()
