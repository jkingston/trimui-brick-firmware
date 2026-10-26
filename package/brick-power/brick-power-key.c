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
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define HOLD_MS 6000
#define BATTERY_POLL_MS 10000
#include "battery-policy.h"
#include "display-state.h"
#include "idle-policy.h"
#include "suspend-policy.h"
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
	if (idle_enabled) idle_power_sync(&idle, down, now);
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
		if (idle_enabled) idle_power_sync(&idle, false, now);
		return 0;
	}
	if (key->dropped) {
		if (event->type == EV_SYN && event->code == SYN_REPORT)
			return synchronize(fd, key, now);
		return 0;
	}
	if (event->type != EV_KEY || event->code != KEY_POWER)
		return 0;
	if (idle_enabled) idle_power_event(&idle, event->value, now);
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

static int request_suspend(void)
{
	const struct timespec pause = { .tv_nsec = 100000000 };
	int status;
	bool signaled = false;
	pid_t child = fork();

	if (child < 0)
		return -1;
	if (!child) {
		struct sigaction terminate = { .sa_handler = SIG_DFL };

		sigemptyset(&terminate.sa_mask);
		if (sigaction(SIGINT, &terminate, NULL) ||
		    sigaction(SIGTERM, &terminate, NULL) ||
		    sigaction(SIGHUP, &terminate, NULL) || stopped)
			_exit(125);
		execl("/usr/sbin/brick-suspend", "brick-suspend",
		      "--disconnect-bluetooth", (char *)NULL);
		_exit(127);
	}
	for (;;) {
		pid_t waited = waitpid(child, &status, WNOHANG);

		if (waited == child)
			break;
		if (waited < 0 && errno != EINTR)
			return -1;
		if (stopped && !signaled) {
			(void)kill(child, SIGTERM);
			signaled = true;
		}
		(void)nanosleep(&pause, NULL);
	}
	return WIFEXITED(status) && !WEXITSTATUS(status) ? 0 : -1;
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
	int fd = -1, inputs[3] = { -1, -1, -1 }, lock, result = 0;
	struct idle_policy extra[2] = { 0 };
	int dim_seconds = 300;
	int suspend_seconds = 0;
	uint32_t previous_generation = UINT32_MAX;
	bool resume_resync = false;
	int64_t gamepad_scan = 0, display_scan = 0, resume_upper = INT64_MAX;
	const char *setting = getenv("BRICK_DIM_SECONDS");
	if (setting) {
		char *end; errno = 0;
		long value = strtol(setting, &end, 10);
		if (errno || !*setting || *end || value < 0 || value > 86400) return 2;
		dim_seconds = value;
	}
	setting = getenv("BRICK_SUSPEND_SECONDS");
	if (setting) {
		char *end; errno = 0;
		long value = strtol(setting, &end, 10);
		if (errno || !*setting || *end || value < 0 || value > 86400) return 2;
		suspend_seconds = value;
	}
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
	struct display_state display;
	idle_enabled = !display_change(DISPLAY_AWAKE, 0, &display);
	idle = (struct idle_policy){ .pending = DISPLAY_QUERY, .last = monotonic_ms(),
		.last_wake = monotonic_ms() };
	while (!stopped) {
		bool suspend_due = false;
		int64_t now = monotonic_ms();
		if (now < 0) { result = 1; break; }
		if (now >= battery_scan) {
			battery_scan = now + BATTERY_POLL_MS;
			if (battery_critical("/sys/class/power_supply") && !stopped) {
				result = request_poweroff("battery at or below one percent, USB offline");
				break;
			}
		}
		/* Sysfs reads may block; never credit their time to a newly held key. */
		now = monotonic_ms();
		if (now < 0) { result = 1; break; }
		if (fd < 0 && now >= scan) {
			fd = discover();
			now = monotonic_ms();
			if (now < 0) { result = 1; break; }
			scan = now + 1000;
			if (fd >= 0 && synchronize(fd, &key, now)) {
				close(fd); fd = -1;
			}
		}
		if (idle_enabled) {
			if (now >= gamepad_scan) {
				for (unsigned i = 0; i < 3; i++) {
					if (inputs[i] >= 0) continue;
					struct idle_policy *state = i ? &extra[i - 1] : &idle;
					inputs[i] = idle_gamepad_open(i);
					now = monotonic_ms();
					if (now < 0) { result = 1; stopped = 1; break; }
					if (inputs[i] >= 0 && idle_gamepad_sync(inputs[i], state, now)) {
						close(inputs[i]); inputs[i] = -1;
					}
					idle_activity(&idle, now);
				}
				gamepad_scan = now + 1000;
			}
		}
		int timeout = 250;
		if (key.down && !key.dropped && !key.fired) {
			int64_t remaining = key.deadline - now;
			if (remaining < timeout) timeout = remaining > 0 ? remaining : 0;
		}
		struct pollfd polls[4] = { { .fd = fd, .events = POLLIN } };
		for (unsigned i = 0; i < 3; i++)
			polls[i + 1] = (struct pollfd){ .fd = inputs[i], .events = POLLIN };
		int ready = poll(polls, 4, timeout);
		if (ready < 0) {
			if (errno == EINTR) continue;
			perror("brick-power-key: poll"); result = 1; break;
		}
		if (stopped) break;
		now = monotonic_ms();
		if (now < 0) { result = 1; break; }
		if (idle_enabled) {
			int64_t before = monotonic_ms(), boot = battery_clock_ms(), after = monotonic_ms();
			if (idle_resumed(before, boot, after, &resume_upper)) {
				resume_resync = true;
				idle_power_sync(&idle, key.down, now);
				idle.last_wake = now; idle.pending = DISPLAY_AWAKE;
			}
		}
		bool gamepad_drained = true;
		for (unsigned i = 0; i < 3; i++) {
			struct idle_policy *state = i ? &extra[i - 1] : &idle;
			if (inputs[i] < 0) { gamepad_drained = false; continue; }
			bool bad = polls[i + 1].revents & (POLLHUP | POLLERR | POLLNVAL);
			bool complete = false;
			for (unsigned n = 0; !bad && n < 256; n++) {
				struct input_event e;
				ssize_t size = read(inputs[i], &e, sizeof(e));
				if (size < 0 && errno == EAGAIN) { complete = true; break; }
				if (size < 0 && errno == EINTR) break;
				if (size != sizeof(e) || idle_gamepad_event(inputs[i], state, &e, now)) bad = true;
				else if (i && (e.type == EV_KEY || (e.type == EV_SYN && e.code == SYN_DROPPED)))
					idle_activity(&idle, now);
			}
			gamepad_drained &= complete && state->gamepad_ready && !state->gamepad_dropped;
			if (bad) { close(inputs[i]); inputs[i] = -1; state->gamepad_ready = false; idle_activity(&idle, now); }
		}
		/* Fn is a latched switch: changes are activity, its on-state is not a hold. */
		gamepad_drained &= !idle_has_keys(&extra[0]);
		int drained = fd < 0 ? 0 : polls[0].revents & (POLLHUP | POLLERR | POLLNVAL) ? -1 :
			drain_events(fd, &key, now);
		if (drained < 0) {
			cancel_hold(&key);
			close(fd); fd = -1;
			scan = now + 1000;
			if (idle_enabled) idle_power_sync(&idle, false, now);
			drained = 0;
		}
		/* Discard the wake press/release sequence before arming a new short
		 * press; a held wake button must not switch the backlight off again.
		 */
		if (resume_resync && (idle.pending == DISPLAY_OFF || idle.pending == DISPLAY_TOGGLE)) idle.pending = DISPLAY_AWAKE;
		if (resume_resync && drained == 1) {
			if (synchronize(fd, &key, now)) { close(fd); fd = -1; drained = 0; }
			idle.pending = DISPLAY_AWAKE;
			resume_resync = false;
		}
		if (idle_enabled && (now >= display_scan || idle.pending != DISPLAY_QUERY)) {
			display_scan = now + 1000;
			int intent = idle.pending;
			if (!display_change(DISPLAY_QUERY, 0, &display)) {
				if (display.mode != idle.mode) idle_applied(&idle, display.mode, now);
				if (previous_generation != display.generation && display.mode == DISPLAY_AWAKE)
					idle_activity(&idle, now);
				previous_generation = display.generation;
				if (intent == DISPLAY_TOGGLE) idle.pending = intent;
				int operation = idle_resolve(&idle, now, drained == 1 && !key.dropped && !resume_resync);
				if (operation == DISPLAY_QUERY && !resume_resync && drained == 1 && gamepad_drained && !key.dropped)
					operation = idle_timeout(&idle, now, dim_seconds, !access("/tmp/stay_awake", F_OK));
				if (operation != DISPLAY_QUERY && !display_change_if(operation, 0, &display, &display))
					idle_applied(&idle, display.mode, monotonic_ms());
				else if (operation == DISPLAY_QUERY)
					suspend_due = idle_suspend_due(&idle, now, suspend_seconds,
						!access("/tmp/stay_alive", F_OK),
						gamepad_drained && !key.dropped && !resume_resync);
			}
		}
		if (suspend_due) {
			int suspended = request_suspend();
			now = monotonic_ms();
			if (now < 0) { result = 1; break; }
			idle_suspend_finished(&idle, now);
			if (!suspended) {
				resume_resync = true;
				idle_power_sync(&idle, key.down, now);
				idle.pending = DISPLAY_AWAKE;
			}
			if (!display_change(DISPLAY_AWAKE, 0, &display))
				idle_applied(&idle, display.mode, now);
			else
				fprintf(stderr, "brick-power-key: display restoration failed after suspend attempt\n");
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
	if (idle_enabled) (void)display_change(DISPLAY_AWAKE, 0, NULL);
	for (unsigned i = 0; i < 3; i++) if (inputs[i] >= 0) close(inputs[i]);
	if (fd >= 0) close(fd);
	close(lock);
	return result;
}
