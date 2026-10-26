/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef BRICK_LED_POLICY_H
#define BRICK_LED_POLICY_H

#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <unistd.h>

#ifndef LED_CLASS_PATH
#define LED_CLASS_PATH "/sys/class/leds"
#endif
#define BRICK_LED_COUNT 23
#ifndef BRICK_LED_LOCK
#define BRICK_LED_LOCK "/run/brick-led-policy.lock"
#endif

struct led_policy_state {
	unsigned char brightness[BRICK_LED_COUNT];
	int lock;
	bool restore;
};

static int led_policy_path(char *path, size_t size, unsigned int index,
			   const char *attribute)
{
	int length = snprintf(path, size, "%s/rgb:indicator-%u/%s",
			      LED_CLASS_PATH, index, attribute);

	return length > 0 && (size_t)length < size ? 0 : -ENAMETOOLONG;
}

static int led_policy_read(unsigned int index, const char *attribute)
{
	char path[160], text[32], *end;
	int fd;
	ssize_t size;
	long value;

	if (led_policy_path(path, sizeof(path), index, attribute))
		return -ENAMETOOLONG;
	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return -errno;
	size = read(fd, text, sizeof(text) - 1);
	close(fd);
	if (size <= 0 || size == sizeof(text) - 1)
		return -EIO;
	text[size] = '\0';
	errno = 0;
	value = strtol(text, &end, 10);
	if (errno || end == text || (*end != '\n' && *end != '\0') ||
	    (*end == '\n' && end[1] != '\0') || value < 0 || value > 255)
		return -EINVAL;
	return value;
}

static int led_policy_trigger_is_none(unsigned int index)
{
	char path[160], text[512];
	int fd;
	ssize_t size;

	if (led_policy_path(path, sizeof(path), index, "trigger"))
		return -ENAMETOOLONG;
	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return -errno;
	size = read(fd, text, sizeof(text) - 1);
	close(fd);
	if (size <= 0 || size == sizeof(text) - 1)
		return -EIO;
	text[size] = '\0';
	return strstr(text, "[none]") ? 1 : 0;
}

static int led_policy_write(unsigned int index, unsigned int value)
{
	char path[160], text[16];
	int fd, length, result = 0;
	ssize_t written;

	if (led_policy_path(path, sizeof(path), index, "brightness"))
		return -ENAMETOOLONG;
	length = snprintf(text, sizeof(text), "%u\n", value);
	fd = open(path, O_WRONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return -errno;
	written = write(fd, text, length);
	if (written != length)
		result = written < 0 ? -errno : -EIO;
	close(fd);
	return result;
}

static int led_policy_quiesce(struct led_policy_state *state)
{
	int value;

	state->lock = open(BRICK_LED_LOCK,
			   O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (state->lock < 0)
		return -errno;
	if (flock(state->lock, LOCK_EX | LOCK_NB)) {
		int result = -errno;

		close(state->lock);
		state->lock = -1;
		return result;
	}
	for (unsigned int i = 0; i < BRICK_LED_COUNT; i++) {
		value = led_policy_trigger_is_none(i);
		/* brightness=0 detaches a trigger. Refuse to lose trigger-owned
		 * state rather than pretending it is a simple brightness value.
		 */
		if (value != 1)
			return value < 0 ? value : -EBUSY;
		value = led_policy_read(i, "max_brightness");
		if (value != 255)
			return value < 0 ? value : -EINVAL;
		value = led_policy_read(i, "brightness");
		if (value < 0)
			return value;
		state->brightness[i] = value;
	}
	/* A failed write may still have changed the controller state. */
	state->restore = true;
	for (unsigned int i = 0; i < BRICK_LED_COUNT; i++) {
		if (state->brightness[i] && led_policy_write(i, 0))
			return -EIO;
		if (led_policy_read(i, "brightness"))
			return -EIO;
	}
	return 0;
}

static int led_policy_restore(struct led_policy_state *state)
{
	int result = 0;

	if (state->restore) {
		for (unsigned int i = 0; i < BRICK_LED_COUNT; i++) {
			int current = led_policy_read(i, "brightness");

			/* Do not detach a trigger acquired while userspace slept. */
			if (led_policy_trigger_is_none(i) != 1 || current < 0 ||
			    (current != state->brightness[i] &&
			     led_policy_write(i, state->brightness[i])) ||
			    led_policy_read(i, "brightness") != state->brightness[i])
				result = -1;
		}
	}
	if (state->lock >= 0) {
		if (flock(state->lock, LOCK_UN))
			result = -1;
		close(state->lock);
		state->lock = -1;
	}
	state->restore = false;
	return result;
}

#endif
