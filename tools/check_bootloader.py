#!/usr/bin/env python3
"""Verify the SD-only SPL layout, embedded firmware and storage configuration."""
import argparse
from pathlib import Path
import struct
import subprocess
import tempfile

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('uboot', type=Path)
p.add_argument('bl31', type=Path)
p.add_argument('fdtget', type=Path)
p.add_argument('--scp', type=Path)
p.add_argument('--scp-fdt', type=Path)
a = p.parse_args()

def require(condition, message):
    if not condition:
        raise ValueError(message)

config = dict(line.split('=', 1) for line in (a.uboot / '.config').read_text().splitlines()
              if line.startswith('CONFIG_') and '=' in line)
scp_enabled = config.get('CONFIG_SUNXI_A133_VENDOR_SCP') == 'y'
require((a.scp is not None) == scp_enabled and
        (a.scp_fdt is not None) == scp_enabled,
        'SCP artifacts do not match the selected startup profile')
for name in ('ENV_IS_IN_FAT', 'ENV_IS_IN_MMC', 'SPL_MMC_WRITE', 'CMD_SAVEENV',
             'BOOTSTD', 'DISTRO_DEFAULTS'):
    require(config.get('CONFIG_' + name) != 'y', f'Unexpected storage writer: {name}')
for name, value in {'ENV_IS_NOWHERE': 'y', 'MMC_SUNXI_SLOT_EXTRA': '-1',
                    'TRIMUI_BRICK_SD_DIAGNOSTIC': 'y', 'SUPPORT_RAW_INITRD': 'y',
                    'DRAM_SUNXI_TPR11': '0x1b191012', 'DRAM_SUNXI_TPR12': '0x0e111010'}.items():
    require(config.get('CONFIG_' + name) == value, f'Unexpected {name}')
for name in ('MMC_WRITE', 'FAT_WRITE', 'BOOTCOUNT_LIMIT', 'BOOTCOUNT_FS',
             'WATCHDOG_AUTOSTART'):
    require(config.get('CONFIG_' + name) == 'y', f'Missing rollback support: {name}')
for name, value in {'BOOTCOUNT_BOOTLIMIT': '1',
                    'SYS_BOOTCOUNT_FS_INTERFACE': '"mmc"',
                    'SYS_BOOTCOUNT_FS_DEVPART': '"0:1"',
                    'SYS_BOOTCOUNT_FS_NAME': '"/brick-bootcount"',
                    'SYS_BOOTCOUNT_ADDR': '0x45000000'}.items():
    require(config.get('CONFIG_' + name) == value, f'Unexpected {name}')
require('for brick_bootdir in brick-diag brick-previous brick-baseline;' in
        config.get('CONFIG_BOOTCOMMAND', ''), 'Missing SD payload fallback')
require('brick.bootdir=${brick_bootdir}' in config.get('CONFIG_BOOTCOMMAND', ''),
        'Booted payload is not identified to userspace')
altbootcmd = config.get('CONFIG_BOOTCOUNT_ALTBOOTCMD', '')
require('for brick_bootdir in brick-previous brick-baseline;' in altbootcmd,
        'Missing retained-payload rollback')
require('brick-diag' not in altbootcmd, 'Rollback must not retry the pending payload')
image = (a.uboot / 'u-boot-sunxi-with-spl.bin').read_bytes()
require(image[4:12] == b'eGON.BT0', 'Invalid SPL magic')
checksum, size = struct.unpack_from('<II', image, 12)
require(0 < size <= len(image) and size % 4 == 0, 'Invalid SPL length')
words = list(struct.unpack('<' + 'I' * (size // 4), image[:size]))
words[3] = 0x5f0a6c39
require(sum(words) & 0xffffffff == checksum, 'SPL checksum mismatch')
offset = max(32768, size)
sector = int(config['CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_SECTOR'], 0)
data_offset = int(config['CONFIG_SYS_MMCSD_RAW_MODE_U_BOOT_DATA_PART_OFFSET'], 0)
read_sector = max(sector, size // 512) + (128 - 8) * 2 + data_offset
require(read_sector * 512 == 128 * 1024 + offset, 'SPL would read the wrong FIT sector')
fit = image[offset:]
require(struct.unpack_from('>II', fit) == (0xd00dfeed, len(fit)), 'Invalid FIT extent')

def prop(path, node, key, kind):
    return subprocess.check_output([str(a.fdtget), '-t', kind, str(path), node, key], text=True).strip()
def binary_prop(path, node, key):
    return bytes(int(x, 16) for x in prop(path, node, key, 'bx').split())

with tempfile.TemporaryDirectory(prefix='brick-boot-') as directory:
    f = Path(directory) / 'firmware.itb'
    f.write_bytes(fit)
    require(binary_prop(f, '/images/atf', 'data') == a.bl31.read_bytes(), 'Embedded TF-A mismatch')
    require(prop(f, '/images/atf', 'load', 'x') == '40000000', 'Unexpected TF-A load address')
    require(prop(f, '/images/uboot', 'load', 'x') == '4a000000', 'Unexpected U-Boot load address')
    if scp_enabled:
        require(binary_prop(f, '/images/scp', 'data') == a.scp.read_bytes(),
                'Embedded SCP mismatch')
        require(binary_prop(f, '/images/scp-service-dtb', 'data') == a.scp_fdt.read_bytes(),
                'Embedded SCP service DT mismatch')
        require(prop(f, '/images/scp', 'load', 'x') == '48fe0000',
                'Unexpected SCP staging address')
        require(prop(f, '/images/scp-service-dtb', 'load', 'x') == '48fb0000',
                'Unexpected SCP service DT staging address')
        loadables = prop(f, '/configurations/config-1', 'loadables', 's').split()
        require(loadables == ['scp', 'scp-service-dtb', 'uboot'],
                'Unexpected SCP FIT load order')
    board = Path(directory) / 'board.dtb'
    board.write_bytes(binary_prop(f, '/images/fdt-1', 'data'))
    require(board.read_bytes() == (a.uboot / 'u-boot.dtb').read_bytes(), 'Embedded board DT mismatch')
    for address, status in [('4020000', 'okay'), ('4021000', 'disabled'), ('4022000', 'disabled')]:
        require(prop(board, '/soc/mmc@' + address, 'status', 's') == status, 'Unexpected MMC state')
    require(prop(board, '/aliases', 'mmc0', 's') == '/soc/mmc@4020000', 'SD alias mismatch')
print('PASS: SPL layout, embedded TF-A/DT and guarded FAT boot rollback')
