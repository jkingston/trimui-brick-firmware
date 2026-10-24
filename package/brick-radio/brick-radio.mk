################################################################################
# TrimUI Brick XR829 radio firmware
################################################################################
BRICK_RADIO_VERSION = 1.1.1-20251126
BRICK_RADIO_SITE = https://github.com/trimui/firmware_brick/releases/download/v$(BRICK_RADIO_VERSION)
BRICK_RADIO_SOURCE = TG3040_Brick_firmware_v1.1.1.zip
BRICK_RADIO_LICENSE = Proprietary
BRICK_RADIO_REDISTRIBUTE = NO
BRICK_RADIO_DEPENDENCIES = host-python3 host-e2fsprogs

define BRICK_RADIO_EXTRACT_CMDS
	PYTHONPATH=$(BR2_EXTERNAL_BRICK_PATH)/support/stock $(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/package/brick-radio/extract.py \
		$(BRICK_RADIO_DL_DIR)/$(BRICK_RADIO_SOURCE) $(@D) $(HOST_DIR)/sbin/debugfs
endef

define BRICK_RADIO_INSTALL_TARGET_CMDS
	cp -a $(@D)/runtime/. $(TARGET_DIR)/
endef

$(eval $(generic-package))
