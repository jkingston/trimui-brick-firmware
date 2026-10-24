# TrimUI Brick firmware

Minimal SD-boot Linux system built with Buildroot. The kernel lives in a
separate Linux fork; this repository owns bootloader patches, GPU userspace,
rootfs configuration and image assembly.

Development baseline: SD boot, display/backlight, GPU rendering, temperature
sensors and USB serial. Controls, audio, battery management and wireless
connectivity are not integrated yet.

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
The image bootloader tries `brick-previous` if `brick-diag` cannot be loaded.
This fallback handles missing payloads, not a kernel hang.

`make dev` enables Buildroot's local source override; use `KERNEL_DIR=/path/to/linux`
for another checkout. Remove
`output/build/local.mk` and use a fresh output directory to verify pinned
release builds. No changes need to be copied between repositories. Use `make kernel-reconfigure`
after changing the kernel defconfig.

The development image exposes a root shell over USB serial. Internal eMMC
is disabled. GPU suspend, power-off and DVFS are unsupported.
`evtest` is included for inspecting input events when developing button support.

The hardware development kernel also enables USB Ethernet at `192.168.7.2/24`.
Configure the host end as `192.168.7.1/24`. SSH accepts public keys only;
install a key in `/root/.ssh/authorized_keys` through the serial console.
The initramfs does not persist keys across boots. Use `scp -O` for transfers.
The pinned baseline kernel does not yet include the Ethernet gadget function.

`Containerfile` supplies the host build dependencies. Host tools use Python's
standard library. Run `make check` with a host C compiler and BusyBox installed.

Build scripts and tools are GPL-2.0-only. Third-party components retain their
own licenses; proprietary GPU binaries are downloaded during the build.
