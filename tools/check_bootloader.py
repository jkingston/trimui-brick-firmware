#!/usr/bin/env python3
"""Verify the SD-only SPL layout, embedded firmware and storage configuration."""
import argparse
import hashlib
from pathlib import Path
import re
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

def c_byte_array(source, name):
    match = re.search(
        rf'static const u8 {re.escape(name)}\[SHA256_SUM_LEN\] = \{{(.*?)\}};',
        source, re.S)
    require(match is not None, f'Missing {name}')
    values = bytes(int(value, 16) for value in
                   re.findall(r'0x([0-9a-fA-F]+)', match.group(1)))
    require(len(values) == hashlib.sha256().digest_size,
            f'Unexpected {name} length')
    return values

def symbol_section(symbols, name):
    match = re.search(
        rf'^\S+\s+\S+\s+O\s+(\.\S+)\s+\S+\s+{re.escape(name)}$',
        symbols, re.M)
    require(match is not None, f'Missing SPL symbol {name}')
    return match.group(1)

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
for name in ('SPL_BOOTCOUNT_LIMIT', 'TPL_BOOTCOUNT_LIMIT'):
    require(config.get('CONFIG_' + name) != 'y',
            f'{name} can mutate rollback state before FEL isolation')
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
boot_pulse = 'gpio set PH3; sleep 0.12; gpio clear PH3;'
require(boot_pulse in config.get('CONFIG_BOOTCOMMAND', ''),
        'Primary boot does not acknowledge power-on')
altbootcmd = config.get('CONFIG_BOOTCOUNT_ALTBOOTCMD', '')
require('for brick_bootdir in brick-previous brick-baseline;' in altbootcmd,
        'Missing retained-payload rollback')
require('brick-diag' not in altbootcmd, 'Rollback must not retry the pending payload')
require(boot_pulse in altbootcmd,
        'Rollback boot does not acknowledge power-on')
sunxi_boot = (a.uboot / 'arch/arm/mach-sunxi/board.c').read_text()
function = sunxi_boot.find('uint32_t sunxi_get_boot_device(void)')
dispatch = sunxi_boot.find('switch (boot_source)', function)
require(function >= 0 and dispatch > function and re.search(
        r'if \(IS_ENABLED\(CONFIG_TRIMUI_BRICK_SD_DIAGNOSTIC\) &&\s*'
        r'boot_source != SUNXI_INVALID_BOOT_SOURCE &&\s*'
        r'boot_source != SUNXI_BOOTED_FROM_MMC0 &&\s*'
        r'boot_source != SUNXI_BOOTED_FROM_MMC0_HIGH\)\s*'
        r'panic\("Brick diagnostic requires removable SD boot\\n"\);',
        sunxi_boot[function:dispatch]),
        'FEL is not narrowly exempted from the SD-only entry guard')
board = (a.uboot / 'board/sunxi/board.c').read_text()
misc_init = board.find('int misc_init_r(void)')
clear_fel = board.find('env_set("fel_booted", NULL);')
read_boot = board.find('boot = sunxi_get_boot_device();', clear_fel)
set_fel = board.find('if (boot == BOOT_DEVICE_BOARD) {\n'
                     '\t\tenv_set("fel_booted", "1");', read_boot)
require(board.count('env_set("fel_booted",') == 2 and
        0 <= misc_init < clear_fel < read_boot < set_fel,
        'fel_booted is not produced solely from the live FEL boot device')
autoboot = (a.uboot / 'common/autoboot.c').read_text()
require('IS_ENABLED(CONFIG_TRIMUI_BRICK_SD_DIAGNOSTIC) &&\n'
        '\t\t\t     env_get("fel_booted")' in autoboot,
        'FEL recovery is not identified before bootcount handling')
require('if (!brick_fel_recovery)\n\t\tbootcount_inc();' in autoboot,
        'FEL recovery can mutate persistent bootcount state')
require('if (!brick_fel_recovery && bootcount_error())' in autoboot,
        'FEL recovery can select the rollback payload')
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
        scp_source = (a.uboot / 'arch/arm/mach-sunxi/a133_vendor_scp.c').read_text()
        spl_symbols = (a.uboot / 'spl/u-boot-spl.sym').read_text()
        require(symbol_section(spl_symbols, 'a133_scp_dram_handoff') == '.data',
                'SCP DRAM handoff is not preserved across SPL BSS initialization')
        require(symbol_section(spl_symbols, 'a133_scp_loaded') == '.data',
                'SCP FIT readiness is not preserved across SPL BSS initialization')
        require('#define A133_SCP_VECTOR_FIRST\t\t0x00000100UL' in scp_source and
                '#define A133_SCP_VECTOR_LAST\t\t0x00000e00UL' in scp_source and
                '#define A133_SCP_SRAM_OFFSET\t\t0x00004000UL' in scp_source and
                '#define A133_SCP_SRAM_SIZE\t\t0x00010000UL' in scp_source,
                'Unexpected A133 sparse vector or SRAM layout')
        require('ret = a133_scp_copy_runtime(scp_dst, scp_src);' in scp_source,
                'SCP runtime image is not copied through its sparse aperture')
        start = scp_source.find('static int a133_scp_start(void)')
        enable = scp_source.find('ret = a133_scp_enable_msgbox();', start)
        release = scp_source.find('ret = a133_scp_set_reset(false);', start)
        require('#define A133_MSGBOX_BGR_REG\t\t0x0300171cUL' in scp_source,
                'Unexpected A133 message-box clock register')
        require('#define A133_MSGBOX_BGR_REQUIRED\t(BIT(16) | BIT(0))' in
                scp_source, 'Unexpected A133 message-box clock/reset bits')
        require('writel(readl(reg) | A133_MSGBOX_BGR_REQUIRED, reg);' in
                scp_source, 'SCP startup does not preserve message-box BGR bits')
        require('(readl(reg) & A133_MSGBOX_BGR_REQUIRED) !=\n'
                '\t    A133_MSGBOX_BGR_REQUIRED' in scp_source,
                'SCP startup does not verify the message-box BGR')
        require(0 <= start < enable < release,
                'Message-box clock/reset is not enabled before SCP release')
        require(c_byte_array(scp_source, 'a133_scp_sha256') ==
                hashlib.sha256(a.scp.read_bytes()).digest(),
                'Compiled SCP digest mismatch')
        require(c_byte_array(scp_source, 'a133_scp_fdt_sha256') ==
                hashlib.sha256(a.scp_fdt.read_bytes()).digest(),
                'Compiled SCP service DT digest mismatch')
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
