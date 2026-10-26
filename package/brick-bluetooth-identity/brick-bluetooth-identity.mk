BRICK_BLUETOOTH_IDENTITY_VERSION = 1
BRICK_BLUETOOTH_IDENTITY_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-bluetooth-identity
BRICK_BLUETOOTH_IDENTITY_SITE_METHOD = local
BRICK_BLUETOOTH_IDENTITY_LICENSE = GPL-2.0-only

define BRICK_BLUETOOTH_IDENTITY_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -Wall -Wextra -Werror \
		$(@D)/brick-bluetooth-identity.c -o $(@D)/brick-bluetooth-identity
endef

define BRICK_BLUETOOTH_IDENTITY_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/brick-bluetooth-identity \
		$(TARGET_DIR)/usr/sbin/brick-bluetooth-identity
endef

$(eval $(generic-package))
