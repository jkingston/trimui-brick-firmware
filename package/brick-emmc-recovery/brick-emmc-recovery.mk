################################################################################
#
# Brick bounded read-only eMMC recovery profile
#
################################################################################

BRICK_EMMC_RECOVERY_VERSION = 1
BRICK_EMMC_RECOVERY_SITE = $(BR2_EXTERNAL_BRICK_PATH)/package/brick-emmc-recovery
BRICK_EMMC_RECOVERY_SITE_METHOD = local
BRICK_EMMC_RECOVERY_LICENSE = GPL-2.0-only

define BRICK_EMMC_RECOVERY_INSTALL_TARGET_CMDS
	$(INSTALL) -D -m 0755 $(@D)/S03emmc-recovery \
		$(TARGET_DIR)/etc/init.d/S03emmc-recovery
	# Do not expose BusyBox's filesystem-check dispatcher in a profile that
	# must never attempt repair of the internal eMMC.
	$(RM) $(TARGET_DIR)/sbin/fsck $(TARGET_DIR)/usr/sbin/fsck
endef

$(eval $(generic-package))
