BRICK_POWER_VERSION = 1
BRICK_POWER_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-power
BRICK_POWER_SITE_METHOD = local
BRICK_POWER_LICENSE = GPL-2.0-only
BRICK_POWER_DEPENDENCIES = dbus host-pkgconf

define BRICK_POWER_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -Wall -Wextra \
		$(@D)/brick-power-key.c -o $(@D)/brick-power-key
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -Wall -Wextra \
		$(@D)/brick-suspend.c `$(PKG_CONFIG_HOST_BINARY) --cflags --libs dbus-1` \
		-o $(@D)/brick-suspend
endef

define BRICK_POWER_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/brick-power-key $(TARGET_DIR)/usr/sbin/brick-power-key
	$(INSTALL) -D -m 0755 $(@D)/brick-suspend $(TARGET_DIR)/usr/sbin/brick-suspend
endef

$(eval $(generic-package))
