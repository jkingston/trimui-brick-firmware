#!/usr/bin/env python3
"""Extract and verify the radio firmware from the pinned stock archive."""
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
        raise RuntimeError(f"radio firmware checksum mismatch: {target}")
image.unlink()
