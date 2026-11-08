// SPDX-License-Identifier: GPL-2.0-only
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <linux/fs.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

#define SECTOR_SIZE 512U
#define TABLE_OFFSET 446U
#define ENTRY_SIZE 16U
#define ROOT_ENTRY 1U
#define DATA_ENTRY 2U

static uint32_t read_le32(const uint8_t *value)
{
	return (uint32_t)value[0] | ((uint32_t)value[1] << 8) |
	       ((uint32_t)value[2] << 16) | ((uint32_t)value[3] << 24);
}

static void write_le32(uint8_t *value, uint32_t number)
{
	value[0] = number;
	value[1] = number >> 8;
	value[2] = number >> 16;
	value[3] = number >> 24;
}

static int all_zero(const uint8_t *value, size_t length)
{
	while (length--)
		if (*value++)
			return 0;
	return 1;
}

static void die(const char *message)
{
	if (errno)
		fprintf(stderr, "brick-grow-data: %s: %s\n", message, strerror(errno));
	else
		fprintf(stderr, "brick-grow-data: %s\n", message);
	exit(EXIT_FAILURE);
}

int main(int argc, char **argv)
{
	uint8_t mbr[SECTOR_SIZE], *boot, *root, *data;
	uint64_t bytes, sectors, target;
	uint32_t boot_start, boot_size, root_start, root_size, start, size;
	struct stat status;
	int fd, block;

	if (argc != 2) {
		fprintf(stderr, "Usage: %s DISK\n", argv[0]);
		return EXIT_FAILURE;
	}
	fd = open(argv[1], O_RDWR | O_CLOEXEC);
	if (fd < 0)
		die("cannot open disk");
	if (fstat(fd, &status) < 0)
		die("cannot identify disk");
	block = S_ISBLK(status.st_mode);
	if (block) {
		if (ioctl(fd, BLKGETSIZE64, &bytes) < 0)
			die("cannot read disk size");
	} else if (S_ISREG(status.st_mode)) {
		bytes = status.st_size;
	} else {
		errno = 0;
		die("disk is neither a block device nor a regular test image");
	}
	if (bytes < SECTOR_SIZE || bytes % SECTOR_SIZE)
		die("disk size is not sector aligned");
	sectors = bytes / SECTOR_SIZE;
	if (pread(fd, mbr, sizeof(mbr), 0) != sizeof(mbr))
		die("cannot read MBR");
	if (mbr[510] != 0x55 || mbr[511] != 0xaa)
		die("invalid MBR signature");
	boot = mbr + TABLE_OFFSET;
	root = boot + ROOT_ENTRY * ENTRY_SIZE;
	data = boot + DATA_ENTRY * ENTRY_SIZE;
	if (boot[4] != 0x0c || root[4] != 0x83 || data[4] != 0x83)
		die("unexpected partition types");
	if (!all_zero(data + ENTRY_SIZE, ENTRY_SIZE))
		die("partitions after data are not empty");
	boot_start = read_le32(boot + 8);
	boot_size = read_le32(boot + 12);
	root_start = read_le32(root + 8);
	root_size = read_le32(root + 12);
	start = read_le32(data + 8);
	size = read_le32(data + 12);
	if (!boot_start || !boot_size || !root_start || !root_size || !start || !size ||
	    (uint64_t)boot_start + boot_size > root_start ||
	    (uint64_t)root_start + root_size > start ||
	    (uint64_t)start + size > sectors)
		die("invalid partition extents");
	target = sectors - start;
	if (target > UINT32_MAX)
		die("data partition exceeds MBR capacity");
	if (target < size)
		die("refusing to shrink data partition");
	if (target == size) {
		printf("Data partition already spans the disk (%" PRIu32 " sectors)\n", size);
		return EXIT_SUCCESS;
	}
	write_le32(data + 12, (uint32_t)target);
	if (pwrite(fd, data + 12, 4, TABLE_OFFSET + DATA_ENTRY * ENTRY_SIZE + 12) != 4)
		die("cannot update data partition extent");
	if (fsync(fd) < 0)
		die("cannot persist partition table");
	if (block && ioctl(fd, BLKRRPART) < 0)
		die("cannot reload partition table");
	printf("Extended data partition from %" PRIu32 " to %" PRIu64 " sectors\n",
	       size, target);
	if (close(fd) < 0)
		die("cannot close disk");
	return EXIT_SUCCESS;
}
