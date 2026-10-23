// SPDX-License-Identifier: GPL-2.0-only
/* Receive acknowledged 4 KiB blocks into a new file under /tmp. */
#define _DEFAULT_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static int64_t now_ms(void)
{
	struct timespec ts;

	if (clock_gettime(CLOCK_MONOTONIC, &ts))
		return -1;
	return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int wait_fd(int fd, short events, int64_t deadline)
{
	struct pollfd p = { .fd = fd, .events = events };

	for (;;) {
		int64_t now = now_ms();
		int64_t left = deadline - now;
		int ret;

		if (now < 0 || left <= 0)
			return -1;
		ret = poll(&p, 1, left > INT_MAX ? INT_MAX : (int)left);
		if (ret < 0 && errno == EINTR)
			continue;
		if (ret <= 0 || (p.revents & (POLLERR | POLLHUP | POLLNVAL)))
			return -1;
		if (p.revents & events)
			return 0;
	}
}

static int write_all(int fd, const void *buffer, size_t size)
{
	const char *p = buffer;

	while (size) {
		ssize_t n = write(fd, p, size);

		if (n < 0 && errno == EINTR)
			continue;
		if (n <= 0)
			return -1;
		p += n;
		size -= n;
	}
	return 0;
}

static int marker(const char *name, unsigned count)
{
	char line[48];
	int size = snprintf(line, sizeof(line), "%s %08x\n", name, count);

	if (wait_fd(STDOUT_FILENO, POLLOUT, now_ms() + 2000))
		return -1;
	return write_all(STDOUT_FILENO, line, size);
}

static int number(const char *text, unsigned limit, unsigned *value)
{
	char *end;
	unsigned long n;

	if (!*text || strspn(text, "0123456789") != strlen(text))
		return -1;
	errno = 0;
	n = strtoul(text, &end, 10);
	if (errno || *end || n > limit)
		return -1;
	*value = n;
	return 0;
}

int main(int argc, char **argv)
{
	unsigned char buffer[4096];
	struct termios old, raw;
	sigset_t mask, previous;
	unsigned size, seconds, received = 0;
	int file, status = 4, changed = 0;
	int64_t deadline;
	const char *name;

	if (argc != 4 || strncmp(argv[1], "/tmp/", 5))
		return 2;
	name = argv[1] + 5;
	if (!*name || strchr(name, '/') || !strcmp(name, ".") ||
	    !strcmp(name, "..") || strlen(name) > 175 ||
	    number(argv[2], 64 * 1024 * 1024, &size) ||
	    number(argv[3], 600, &seconds) || !seconds)
		return 2;
	if (tcgetattr(STDIN_FILENO, &old))
		return 3;
	/* Delay catchable signals until the bounded transfer restores the tty. */
	sigfillset(&mask);
	if (sigprocmask(SIG_BLOCK, &mask, &previous))
		return 3;
	file = open(argv[1], O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
	if (file < 0)
		goto restore_signals;
	raw = old;
	cfmakeraw(&raw);
	raw.c_cflag |= CREAD;
	if (tcsetattr(STDIN_FILENO, TCSANOW, &raw))
		goto cleanup;
	changed = 1;
	if (marker("BRX_READY", size))
		goto cleanup;
	deadline = now_ms() + seconds * 1000;
	while (received < size) {
		unsigned chunk = size - received, got = 0;

		if (chunk > sizeof(buffer))
			chunk = sizeof(buffer);
		while (got < chunk) {
			ssize_t n;

			if (wait_fd(STDIN_FILENO, POLLIN, deadline))
				goto cleanup;
			n = read(STDIN_FILENO, buffer + got, chunk - got);
			if (n < 0 && errno == EINTR)
				continue;
			if (n <= 0)
				goto cleanup;
			got += n;
		}
		if (write_all(file, buffer, got))
			goto cleanup;
		received += got;
		if (marker("BRX_ACK", received))
			goto cleanup;
	}
	status = 0;
cleanup:
	if (close(file))
		status = 4;
	if (changed && tcsetattr(STDIN_FILENO, TCSANOW, &old))
		status = 5;
	if (status)
		unlink(argv[1]);
	marker(status ? "BRX_FAIL" : "BRX_DONE", received);
restore_signals:
	sigprocmask(SIG_SETMASK, &previous, NULL);
	return status;
}
