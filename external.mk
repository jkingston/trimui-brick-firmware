include $(sort $(wildcard $(BR2_EXTERNAL_BRICK_PATH)/package/*/*.mk))

# Record the selected source even when Buildroot uses a local checkout.
define BRICK_SOURCE_RECORD
	cp $(LINUX_DIR)/include/config/kernel.release $(BINARIES_DIR)/kernel.release
	printf '%s\n' '$(LINUX_VERSION)' '$(LINUX_OVERRIDE_SRCDIR)' > $(BINARIES_DIR)/kernel-source.txt
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += BRICK_SOURCE_RECORD

define BRICK_CHECK_BOOTLOADER
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/tools/check_bootloader.py \
		$(UBOOT_DIR) $(BINARIES_DIR)/bl31.bin $(HOST_DIR)/bin/fdtget
endef
UBOOT_POST_INSTALL_IMAGES_HOOKS += BRICK_CHECK_BOOTLOADER

LINUX_DEPENDENCIES += host-python3 host-dtc

# Validate the selected controller role before normalizing the boot payload name.
define BRICK_USB_PROFILE
	$(HOST_DIR)/bin/python3 $(BR2_EXTERNAL_BRICK_PATH)/tools/usb_profile.py install \
		$(BINARIES_DIR) $(BR2_CONFIG) $(HOST_DIR)/bin/fdtget
endef
LINUX_POST_INSTALL_IMAGES_HOOKS += BRICK_USB_PROFILE
