// SPDX-License-Identifier: GPL-2.0-only
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <linux/input.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

struct input { int fd; bool dropped; dev_t device; };
#define AUDIO_POLICY_LOCK "/run/brick-audio-policy.lock"
static volatile sig_atomic_t stopped;
static void stop(int signal) { (void)signal; stopped = 1; }
static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

/* Stock's 0..20 user scale, expressed through ALSA's inverted attenuation. */
static int attenuation(int volume) { return 63 - 63 * (20 - volume) / 20; }

static int control(snd_ctl_t *ctl, const char *name, long *values,
		   unsigned count, bool write)
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
		for (unsigned i = 0; i < count; i++)
			values[i] = snd_ctl_elem_value_get_integer(value, i);
		return 0;
	}
	for (unsigned i = 0; i < count; i++) {
		if (values[i] < snd_ctl_elem_info_get_min(info) ||
		    values[i] > snd_ctl_elem_info_get_max(info))
			return -ERANGE;
		snd_ctl_elem_value_set_integer(value, i, values[i]);
	}
	return snd_ctl_elem_write(ctl, value);
}

static int open_codec(snd_ctl_t **ctl)
{
	int card = -1, err;
	snd_ctl_card_info_t *info;
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

static int apply_volume(snd_ctl_t *ctl, int volume, bool mute)
{
	long level = attenuation(volume), gain[2] = { 0, 0 };
	int err;
	/* Mute before attenuation changes; unmute only after attenuation is set. */
	if (!volume || mute) {
		err = control(ctl, "DAC Digital Playback Volume", gain, 2, true);
		if (err < 0)
			return err;
	}
	err = control(ctl, "DAC Playback Volume", &level, 1, true);
	if (err < 0 || !volume || mute)
		return err;
	gain[0] = gain[1] = 160;
	return control(ctl, "DAC Digital Playback Volume", gain, 2, true);
}

static int apply_volume_locked(int lock, snd_ctl_t *ctl, int volume, bool mute)
{
	int err;

	if (flock(lock, LOCK_EX))
		return -errno;
	err = apply_volume(ctl, volume, mute);
	if (flock(lock, LOCK_UN) && err >= 0)
		err = -errno;
	return err;
}

#include "settings.h"

static int fn_state(int fd)
{
	unsigned char keys[(KEY_MAX + 8) / 8] = { 0 };
	if (ioctl(fd, EVIOCGKEY(sizeof(keys)), keys) < 0) return -errno;
	return !!(keys[KEY_FN / 8] & (1U << (KEY_FN % 8)));
}

static void discover(struct input inputs[2], bool *mute, bool *changed, bool fn_mute)
{
	glob_t paths;
	if (glob("/dev/input/event*", 0, NULL, &paths)) { globfree(&paths); return; }
	for (size_t i = 0; i < paths.gl_pathc; i++) {
		char name[128] = { 0 };
		struct stat st;
		int fd, slot;
		if (stat(paths.gl_pathv[i], &st) ||
		    (inputs[0].fd >= 0 && inputs[0].device == st.st_rdev) ||
		    (inputs[1].fd >= 0 && inputs[1].device == st.st_rdev))
			continue;
		fd = open(paths.gl_pathv[i], O_RDONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0) continue;
		if (ioctl(fd, EVIOCGNAME(sizeof(name)), name) < 0) { close(fd); continue; }
		name[sizeof(name) - 1] = 0;
		slot = !strcmp(name, "TrimUI Brick Volume") ? 0 :
			(fn_mute && !strcmp(name, "TrimUI Brick Fn") ? 1 : -1);
		if (slot < 0 || inputs[slot].fd >= 0) { close(fd); continue; }
		if (slot == 1) {
			int active = fn_state(fd);
			if (active < 0) { close(fd); continue; }
			*mute = active; *changed = true;
		}
		inputs[slot] = (struct input){ .fd = fd, .device = st.st_rdev };
	}
	globfree(&paths);
}

static void input_event(struct input *input, int slot, const struct input_event *event,
			int *volume, bool *mute, bool *changed, bool *dirty, double *save)
{
	if (event->type == EV_SYN && event->code == SYN_DROPPED) {
		input->dropped = true;
		return;
	}
	if (input->dropped) {
		if (event->type == EV_SYN && event->code == SYN_REPORT) {
			input->dropped = false;
			if (slot == 1) {
				int active = fn_state(input->fd);
				if (active >= 0) { *mute = active; *changed = true; }
			}
		}
		return;
	}
	if (event->type != EV_KEY) return;
	if (slot == 1 && event->code == KEY_FN && event->value >= 0 && event->value <= 2) {
		*mute = event->value != 0;
		*changed = true;
	}
	if (slot == 0 && (event->value == 1 || event->value == 2)) {
		int next = *volume;
		if (event->code == KEY_VOLUMEUP && next < 20) next++;
		if (event->code == KEY_VOLUMEDOWN && next > 0) next--;
		if (next != *volume) {
			*volume = next;
			*changed = *dirty = true;
			*save = now() + 2;
		}
	}
}

int main(int argc, char **argv)
{
	snd_ctl_t *ctl = NULL;
	struct input inputs[2] = { { .fd = -1 }, { .fd = -1 } };
	bool mute = false, changed = false, dirty = false, fn_mute;
	double scan = 0, save = 0;
	int volume, err, lock, audio_lock;
	long level, gains[2];
	if (argc < 2 || argc > 3 || (argc == 3 && strcmp(argv[2], "--fn-mute"))) {
		fprintf(stderr, "Usage: brick-controls STATEFILE [--fn-mute]\n");
		return 2;
	}
	fn_mute = argc == 3;
	/* With Fn policy selected, wait for a real initial state before unmuting. */
	mute = changed = fn_mute;
	lock = open("/run/brick-controls.lock", O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) return 1;
	audio_lock = open(AUDIO_POLICY_LOCK,
			  O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (audio_lock < 0) {
		close(lock);
		return 1;
	}
	signal(SIGINT, stop); signal(SIGTERM, stop);
	if (flock(audio_lock, LOCK_EX)) {
		close(audio_lock); close(lock);
		return 1;
	}
	if ((err = open_codec(&ctl)) < 0 ||
	    (err = control(ctl, "DAC Playback Volume", &level, 1, false)) < 0 ||
	    (err = control(ctl, "DAC Digital Playback Volume", gains, 2, false)) < 0) {
		fprintf(stderr, "brick-controls: codec unavailable: %s\n", snd_strerror(err));
		if (ctl) snd_ctl_close(ctl);
		close(audio_lock); close(lock);
		return 1;
	}
	if (flock(audio_lock, LOCK_UN)) {
		snd_ctl_close(ctl); close(audio_lock); close(lock);
		return 1;
	}
	volume = read_state(argv[1], 20);
	if (volume >= 0) changed = true;
	else if (volume == -ENOENT) {
		volume = 0;
		if (gains[0] || gains[1])
			while (volume < 20 && attenuation(volume) < level) volume++;
		dirty = true;
	}
	else {
		fprintf(stderr, "brick-controls: invalid saved settings; leaving them unchanged\n");
		snd_ctl_close(ctl); close(audio_lock); close(lock); return 1;
	}
	while (!stopped) {
		if (now() >= scan) { discover(inputs, &mute, &changed, fn_mute); scan = now() + 1; }
		if (changed) {
			err = apply_volume_locked(audio_lock, ctl, volume, mute);
			if (err < 0) { fprintf(stderr, "brick-controls: mixer: %s\n", snd_strerror(err)); break; }
			changed = false;
		}
		if (dirty && now() >= save) {
			if (!save_state(argv[1], volume)) dirty = false;
			else { perror("brick-controls: save"); save = now() + 5; }
		}
		struct pollfd fds[2] = { { inputs[0].fd, POLLIN, 0 }, { inputs[1].fd, POLLIN, 0 } };
		if (poll(fds, 2, 100) < 0 && errno != EINTR) break;
		for (int i = 0; i < 2; i++) {
			if (fds[i].revents & (POLLERR | POLLHUP | POLLNVAL)) {
				close(inputs[i].fd); inputs[i].fd = -1;
				/* Device removal does not establish a new physical Fn state. */
				continue;
			}
			if (!(fds[i].revents & POLLIN)) continue;
			struct input_event e;
			for (unsigned n = 0; n < 64 && !stopped; n++) {
				if (read(inputs[i].fd, &e, sizeof(e)) != sizeof(e)) break;
				input_event(&inputs[i], i, &e, &volume, &mute, &changed, &dirty, &save);
			}
		}
	}
	if (dirty && save_state(argv[1], volume)) perror("brick-controls: final save");
	for (int i = 0; i < 2; i++) if (inputs[i].fd >= 0) close(inputs[i].fd);
	snd_ctl_close(ctl); close(audio_lock); close(lock);
	return stopped ? 0 : 1;
}
