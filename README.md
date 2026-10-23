# TrimUI Brick firmware

Minimal SD-boot Linux system built with Buildroot. The kernel lives in a
separate Linux fork; this repository owns bootloader patches, GPU userspace,
rootfs configuration and image assembly.

Place the kernel checkout at `../linux`, then:

```sh
make configure
make
```

The image and checksums are in `output/build/images/`. Source revisions are
pinned in `configs/brick_defconfig`. GPU binaries are extracted from the
checksum-verified stock archive and are not stored in Git.

For local kernel changes:

```sh
make dev
make kernel-rebuild
```

To update an existing development SD card and wait for the reboot:

```sh
python3 tools/deploy.py output/build/images
```

The updater verifies payload hashes and retains the previous boot payloads.
It updates kernel, device tree and initramfs; it does not replace the bootloader.

`make dev` enables Buildroot's local source override. Remove
`output/build/local.mk` and use a fresh output directory to verify pinned
release builds. No changes need to be copied between repositories.

The development image exposes a root shell over USB serial. Internal eMMC
is disabled. GPU suspend, power-off and DVFS are unsupported.

`Containerfile` supplies the host build dependencies. Host tools use Python's
standard library; run their tests with `python3 -m unittest discover -s tests`.
