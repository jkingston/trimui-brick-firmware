// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
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

#include "led-policy.h"

#define ADAPTER "/org/bluez/hci0"
#define TIMEOUT_MS 10000
#define AUDIO_POLICY_LOCK "/run/brick-audio-policy.lock"
#ifndef BRICK_POWER_STATE_PATH
#define BRICK_POWER_STATE_PATH "/sys/power/state"
#endif
#ifndef BRICK_MEM_SLEEP_PATH
#define BRICK_MEM_SLEEP_PATH "/sys/power/mem_sleep"
#endif
static DBusConnection *bus;
static char *owner;
static sigset_t blocked;

struct audio_state {
	snd_ctl_t *ctl;
	long gains[2];
	bool speaker;
	int lock;
	bool restore;
};

static int audio_integer_control(snd_ctl_t *ctl, const char *name,
				 long *values, unsigned int count, bool write)
{
	snd_ctl_elem_id_t *id;
	snd_ctl_elem_info_t *info;
	snd_ctl_elem_value_t *value;
	int err;

	snd_ctl_elem_id_alloca(&id);
	snd_ctl_elem_info_alloca(&info);
	snd_ctl_elem_value_alloca(&value);
	snd_ctl_elem_id_set_interface(id, SND_CTL_ELEM_IFACE_MIXER);
	snd_ctl_elem_id_set_name(id, name);
	snd_ctl_elem_info_set_id(info, id);
	if ((err = snd_ctl_elem_info(ctl, info)) < 0)
		return err;
	if (snd_ctl_elem_info_get_type(info) != SND_CTL_ELEM_TYPE_INTEGER ||
	    snd_ctl_elem_info_get_count(info) != count)
		return -EINVAL;
	snd_ctl_elem_value_set_id(value, id);
	if (!write) {
		if ((err = snd_ctl_elem_read(ctl, value)) < 0)
			return err;
		for (unsigned int i = 0; i < count; i++)
			values[i] = snd_ctl_elem_value_get_integer(value, i);
		return 0;
	}
	for (unsigned int i = 0; i < count; i++) {
		if (values[i] < snd_ctl_elem_info_get_min(info) ||
		    values[i] > snd_ctl_elem_info_get_max(info))
			return -ERANGE;
		snd_ctl_elem_value_set_integer(value, i, values[i]);
	}
	return snd_ctl_elem_write(ctl, value);
}

static int audio_switch_control(snd_ctl_t *ctl, const char *name,
				bool *enabled, bool write)
{
	snd_ctl_elem_id_t *id;
	snd_ctl_elem_info_t *info;
	snd_ctl_elem_value_t *value;
	int err;

	snd_ctl_elem_id_alloca(&id);
	snd_ctl_elem_info_alloca(&info);
	snd_ctl_elem_value_alloca(&value);
	snd_ctl_elem_id_set_interface(id, SND_CTL_ELEM_IFACE_MIXER);
	snd_ctl_elem_id_set_name(id, name);
	snd_ctl_elem_info_set_id(info, id);
	if ((err = snd_ctl_elem_info(ctl, info)) < 0)
		return err;
	if (snd_ctl_elem_info_get_type(info) != SND_CTL_ELEM_TYPE_BOOLEAN ||
	    snd_ctl_elem_info_get_count(info) != 1)
		return -EINVAL;
	snd_ctl_elem_value_set_id(value, id);
	if (!write) {
		if ((err = snd_ctl_elem_read(ctl, value)) < 0)
			return err;
		*enabled = !!snd_ctl_elem_value_get_boolean(value, 0);
		return 0;
	}
	snd_ctl_elem_value_set_boolean(value, 0, *enabled);
	return snd_ctl_elem_write(ctl, value);
}

static int audio_open(snd_ctl_t **ctl)
{
	snd_ctl_card_info_t *info;
	int card = -1, err;

	snd_ctl_card_info_alloca(&info);
	while ((err = snd_card_next(&card)) >= 0 && card >= 0) {
		char name[32];

		snprintf(name, sizeof(name), "hw:%d", card);
		if (snd_ctl_open(ctl, name, 0) < 0)
			continue;
		if (snd_ctl_card_info(*ctl, info) >= 0 &&
		    !strcmp(snd_ctl_card_info_get_name(info), "A133 Audio Codec"))
			return 0;
		snd_ctl_close(*ctl);
		*ctl = NULL;
	}
	return err < 0 ? err : -ENODEV;
}

static int audio_quiesce(struct audio_state *state)
{
	long muted[2] = { 0, 0 }, readback[2];
	bool off = false, speaker_readback;
	int err;

	state->lock = open(AUDIO_POLICY_LOCK,
			   O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (state->lock < 0)
		return -errno;
	if (flock(state->lock, LOCK_EX | LOCK_NB))
		return -errno;
	if ((err = audio_open(&state->ctl)) < 0 ||
	    (err = audio_integer_control(state->ctl,
					 "DAC Digital Playback Volume",
					 state->gains, 2, false)) < 0 ||
	    (err = audio_switch_control(state->ctl, "Speaker Switch",
					&state->speaker, false)) < 0)
		return err;
	/* A failed control write may still have reached the codec. */
	state->restore = true;
	if ((err = audio_integer_control(state->ctl,
					 "DAC Digital Playback Volume",
					 muted, 2, true)) < 0 ||
	    (err = audio_integer_control(state->ctl,
					 "DAC Digital Playback Volume",
					 readback, 2, false)) < 0)
		return err;
	if (readback[0] || readback[1])
		return -EIO;
	if ((err = audio_switch_control(state->ctl, "Speaker Switch", &off, true)) < 0 ||
	    (err = audio_switch_control(state->ctl, "Speaker Switch",
					&speaker_readback, false)) < 0)
		return err;
	if (speaker_readback)
		return -EIO;
	return 0;
}

static int audio_restore(struct audio_state *state)
{
	long readback[2];
	bool speaker_readback;
	int result = 0;

	if (state->restore) {
		if (audio_integer_control(state->ctl, "DAC Digital Playback Volume",
					  state->gains, 2, true) < 0 ||
		    audio_integer_control(state->ctl, "DAC Digital Playback Volume",
					  readback, 2, false) < 0 ||
		    readback[0] != state->gains[0] || readback[1] != state->gains[1])
			result = -1;
		/* Restore the amplifier route only after the requested gain. */
		if (audio_switch_control(state->ctl, "Speaker Switch",
					 &state->speaker, true) < 0 ||
		    audio_switch_control(state->ctl, "Speaker Switch",
					 &speaker_readback, false) < 0 ||
		    speaker_readback != state->speaker)
			result = -1;
	}
	if (state->ctl)
		snd_ctl_close(state->ctl);
	if (state->lock >= 0) {
		if (flock(state->lock, LOCK_UN))
			result = -1;
		close(state->lock);
	}
	return result;
}

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

/* Confirm absence separately from a D-Bus failure. An unprovisioned image
 * deliberately has no bluetoothd, which is already a quiescent radio state.
 */
static int bluez_present(void)
{
	DBusError error = DBUS_ERROR_INIT;
	dbus_bool_t present;

	present = dbus_bus_name_has_owner(bus, "org.bluez", &error);
	if (dbus_error_is_set(&error)) {
		fprintf(stderr, "D-Bus: %s\n", error.message);
		dbus_error_free(&error);
		return -1;
	}
	return present;
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

static int read_value(const char *path, char *value, size_t size)
{
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	ssize_t count;
	int saved;

	if (fd < 0)
		return -1;
	count = read(fd, value, size - 1);
	saved = errno;
	if (close(fd) && count >= 0)
		return -1;
	if (count <= 0 || (size_t)count == size - 1) {
		errno = count < 0 ? saved : EIO;
		return -1;
	}
	value[count] = '\0';
	return 0;
}

static bool has_word(char *text, const char *wanted)
{
	char *word, *save;

	for (word = strtok_r(text, " \t\r\n", &save); word;
	     word = strtok_r(NULL, " \t\r\n", &save))
		if (!strcmp(word, wanted))
			return true;
	return false;
}

static bool supports_state(const char *wanted)
{
	char states[128];

	return !read_value(BRICK_POWER_STATE_PATH, states, sizeof(states)) &&
	       has_word(states, wanted);
}

#ifdef BRICK_DEEP_SUSPEND
static int mem_sleep_state(char *selected, size_t selected_size, bool *has_deep)
{
	char states[128], *word, *save;
	unsigned int selections = 0;

	if (read_value(BRICK_MEM_SLEEP_PATH, states, sizeof(states)))
		return -1;
	*has_deep = false;
	for (word = strtok_r(states, " \t\r\n", &save); word;
	     word = strtok_r(NULL, " \t\r\n", &save)) {
		size_t length = strlen(word);
		bool chosen = length >= 3 && word[0] == '[' && word[length - 1] == ']';
		const char *name = word;

		if (chosen) {
			word[length - 1] = '\0';
			name = word + 1;
			selections++;
			if (!*name || strlen(name) >= selected_size) {
				errno = EINVAL;
				return -1;
			}
			strcpy(selected, name);
		}
		if (!strcmp(name, "deep"))
			*has_deep = true;
	}
	if (selections != 1) {
		errno = EINVAL;
		return -1;
	}
	return 0;
}

static int restore_mem_sleep(const char *previous)
{
	char selected[16];
	bool has_deep;
	bool already_restored;

	already_restored = !mem_sleep_state(selected, sizeof(selected), &has_deep) &&
			   !strcmp(selected, previous);
	if (!already_restored) {
		char request[18];
		int length = snprintf(request, sizeof(request), "%s\n", previous);

		if (length <= 0 || (size_t)length >= sizeof(request) ||
		    write_value(BRICK_MEM_SLEEP_PATH, request) ||
		    mem_sleep_state(selected, sizeof(selected), &has_deep))
			return -1;
	}
	if (strcmp(selected, previous)) {
		errno = EIO;
		return -1;
	}
	return 0;
}

static int restore_mem_sleep_report(const char *previous)
{
	int saved_errno;

	if (!restore_mem_sleep(previous))
		return 0;
	saved_errno = errno;
	fprintf(stderr, "Deep sleep mode restoration failed: %s\n",
		strerror(saved_errno));
	errno = saved_errno;
	return -1;
}

static bool supports_suspend(void)
{
	char selected[16];
	bool has_deep;

	return supports_state("mem") &&
	       !mem_sleep_state(selected, sizeof(selected), &has_deep) && has_deep;
}
#else
static bool supports_suspend(void)
{
	return supports_state("freeze");
}
#endif

static int suspend_once(void)
{
	char count[32];
	int fd;
	ssize_t n;
#ifdef BRICK_DEEP_SUSPEND
	char previous[16], selected[16];
	bool has_deep;
	int result, saved_errno;

	if (mem_sleep_state(previous, sizeof(previous), &has_deep))
		return -1;
	if (!has_deep) {
		errno = EOPNOTSUPP;
		return -1;
	}
	if (strcmp(previous, "deep")) {
		if (write_value(BRICK_MEM_SLEEP_PATH, "deep\n"))
			saved_errno = errno;
		else if (mem_sleep_state(selected, sizeof(selected), &has_deep))
			saved_errno = errno;
		else if (strcmp(selected, "deep"))
			saved_errno = EIO;
		else
			goto selected;
		if (!saved_errno)
			saved_errno = EIO;
		(void)restore_mem_sleep_report(previous);
		errno = saved_errno;
		return -1;
	}
selected:
#endif

	fd = open("/sys/power/wakeup_count", O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		goto failed;
	/* sysfs wakeup_count ignores O_NONBLOCK; bound its interruptible wait. */
	alarm(3);
	n = read(fd, count, sizeof(count) - 1);
	alarm(0);
	close(fd);
	if (n <= 0)
		goto failed;
	count[n] = '\0';
	if (interrupted()) {
		errno = EINTR;
		goto failed;
	}
	if (write_value("/sys/power/wakeup_count", count))
		goto failed;
	/* One attempt: the kernel's closed/unpowered gate is authoritative.
	 * Powered=false alone does not prove that UART shutdown has completed.
	 */
#ifdef BRICK_DEEP_SUSPEND
	result = write_value(BRICK_POWER_STATE_PATH, "mem\n");
	saved_errno = errno;
	if (restore_mem_sleep_report(previous))
		return -1;
	errno = saved_errno;
	return result;
#else
	return write_value(BRICK_POWER_STATE_PATH, "freeze\n");
#endif

failed:
#ifdef BRICK_DEEP_SUSPEND
	saved_errno = errno;
	if (restore_mem_sleep_report(previous))
		return -1;
	errno = saved_errno;
#endif
	return -1;
}

static int transaction(void)
{
	struct audio_state audio = { .lock = -1 };
	struct led_policy_state leds = { .lock = -1 };
	bool bluetooth = owner != NULL;
	bool was_on = false, restored;
	bool attempted_off = false;
	int result = -1;

	if (!supports_suspend()) {
#ifdef BRICK_DEEP_SUSPEND
		fprintf(stderr, "Kernel does not offer the selected deep suspend path.\n");
#else
		fprintf(stderr, "Kernel does not offer s2idle (freeze).\n");
#endif
		return -1;
	}
	if (audio_quiesce(&audio) < 0) {
		fprintf(stderr, "Audio quiesce failed; suspend aborted.\n");
		goto restore_audio;
	}
	if (led_policy_quiesce(&leds) < 0) {
		fprintf(stderr, "Mood LED quiesce failed; suspend aborted.\n");
		goto restore_leds;
	}
	if (bluetooth) {
		if (get_power(&was_on))
			goto restore_leds;
		if (interrupted())
			goto restore_leds;
		if (was_on) {
			/* A timeout can still mean the daemon applied our request. */
			attempted_off = true;
			if (set_power(false))
				goto restore;
		}
		if (!same_owner() || wait_power_off() || interrupted())
			goto restore;
	} else if (bluez_present() != 0 || interrupted()) {
		/* A newly started daemon may be opening or powering the controller.
		 * Let a later request retry from a stable, observable state.
		 */
		goto restore;
	}
	result = suspend_once();
	if (result)
		fprintf(stderr, "Suspend not completed: %s\n", strerror(errno));
restore:
	if (audio_restore(&audio)) {
		fprintf(stderr, "Audio restoration failed; check codec mixer state.\n");
		result = -1;
	}
	if (bluetooth && was_on && attempted_off) {
		/* Never send restoration to a replacement daemon/controller owner.
		 * Always issue Set, even if a timed-out off request still reports
		 * Powered=true: BlueZ checks pending requests before its no-op path.
		 */
		if (!same_owner() || set_power(true) || get_power(&restored) || !restored) {
			fprintf(stderr, "Bluetooth power restoration failed; check bluetoothctl show\n");
			result = -1;
		}
	}
	if (led_policy_restore(&leds)) {
		fprintf(stderr, "Mood LED restoration failed; check LED class state.\n");
		result = -1;
	}
	return result;
restore_leds:
	if (led_policy_restore(&leds))
		fprintf(stderr, "Mood LED restoration failed; check LED class state.\n");
restore_audio:
	if (audio_restore(&audio))
		fprintf(stderr, "Audio restoration failed; check codec mixer state.\n");
	return -1;
}

int main(int argc, char **argv)
{
	DBusError error = DBUS_ERROR_INIT;
	int lockfd, result = EXIT_FAILURE;
	sigset_t previous;
	struct sigaction action = { .sa_handler = alarm_handler };
	if (argc != 2 || strcmp(argv[1], "--disconnect-bluetooth")) {
		fprintf(stderr, "Usage: brick-suspend --disconnect-bluetooth\n"
#ifdef BRICK_DEEP_SUSPEND
			"Mutes and restores the internal codec around deep suspend.\n"
#else
			"Mutes and restores the internal codec around s2idle.\n"
#endif
			"Turns off and restores the 23 standard mood LED devices.\n"
#ifdef BRICK_DEEP_SUSPEND
			"Disconnects Bluetooth for deep suspend; restores adapter power, not connections.\n"
#else
			"Disconnects Bluetooth for s2idle; restores adapter power, not connections.\n"
#endif
			"Wi-Fi disconnects and reloads on resume.\n"
			"Do not change audio, mood LEDs or Bluetooth power concurrently.\n");
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
	int bluetooth = bluez_present();
	if (bluetooth < 0)
		goto close_bus;
	if (bluetooth) {
		owner = bluez_owner();
		if (!owner)
			goto close_bus;
#ifdef BRICK_DEEP_SUSPEND
		fprintf(stderr, "Requesting deep suspend; Bluetooth and Wi-Fi will disconnect.\n");
#else
		fprintf(stderr, "Requesting s2idle; Bluetooth and Wi-Fi will disconnect.\n");
#endif
	} else {
#ifdef BRICK_DEEP_SUSPEND
		fprintf(stderr, "Requesting deep suspend; Bluetooth is already stopped and Wi-Fi will disconnect.\n");
#else
		fprintf(stderr, "Requesting s2idle; Bluetooth is already stopped and Wi-Fi will disconnect.\n");
#endif
	}
	if (!transaction())
		result = EXIT_SUCCESS;
	free(owner);
close_bus:
	dbus_connection_close(bus);
	dbus_connection_unref(bus);
signals:
	/* Deliver deferred termination only after attempted power restoration. */
	sigprocmask(SIG_SETMASK, &previous, NULL);
unlock:
	close(lockfd);
	return result;
}
