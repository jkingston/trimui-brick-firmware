################################################################################
# Brick accelerator qualification
################################################################################
BRICK_ACCELERATOR_QUALIFICATION_VERSION = 1
BRICK_ACCELERATOR_QUALIFICATION_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-accelerator-qualification
BRICK_ACCELERATOR_QUALIFICATION_SITE_METHOD = local
BRICK_ACCELERATOR_QUALIFICATION_LICENSE = GPL-2.0-only

ifeq ($(BR2_BRICK_G2D_QUALIFICATION),y)
BRICK_ACCELERATOR_QUALIFICATION_DEPENDENCIES += host-python3
define BRICK_ACCELERATOR_QUALIFICATION_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -std=c11 -Wall -Wextra -Werror \
		$(@D)/g2d-m2m.c -o $(@D)/brick-g2d-m2m
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -std=c11 -Wall -Wextra -Werror \
		$(@D)/g2d-compare.c -o $(@D)/brick-g2d-compare
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -std=c11 -Wall -Wextra -Werror \
		$(@D)/g2d-compose.c -o $(@D)/brick-g2d-compose
	$(HOST_DIR)/bin/python3 $(@D)/generate-g2d-fixtures.py
endef

define BRICK_ACCELERATOR_QUALIFICATION_INSTALL_G2D
	$(INSTALL) -D -m 0755 $(@D)/brick-g2d-m2m \
		$(TARGET_DIR)/usr/libexec/brick-qualification/brick-g2d-m2m
	$(INSTALL) -D -m 0755 $(@D)/brick-g2d-compare \
		$(TARGET_DIR)/usr/libexec/brick-qualification/brick-g2d-compare
	$(INSTALL) -D -m 0755 $(@D)/brick-g2d-compose \
		$(TARGET_DIR)/usr/libexec/brick-qualification/brick-g2d-compose
	$(INSTALL) -D -m 0755 $(@D)/brick-qualify-g2d \
		$(TARGET_DIR)/usr/sbin/brick-qualify-g2d
	mkdir -p $(TARGET_DIR)/usr/share/brick-qualification/g2d/fixtures
	cp -a $(@D)/fixtures/g2d/. \
		$(TARGET_DIR)/usr/share/brick-qualification/g2d/fixtures/
	cp -a $(@D)/cases.tsv $(@D)/manifest.json \
		$(TARGET_DIR)/usr/share/brick-qualification/g2d/
endef
BRICK_ACCELERATOR_QUALIFICATION_POST_INSTALL_TARGET_HOOKS += BRICK_ACCELERATOR_QUALIFICATION_INSTALL_G2D
endif

ifeq ($(BR2_BRICK_VE_QUALIFICATION),y)
define BRICK_ACCELERATOR_QUALIFICATION_INSTALL_VE
	$(INSTALL) -D -m 0755 $(@D)/brick-qualify-ve \
		$(TARGET_DIR)/usr/sbin/brick-qualify-ve
	mkdir -p $(TARGET_DIR)/usr/share/brick-qualification/ve
	cp -a $(@D)/fixtures/ve/. $(TARGET_DIR)/usr/share/brick-qualification/ve/
endef
BRICK_ACCELERATOR_QUALIFICATION_POST_INSTALL_TARGET_HOOKS += BRICK_ACCELERATOR_QUALIFICATION_INSTALL_VE
endif

$(eval $(generic-package))
