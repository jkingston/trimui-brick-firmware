/* SPDX-License-Identifier: MIT
 *
 * Non-modesetting probe for Mesa kms_swrast on a DRM primary node. Success
 * requires rendering a GBM surface and exporting its front buffer as DMA-BUF,
 * the minimum transport shape needed by WPEPlatform's built-in DRM view.
 */
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <errno.h>
#include <fcntl.h>
#include <gbm.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

static uint64_t now_ns(void)
{
	struct timespec value;
	clock_gettime(CLOCK_MONOTONIC, &value);
	return (uint64_t)value.tv_sec * 1000000000ULL + value.tv_nsec;
}

static void fail(const char *what)
{
	fprintf(stderr, "%s failed (EGL 0x%04x)\n", what, eglGetError());
	exit(1);
}

int main(int argc, char **argv)
{
	const char *node = argc > 1 ? argv[1] : "/dev/dri/card0";
	const int width = argc > 2 ? atoi(argv[2]) : 1024;
	const int height = argc > 3 ? atoi(argv[3]) : 768;
	const int frames = argc > 4 ? atoi(argv[4]) : 120;
	PFNEGLGETPLATFORMDISPLAYEXTPROC get_platform_display;
	struct gbm_device *gbm;
	struct gbm_surface *gbm_surface;
	struct gbm_bo *bo;
	struct gbm_bo *plain_bo;
	struct gbm_bo *imported_bo;
	struct gbm_import_fd_modifier_data import_data = { 0 };
	EGLDisplay display;
	EGLConfig config;
	EGLConfig configs[128];
	EGLContext context;
	EGLSurface surface;
	EGLint count;
	EGLint visual_id;
	EGLint surface_type;
	EGLint renderable_type;
	int drm_fd, dma_fd;
	uint64_t start, finish;
	struct rusage usage;
	if (width <= 0 || height <= 0 || frames <= 0)
		return fprintf(stderr, "usage: %s [drm-node width height frames]\n",
			argv[0]), 2;

	drm_fd = open(node, O_RDWR | O_CLOEXEC);
	if (drm_fd < 0)
		return perror(node), 1;
	gbm = gbm_create_device(drm_fd);
	if (!gbm)
		return perror("gbm_create_device"), 1;
	printf("GBM backend: %s\n", gbm_device_get_backend_name(gbm));
	plain_bo = gbm_bo_create(gbm, width, height, GBM_FORMAT_XRGB8888,
		GBM_BO_USE_RENDERING | GBM_BO_USE_SCANOUT);
	if (!plain_bo) {
		printf("plain render+scanout BO: allocation failed errno=%d\n", errno);
	} else {
		dma_fd = gbm_bo_get_fd(plain_bo);
		printf("plain render+scanout BO: stride=%u dma_fd=%d errno=%d\n",
			gbm_bo_get_stride(plain_bo), dma_fd, dma_fd < 0 ? errno : 0);
		if (dma_fd >= 0)
			close(dma_fd);
		gbm_bo_destroy(plain_bo);
	}
	gbm_surface = gbm_surface_create(gbm, width, height, GBM_FORMAT_XRGB8888,
		GBM_BO_USE_RENDERING | GBM_BO_USE_SCANOUT);
	if (!gbm_surface)
		return perror("gbm_surface_create(rendering|scanout)"), 1;

	get_platform_display = (void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
	if (!get_platform_display)
		return fprintf(stderr, "EGL_EXT_platform_base unavailable\n"), 1;
	display = get_platform_display(EGL_PLATFORM_GBM_KHR, gbm, NULL);
	if (display == EGL_NO_DISPLAY || !eglInitialize(display, NULL, NULL))
		fail("GBM eglInitialize");
	{
		const EGLint context_attributes[] = {
			EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE
		};
		if (!eglGetConfigs(display, configs, 128, &count))
			fail("eglGetConfigs");
		config = NULL;
		for (int i = 0; i < count; i++) {
			eglGetConfigAttrib(display, configs[i], EGL_NATIVE_VISUAL_ID,
				&visual_id);
			eglGetConfigAttrib(display, configs[i], EGL_SURFACE_TYPE,
				&surface_type);
			eglGetConfigAttrib(display, configs[i], EGL_RENDERABLE_TYPE,
				&renderable_type);
			if (visual_id == GBM_FORMAT_XRGB8888 &&
			    (surface_type & EGL_WINDOW_BIT) &&
			    (renderable_type & EGL_OPENGL_ES2_BIT)) {
				config = configs[i];
				break;
			}
		}
		if (!config)
			fail("find XRGB8888 window config");
		if (!eglGetConfigAttrib(display, config, EGL_NATIVE_VISUAL_ID, &visual_id))
			fail("eglGetConfigAttrib(EGL_NATIVE_VISUAL_ID)");
		printf("EGL native visual: 0x%08x\n", visual_id);
		context = eglCreateContext(display, config, EGL_NO_CONTEXT,
			context_attributes);
		if (context == EGL_NO_CONTEXT)
			fail("eglCreateContext");
		surface = eglCreateWindowSurface(display, config,
			(EGLNativeWindowType)gbm_surface, NULL);
		if (surface == EGL_NO_SURFACE)
			fail("eglCreateWindowSurface");
	}
	if (!eglMakeCurrent(display, surface, surface, context))
		fail("eglMakeCurrent");
	glViewport(0, 0, width, height);
	start = now_ns();
	for (int i = 0; i < frames; i++) {
		glClearColor((i & 31) / 31.0f, 0.5f, 0.875f, 1.0f);
		glClear(GL_COLOR_BUFFER_BIT);
		if (!eglSwapBuffers(display, surface))
			fail("eglSwapBuffers");
	}
	glFinish();
	finish = now_ns();
	getrusage(RUSAGE_SELF, &usage);
	bo = gbm_surface_lock_front_buffer(gbm_surface);
	if (!bo)
		return perror("gbm_surface_lock_front_buffer"), 1;
	dma_fd = gbm_bo_get_fd(bo);
	printf("renderer=%s size=%ux%u stride=%u format=0x%08x dma_fd=%d\n",
		glGetString(GL_RENDERER), gbm_bo_get_width(bo), gbm_bo_get_height(bo),
		gbm_bo_get_stride(bo), gbm_bo_get_format(bo), dma_fd);
	printf("render+swap: %.3f ms/frame over %d frames; maxrss=%ld KiB\n",
		(double)(finish - start) / frames / 1e6, frames, usage.ru_maxrss);
	if (dma_fd < 0)
		return fprintf(stderr, "FAIL: rendered BO is not DMA-BUF exportable\n"), 1;
	import_data.width = gbm_bo_get_width(bo);
	import_data.height = gbm_bo_get_height(bo);
	import_data.format = gbm_bo_get_format(bo);
	import_data.num_fds = 1;
	import_data.fds[0] = dma_fd;
	import_data.strides[0] = gbm_bo_get_stride(bo);
	import_data.offsets[0] = gbm_bo_get_offset(bo, 0);
	import_data.modifier = gbm_bo_get_modifier(bo);
	imported_bo = gbm_bo_import(gbm, GBM_BO_IMPORT_FD_MODIFIER, &import_data,
		GBM_BO_USE_SCANOUT);
	printf("WPE-style modifier import: modifier=0x%016llx imported=%s\n",
		(unsigned long long)import_data.modifier, imported_bo ? "yes" : "no");
	if (!imported_bo)
		return fprintf(stderr, "FAIL: rendered DMA-BUF cannot be re-imported for scanout\n"), 1;
	gbm_bo_destroy(imported_bo);
	close(dma_fd);
	gbm_surface_release_buffer(gbm_surface, bo);
	eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
	eglDestroySurface(display, surface);
	eglDestroyContext(display, context);
	eglTerminate(display);
	gbm_surface_destroy(gbm_surface);
	gbm_device_destroy(gbm);
	close(drm_fd);
	return 0;
}
