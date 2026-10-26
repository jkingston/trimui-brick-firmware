# TrimUI Brick firmware

Minimal SD-boot Linux firmware for the original TrimUI Brick, built with
Buildroot. The kernel is maintained in the separate
[TrimUI Brick kernel repository](https://github.com/jkingston/trimui-brick-kernel).

## Build

```sh
make configure
make
```

Images and checksums are written to `output/build/images/`. Source revisions
are pinned in `configs/brick_defconfig`. GPU and radio binaries are extracted
from a checksum-verified stock archive and are not stored in Git.

For tandem kernel development, place the kernel checkout at `../linux` and run:

```sh
make dev
make kernel-rebuild
```

Set `KERNEL_DIR=/path/to/linux` to use another checkout. Remove
`output/build/local.mk` and use a fresh output directory when verifying a pinned
release build.

## Update an SD card

```sh
python3 tools/deploy.py output/build/images
```

The updater verifies payload hashes, retains previous boot payloads, and does
not replace the bootloader. Flashing the complete SD image erases the card.

Import the preserved stock Bluetooth address once after the new image boots:

```sh
python3 tools/provision_bluetooth_identity.py /path/to/xr_bt.conf
```

The default image uses the bottom port as a USB peripheral and leaves internal
eMMC, video/G2D/crypto accelerator nodes, and the experimental deep-sleep
profile disabled until they pass device qualification. Run `make check` for the
host test suite.

Build scripts and tools are GPL-2.0-only. Third-party components retain their
own licenses.
