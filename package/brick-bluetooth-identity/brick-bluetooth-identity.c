// SPDX-License-Identifier: GPL-2.0-only
/* Apply a provisioned XR829 public address through the Linux management API. */
#include <ctype.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifndef AF_BLUETOOTH
#define AF_BLUETOOTH 31
#endif
#define BTPROTO_HCI 1
#define HCI_CHANNEL_CONTROL 3
#define MGMT_INDEX_NONE 0xffff
#define MGMT_OP_READ_INDEX_LIST 0x0003
#define MGMT_OP_READ_INFO 0x0004
#define MGMT_OP_READ_UNCONF_INDEX_LIST 0x0036
#define MGMT_OP_READ_CONFIG_INFO 0x0037
#define MGMT_OP_SET_PUBLIC_ADDRESS 0x0039
#define MGMT_EV_CMD_COMPLETE 0x0001
#define MGMT_EV_CMD_STATUS 0x0002
#define MGMT_SETTING_POWERED (1U << 0)
#define MGMT_SETTING_BREDR (1U << 7)
#define MGMT_SETTING_LE (1U << 9)
#define MGMT_OPTION_PUBLIC_ADDRESS (1U << 1)
#define XR829_MANUFACTURER 1597
#define COMMAND_TIMEOUT_MS 5000
#define APPLY_TIMEOUT_MS 15000
#ifndef DATA_STATE
#define DATA_STATE "/run/brick-data/state"
#endif
#ifndef IDENTITY_DIRECTORY
#define IDENTITY_DIRECTORY "/data/bluetooth"
#endif
#define IDENTITY_FILE "public-address"
#define IDENTITY_TEMP ".public-address.new"

struct sockaddr_hci {
	sa_family_t family;
	uint16_t dev;
	uint16_t channel;
};

struct __attribute__((packed)) mgmt_hdr {
	uint16_t opcode;
	uint16_t index;
	uint16_t len;
};

struct __attribute__((packed)) mgmt_complete {
	uint16_t opcode;
	uint8_t status;
	uint8_t data[];
};

struct __attribute__((packed)) mgmt_info {
	uint8_t address[6];
	uint8_t version;
	uint16_t manufacturer;
	uint32_t supported;
	uint32_t current;
	uint8_t device_class[3];
	uint8_t name[249];
	uint8_t short_name[11];
};

struct __attribute__((packed)) mgmt_config_info {
	uint16_t manufacturer;
	uint32_t supported;
	uint32_t missing;
};

enum controller_state {
	CONTROLLER_NORMAL,
	CONTROLLER_UNCONFIGURED,
};

struct controller {
	uint16_t index;
	enum controller_state state;
	struct mgmt_info info;
	struct mgmt_config_info config;
};

static int hex_digit(int c)
{
	if (c >= '0' && c <= '9')
		return c - '0';
	c = tolower((unsigned char)c);
	return c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}

static int parse_address(const char *text, uint8_t address[6])
{
	uint8_t display[6];
	const char *p = text;
	int colon = -1;

	while (isspace((unsigned char)*p))
		p++;
	for (size_t i = 0; i < 6; i++) {
		if (!p[0] || !p[1])
			return -EINVAL;
		int high = hex_digit((unsigned char)p[0]);
		int low = hex_digit((unsigned char)p[1]);

		if (high < 0 || low < 0)
			return -EINVAL;
		display[i] = high << 4 | low;
		p += 2;
		if (i == 5)
			break;
		if (colon < 0)
			colon = *p == ':';
		if (colon) {
			if (*p++ != ':')
				return -EINVAL;
		} else {
			if (!isspace((unsigned char)*p))
				return -EINVAL;
			while (isspace((unsigned char)*p))
				p++;
		}
	}
	while (isspace((unsigned char)*p))
		p++;
	if (*p)
		return -EINVAL;
	if ((!display[0] && !display[1] && !display[2] && !display[3] &&
	     !display[4] && !display[5]) ||
	    (display[0] == 0xff && display[1] == 0xff && display[2] == 0xff &&
	     display[3] == 0xff && display[4] == 0xff && display[5] == 0xff))
		return -EINVAL;
	/* Bluetooth inquiry LAPs are not device addresses. */
	if (display[3] == 0x9e && display[4] == 0x8b && display[5] <= 0x3f)
		return -EINVAL;
	for (size_t i = 0; i < 6; i++)
		address[i] = display[5 - i];
	return 0;
}

static void format_address(const uint8_t address[6], char output[18])
{
	snprintf(output, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
		 address[5], address[4], address[3], address[2], address[1],
		 address[0]);
}

static int read_address_fd(int fd, int strict, uint8_t address[6])
{
	char text[129];
	struct stat st;
	ssize_t size;

	if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size > 128 ||
	    (strict && (st.st_uid != geteuid() || (st.st_mode & 0022))))
		return -EPERM;
	size = read(fd, text, sizeof(text));
	if (size < 0)
		return -errno;
	if (size == sizeof(text) || memchr(text, '\0', size))
		return -EINVAL;
	text[size] = '\0';
	return parse_address(text, address);
}

static int read_address(const char *path, int strict, uint8_t address[6])
{
	int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	int err;

	if (fd < 0)
		return -errno;
	err = read_address_fd(fd, strict, address);
	close(fd);
	return err;
}

static int read_address_at(int directory, const char *name,
			   uint8_t address[6])
{
	int fd = openat(directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	int err;

	if (fd < 0)
		return -errno;
	err = read_address_fd(fd, 1, address);
	close(fd);
	return err;
}

static int64_t milliseconds(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now))
		return -1;
	return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int mgmt_command(int fd, uint16_t opcode, uint16_t index,
			const void *payload, uint16_t payload_size,
			void *reply, size_t *reply_size)
{
	uint8_t request[sizeof(struct mgmt_hdr) + 6];
	uint8_t response[1024];
	struct mgmt_hdr *header = (void *)request;
	int64_t end = milliseconds() + COMMAND_TIMEOUT_MS;

	if (payload_size > sizeof(request) - sizeof(*header))
		return -E2BIG;
	header->opcode = htole16(opcode);
	header->index = htole16(index);
	header->len = htole16(payload_size);
	if (payload_size)
		memcpy(request + sizeof(*header), payload, payload_size);
	if (write(fd, request, sizeof(*header) + payload_size) !=
	    (ssize_t)(sizeof(*header) + payload_size))
		return -EIO;

	for (;;) {
		struct pollfd pollfd = { .fd = fd, .events = POLLIN };
		int64_t left = end - milliseconds();
		ssize_t size;

		if (left <= 0)
			return -ETIMEDOUT;
		if (poll(&pollfd, 1, left) <= 0)
			return errno == EINTR ? -EINTR : -ETIMEDOUT;
		size = read(fd, response, sizeof(response));
		if (size < (ssize_t)sizeof(struct mgmt_hdr))
			return -EPROTO;
		struct mgmt_hdr *event = (void *)response;
		uint16_t length = le16toh(event->len);

		if (size != (ssize_t)(sizeof(*event) + length))
			return -EPROTO;
		if (le16toh(event->index) != index || length < 3)
			continue;
		if (le16toh(event->opcode) == MGMT_EV_CMD_STATUS) {
			struct mgmt_complete *status =
				(void *)(response + sizeof(*event));

			if (le16toh(status->opcode) == opcode)
				return status->status ? -EREMOTEIO : -EPROTO;
			continue;
		}
		if (le16toh(event->opcode) != MGMT_EV_CMD_COMPLETE)
			continue;
		struct mgmt_complete *complete =
			(void *)(response + sizeof(*event));

		if (le16toh(complete->opcode) != opcode)
			continue;
		if (complete->status)
			return -EREMOTEIO;
		length -= 3;
		if (reply_size) {
			if (*reply_size < length)
				return -EMSGSIZE;
			memcpy(reply, complete->data, length);
			*reply_size = length;
		} else if (length) {
			return -EPROTO;
		}
		return 0;
	}
}

static int read_info(int fd, uint16_t index, struct mgmt_info *info)
{
	size_t size = sizeof(*info);
	int err = mgmt_command(fd, MGMT_OP_READ_INFO, index, NULL, 0,
			       info, &size);

	return err ?: (size == sizeof(*info) ? 0 : -EPROTO);
}

static int read_config_info(int fd, uint16_t index,
			    struct mgmt_config_info *info)
{
	size_t size = sizeof(*info);
	int err = mgmt_command(fd, MGMT_OP_READ_CONFIG_INFO, index, NULL, 0,
			       info, &size);

	return err ?: (size == sizeof(*info) ? 0 : -EPROTO);
}

static int find_in_list(int fd, uint16_t opcode, struct controller *selected,
			int *found)
{
	uint8_t list[514];
	size_t size = sizeof(list);
	uint16_t count;
	int err = mgmt_command(fd, opcode, MGMT_INDEX_NONE, NULL, 0, list, &size);

	if (err)
		return err;
	if (size < 2)
		return -EPROTO;
	memcpy(&count, list, sizeof(count));
	count = le16toh(count);
	if (size != 2 + (size_t)count * 2)
		return -EPROTO;
	for (uint16_t i = 0; i < count; i++) {
		struct controller candidate = { 0 };

		memcpy(&candidate.index, list + 2 + i * 2,
		       sizeof(candidate.index));
		candidate.index = le16toh(candidate.index);
		if (opcode == MGMT_OP_READ_INDEX_LIST) {
			candidate.state = CONTROLLER_NORMAL;
			if (read_info(fd, candidate.index, &candidate.info) ||
			    le16toh(candidate.info.manufacturer) != XR829_MANUFACTURER)
				continue;
		} else {
			candidate.state = CONTROLLER_UNCONFIGURED;
			if (read_config_info(fd, candidate.index, &candidate.config) ||
			    le16toh(candidate.config.manufacturer) != XR829_MANUFACTURER ||
			    !(le32toh(candidate.config.supported) &
			      MGMT_OPTION_PUBLIC_ADDRESS))
				continue;
		}
		if ((*found)++)
			return -EEXIST;
		*selected = candidate;
	}
	return 0;
}

static int find_xr829_once(int fd, struct controller *selected)
{
	int found = 0;
	int err = find_in_list(fd, MGMT_OP_READ_INDEX_LIST, selected, &found);

	if (!err)
		err = find_in_list(fd, MGMT_OP_READ_UNCONF_INDEX_LIST,
				   selected, &found);
	return err ?: (found == 1 ? 0 : -ENOENT);
}

static int find_xr829(int fd, int64_t end, struct controller *selected)
{
	int err;

	while (milliseconds() < end) {
		err = find_xr829_once(fd, selected);
		if (!err)
			return 0;
		if (err != -ENOENT)
			return err;
		usleep(100000);
	}
	return -ETIMEDOUT;
}

static int apply_address(const uint8_t address[6])
{
	struct sockaddr_hci socket_address = {
		.family = AF_BLUETOOTH,
		.dev = MGMT_INDEX_NONE,
		.channel = HCI_CHANNEL_CONTROL,
	};
	struct controller controller;
	int64_t end = milliseconds() + APPLY_TIMEOUT_MS;
	int fd = socket(AF_BLUETOOTH, SOCK_RAW | SOCK_CLOEXEC, BTPROTO_HCI);
	int err;

	if (fd < 0)
		return -errno;
	if (bind(fd, (struct sockaddr *)&socket_address,
		 sizeof(socket_address))) {
		err = -errno;
		goto out;
	}
	err = find_xr829(fd, end, &controller);
	if (err)
		goto out;
	if (controller.state == CONTROLLER_NORMAL &&
	    (le32toh(controller.info.supported) &
	     (MGMT_SETTING_BREDR | MGMT_SETTING_LE)) !=
	    (MGMT_SETTING_BREDR | MGMT_SETTING_LE)) {
		err = -ENOTSUP;
		goto out;
	}
	if (controller.state == CONTROLLER_UNCONFIGURED &&
	    !(le32toh(controller.config.missing) &
	      MGMT_OPTION_PUBLIC_ADDRESS)) {
		err = -EBUSY;
		goto out;
	}
	/* Registration briefly powers the controller.  S39 runs before any
	 * Bluetooth consumer, so wait for the core's automatic power-off rather
	 * than turning a legitimate boot-time race into a permanent boot failure.
	 */
	while (controller.state == CONTROLLER_NORMAL &&
	       (le32toh(controller.info.current) & MGMT_SETTING_POWERED)) {
		if (milliseconds() >= end) {
			err = -EBUSY;
			goto out;
		}
		usleep(100000);
		err = read_info(fd, controller.index, &controller.info);
		if (err)
			goto out;
		if (le16toh(controller.info.manufacturer) != XR829_MANUFACTURER) {
			err = -ENODEV;
			goto out;
		}
	}
	if (controller.state == CONTROLLER_NORMAL &&
	    !memcmp(controller.info.address, address, 6)) {
		err = 0;
		goto out;
	}
	size_t size = sizeof(uint32_t);
	uint32_t options;

	err = mgmt_command(fd, MGMT_OP_SET_PUBLIC_ADDRESS, controller.index,
			   address, 6,
			   &options, &size);
	if (err || size != sizeof(options)) {
		err = err ?: -EPROTO;
		goto out;
	}
	while (milliseconds() < end) {
		err = find_xr829_once(fd, &controller);
		if (!err && controller.state == CONTROLLER_NORMAL &&
		    !memcmp(controller.info.address, address, 6) &&
		    !(le32toh(controller.info.current) & MGMT_SETTING_POWERED)) {
			err = 0;
			goto out;
		}
		if (!err && controller.state == CONTROLLER_NORMAL &&
		    memcmp(controller.info.address, address, 6)) {
			err = -EADDRNOTAVAIL;
			goto out;
		}
		if (!err && controller.state == CONTROLLER_UNCONFIGURED) {
			err = -EIO;
			goto out;
		}
		if (err != -ENOENT)
			goto out;
		usleep(100000);
	}
	err = -ETIMEDOUT;
out:
	close(fd);
	return err;
}

static int write_all(int fd, const void *buffer, size_t size)
{
	const uint8_t *position = buffer;

	while (size) {
		ssize_t written = write(fd, position, size);

		if (written < 0)
			return -errno;
		if (!written)
			return -EIO;
		position += written;
		size -= written;
	}
	return 0;
}

static int persistent_data_active(void)
{
	char state[12];
	struct stat st;
	int fd = open(DATA_STATE, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	ssize_t size;

	if (fd < 0)
		return -errno;
	if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != geteuid() ||
	    (st.st_mode & 0022)) {
		close(fd);
		return -EPERM;
	}
	size = read(fd, state, sizeof(state));
	int saved_errno = errno;

	close(fd);
	if (size < 0)
		return -saved_errno;
	return size == 11 && !memcmp(state, "persistent\n", 11) ? 0 : -EROFS;
}

static int provision_address(const uint8_t address[6], int *created)
{
	uint8_t existing[6];
	char formatted[18];
	char line[18];
	struct stat st;
	int directory, fd, err;

	*created = 0;
	err = persistent_data_active();
	if (err)
		return err;
	directory = open(IDENTITY_DIRECTORY,
			 O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	if (directory < 0)
		return -errno;
	if (fstat(directory, &st) || !S_ISDIR(st.st_mode) ||
	    st.st_uid != geteuid() ||
	    (st.st_mode & 0022)) {
		err = -EPERM;
		goto out_directory;
	}
	err = read_address_at(directory, IDENTITY_FILE, existing);
	if (!err) {
		err = memcmp(existing, address, sizeof(existing)) ? -EEXIST : 0;
		goto out_directory;
	}
	if (err != -ENOENT)
		goto out_directory;
	fd = openat(directory, IDENTITY_TEMP,
		    O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (fd < 0) {
		err = -errno;
		goto out_directory;
	}
	format_address(address, formatted);
	memcpy(line, formatted, 17);
	line[17] = '\n';
	err = write_all(fd, line, sizeof(line));
	if (!err && fsync(fd))
		err = -errno;
	if (close(fd) && !err)
		err = -errno;
	if (err)
		goto out_temp;
	if (linkat(directory, IDENTITY_TEMP, directory, IDENTITY_FILE, 0)) {
		err = -errno;
		if (err == -EEXIST &&
		    !read_address_at(directory, IDENTITY_FILE, existing) &&
		    !memcmp(existing, address, sizeof(existing)))
			err = 0;
	} else {
		*created = 1;
	}
out_temp:
	if (unlinkat(directory, IDENTITY_TEMP, 0) && errno != ENOENT && !err)
		err = -errno;
	if (!err && fsync(directory))
		err = -errno;
	if (err && *created && !unlinkat(directory, IDENTITY_FILE, 0)) {
		*created = 0;
		fsync(directory);
	}
out_directory:
	close(directory);
	return err;
}

static int unprovision_address(const uint8_t address[6])
{
	uint8_t existing[6];
	struct stat st;
	int directory, err;

	err = persistent_data_active();
	if (err)
		return err;
	directory = open(IDENTITY_DIRECTORY,
			 O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
	if (directory < 0)
		return -errno;
	if (fstat(directory, &st) || !S_ISDIR(st.st_mode) ||
	    st.st_uid != geteuid() || (st.st_mode & 0022)) {
		err = -EPERM;
		goto out;
	}
	err = read_address_at(directory, IDENTITY_FILE, existing);
	if (err)
		goto out;
	if (memcmp(existing, address, sizeof(existing))) {
		err = -EEXIST;
		goto out;
	}
	if (unlinkat(directory, IDENTITY_FILE, 0)) {
		err = -errno;
		goto out;
	}
	err = fsync(directory) ? -errno : 0;
out:
	close(directory);
	return err;
}

int main(int argc, char **argv)
{
	uint8_t address[6];
	char formatted[18];
	int created = 0, strict, err;

	if (argc != 3 || (strcmp(argv[1], "check") && strcmp(argv[1], "apply") &&
			 strcmp(argv[1], "provision") &&
			 strcmp(argv[1], "unprovision"))) {
		fprintf(stderr,
			"Usage: %s {check|apply|provision|unprovision} ADDRESS_FILE\n",
			argv[0]);
		return 2;
	}
	strict = strcmp(argv[1], "check");
	err = read_address(argv[2], strict, address);
	if (!err && !strcmp(argv[1], "apply"))
		err = apply_address(address);
	if (!err && !strcmp(argv[1], "provision"))
		err = provision_address(address, &created);
	if (!err && !strcmp(argv[1], "unprovision"))
		err = unprovision_address(address);
	if (err) {
		fprintf(stderr, "Bluetooth identity failed: %s\n", strerror(-err));
		return 1;
	}
	format_address(address, formatted);
	if (!strcmp(argv[1], "provision"))
		printf("%s %s\n", created ? "CREATED" : "EXISTING", formatted);
	else
		puts(formatted);
	return 0;
}
