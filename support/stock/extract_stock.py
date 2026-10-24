#!/usr/bin/env python3
"""Extract only rootfs.fex from the pinned IMAGEWTY v0x300 stock ZIP.

Format reference: ChrisHughes/allwinner-image-tool, allwinner_imagewty.py.
No mounting, device nodes, firmware flashing, or stock code execution.
"""
if not __debug__:
    raise RuntimeError("Run without Python optimization; validation checks are required")

import hashlib
import pathlib
import struct
import zipfile

ROOTFS_SHA256 = '023de6fe3d9ef2a268155bcc61c4d2f4e8f17d35da4d17fa815157e69aae12f8'


def extract_rootfs(archive, destination):
    with zipfile.ZipFile(archive) as source:
        # ZipFile.read validates this entry's CRC as well.
        image = source.read('trimui_tg3040.awimg')
    if image[:8] != b'IMAGEWTY' or struct.unpack_from('<I', image, 8)[0] != 0x300:
        raise ValueError('Unsupported IMAGEWTY format')
    if struct.unpack_from('<I', image, 24)[0] != len(image):
        raise ValueError('Truncated IMAGEWTY image')
    stride = struct.unpack_from('<I', image, 32)[0]
    count = struct.unpack_from('<I', image, 60)[0]
    if stride < 56 or count > len(image) // stride - 1:
        raise ValueError('Invalid IMAGEWTY record table')
    matches = []
    for index in range(count):
        base = (index + 1) * stride
        size = struct.unpack_from('<I', image, base)[0]
        if size > stride - 56:
            raise ValueError('Invalid IMAGEWTY filename size')
        name = image[base + 36:base + 36 + size].split(b'\0', 1)[0]
        stored, _, original, _, offset = struct.unpack_from('<5I', image, base + 36 + size)
        if offset + stored > len(image) or original > stored:
            raise ValueError('Invalid IMAGEWTY record extent')
        if name == b'rootfs.fex':
            matches.append(image[offset:offset + stored])
    if len(matches) != 1:
        raise ValueError('Expected one rootfs.fex')
    data = matches[0]
    if hashlib.sha256(data).hexdigest() != ROOTFS_SHA256:
        raise ValueError('Stock rootfs checksum mismatch')
    pathlib.Path(destination).write_bytes(data)
