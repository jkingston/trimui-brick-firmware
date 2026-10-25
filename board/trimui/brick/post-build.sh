#!/bin/sh
set -eu
board=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$board/prepare-state.sh" "$TARGET_DIR"
# Run before the generic modalias rule; retain Buildroot's device permissions.
rules='$SUBSYSTEM=udc root:root 660 */etc/init.d/S30usb hotplug
$SUBSYSTEM=gadget root:root 660 */etc/init.d/S30usb hotplug'
if ! grep -q '^\$SUBSYSTEM=udc .*S30usb hotplug$' "$TARGET_DIR/etc/mdev.conf"; then
	{ printf '%s\n' "$rules"; cat "$TARGET_DIR/etc/mdev.conf"; } > "$TARGET_DIR/etc/mdev.conf.tmp"
	mv "$TARGET_DIR/etc/mdev.conf.tmp" "$TARGET_DIR/etc/mdev.conf"
fi
"$HOST_DIR/bin/python3" "$BR2_EXTERNAL_BRICK_PATH/tools/usb_profile.py" install \
	"$BINARIES_DIR" "$BR2_CONFIG" "$HOST_DIR/bin/fdtget"
revision=$(sed -n '1p' "$BINARIES_DIR/kernel-source.txt")
override=$(sed -n '2p' "$BINARIES_DIR/kernel-source.txt")
"$HOST_DIR/bin/python3" "$BR2_EXTERNAL_BRICK_PATH/tools/manifest.py" \
	--rootfs "$TARGET_DIR" "$BINARIES_DIR" "$BR2_CONFIG" "$revision" "$override"
