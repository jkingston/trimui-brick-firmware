// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/input.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "settings.h"

#define BACKLIGHT "/sys/class/backlight/backlight/"
static volatile sig_atomic_t stopped;
static void stop(int sig) { (void)sig; stopped = 1; }
static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

/* Shipped keymon's eleven user levels, independent of the kernel PWM scale. */
static int raw_level(int level) { return 4 + 172 * level / 10; }

static int read_attribute(const char *path)
{
	char data[32], *end;
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return -errno;
	ssize_t size = read(fd, data, sizeof(data) - 1);
	close(fd);
	if (size <= 0) return -EIO;
	data[size] = 0;
	errno = 0;
	long value = strtol(data, &end, 10);
	if (errno || end == data || end != data + size - 1 || *end != '\n' ||
	    value < 0 || value > 255) return -EINVAL;
	return value;
}

static int set_level(int level)
{
	char data[16];
	int length = snprintf(data, sizeof(data), "%d\n", raw_level(level));
	int fd = open(BACKLIGHT "brightness", O_WRONLY | O_CLOEXEC);
	if (fd < 0) return -errno;
	int result = write(fd, data, length) == length ? 0 : -EIO;
	close(fd);
	return result;
}

/* Read the current value for every shortcut so frontend changes are respected. */
static int next_level(int raw, bool up)
{
	if (up) {
		for (int i = 0; i <= 10; i++) if (raw_level(i) > raw) return i;
	} else {
		for (int i = 10; i >= 0; i--) if (raw_level(i) < raw) return i;
	}
	return -1;
}

struct keys { bool start, select, dropped; };
static int sync_keys(int fd, struct keys *keys)
{
	unsigned char bits[(KEY_MAX + 8) / 8] = { 0 };
	if (ioctl(fd, EVIOCGKEY(sizeof(bits)), bits) < 0) return -errno;
	keys->start = !!(bits[BTN_START / 8] & (1U << (BTN_START % 8)));
	keys->select = !!(bits[BTN_SELECT / 8] & (1U << (BTN_SELECT % 8)));
	keys->dropped = false;
	return 0;
}

static int discover(struct keys *keys)
{
	glob_t paths = { 0 };
	int found = -1;
	if (glob("/dev/input/event*", 0, NULL, &paths)) goto out;
	for (size_t i = 0; i < paths.gl_pathc; i++) {
		char name[128] = { 0 };
		int fd = open(paths.gl_pathv[i], O_RDONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0) continue;
		if (ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name) >= 0 &&
		    !strcmp(name, "TrimUI Brick Gamepad") && !sync_keys(fd, keys)) {
			found = fd;
			break;
		}
		close(fd);
	}
out:
	globfree(&paths);
	return found;
}

/* No synthesized repeats or EVIOCGRAB: applications retain normal input. */
static int key_direction(struct keys *keys, const struct input_event *e)
{
	if (e->type != EV_KEY || e->value < 0 || e->value > 2) return 0;
	if (e->code == BTN_START) keys->start = e->value != 0;
	if (e->code == BTN_SELECT) keys->select = e->value != 0;
	if (e->value != 1 || !keys->start || keys->select) return 0;
	return e->code == BTN_TR ? 1 : e->code == BTN_TL ? -1 : 0;
}

static int event_direction(int fd, struct keys *keys, const struct input_event *e,
			   int *direction)
{
	*direction = 0;
	if (e->type == EV_SYN && e->code == SYN_DROPPED) keys->dropped = true;
	if (keys->dropped) {
		if (e->type == EV_SYN && e->code == SYN_REPORT) return sync_keys(fd, keys);
		return 0;
	}
	*direction = key_direction(keys, e);
	return 0;
}

int main(int argc, char **argv)
{
	struct keys keys = { 0 };
	int fd = -1, lock, saved, result = 1;
	double scan = 0, save = 0;
	bool dirty = false;
	if (argc != 2) {
		fprintf(stderr, "Usage: brick-brightness STATEFILE\n");
		return 2;
	}
	lock = open("/run/brick-brightness.lock", O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) return 1;
	/* Reject older seven-level kernels instead of silently using wrong units. */
	if (read_attribute(BACKLIGHT "max_brightness") != 255) {
		fprintf(stderr, "brick-brightness: matching 0..255 backlight required\n");
		goto out;
	}
	saved = read_state(argv[1], 10);
	if (saved >= 0) {
		if (set_level(saved)) goto out;
	} else if (saved != -ENOENT) {
		fprintf(stderr, "brick-brightness: invalid saved settings\n");
		goto out;
	}
	signal(SIGINT, stop); signal(SIGTERM, stop);
	while (!stopped) {
		if (fd < 0 && now() >= scan) { fd = discover(&keys); scan = now() + 1; }
		struct pollfd pfd = { fd, POLLIN, 0 };
		if (poll(&pfd, 1, 100) < 0 && errno != EINTR) goto out;
		if (pfd.revents & (POLLHUP | POLLERR | POLLNVAL)) {
			close(fd); fd = -1; keys = (struct keys){ 0 };
		} else if (pfd.revents & POLLIN) {
			struct input_event e;
			for (unsigned n = 0; n < 64 && !stopped; n++) {
				ssize_t size = read(fd, &e, sizeof(e));
				if (size != sizeof(e)) {
					if (size < 0 && (errno == EAGAIN || errno == EINTR)) break;
					close(fd); fd = -1; keys = (struct keys){ 0 }; break;
				}
				int direction;
				if (event_direction(fd, &keys, &e, &direction)) {
					close(fd); fd = -1; keys = (struct keys){ 0 }; break;
				}
				if (!direction) continue;
				int raw = read_attribute(BACKLIGHT "brightness");
				if (raw < 0) goto out;
				int next = next_level(raw, direction > 0);
				if (next < 0) continue;
				if (set_level(next)) goto out;
				saved = next; dirty = true; save = now() + 2;
			}
		}
		if (dirty && now() >= save) {
			if (!save_state(argv[1], saved)) dirty = false;
			else { perror("brick-brightness: save"); save = now() + 5; }
		}
	}
	result = 0;
out:
	if (dirty && save_state(argv[1], saved)) perror("brick-brightness: final save");
	if (fd >= 0) close(fd);
	close(lock);
	return result;
}
