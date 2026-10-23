#!/bin/sh
set -eu
revision=$(sed -n '1p' "$BINARIES_DIR/kernel-source.txt")
override=$(sed -n '2p' "$BINARIES_DIR/kernel-source.txt")
"$HOST_DIR/bin/python3" "$BR2_EXTERNAL_BRICK_PATH/tools/manifest.py" \
	--rootfs "$TARGET_DIR" "$BINARIES_DIR" "$BR2_CONFIG" "$revision" "$override"
