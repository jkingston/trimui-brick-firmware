// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <errno.h>
#include <stdlib.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/input.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define HOLD_MS 6000
#define BATTERY_POLL_MS 10000
#include "battery-policy.h"
struct key_state {
	bool down, dropped, fired;
	int64_t deadline;
};
static volatile sig_atomic_t stopped;

static void stop(int sig)
{
	(void)sig;
	stopped = 1;
}

static int64_t monotonic_ms(void)
{
	struct timespec t;
	if (clock_gettime(CLOCK_MONOTONIC, &t))
		return -1;
	return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static void cancel_hold(struct key_state *key)
{
	memset(key, 0, sizeof(*key));
}

static int key_down(int fd)
{
	unsigned char bits[(KEY_MAX + 8) / 8] = { 0 };
	if (ioctl(fd, EVIOCGKEY(sizeof(bits)), bits) < 0)
		return -1;
	return !!(bits[KEY_POWER / 8] & (1U << (KEY_POWER % 8)));
}

/* A state resynchronization cannot establish how long the key was held. */
static int synchronize(int fd, struct key_state *key, int64_t now)
{
	int down = key_down(fd);
	cancel_hold(key);
	if (down < 0)
		return -1;
	key->down = down;
	if (down)
		key->deadline = now + HOLD_MS;
	return 0;
}

static int consume_event(int fd, struct key_state *key,
			 const struct input_event *event, int64_t now)
{
	if (event->type == EV_SYN && event->code == SYN_DROPPED) {
		cancel_hold(key);
		key->dropped = true;
		return 0;
	}
	if (key->dropped) {
		if (event->type == EV_SYN && event->code == SYN_REPORT)
			return synchronize(fd, key, now);
		return 0;
	}
	if (event->type != EV_KEY || event->code != KEY_POWER)
		return 0;
	if (event->value == 0)
		cancel_hold(key);
	else if (event->value == 1 && !key->down) {
		key->down = true;
		key->deadline = now + HOLD_MS;
	}
	/* Repeats and duplicate presses neither trigger nor extend the hold. */
	return 0;
}

static bool hold_due(const struct key_state *key, int64_t now)
{
	return key->down && !key->dropped && !key->fired && now >= key->deadline;
}

/* Drain release events before checking the timer; bound an event-flood turn. */
static int drain_events(int fd, struct key_state *key, int64_t now)
{
	for (unsigned count = 0; count < 256; count++) {
		struct input_event event;
		ssize_t size = read(fd, &event, sizeof(event));
		if (size < 0 && errno == EINTR) {
			if (stopped) return 0;
			continue;
		}
		if (size < 0 && errno == EAGAIN)
			return 1;
		if (size != sizeof(event) || consume_event(fd, key, &event, now))
			return -1;
	}
	return 0;
}

static int request_poweroff(const char *reason)
{
	fprintf(stderr, "brick-power-key: %s; requesting orderly poweroff\n", reason);
	/* No -f: init runs the normal shutdown path. */
	execl("/sbin/poweroff", "poweroff", (char *)NULL);
	perror("brick-power-key: poweroff");
	return 1;
}

static int discover(void)
{
	glob_t paths = { 0 };
	int found = -1;
	if (glob("/dev/input/event*", 0, NULL, &paths)) {
		globfree(&paths);
		return -1;
	}
	for (size_t i = 0; i < paths.gl_pathc; i++) {
		char name[80] = { 0 }, phys[80] = { 0 };
		unsigned char keys[(KEY_MAX + 8) / 8] = { 0 };
		struct input_id id;
		struct stat st;
		int fd = open(paths.gl_pathv[i], O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
		if (fd < 0) continue;
		if (!fstat(fd, &st) && S_ISCHR(st.st_mode) &&
		    ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name) >= 0 &&
		    ioctl(fd, EVIOCGPHYS(sizeof(phys) - 1), phys) >= 0 &&
		    !strcmp(name, "AXP2202 Power Key") && !strcmp(phys, "axp2202-pek/input0") &&
		    ioctl(fd, EVIOCGID, &id) >= 0 && id.bustype == BUS_I2C &&
		    ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keys)), keys) >= 0 &&
		    (keys[KEY_POWER / 8] & (1U << (KEY_POWER % 8)))) {
			found = fd;
			break;
		}
		close(fd);
	}
	globfree(&paths);
	return found;
}

int main(int argc, char **argv)
{
	struct key_state key = { 0 };
	struct sigaction action = { .sa_handler = stop };
	int fd = -1, lock, result = 0;
	int64_t scan = 0, battery_scan = monotonic_ms() + 1000;
	(void)argv;
	if (argc != 1) {
		fprintf(stderr, "Usage: brick-power-key\n");
		return 2;
	}
	lock = open("/run/brick-power-key.lock", O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) {
		perror("brick-power-key: lock");
		if (lock >= 0) close(lock);
		return 1;
	}
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL)) {
		perror("brick-power-key: signal");
		close(lock);
		return 1;
	}
	while (!stopped) {
		int64_t now = monotonic_ms();
		if (now < 0) { result = 1; break; }
		if (now >= battery_scan) {
			battery_scan = now + BATTERY_POLL_MS;
			if (battery_critical("/sys/class/power_supply") && !stopped) {
				result = request_poweroff("battery at or below one percent, USB offline");
				break;
			}
		}
		if (fd < 0 && now >= scan) {
			fd = discover();
			scan = now + 1000;
			if (fd >= 0 && synchronize(fd, &key, now)) {
				close(fd); fd = -1;
			}
		}
		int timeout = 250;
		if (key.down && !key.dropped && !key.fired) {
			int64_t remaining = key.deadline - now;
			if (remaining < timeout) timeout = remaining > 0 ? remaining : 0;
		}
		struct pollfd p = { .fd = fd, .events = POLLIN };
		int ready = poll(&p, 1, timeout);
		if (ready < 0) {
			if (errno == EINTR) continue;
			perror("brick-power-key: poll"); result = 1; break;
		}
		if (stopped) break;
		now = monotonic_ms();
		if (now < 0) { result = 1; break; }
		if (fd < 0) continue;
		int drained = p.revents & (POLLHUP | POLLERR | POLLNVAL) ? -1 :
			drain_events(fd, &key, now);
		if (drained < 0) {
			cancel_hold(&key);
			close(fd); fd = -1;
			scan = now + 1000;
			continue;
		}
		if (stopped || !drained || !hold_due(&key, now)) continue;
		/* A release can arrive after the drain; confirm the current input state. */
		int down = key_down(fd);
		if (down <= 0) {
			cancel_hold(&key);
			if (down < 0) { close(fd); fd = -1; scan = now + 1000; }
			continue;
		}
		if (stopped) break;
		key.fired = true;
		result = request_poweroff("six-second hold");
		break;
	}
	if (fd >= 0) close(fd);
	close(lock);
	return result;
}
