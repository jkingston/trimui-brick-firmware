################################################################################
# Brick GE8300 firmware and userspace (DDK 1.19@6345021)
################################################################################
BRICK_GPU_VERSION = 1.1.1-20251126
BRICK_GPU_SITE = https://github.com/trimui/firmware_brick/releases/download/v$(BRICK_GPU_VERSION)
BRICK_GPU_SOURCE = TG3040_Brick_firmware_v1.1.1.zip
BRICK_GPU_LICENSE = Proprietary
BRICK_GPU_REDISTRIBUTE = NO
BRICK_GPU_DEPENDENCIES = host-python3 host-e2fsprogs
BRICK_GPU_INSTALL_STAGING = YES

define BRICK_GPU_EXTRACT_CMDS
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/package/brick-gpu/extract.py \
		$(BRICK_GPU_DL_DIR)/$(BRICK_GPU_SOURCE) $(@D) $(HOST_DIR)/sbin/debugfs
endef

define BRICK_GPU_INSTALL_TARGET_CMDS
	cp -a $(@D)/runtime/. $(TARGET_DIR)/
endef

define BRICK_GPU_INSTALL_STAGING_CMDS
	cp -a $(@D)/runtime/usr/lib/. $(STAGING_DIR)/usr/lib/
endef

$(eval $(generic-package))
