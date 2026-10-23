BUILDROOT_VERSION := 2026.08.1
BUILDROOT_SHA256 := 34fb7bea8a213ffde39cc02e61703391560f26e0850f03138c722d0b09880f2b
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
BUILDROOT := $(ROOT)/output/buildroot-$(BUILDROOT_VERSION)
O ?= $(ROOT)/output/build
override O := $(abspath $(O))
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
	$(MAKE) -C $(BUILDROOT) O=$(O) BR2_EXTERNAL=$(ROOT) BR2_DL_DIR=$(ROOT)/output/downloads brick_defconfig

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
