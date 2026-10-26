################################################################################
#
# TrimUI Brick system-control firmware
#
################################################################################

BRICK_SCP_VERSION = 1.1.1-20251126
BRICK_SCP_SITE = https://github.com/trimui/firmware_brick/releases/download/v$(BRICK_SCP_VERSION)
BRICK_SCP_SOURCE = TG3040_Brick_firmware_v1.1.1.zip
BRICK_SCP_LICENSE = Proprietary
BRICK_SCP_REDISTRIBUTE = NO
BRICK_SCP_DEPENDENCIES = host-python3
BRICK_SCP_INSTALL_IMAGES = YES

define BRICK_SCP_EXTRACT_CMDS
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/package/brick-scp/extract.py \
		$(BRICK_SCP_DL_DIR)/$(BRICK_SCP_SOURCE) $(@D)
endef

define BRICK_SCP_INSTALL_IMAGES_CMDS
	$(INSTALL) -D -m 0644 $(@D)/scp.bin $(BINARIES_DIR)/brick-scp.bin
	$(INSTALL) -D -m 0644 $(@D)/service.dtb $(BINARIES_DIR)/brick-scp-service.dtb
endef

$(eval $(generic-package))
