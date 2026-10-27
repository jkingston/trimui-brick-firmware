// SPDX-License-Identifier: GPL-2.0-only
/* Stock-equivalent Brick USB input-current policy using mainline sysfs state. */
#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "usb-current-policy.h"

#define USB_ONLINE "/sys/class/power_supply/axp2202-usb/online"
#define USB_LIMIT "/sys/class/power_supply/axp2202-usb/input_current_limit"
#define UDC_CLASS "/sys/class/udc"
#define POLL_US 250000

static volatile sig_atomic_t stopping;

static void stop(int signal)
{
	(void)signal;
	stopping = 1;
}

static int read_integer(const char *path, int *value)
{
	char extra, line[32];
	FILE *file;
	int parsed;

	file = fopen(path, "r");
	if (!file)
		return -errno;
	if (!fgets(line, sizeof(line), file) || ferror(file)) {
		parsed = -EIO;
	} else if (sscanf(line, "%d %c", value, &extra) != 1) {
		parsed = -EINVAL;
	} else {
		parsed = 0;
	}
	if (fclose(file) && !parsed)
		parsed = -errno;
	return parsed;
}

static int write_integer(const char *path, int value)
{
	FILE *file = fopen(path, "w");
	int ret = 0;

	if (!file)
		return -errno;
	if (fprintf(file, "%d\n", value) < 0 || fflush(file))
		ret = -EIO;
	if (fclose(file) && !ret)
		ret = -errno;
	return ret;
}

static bool udc_configured(void)
{
	struct dirent *entry;
	DIR *directory;
	char path[512], state[32];
	FILE *file;
	bool configured = false;

	directory = opendir(UDC_CLASS);
	if (!directory)
		return false;
	while ((entry = readdir(directory))) {
		if (entry->d_name[0] == '.')
			continue;
		if (snprintf(path, sizeof(path), "%s/%s/state", UDC_CLASS,
			     entry->d_name) >= (int)sizeof(path))
			continue;
		file = fopen(path, "r");
		if (!file)
			continue;
		if (fgets(state, sizeof(state), file) &&
		    !strcmp(state, "configured\n"))
			configured = true;
		fclose(file);
		if (configured)
			break;
	}
	closedir(directory);
	return configured;
}

static int boot_ms(uint64_t *milliseconds)
{
	struct timespec now;

	/* Match the vendor driver's immediate post-resume delayed-work decision. */
	if (clock_gettime(CLOCK_BOOTTIME, &now))
		return -errno;
	*milliseconds = (uint64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
	return 0;
}

int main(int argc, char **argv)
{
	struct brick_usb_current_state state = { 0 };
	struct sigaction action = { .sa_handler = stop };
	int applied = 0;

	if (argc != 1) {
		fprintf(stderr, "Usage: %s\n", argv[0]);
		return 2;
	}
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGINT, &action, NULL) || sigaction(SIGTERM, &action, NULL)) {
		perror("brick-usb-current: sigaction");
		return 1;
	}

	while (!stopping) {
		uint64_t now = 0;
		int desired, online;

		if (read_integer(USB_ONLINE, &online) || (online != 0 && online != 1) ||
		    boot_ms(&now)) {
			/* Unknown hardware state starts a new conservative boot session. */
			memset(&state, 0, sizeof(state));
			applied = 0;
		} else {
			desired = brick_usb_current(&state, online, udc_configured(), now);
			if (desired && desired != applied) {
				if (!write_integer(USB_LIMIT, desired))
					applied = desired;
				else
					fprintf(stderr, "brick-usb-current: cannot set %d uA\n",
						desired);
			}
			if (!online)
				applied = 0;
		}
		usleep(POLL_US);
	}
	return 0;
}
