/* SPDX-License-Identifier: GPL-2.0-only */
/* Only freshly read, validated AXP2202 telemetry can request shutdown. */
static int64_t battery_clock_ms(void)
{
	struct timespec t;
	if (clock_gettime(CLOCK_BOOTTIME, &t))
		return -1;
	return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}

static int battery_text(int directory, const char *name, char *text, size_t size)
{
	int fd = openat(directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) return -1;
	ssize_t count = read(fd, text, size);
	close(fd);
	if (count <= 0 || (size_t)count >= size) return -1;
	/* Reject embedded NULs and require the complete sysfs line. */
	if (memchr(text, '\0', count) || text[count - 1] != '\n') return -1;
	text[count - 1] = '\0';
	return 0;
}

static int battery_capacity(int directory)
{
	char text[16];
	if (battery_text(directory, "capacity", text, sizeof(text))) return -1;
	if (!text[0]) return -1;
	for (size_t i = 0; text[i]; i++)
		if (text[i] < '0' || text[i] > '9') return -1;
	char *end;
	errno = 0;
	long value = strtol(text, &end, 10);
	return errno || *end || value > 100 ? -1 : (int)value;
}

static bool battery_offline(int battery, int usb)
{
	char online[8], status[32];
	if (battery_text(usb, "online", online, sizeof(online)) || strcmp(online, "0") ||
	    battery_text(battery, "status", status, sizeof(status))) return false;
	/* This driver reports Unknown for idle; offline supplies disambiguate it.
	 * Unknown alone is never sufficient. Charging/full/other states inhibit.
	 */
	return !strcmp(status, "Unknown") || !strcmp(status, "Discharging");
}

static bool battery_critical(const char *root)
{
	int64_t start = battery_clock_ms();
	if (start < 0) return false;
	int directory = open(root, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	if (directory < 0) return false;
	/* Class entries are symlinks; pin each supply directory during the sample. */
	int battery = openat(directory, "axp2202-battery", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	int usb = openat(directory, "axp2202-usb", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
	close(directory);
	bool critical = false;
	if (battery < 0 || usb < 0 || !battery_offline(battery, usb)) goto out;
	int capacity = battery_capacity(battery);
	if (capacity < 0 || capacity > 1) goto out;
	/* Re-read, never reuse a prior sample or infer charge state from capacity. */
	capacity = battery_capacity(battery);
	if (capacity < 0 || capacity > 1 || !battery_offline(battery, usb)) goto out;
	int64_t end = battery_clock_ms();
	/* Reject delayed acquisition, including time suspended between sysfs reads. */
	critical = end >= start && end - start <= 1000;
out:
	if (battery >= 0) close(battery);
	if (usb >= 0) close(usb);
	return critical;
}
