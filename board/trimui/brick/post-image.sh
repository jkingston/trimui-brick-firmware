#!/bin/sh
set -eu
board=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
genimage --rootpath "$TARGET_DIR" --tmppath "$BUILD_DIR/genimage.tmp" \
	--inputpath "$BINARIES_DIR" --outputpath "$BINARIES_DIR" \
	--config "$board/genimage.cfg"
(cd "$BINARIES_DIR" && sha256sum Image sun50i-a133-trimui-brick.dtb rootfs.cpio.gz u-boot-sunxi-with-spl.bin sdcard.img > SHA256SUMS)
revision=$(sed -n '1p' "$BINARIES_DIR/kernel-source.txt")
override=$(sed -n '2p' "$BINARIES_DIR/kernel-source.txt")
"$HOST_DIR/bin/python3" "$BR2_EXTERNAL_BRICK_PATH/tools/manifest.py" \
	"$BINARIES_DIR" "$BR2_CONFIG" "$revision" "$override"
