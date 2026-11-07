// SPDX-License-Identifier: MIT
/* Executable evdev-to-normalized-WPE-input design spike. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define BRICK_BTN_SOUTH 0x130
#define BRICK_BTN_EAST 0x131
#define BRICK_BTN_MODE 0x13c
#define BRICK_BTN_VIEW 0x2c0
#define BRICK_BTN_FILTER 0x2c1
#define BRICK_DPAD_UP 0x220
#define BRICK_DPAD_DOWN 0x221
#define BRICK_DPAD_LEFT 0x222
#define BRICK_DPAD_RIGHT 0x223

static volatile sig_atomic_t running = 1;

static void stop(int signal_number)
{
	(void)signal_number;
	running = 0;
}

static void emit_key(unsigned code, int value)
{
	const char *state = value ? "down" : "up";

	switch (code) {
	case BRICK_BTN_EAST:
		printf("pointer-button primary %s\n", state);
		break;
	case BRICK_BTN_MODE:
		printf("keyboard enter %s\n", state);
		break;
	case BRICK_BTN_SOUTH:
		if (value == 1)
			puts("navigate back");
		break;
	case BRICK_BTN_VIEW:
		if (value)
			puts("scroll 0 -120");
		break;
	case BRICK_BTN_FILTER:
		if (value)
			puts("scroll 0 120");
		break;
	case BRICK_DPAD_UP:
	case BRICK_DPAD_DOWN:
	case BRICK_DPAD_LEFT:
	case BRICK_DPAD_RIGHT:
		if (value) {
			int step = value == 2 ? 30 : 18;
			int x = code == BRICK_DPAD_LEFT ? -step :
				code == BRICK_DPAD_RIGHT ? step : 0;
			int y = code == BRICK_DPAD_UP ? -step :
				code == BRICK_DPAD_DOWN ? step : 0;
			printf("pointer-motion %d %d\n", x, y);
		}
		break;
	default:
		break;
	}
	fflush(stdout);
}

static void resync_keys(int fd)
{
	unsigned long keys[(KEY_MAX + 8 * sizeof(long)) / (8 * sizeof(long))] = { 0 };
	const unsigned watched[] = { BRICK_BTN_EAST, BRICK_BTN_MODE };
	size_t i;

	if (ioctl(fd, EVIOCGKEY(sizeof(keys)), keys) < 0) {
		puts("resynced unavailable");
		fflush(stdout);
		return;
	}
	for (i = 0; i < sizeof(watched) / sizeof(watched[0]); i++) {
		unsigned code = watched[i];
		bool pressed = keys[code / (8 * sizeof(long))] &
			(1UL << (code % (8 * sizeof(long))));
		emit_key(code, pressed);
	}
	puts("resynced keys");
	fflush(stdout);
}

int main(int argc, char **argv)
{
	bool grab = true;
	bool dropped = false;
	const char *device;
	struct input_event event;
	int fd;
	ssize_t size;

	if (argc == 3 && !strcmp(argv[1], "--no-grab")) {
		grab = false;
		device = argv[2];
	} else if (argc == 2) {
		device = argv[1];
	} else {
		fprintf(stderr, "usage: %s [--no-grab] /dev/input/eventN\n", argv[0]);
		return 2;
	}
	fd = open(device, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		perror("open input");
		return 1;
	}
	if (grab && ioctl(fd, EVIOCGRAB, 1) < 0) {
		perror("EVIOCGRAB");
		close(fd);
		return 1;
	}
	signal(SIGINT, stop);
	signal(SIGTERM, stop);
	while (running && (size = read(fd, &event, sizeof(event))) > 0) {
		if (size != sizeof(event)) {
			fprintf(stderr, "short input event\n");
			close(fd);
			return 1;
		}
		if (event.type == EV_SYN && event.code == SYN_DROPPED) {
			dropped = true;
			puts("resync begin");
			fflush(stdout);
			continue;
		}
		if (dropped) {
			if (event.type == EV_SYN && event.code == SYN_REPORT) {
				dropped = false;
				resync_keys(fd);
			}
			continue;
		}
		if (event.type == EV_KEY && event.value >= 0 && event.value <= 2)
			emit_key(event.code, event.value);
	}
	if (size < 0 && errno != EINTR) {
		perror("read input");
		close(fd);
		return 1;
	}
	if (grab)
		(void)ioctl(fd, EVIOCGRAB, 0);
	close(fd);
	return 0;
}
