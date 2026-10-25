/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef BRICK_DISPLAY_STATE_H
#define BRICK_DISPLAY_STATE_H
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <unistd.h>
#ifndef DISPLAY_BRIGHTNESS
#define DISPLAY_BRIGHTNESS "/sys/class/backlight/backlight/brightness"
#define DISPLAY_MAXIMUM "/sys/class/backlight/backlight/max_brightness"
#define DISPLAY_LOCK "/run/brick-display.lock"
#endif
/* The lock inode also contains the desired value, never a dim/off preference. */
struct display_state { uint32_t magic, generation; int mode, desired, applied; };
enum { DISPLAY_QUERY = -1, DISPLAY_AWAKE, DISPLAY_DIM, DISPLAY_OFF,
       DISPLAY_SET, DISPLAY_UP, DISPLAY_DOWN };
static inline int display_read(const char *path)
{
	char text[32], *end;
	int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) return -1;
	ssize_t size = read(fd, text, sizeof(text) - 1);
	close(fd);
	if (size <= 0) return -1;
	text[size] = 0; errno = 0;
	long value = strtol(text, &end, 10);
	return errno || end == text || end != text + size - 1 || *end != '\n' ||
		value < 0 || value > 255 ? -1 : (int)value;
}
static inline int display_write(int value)
{
	char text[16];
	int length = snprintf(text, sizeof(text), "%d\n", value);
	int fd = open(DISPLAY_BRIGHTNESS, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) return -1;
	int result = write(fd, text, length) == length ? 0 : -1;
	close(fd); return result;
}
static inline int display_level(int level) { return 4 + 172 * level / 10; }
/* All cooperating writers use this lock. Direct root sysfs writes are detected
 * best-effort; they cannot participate in this userspace transaction.
 */
static inline int display_change_if(int operation, int value, struct display_state *out,
				    const struct display_state *expected)
{
	struct display_state state, old;
	int fd = open(DISPLAY_LOCK, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (fd < 0) return -1;
	int result = -1;
	if (flock(fd, LOCK_EX) || display_read(DISPLAY_MAXIMUM) != 255) goto done;
	int raw = display_read(DISPLAY_BRIGHTNESS);
	if (raw < 0) goto done;
	if (pread(fd, &state, sizeof(state), 0) != sizeof(state) ||
	    state.magic != 0x42524432 || state.mode < 0 || state.mode > 2 ||
	    state.desired < 0 || state.desired > 255 || state.applied != raw)
		state = (struct display_state){ 0x42524432, 0, DISPLAY_AWAKE, raw, raw };
	if (expected && memcmp(expected, &state, sizeof(state))) {
		if (out) *out = state;
		result = 1; goto done;
	}
	old = state;
	if (operation == DISPLAY_SET) {
		if (value < 0 || value > 255) goto done;
		state.desired = value; state.mode = DISPLAY_AWAKE;
	} else if (operation == DISPLAY_UP || operation == DISPLAY_DOWN) {
		for (int i = operation == DISPLAY_UP ? 0 : 10; i >= 0 && i <= 10;
		     i += operation == DISPLAY_UP ? 1 : -1) {
			if ((operation == DISPLAY_UP && display_level(i) > state.desired) ||
			    (operation == DISPLAY_DOWN && display_level(i) < state.desired)) {
				state.desired = display_level(i); break;
			}
		}
		state.mode = DISPLAY_AWAKE;
	} else if (operation >= DISPLAY_AWAKE && operation <= DISPLAY_OFF) {
		state.mode = operation;
	} else if (operation != DISPLAY_QUERY) goto done;
	if (operation != DISPLAY_QUERY) state.generation++;
	state.applied = state.mode == DISPLAY_OFF ? 0 :
		state.mode == DISPLAY_DIM ? (state.desired ? 1 : 0) : state.desired;
	/* Publish intent before the hardware write: restart can recover a dim/off
	 * value without mistaking it for the user's desired brightness.
	 */
	if (pwrite(fd, &state, sizeof(state), 0) != sizeof(state)) goto done;
	if (state.applied != raw && display_write(state.applied)) {
		(void)pwrite(fd, &old, sizeof(old), 0); goto done;
	}
	if (out) *out = state;
	result = 0;
done:
	close(fd); return result;
}
static inline int display_change(int operation, int value, struct display_state *out)
{
	return display_change_if(operation, value, out, NULL);
}

#endif
