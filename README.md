# TrimUI Brick firmware

Minimal SD-boot Linux system built with Buildroot. The kernel lives in a
[separate Linux fork](https://github.com/jkingston/trimui-brick-kernel); this repository owns bootloader patches, GPU userspace,
radio firmware, rootfs configuration and image assembly.

Development baseline: SD boot, display/backlight, GPU rendering, GPIO controls,
battery telemetry, temperature sensors, CPU frequency scaling and USB
serial/Ethernet. Stereo headphone playback works at 48 kHz; shutdown can pop.
Speaker output and jack detection still need validation. Wireless networking,
charging control, suspend and GPU power management remain incomplete.

Build the pinned kernel and firmware:

```sh
make configure
make
```

The image and checksums are in `output/build/images/`. Source revisions are
pinned in `configs/brick_defconfig`. GPU and radio binaries are extracted from the
checksum-verified stock archive and are not stored in Git.

For local kernel changes, place its checkout at `../linux`, then:

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
is disabled. The GPU powers down internally between workloads; external
clocks and rails stay enabled. GPU s2idle handling awaits device validation;
whole-system sleep remains incomplete. GPU devfreq exposes four fixed-rail rates,
defaulting to 702 MHz; frequency changes await device validation.
ADC capture support is present, but microphone routing awaits hardware verification.
The video decoder, G2D rotation/scaling/color conversion/compositing and IOMMU
remain disabled pending qualification. The compositor supports two RGB inputs
with source-over blending through MMAP buffers; DMA-BUF sharing, color key and
general raster operations remain unfinished.
`evtest` and ALSA utilities are included for hardware validation.

Volume keys control the internal codec with a persistent 20-step setting.
Set `FN_MUTE=1` in `/etc/default/brick-controls` to mute while Fn is active,
then restart `/etc/init.d/S47controls`. This does not select an audio route
or control Bluetooth/USB audio volume. These controls require the matching
development kernel's digital-gain controls; device validation is pending.

Start + L/R adjusts brightness through stock's eleven levels and saves the
setting. Select suppresses this shortcut; games still receive the buttons.
The separate brightness service requires the matching 0–255 backlight driver.
Shortcut and brightness restoration await device validation.

The top USB port has independent host support; device validation is pending.
An explicit fixed-host build for the bottom port uses `make O=output/host USB_ROLE=host configure`,
then `make O=output/host`. The default keeps the bottom port in peripheral mode.
Host images keep the normal boot payload filenames and skip gadget setup;
USB serial/Ethernet are unavailable. The updater requests reboot but cannot
confirm host-profile boot over USB. Returning to peripheral mode requires its
image and a reboot. Host operation still needs device validation.

An experimental automatic bottom-port profile uses `USB_ROLE=auto` with a fresh
output directory and a kernel providing the role-coordinator DTB. Serial and
Ethernet return in device mode; host-mode boot cannot provide a USB console.
Automatic transitions still require device qualification.

USB Ethernet uses `192.168.7.2/24`.
Configure the host end as `192.168.7.1/24`. SSH accepts public keys only;
install a key in `/root/.ssh/authorized_keys` through the serial console.
Use `scp -O` for transfers.

Wi-Fi starts with no saved networks. Configure `/etc/wpa_supplicant.conf` and run
`/etc/init.d/S45wifi restart`, or configure the running supplicant with
`wpa_cli -p /run/wpa_supplicant -i wlan0`. DHCP follows connection events.
`WIFI_CONFIG` and `WIFI_CTRL_DIR` override the profile and socket directory when
starting the service. Use `wpa_cli save_config` to save a running configuration when persistence is available.

Bluetooth starts powered off. Use `bluetoothctl` to power on and pair devices.
BlueALSA provides SBC, aptX and aptX HD output. Pairing state is stored in `/var/lib/bluetooth`.

`brick-suspend --disconnect-bluetooth` requests s2idle, disconnecting
Bluetooth and restoring its prior powered state on return. Connections are not
guaranteed to reconnect. Keep Bluetooth power settings unchanged while it runs.
Wi-Fi disconnects and reloads its firmware on resume; association is not retained.
Whole-system sleep/wake and reconnection await device qualification. Stock deep
sleep and wake-on-Wi-Fi remain unfinished.

Fresh images include a separate ext4 data partition for Wi-Fi profiles, Bluetooth
bonds, SSH keys and system identity. Existing single-partition cards use temporary
state. Payload updates preserve the data partition; flashing a complete image
erases it. If the data partition exists but cannot be used, dependent services
stay stopped and USB serial remains available for recovery. Wi-Fi profiles, SSH host keys and system identity have survived tested reboots
and payload updates. Bluetooth bond retention and power-loss recovery remain unverified.

`Containerfile` supplies the host build dependencies. Host tools use Python's
standard library. Run `make check` with a host C compiler and BusyBox installed.

Build scripts and tools are GPL-2.0-only. Third-party components retain their
own licenses; proprietary GPU and radio binaries are extracted during the build.
