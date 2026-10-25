// SPDX-License-Identifier: GPL-2.0-only
/* Versioned integer settings with atomic replacement and durable directory entry. */
static int read_state(const char *path, int maximum)
{
	char data[32], *end;
	long volume;
	int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
	struct stat st;
	if (fd < 0)
		return -errno;
	ssize_t n = fstat(fd, &st) || !S_ISREG(st.st_mode) ||
		st.st_size <= 0 || st.st_size >= (off_t)sizeof(data) ? -1 : read(fd, data, sizeof(data) - 1);
	close(fd);
	if (n < 0 || n != st.st_size)
		return -EINVAL;
	data[n] = 0;
	if (n < 4 || memchr(data, 0, n) || data[0] != '1' || data[1] != ' ' ||
	    data[2] < '0' || data[2] > '9')
		return -EINVAL;
	errno = 0;
	volume = strtol(data + 2, &end, 10);
	if (errno || volume > maximum || end != data + n - 1 || *end != '\n')
		return -EINVAL;
	return volume;
}

static int save_state(const char *path, int volume)
{
	char *tmp, *dir, *slash;
	char data[16];
	int fd, dirfd, result = -1;
	int length = snprintf(data, sizeof(data), "1 %d\n", volume);
	if (asprintf(&tmp, "%s.XXXXXX", path) < 0)
		return -1;
	fd = mkstemp(tmp);
	if (fd < 0) { free(tmp); return -1; }
	if (write(fd, data, length) == length && !fsync(fd) && !rename(tmp, path)) {
		dir = strdup(path);
		if (dir) {
			slash = strrchr(dir, '/');
			if (slash) slash[slash == dir ? 1 : 0] = 0;
			dirfd = open(slash ? dir : ".", O_DIRECTORY | O_RDONLY | O_CLOEXEC);
			if (dirfd >= 0) { result = fsync(dirfd); close(dirfd); }
			free(dir);
		}
	}
	close(fd);
	unlink(tmp);
	free(tmp);
	return result;
}

