// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <dbus/dbus.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <time.h>
#include <unistd.h>

#define ADAPTER "/org/bluez/hci0"
#define TIMEOUT_MS 10000
static DBusConnection *bus;
static char *owner;
static sigset_t blocked;

static DBusMessage *call(DBusMessage *request)
{
	DBusError error = DBUS_ERROR_INIT;
	DBusMessage *reply;
	if (!request)
		return NULL;
	reply = dbus_connection_send_with_reply_and_block(bus, request,
							TIMEOUT_MS, &error);
	dbus_message_unref(request);
	if (dbus_error_is_set(&error)) {
		fprintf(stderr, "D-Bus: %s\n", error.message);
		dbus_error_free(&error);
	}
	return reply;
}

static char *bluez_owner(void)
{
	DBusMessage *m = dbus_message_new_method_call(DBUS_SERVICE_DBUS,
		DBUS_PATH_DBUS, DBUS_INTERFACE_DBUS, "GetNameOwner");
	const char *name = "org.bluez", *value;
	char *result = NULL;
	if (!m)
		return NULL;
	if (!dbus_message_append_args(m, DBUS_TYPE_STRING, &name, DBUS_TYPE_INVALID)) {
		dbus_message_unref(m);
		return NULL;
	}
	m = call(m);
	if (m && dbus_message_get_args(m, NULL, DBUS_TYPE_STRING, &value,
				      DBUS_TYPE_INVALID))
		result = strdup(value);
	if (m)
		dbus_message_unref(m);
	return result;
}

static bool same_owner(void)
{
	char *current = bluez_owner();
	bool same = current && !strcmp(current, owner);
	free(current);
	return same;
}

static int get_power(bool *powered)
{
	const char *interface = "org.bluez.Adapter1", *property = "Powered";
	DBusMessage *m = dbus_message_new_method_call(owner, ADAPTER,
		"org.freedesktop.DBus.Properties", "Get");
	DBusMessageIter iter, value;
	dbus_bool_t state;
	int ret = -1;
	if (!m)
		return -1;
	if (!dbus_message_append_args(m, DBUS_TYPE_STRING, &interface,
			DBUS_TYPE_STRING, &property, DBUS_TYPE_INVALID)) {
		dbus_message_unref(m);
		return -1;
	}
	m = call(m);
	if (!m)
		return -1;
	if (dbus_message_iter_init(m, &iter) &&
	    dbus_message_iter_get_arg_type(&iter) == DBUS_TYPE_VARIANT) {
		dbus_message_iter_recurse(&iter, &value);
		if (dbus_message_iter_get_arg_type(&value) == DBUS_TYPE_BOOLEAN) {
			dbus_message_iter_get_basic(&value, &state);
			*powered = state;
			ret = 0;
		}
	}
	dbus_message_unref(m);
	return ret;
}

static int set_power(bool powered)
{
	const char *interface = "org.bluez.Adapter1", *property = "Powered";
	DBusMessage *m = dbus_message_new_method_call(owner, ADAPTER,
		"org.freedesktop.DBus.Properties", "Set");
	DBusMessageIter iter, variant;
	dbus_bool_t state = powered;
	if (!m)
		return -1;
	dbus_message_iter_init_append(m, &iter);
	if (!dbus_message_iter_append_basic(&iter, DBUS_TYPE_STRING, &interface) ||
	    !dbus_message_iter_append_basic(&iter, DBUS_TYPE_STRING, &property) ||
	    !dbus_message_iter_open_container(&iter, DBUS_TYPE_VARIANT, "b", &variant))
		goto fail;
	if (!dbus_message_iter_append_basic(&variant, DBUS_TYPE_BOOLEAN, &state)) {
		dbus_message_iter_abandon_container(&iter, &variant);
		goto fail;
	}
	if (!dbus_message_iter_close_container(&iter, &variant))
		goto fail;
	m = call(m);
	if (!m)
		return -1;
	dbus_message_unref(m);
	return 0;
fail:
	dbus_message_unref(m);
	return -1;
}

static bool interrupted(void)
{
	sigset_t pending;
	if (sigpending(&pending))
		return true;
	return sigismember(&pending, SIGINT) || sigismember(&pending, SIGTERM) ||
	       sigismember(&pending, SIGHUP);
}

/* Coarse BlueZ state only. Admission remains the kernel PM callback's job. */
static int wait_power_off(void)
{
	struct timespec start, now, pause = { .tv_nsec = 100000000 };
	bool powered;
	if (clock_gettime(CLOCK_MONOTONIC, &start))
		return -1;
	for (;;) {
		if (interrupted() || get_power(&powered))
			return -1;
		if (!powered)
			return 0;
		if (clock_gettime(CLOCK_MONOTONIC, &now) || now.tv_sec - start.tv_sec >= 3)
			return -1;
		nanosleep(&pause, NULL);
	}
}

static int write_value(const char *path, const char *value)
{
	int fd = open(path, O_WRONLY | O_CLOEXEC);
	ssize_t result;
	int saved;
	if (fd < 0)
		return -1;
	result = write(fd, value, strlen(value));
	saved = errno;
	close(fd);
	errno = result < 0 ? saved : EIO;
	return result == (ssize_t)strlen(value) ? 0 : -1;
}

static void alarm_handler(int signal)
{
	(void)signal;
}

static int freeze_once(void)
{
	char count[32];
	int fd = open("/sys/power/wakeup_count", O_RDONLY | O_CLOEXEC);
	ssize_t n;
	if (fd < 0)
		return -1;
	/* sysfs wakeup_count ignores O_NONBLOCK; bound its interruptible wait. */
	alarm(3);
	n = read(fd, count, sizeof(count) - 1);
	alarm(0);
	close(fd);
	if (n <= 0)
		return -1;
	count[n] = '\0';
	if (interrupted()) {
		errno = EINTR;
		return -1;
	}
	if (write_value("/sys/power/wakeup_count", count))
		return -1;
	/* One attempt: the kernel's closed/unpowered gate is authoritative.
	 * Powered=false alone does not prove that UART shutdown has completed.
	 */
	return write_value("/sys/power/state", "freeze\n");
}

static bool supports_freeze(void)
{
	char states[128];
	int fd = open("/sys/power/state", O_RDONLY | O_CLOEXEC);
	ssize_t n;
	char *word, *save;
	if (fd < 0)
		return false;
	n = read(fd, states, sizeof(states) - 1);
	close(fd);
	if (n <= 0 || n == sizeof(states) - 1)
		return false;
	states[n] = '\0';
	for (word = strtok_r(states, " \t\r\n", &save); word;
	     word = strtok_r(NULL, " \t\r\n", &save))
		if (!strcmp(word, "freeze"))
			return true;
	return false;
}

static int transaction(void)
{
	bool was_on, restored;
	bool attempted_off = false;
	int result = -1;

	if (!supports_freeze()) {
		fprintf(stderr, "Kernel does not offer s2idle (freeze).\n");
		return -1;
	}
	if (get_power(&was_on))
		return -1;
	if (interrupted())
		return -1;
	if (was_on) {
		/* A timeout can still mean the daemon applied our request. */
		attempted_off = true;
		if (set_power(false))
			goto restore;
	}
	if (!same_owner() || wait_power_off() || interrupted())
		goto restore;
	result = freeze_once();
	if (result)
		fprintf(stderr, "Suspend not completed: %s\n", strerror(errno));
restore:
	if (was_on && attempted_off) {
		/* Never send restoration to a replacement daemon/controller owner.
		 * Always issue Set, even if a timed-out off request still reports
		 * Powered=true: BlueZ checks pending requests before its no-op path.
		 */
		if (!same_owner() || set_power(true) || get_power(&restored) || !restored) {
			fprintf(stderr, "Bluetooth power restoration failed; check bluetoothctl show\n");
			return -1;
		}
	}
	return result;
}

int main(int argc, char **argv)
{
	DBusError error = DBUS_ERROR_INIT;
	int lockfd, result = EXIT_FAILURE;
	sigset_t previous;
	struct sigaction action = { .sa_handler = alarm_handler };
	if (argc != 2 || strcmp(argv[1], "--disconnect-bluetooth")) {
		fprintf(stderr, "Usage: brick-suspend --disconnect-bluetooth\n"
			"Disconnects Bluetooth for s2idle; restores adapter power, not connections.\n"
			"Wi-Fi disconnects and reloads on resume.\n"
			"Do not change Bluetooth power concurrently.\n");
		return EXIT_FAILURE;
	}
	if (geteuid()) {
		fprintf(stderr, "Run as root.\n");
		return EXIT_FAILURE;
	}
	lockfd = open("/run/brick-suspend.lock", O_CREAT | O_RDWR | O_CLOEXEC, 0600);
	if (lockfd < 0 || flock(lockfd, LOCK_EX | LOCK_NB)) {
		fprintf(stderr, "Another suspend operation is active or lock unavailable.\n");
		if (lockfd >= 0)
			close(lockfd);
		return EXIT_FAILURE;
	}
	sigemptyset(&blocked);
	sigaddset(&blocked, SIGINT);
	sigaddset(&blocked, SIGTERM);
	sigaddset(&blocked, SIGHUP);
	if (sigprocmask(SIG_BLOCK, &blocked, &previous))
		goto unlock;
	/* SIGALRM interrupts a wakeup_count read without skipping cleanup. */
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGALRM, &action, NULL))
		goto signals;
	sigset_t alarm_signal;
	sigemptyset(&alarm_signal);
	sigaddset(&alarm_signal, SIGALRM);
	if (sigprocmask(SIG_UNBLOCK, &alarm_signal, NULL))
		goto signals;
	bus = dbus_bus_get_private(DBUS_BUS_SYSTEM, &error);
	if (!bus) {
		fprintf(stderr, "System D-Bus unavailable: %s\n", error.message);
		dbus_error_free(&error);
		goto signals;
	}
	dbus_connection_set_exit_on_disconnect(bus, false);
	owner = bluez_owner();
	if (owner) {
		fprintf(stderr, "Requesting s2idle; Bluetooth and Wi-Fi will disconnect.\n");
		if (!transaction())
			result = EXIT_SUCCESS;
		free(owner);
	}
	dbus_connection_close(bus);
	dbus_connection_unref(bus);
signals:
	/* Deliver deferred termination only after attempted power restoration. */
	sigprocmask(SIG_SETMASK, &previous, NULL);
unlock:
	close(lockfd);
	return result;
}
