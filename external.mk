include $(sort $(wildcard $(BR2_EXTERNAL_BRICK_PATH)/package/*/*.mk))

# Record the selected source even when Buildroot uses a local checkout.
define BRICK_SOURCE_RECORD
	cp $(LINUX_DIR)/include/config/kernel.release $(BINARIES_DIR)/kernel.release
	printf '%s\n' '$(LINUX_VERSION)' '$(LINUX_OVERRIDE_SRCDIR)' > $(BINARIES_DIR)/kernel-source.txt
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += BRICK_SOURCE_RECORD

BRICK_CHECK_BOOTLOADER_ARGS = \
	$(UBOOT_DIR) $(BINARIES_DIR)/bl31.bin $(HOST_DIR)/bin/fdtget

define BRICK_CHECK_BOOTLOADER
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/tools/check_bootloader.py \
		$(BRICK_CHECK_BOOTLOADER_ARGS)
endef
UBOOT_POST_INSTALL_IMAGES_HOOKS += BRICK_CHECK_BOOTLOADER

ifeq ($(BR2_PACKAGE_BRICK_SCP),y)
UBOOT_DEPENDENCIES += brick-scp
# external.mk is parsed after U-Boot's generic dependency prerequisites have
# been expanded. Add the concrete artifact edge needed by binman explicitly.
$(UBOOT_TARGET_BUILD): | $(BRICK_SCP_TARGET_INSTALL)
UBOOT_MAKE_OPTS += \
	SCP=$(BINARIES_DIR)/brick-scp.bin \
	SCP_FDT=$(BINARIES_DIR)/brick-scp-service.dtb
BRICK_CHECK_BOOTLOADER_ARGS += \
	--scp $(BINARIES_DIR)/brick-scp.bin \
	--scp-fdt $(BINARIES_DIR)/brick-scp-service.dtb
ARM_TRUSTED_FIRMWARE_MAKE_OPTS += \
	SUNXI_VENDOR_SCP_TRANSPORT=1 \
	SUNXI_A133_STANDBY_SCRATCH=1 \
	SUNXI_A133_VENDOR_SCP_STARTUP=1 \
	SUNXI_A133_NATIVE_WAKE_ABI=1 \
	SUNXI_A133_SUSPEND_ORCHESTRATOR=1 \
	SUNXI_A133_PSCI_SUSPEND_PRECURSOR=1 \
	SUNXI_A133_SYSTEM_SUSPEND=1

define BRICK_SCP_UBOOT_KCONFIG
	$(call KCONFIG_ENABLE_OPT,CONFIG_SUNXI_A133_VENDOR_SCP,$(@D)/.config)
endef
UBOOT_KCONFIG_FIXUP_CMDS += $(BRICK_SCP_UBOOT_KCONFIG)
endif

LINUX_DEPENDENCIES += host-python3 host-dtc

define BRICK_SCP_KERNEL_PROFILE
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/tools/scp_profile.py verify-kernel \
		$(BR2_CONFIG) $(LINUX_DIR)
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += BRICK_SCP_KERNEL_PROFILE

# Validate the selected controller role before normalizing the boot payload name.
define BRICK_USB_PROFILE
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/tools/usb_profile.py install \
		$(BINARIES_DIR) $(BR2_CONFIG) $(HOST_DIR)/bin/fdtget
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += BRICK_USB_PROFILE

ifeq ($(BR2_PACKAGE_BRICK_EMMC_RECOVERY),y)
define BRICK_EMMC_RECOVERY_PROFILE
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/tools/emmc_recovery.py install \
		$(BINARIES_DIR) $(BR2_CONFIG) $(HOST_DIR)/bin/fdtget $(HOST_DIR)/bin/fdtput
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += BRICK_EMMC_RECOVERY_PROFILE
endif

ifeq ($(BR2_BRICK_G2D_QUALIFICATION),y)
define BRICK_G2D_QUALIFICATION_PROFILE
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/tools/g2d_profile.py install \
		$(BINARIES_DIR) $(BR2_CONFIG) $(HOST_DIR)/bin/fdtget $(HOST_DIR)/bin/fdtput
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += BRICK_G2D_QUALIFICATION_PROFILE
endif

ifeq ($(BR2_BRICK_VE_QUALIFICATION),y)
define BRICK_VE_QUALIFICATION_PROFILE
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/tools/ve_profile.py install \
		$(BINARIES_DIR) $(BR2_CONFIG) $(HOST_DIR)/bin/fdtget $(HOST_DIR)/bin/fdtput
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += BRICK_VE_QUALIFICATION_PROFILE
endif
