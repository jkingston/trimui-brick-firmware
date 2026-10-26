#!/bin/sh
set -eu
board=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=$(command -p -v python3)
"$board/prepare-state.sh" "$TARGET_DIR"
if grep -q '^BR2_ROOTFS_DEVICE_CREATION_DYNAMIC_MDEV=y$' "$BR2_CONFIG"; then
	# Run before the generic modalias rule; retain Buildroot's permissions.
	rules='$SUBSYSTEM=udc root:root 660 */etc/init.d/S30usb hotplug
$SUBSYSTEM=gadget root:root 660 */etc/init.d/S30usb hotplug'
	if ! grep -q '^\$SUBSYSTEM=udc .*S30usb hotplug$' "$TARGET_DIR/etc/mdev.conf"; then
		{ printf '%s\n' "$rules"; cat "$TARGET_DIR/etc/mdev.conf"; } > "$TARGET_DIR/etc/mdev.conf.tmp"
		mv "$TARGET_DIR/etc/mdev.conf.tmp" "$TARGET_DIR/etc/mdev.conf"
	fi
	if grep -q '^BR2_PACKAGE_BRICK_EMMC_RECOVERY=y$' "$BR2_CONFIG" &&
		! grep -q '^mmcblk\[0-9\]+rpmb root:root 000 !$' "$TARGET_DIR/etc/mdev.conf"; then
		# RPMB has a character-device ioctl interface, so block it independently
		# of the normal block-device force_ro policy.
		{ printf '%s\n' 'mmcblk[0-9]+rpmb root:root 000 !'; cat "$TARGET_DIR/etc/mdev.conf"; } > "$TARGET_DIR/etc/mdev.conf.tmp"
		mv "$TARGET_DIR/etc/mdev.conf.tmp" "$TARGET_DIR/etc/mdev.conf"
	fi
else
	install -d "$TARGET_DIR/etc/udev/rules.d"
	cat > "$TARGET_DIR/etc/udev/rules.d/90-brick-usb.rules" <<'EOF'
SUBSYSTEM=="udc", ACTION=="add|change|bind", RUN+="/etc/init.d/S30usb hotplug"
SUBSYSTEM=="gadget", ACTION=="add|change|bind", RUN+="/etc/init.d/S30usb hotplug"
EOF
fi
"$python" "$BR2_EXTERNAL_BRICK_PATH/tools/usb_profile.py" install \
	"$BINARIES_DIR" "$BR2_CONFIG" "$HOST_DIR/bin/fdtget"
revision=$(sed -n '1p' "$BINARIES_DIR/kernel-source.txt")
override=$(sed -n '2p' "$BINARIES_DIR/kernel-source.txt")
"$python" "$BR2_EXTERNAL_BRICK_PATH/tools/manifest.py" \
	--rootfs "$TARGET_DIR" "$BINARIES_DIR" "$BR2_CONFIG" "$revision" "$override"
