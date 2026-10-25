BRICK_CONTROLS_VERSION = 1
BRICK_CONTROLS_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-controls
BRICK_CONTROLS_SITE_METHOD = local
BRICK_CONTROLS_LICENSE = GPL-2.0-only
BRICK_CONTROLS_DEPENDENCIES = alsa-lib

define BRICK_CONTROLS_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -Wall -Wextra \
		$(@D)/brick-controls.c -lasound -o $(@D)/brick-controls
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -Wall -Wextra \
		$(@D)/brick-brightness.c -o $(@D)/brick-brightness
endef

define BRICK_CONTROLS_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/brick-controls $(TARGET_DIR)/usr/sbin/brick-controls
	$(INSTALL) -D -m 0755 $(@D)/brick-brightness $(TARGET_DIR)/usr/sbin/brick-brightness
endef

$(eval $(generic-package))
