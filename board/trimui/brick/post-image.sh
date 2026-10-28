#!/bin/sh
set -eu
board=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python=$(command -p -v python3)
revision=$(sed -n '1p' "$BINARIES_DIR/kernel-source.txt")
override=$(sed -n '2p' "$BINARIES_DIR/kernel-source.txt")
"$python" "$BR2_EXTERNAL_BRICK_PATH/tools/manifest.py" \
	--payload "$BINARIES_DIR" "$BR2_CONFIG" "$revision" "$override"
(cd "$BINARIES_DIR" && sha256sum Image sun50i-a133-trimui-brick.dtb rootfs.cpio.gz | sed -e "s/sun50i-a133-trimui-brick.dtb/brick-usb.dtb/" -e "s/rootfs.cpio.gz/initramfs.cpio.gz/" > payload-SHA256SUMS)
# A full image has no retained previous payload, so it must start in the good
# state. Payload-only updates arm rollback after preserving brick-previous.
# U-Boot bootcount_fs: magic, version, count=0, upgrade_available=0.
printf '\275\001\000\000' > "$BINARIES_DIR/brick-bootcount.good"
genimage --rootpath "$TARGET_DIR" --tmppath "$BUILD_DIR/genimage.tmp" \
	--inputpath "$BINARIES_DIR" --outputpath "$BINARIES_DIR" \
	--config "$board/genimage.cfg"
(cd "$BINARIES_DIR" && sha256sum Image sun50i-a133-trimui-brick.dtb rootfs.cpio.gz u-boot-sunxi-with-spl.bin sdcard.img > SHA256SUMS)
"$python" "$BR2_EXTERNAL_BRICK_PATH/tools/manifest.py" \
	"$BINARIES_DIR" "$BR2_CONFIG" "$revision" "$override"
