/* SPDX-License-Identifier: MIT
 *
 * Exercise Mesa's surfaceless EGL software path and report render/readback
 * cost and maximum RSS. This is a render probe, not a display backend.
 */
#define _POSIX_C_SOURCE 200809L
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>

#ifndef EGL_PLATFORM_SURFACELESS_MESA
#define EGL_PLATFORM_SURFACELESS_MESA 0x31DD
#endif

static uint64_t now_ns(void)
{
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts)) {
		perror("clock_gettime");
		exit(2);
	}
	return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

static void egl_fail(const char *what)
{
	fprintf(stderr, "%s: EGL error 0x%04x\n", what, eglGetError());
	exit(1);
}

int main(int argc, char **argv)
{
	const int width = argc > 1 ? atoi(argv[1]) : 1024;
	const int height = argc > 2 ? atoi(argv[2]) : 768;
	const int frames = argc > 3 ? atoi(argv[3]) : 120;
	PFNEGLGETPLATFORMDISPLAYEXTPROC get_platform_display;
	EGLDisplay display;
	EGLConfig config;
	EGLint count;
	EGLSurface surface;
	EGLContext context;
	unsigned char *pixels;
	uint64_t render_start, render_end, read_start, read_end;
	struct rusage usage;

	if (width <= 0 || height <= 0 || frames <= 0)
		return fprintf(stderr, "usage: %s [width height frames]\n", argv[0]), 2;

	get_platform_display = (void *)eglGetProcAddress("eglGetPlatformDisplayEXT");
	if (!get_platform_display)
		return fprintf(stderr, "EGL_EXT_platform_base is unavailable\n"), 1;
	display = get_platform_display(EGL_PLATFORM_SURFACELESS_MESA,
		EGL_DEFAULT_DISPLAY, NULL);
	if (display == EGL_NO_DISPLAY || !eglInitialize(display, NULL, NULL))
		egl_fail("surfaceless eglInitialize");

	{
		const EGLint attributes[] = {
			EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
			EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
			EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8,
			EGL_ALPHA_SIZE, 8, EGL_NONE
		};
		const EGLint pbuffer[] = {
			EGL_WIDTH, width, EGL_HEIGHT, height, EGL_NONE
		};
		const EGLint context_attributes[] = {
			EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE
		};
		if (!eglChooseConfig(display, attributes, &config, 1, &count) || !count)
			egl_fail("eglChooseConfig");
		surface = eglCreatePbufferSurface(display, config, pbuffer);
		context = eglCreateContext(display, config, EGL_NO_CONTEXT,
			context_attributes);
	}
	if (surface == EGL_NO_SURFACE || context == EGL_NO_CONTEXT)
		egl_fail("create surface/context");
	if (!eglMakeCurrent(display, surface, surface, context))
		egl_fail("eglMakeCurrent");

	pixels = malloc((size_t)width * height * 4);
	if (!pixels)
		return perror("malloc"), 1;
	printf("EGL vendor: %s\nGL renderer: %s\nGL version: %s\n",
		eglQueryString(display, EGL_VENDOR), glGetString(GL_RENDERER),
		glGetString(GL_VERSION));

	glViewport(0, 0, width, height);
	render_start = now_ns();
	for (int frame = 0; frame < frames; frame++) {
		glClearColor((frame & 31) / 31.0f, 0.25f, 0.5f, 1.0f);
		glClear(GL_COLOR_BUFFER_BIT);
		glFinish();
	}
	render_end = now_ns();
	read_start = now_ns();
	glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
	glFinish();
	read_end = now_ns();
	if (glGetError() != GL_NO_ERROR)
		return fprintf(stderr, "OpenGL error after readback\n"), 1;
	getrusage(RUSAGE_SELF, &usage);
	printf("surface: %dx%d, frames: %d, bytes/frame: %zu\n",
		width, height, frames, (size_t)width * height * 4);
	printf("clear+finish: %.3f ms/frame; one readback: %.3f ms; maxrss: %ld KiB\n",
		(double)(render_end - render_start) / frames / 1e6,
		(double)(read_end - read_start) / 1e6, usage.ru_maxrss);
	printf("readback checksum bytes: %02x %02x %02x %02x\n",
		pixels[0], pixels[1], pixels[2], pixels[3]);

	free(pixels);
	eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
	eglDestroyContext(display, context);
	eglDestroySurface(display, surface);
	eglTerminate(display);
	return 0;
}

