################################################################################
# Brick CE/TRNG qualification
################################################################################
BRICK_CE_TRNG_QUALIFICATION_VERSION = 1
BRICK_CE_TRNG_QUALIFICATION_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-ce-trng-qualification
BRICK_CE_TRNG_QUALIFICATION_SITE_METHOD = local
BRICK_CE_TRNG_QUALIFICATION_LICENSE = GPL-2.0-only

define BRICK_CE_TRNG_QUALIFICATION_BUILD_CMDS
	$(TARGET_CC) $(TARGET_CFLAGS) $(TARGET_LDFLAGS) -std=c11 -Wall -Wextra -Werror \
		-pthread $(@D)/ce-qualify.c -o $(@D)/brick-ce-kat
endef

define BRICK_CE_TRNG_QUALIFICATION_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/brick-ce-kat \
		$(TARGET_DIR)/usr/libexec/brick-qualification/brick-ce-kat
	$(INSTALL) -D -m 0755 $(@D)/brick-qualify-ce-trng \
		$(TARGET_DIR)/usr/sbin/brick-qualify-ce-trng
endef

$(eval $(generic-package))
