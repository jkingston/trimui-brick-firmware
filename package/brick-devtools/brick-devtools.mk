################################################################################
# Brick development transfer tools
################################################################################
BRICK_DEVTOOLS_VERSION = 1
BRICK_DEVTOOLS_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-devtools
BRICK_DEVTOOLS_SITE_METHOD = local
BRICK_DEVTOOLS_LICENSE = GPL-2.0-only

define BRICK_DEVTOOLS_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -Wall -Wextra \
		$(@D)/brick-receive.c -o $(@D)/brick-receive
endef

define BRICK_DEVTOOLS_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/brick-receive $(TARGET_DIR)/usr/bin/brick-receive
endef

$(eval $(generic-package))
