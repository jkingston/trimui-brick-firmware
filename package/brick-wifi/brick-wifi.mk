################################################################################
# Brick Wi-Fi authority
################################################################################

BRICK_WIFI_VERSION = 1
BRICK_WIFI_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-wifi
BRICK_WIFI_SITE_METHOD = local
BRICK_WIFI_LICENSE = GPL-2.0-only

define BRICK_WIFI_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -std=c11 \
		-Wall -Wextra -Werror $(@D)/brick-wifi-authority.c \
		-o $(@D)/brick-wifi-authority
endef

define BRICK_WIFI_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/brick-wifi-authority \
		$(TARGET_DIR)/usr/sbin/brick-wifi-authority
	$(INSTALL) -D -m 0755 $(@D)/S44brick-wifi-authority \
		$(TARGET_DIR)/etc/init.d/S44brick-wifi-authority
	$(INSTALL) -D -m 0755 $(@D)/brick-wifi-recover \
		$(TARGET_DIR)/usr/libexec/brick-wifi-recover
endef

$(eval $(generic-package))
