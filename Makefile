BUILDROOT_VERSION := 2026.08.1
BUILDROOT_SHA256 := 34fb7bea8a213ffde39cc02e61703391560f26e0850f03138c722d0b09880f2b
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BUILDROOT := $(ROOT)/output/buildroot-$(BUILDROOT_VERSION)
O ?= $(ROOT)/output/build
override O := $(abspath $(O))
USB_ROLE ?= peripheral
EMMC_RECOVERY ?= 0
SCP_PROFILE ?= disabled
G2D_PROFILE ?= disabled
VE_PROFILE ?= disabled
ifeq ($(filter $(USB_ROLE),peripheral host auto),)
$(error USB_ROLE must be peripheral, host or auto)
endif
ifeq ($(filter $(SCP_PROFILE),disabled startup deep),)
$(error SCP_PROFILE must be disabled, startup or deep)
endif
ifneq ($(SCP_PROFILE),disabled)
ifneq ($(USB_ROLE),peripheral)
$(error SCP_PROFILE=$(SCP_PROFILE) requires USB_ROLE=peripheral)
endif
endif
ifeq ($(filter $(VE_PROFILE),disabled qualification),)
$(error VE_PROFILE must be disabled or qualification)
endif
ifeq ($(filter $(G2D_PROFILE),disabled qualification),)
$(error G2D_PROFILE must be disabled or qualification)
endif
ifeq ($(G2D_PROFILE):$(VE_PROFILE),qualification:qualification)
$(error G2D_PROFILE and VE_PROFILE qualification modes are mutually exclusive)
endif
ifeq ($(filter $(EMMC_RECOVERY),0 1),)
$(error EMMC_RECOVERY must be 0 or 1)
endif
KERNEL_DIR ?= $(abspath $(ROOT)/../linux)
ARCHIVE := $(ROOT)/output/downloads/buildroot-$(BUILDROOT_VERSION).tar.xz

.PHONY: all configure dev kernel-rebuild kernel-reconfigure check clean
all: $(O)/.config
	$(MAKE) -C $(BUILDROOT) O=$(O) BR2_EXTERNAL=$(ROOT) BR2_DL_DIR=$(ROOT)/output/downloads

$(ARCHIVE):
	mkdir -p $(@D)
	curl -fL https://buildroot.org/downloads/$(@F) -o $@.tmp
	echo "$(BUILDROOT_SHA256)  $@.tmp" | sha256sum -c -
	mv $@.tmp $@

$(BUILDROOT)/.brick-extracted: $(ARCHIVE)
	echo "$(BUILDROOT_SHA256)  $(ARCHIVE)" | sha256sum -c -
	tar -xJf $(ARCHIVE) -C $(ROOT)/output
	touch $@

configure: $(BUILDROOT)/.brick-extracted
	python3 $(ROOT)/tools/usb_profile.py check-output $(O)/.config $(USB_ROLE) $(SCP_PROFILE)
	python3 $(ROOT)/tools/scp_profile.py check-output $(O)/.config $(SCP_PROFILE)
	python3 $(ROOT)/tools/emmc_recovery.py check-output $(O)/.config $(EMMC_RECOVERY)
	python3 $(ROOT)/tools/g2d_profile.py check-output $(O)/.config $(G2D_PROFILE)
	python3 $(ROOT)/tools/ve_profile.py check-output $(O)/.config $(VE_PROFILE)
	$(MAKE) -C $(BUILDROOT) O=$(O) BR2_EXTERNAL=$(ROOT) BR2_DL_DIR=$(ROOT)/output/downloads brick_defconfig
	python3 $(ROOT)/tools/usb_profile.py configure $(O)/.config $(USB_ROLE) $(SCP_PROFILE)
	python3 $(ROOT)/tools/scp_profile.py configure $(O)/.config $(SCP_PROFILE)
	python3 $(ROOT)/tools/emmc_recovery.py configure $(O)/.config $(EMMC_RECOVERY)
	python3 $(ROOT)/tools/g2d_profile.py configure $(O)/.config $(G2D_PROFILE)
	python3 $(ROOT)/tools/ve_profile.py configure $(O)/.config $(VE_PROFILE)
	$(MAKE) -C $(BUILDROOT) O=$(O) BR2_EXTERNAL=$(ROOT) BR2_DL_DIR=$(ROOT)/output/downloads olddefconfig
	python3 $(ROOT)/tools/usb_profile.py check-output $(O)/.config $(USB_ROLE) $(SCP_PROFILE)
	python3 $(ROOT)/tools/scp_profile.py check-output $(O)/.config $(SCP_PROFILE)
	python3 $(ROOT)/tools/emmc_recovery.py check-output $(O)/.config $(EMMC_RECOVERY)
	python3 $(ROOT)/tools/g2d_profile.py check-output $(O)/.config $(G2D_PROFILE)
	python3 $(ROOT)/tools/ve_profile.py check-output $(O)/.config $(VE_PROFILE)

$(O)/.config: $(BUILDROOT)/.brick-extracted
	$(MAKE) configure

dev: $(O)/.config
	test -f "$(KERNEL_DIR)/arch/arm64/configs/trimui_brick_defconfig"
	printf 'LINUX_OVERRIDE_SRCDIR = %s\n' "$(abspath $(KERNEL_DIR))" > $(O)/local.mk

kernel-rebuild: $(O)/.config
	$(MAKE) -C $(BUILDROOT) O=$(O) BR2_EXTERNAL=$(ROOT) BR2_DL_DIR=$(ROOT)/output/downloads linux-rebuild all

kernel-reconfigure: $(O)/.config
	$(MAKE) -C $(BUILDROOT) O=$(O) BR2_EXTERNAL=$(ROOT) BR2_DL_DIR=$(ROOT)/output/downloads linux-reconfigure all

check:
	python3 -m unittest discover -s $(ROOT)/tests

clean:
	$(MAKE) -C $(BUILDROOT) O=$(O) BR2_EXTERNAL=$(ROOT) BR2_DL_DIR=$(ROOT)/output/downloads clean
