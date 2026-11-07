################################################################################
# Brick WPE WebKit captive engine
################################################################################

BRICK_WPEWEBKIT_VERSION = 2.54.1-r1
BRICK_WPEWEBKIT_SITE = https://github.com/jkingston/trimui-brick-firmware/releases/download/wpewebkit-brick-2.54.1-r1
BRICK_WPEWEBKIT_SOURCE = brick-wpewebkit-2.54.1-r1-aarch64-buildroot-62474ed54affdd6d.tar.gz
BRICK_WPEWEBKIT_INSTALL_STAGING = YES
BRICK_WPEWEBKIT_LICENSE = LGPL-2.1+, BSD-2-Clause
BRICK_WPEWEBKIT_LICENSE_FILES = \
	licenses/LICENSE-APPLE \
	licenses/LICENSE-LGPL-2.1

define BRICK_WPEWEBKIT_USERS
	brick-captive -1 brick-captive -1 * /data/wifi/captive-browser - - Captive browser
endef

BRICK_WPEWEBKIT_DEPENDENCIES = \
	host-gperf host-perl host-python3 host-ruby host-unifdef \
	bubblewrap fontconfig freetype harfbuzz icu jpeg libdrm libepoxy libgbm \
	libgcrypt libgles libinput libpng libseccomp libsoup3 libtasn1 libudev \
	libxkbcommon libxslt sqlite webp woff2 xdg-dbus-proxy xkeyboard-config

BRICK_WPEWEBKIT_LAUNCHER_DIR = \
	$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/wpe-platform-brick/launcher
BRICK_WPEWEBKIT_INTEGRATION_DIR = \
	$(BR2_EXTERNAL_BRICK_PATH)/board/trimui/brick/wpe-platform-brick/integration

define BRICK_WPEWEBKIT_BUILD_CAPTIVE_LAUNCHER
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror \
		`$(PKG_CONFIG_HOST_BINARY) --cflags wpe-webkit-2.0 glib-2.0 gio-2.0 sqlite3` \
		$(BRICK_WPEWEBKIT_LAUNCHER_DIR)/brick-captive-wpe.c \
		$(BRICK_WPEWEBKIT_LAUNCHER_DIR)/brick-controller.c \
		$(BRICK_WPEWEBKIT_LAUNCHER_DIR)/brick-osk-context.c \
		$(BRICK_WPEWEBKIT_LAUNCHER_DIR)/brick-osk-model.c \
		$(BRICK_WPEWEBKIT_LAUNCHER_DIR)/captive-policy.c \
		$(TARGET_LDFLAGS) \
		`$(PKG_CONFIG_HOST_BINARY) --libs wpe-webkit-2.0 glib-2.0 gio-2.0 sqlite3` \
		-o $(@D)/brick-captive-wpe
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror \
		$(BRICK_WPEWEBKIT_LAUNCHER_DIR)/brick-captive-run.c \
		$(TARGET_LDFLAGS) -o $(@D)/brick-captive-run
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror \
		$(BRICK_WPEWEBKIT_INTEGRATION_DIR)/brick-captive-authority.c \
		$(TARGET_LDFLAGS) -o $(@D)/brick-captive-authority
	$(TARGET_CC) $(TARGET_CFLAGS) -std=c11 -Wall -Wextra -Werror \
		`$(PKG_CONFIG_HOST_BINARY) --cflags libdrm` \
		$(BRICK_WPEWEBKIT_INTEGRATION_DIR)/brick-kms-watchdog.c \
		$(TARGET_LDFLAGS) `$(PKG_CONFIG_HOST_BINARY) --libs libdrm` \
		-o $(@D)/brick-captive-watchdog
endef
define BRICK_WPEWEBKIT_INSTALL_PREBUILT_STAGING
	cp -a $(@D)/staging/. $(STAGING_DIR)/
endef
BRICK_WPEWEBKIT_POST_EXTRACT_HOOKS += \
	BRICK_WPEWEBKIT_INSTALL_PREBUILT_STAGING

define BRICK_WPEWEBKIT_BUILD_CMDS
	$(BRICK_WPEWEBKIT_BUILD_CAPTIVE_LAUNCHER)
endef

define BRICK_WPEWEBKIT_INSTALL_STAGING_CMDS
	cp -a $(@D)/staging/. $(STAGING_DIR)/
endef

define BRICK_WPEWEBKIT_INSTALL_ENGINE_TARGET
	while IFS= read -r path; do \
		mkdir -p "$(TARGET_DIR)/$$(dirname "$$path")"; \
		cp -a "$(@D)/staging/$$path" "$(TARGET_DIR)/$$path"; \
	done < $(@D)/target-files.txt
endef

define BRICK_WPEWEBKIT_INSTALL_CAPTIVE_LAUNCHER
	$(INSTALL) -D -m 0755 $(@D)/brick-captive-wpe \
		$(TARGET_DIR)/usr/libexec/brick-captive-wpe
	$(INSTALL) -D -m 0755 $(@D)/brick-captive-run \
		$(TARGET_DIR)/usr/bin/brick-captive-wpe
	$(INSTALL) -D -m 0755 \
		$(BRICK_WPEWEBKIT_INTEGRATION_DIR)/brick-captive-supervisor \
		$(TARGET_DIR)/usr/libexec/brick-captive-supervisor
	$(INSTALL) -D -m 0755 $(@D)/brick-captive-authority \
		$(TARGET_DIR)/usr/libexec/brick-captive-authority
	$(INSTALL) -D -m 0755 $(@D)/brick-captive-watchdog \
		$(TARGET_DIR)/usr/libexec/brick-captive-watchdog
	$(INSTALL) -D -m 0755 \
		$(BRICK_WPEWEBKIT_INTEGRATION_DIR)/brick-captive-display \
		$(TARGET_DIR)/usr/libexec/brick-captive-display
	ln -sf brick-captive-display \
		$(TARGET_DIR)/usr/libexec/brick-captive-display-pause
	ln -sf brick-captive-display \
		$(TARGET_DIR)/usr/libexec/brick-captive-display-resume
	$(INSTALL) -D -m 0755 \
		$(BRICK_WPEWEBKIT_INTEGRATION_DIR)/brick-captive-control \
		$(TARGET_DIR)/usr/bin/brick-captive-control
	$(INSTALL) -D -m 0755 \
		$(BRICK_WPEWEBKIT_INTEGRATION_DIR)/brick-captive-broker \
		$(TARGET_DIR)/usr/libexec/brick-captive-broker
endef
BRICK_WPEWEBKIT_POST_INSTALL_TARGET_HOOKS += \
	BRICK_WPEWEBKIT_INSTALL_CAPTIVE_LAUNCHER

define BRICK_WPEWEBKIT_INSTALL_TARGET_CMDS
	$(BRICK_WPEWEBKIT_INSTALL_ENGINE_TARGET)
endef

$(eval $(generic-package))
