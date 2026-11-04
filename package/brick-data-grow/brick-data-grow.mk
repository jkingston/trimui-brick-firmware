BRICK_DATA_GROW_VERSION = 1
BRICK_DATA_GROW_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-data-grow
BRICK_DATA_GROW_SITE_METHOD = local
BRICK_DATA_GROW_LICENSE = GPL-2.0-only

define BRICK_DATA_GROW_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -Wall -Wextra -Werror \
		$(@D)/brick-grow-data.c -o $(@D)/brick-grow-data
endef

define BRICK_DATA_GROW_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/brick-grow-data \
		$(TARGET_DIR)/usr/sbin/brick-grow-data
endef

$(eval $(generic-package))
