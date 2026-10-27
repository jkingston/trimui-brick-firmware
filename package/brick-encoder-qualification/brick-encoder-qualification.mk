################################################################################
# Brick bounded encoder qualification
################################################################################

BRICK_ENCODER_QUALIFICATION_VERSION = 1
BRICK_ENCODER_QUALIFICATION_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-encoder-qualification
BRICK_ENCODER_QUALIFICATION_SITE_METHOD = local
BRICK_ENCODER_QUALIFICATION_LICENSE = GPL-2.0-only

define BRICK_ENCODER_QUALIFICATION_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -std=c11 -Wall -Wextra \
		-Werror $(@D)/brick-qualify-encoder.c -o $(@D)/brick-qualify-encoder
endef

define BRICK_ENCODER_QUALIFICATION_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/brick-qualify-encoder \
		$(TARGET_DIR)/usr/bin/brick-qualify-encoder
	$(INSTALL) -D -m 0755 $(@D)/run-encoder-qualification \
		$(TARGET_DIR)/usr/libexec/run-encoder-qualification
endef

$(eval $(generic-package))
