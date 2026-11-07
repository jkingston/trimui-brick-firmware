################################################################################
# Brick WPE WebKit captive engine
################################################################################

BRICK_WPEWEBKIT_VERSION = 2.54.1
BRICK_WPEWEBKIT_SITE = https://wpewebkit.org/releases
BRICK_WPEWEBKIT_SOURCE = wpewebkit-$(BRICK_WPEWEBKIT_VERSION).tar.xz
BRICK_WPEWEBKIT_INSTALL_STAGING = YES
BRICK_WPEWEBKIT_LICENSE = LGPL-2.1+, BSD-2-Clause
BRICK_WPEWEBKIT_LICENSE_FILES = \
	Source/WebCore/LICENSE-APPLE \
	Source/WebCore/LICENSE-LGPL-2.1

define BRICK_WPEWEBKIT_USERS
	brick-captive -1 brick-captive -1 * /data/wifi/captive-browser - - Captive browser
endef

BRICK_WPEWEBKIT_DEPENDENCIES = \
	host-gperf host-perl host-python3 host-ruby host-unifdef \
	bubblewrap fontconfig freetype harfbuzz icu jpeg libdrm libepoxy libgbm \
	libgcrypt libgles libinput libpng libseccomp libsoup3 libtasn1 libudev \
	libxkbcommon libxslt sqlite webp woff2 xdg-dbus-proxy xkeyboard-config

BRICK_WPEWEBKIT_CMAKE_BACKEND = ninja
BRICK_WPEWEBKIT_CONF_ENV = WK_USE_CCACHE=NO
BRICK_WPEWEBKIT_BUILD_ENV = \
	WK_USE_CCACHE=NO \
	GBM_ALWAYS_SOFTWARE=true \
	GALLIUM_DRIVER=softpipe

BRICK_WPEWEBKIT_CONF_OPTS = \
	-DPORT=WPE \
	-DENABLE_API_TESTS=OFF \
	-DENABLE_BUBBLEWRAP_SANDBOX=ON \
	-DBWRAP_EXECUTABLE=/usr/bin/bwrap \
	-DDBUS_PROXY_EXECUTABLE=/usr/bin/xdg-dbus-proxy \
	-DENABLE_DOCUMENTATION=OFF \
	-DENABLE_GAMEPAD=OFF \
	-DENABLE_GPU_PROCESS=ON \
	-DENABLE_INTROSPECTION=OFF \
	-DENABLE_JOURNALD_LOG=OFF \
	-DENABLE_MEDIA_STREAM=OFF \
	-DENABLE_MINIBROWSER=OFF \
	-DENABLE_SPEECH_SYNTHESIS=OFF \
	-DENABLE_SPELLCHECK=OFF \
	-DENABLE_VIDEO=OFF \
	-DENABLE_WEB_AUDIO=OFF \
	-DENABLE_WEB_CODECS=OFF \
	-DENABLE_WEB_RTC=OFF \
	-DENABLE_WEBDRIVER=OFF \
	-DENABLE_WEBGL=OFF \
	-DENABLE_WEBGPU=OFF \
	-DENABLE_WEBXR=OFF \
	-DENABLE_WPE_LEGACY_API=OFF \
	-DENABLE_WPE_PLATFORM=ON \
	-DENABLE_WPE_PLATFORM_DRM=ON \
	-DENABLE_WPE_PLATFORM_HEADLESS=OFF \
	-DENABLE_WPE_PLATFORM_WAYLAND=OFF \
	-DUSE_ATK=OFF \
	-DUSE_AVIF=OFF \
	-DUSE_GBM=ON \
	-DUSE_GSTREAMER=OFF \
	-DUSE_GSTREAMER_GL=OFF \
	-DUSE_JPEGXL=OFF \
	-DUSE_LCMS=OFF \
	-DUSE_LIBBACKTRACE=OFF \
	-DUSE_LIBDRM=ON \
	-DUSE_LIBHYPHEN=OFF \
	-DUSE_SKIA=ON \
	-DUSE_SYSPROF_CAPTURE=OFF \
	-DUSE_WOFF2=ON \
	-DUSE_VULKAN=OFF

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
# The launcher links against this package's freshly built WPE library.  Build
# it only after the normal CMake staging install has made the generated
# headers, pkg-config file and library available in the target sysroot.
BRICK_WPEWEBKIT_POST_INSTALL_STAGING_HOOKS += \
	BRICK_WPEWEBKIT_BUILD_CAPTIVE_LAUNCHER

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

$(eval $(cmake-package))
