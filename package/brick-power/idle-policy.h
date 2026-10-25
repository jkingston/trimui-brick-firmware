/* SPDX-License-Identifier: GPL-2.0-only */
struct idle_policy {
	int mode, pending;
	int64_t last, last_wake, press_at;
	bool power_held, power_armed, gamepad_ready, gamepad_dropped;
	unsigned char keys[(KEY_MAX + 8) / 8];
};
static struct idle_policy idle;
static bool idle_enabled;
static void idle_activity(struct idle_policy *p, int64_t now)
{
	p->last = now;
	if (p->mode == DISPLAY_DIM) p->pending = DISPLAY_AWAKE;
}
static void idle_power_sync(struct idle_policy *p, bool held, int64_t now)
{
	p->power_held = held; p->power_armed = false;
	idle_activity(p, now);
}
static void idle_power_event(struct idle_policy *p, int value, int64_t now)
{
	if (value == 1 && !p->power_held) {
		p->power_held = p->power_armed = true; p->press_at = now;
	} else if (!value) {
		if (p->power_armed && now - p->press_at < HOLD_MS &&
		    (p->mode == DISPLAY_OFF || now - p->last_wake > 50))
			p->pending = p->mode == DISPLAY_OFF ? DISPLAY_AWAKE : DISPLAY_OFF;
		p->power_held = p->power_armed = false;
	}
	p->last = now;
}
static bool idle_has_keys(const struct idle_policy *p)
{
	for (size_t i = 0; i < sizeof(p->keys); i++) if (p->keys[i]) return true;
	return false;
}
static int idle_gamepad_sync(int fd, struct idle_policy *p, int64_t now)
{
	memset(p->keys, 0, sizeof(p->keys));
	p->gamepad_ready = ioctl(fd, EVIOCGKEY(sizeof(p->keys)), p->keys) >= 0;
	p->gamepad_dropped = false;
	idle_activity(p, now);
	return p->gamepad_ready ? 0 : -1;
}
static int idle_gamepad_event(int fd, struct idle_policy *p,
			      const struct input_event *e, int64_t now)
{
	if (e->type == EV_SYN && e->code == SYN_DROPPED) {
		p->gamepad_dropped = true; p->gamepad_ready = false;
		idle_activity(p, now);
	}
	if (p->gamepad_dropped) {
		if (e->type == EV_SYN && e->code == SYN_REPORT)
			return idle_gamepad_sync(fd, p, now);
		return 0;
	}
	if (e->type == EV_KEY && e->code <= KEY_MAX && e->value >= 0 && e->value <= 2) {
		if (e->value) p->keys[e->code / 8] |= 1U << (e->code % 8);
		else p->keys[e->code / 8] &= ~(1U << (e->code % 8));
		idle_activity(p, now);
	}
	return 0;
}
static int idle_timeout(const struct idle_policy *p, int64_t now, int seconds, bool awake)
{
	if (awake || seconds <= 0 || p->power_held || !p->gamepad_ready ||
	    p->gamepad_dropped || idle_has_keys(p)) return DISPLAY_QUERY;
	if (p->mode == DISPLAY_AWAKE && now - p->last > (int64_t)seconds * 1000)
		return DISPLAY_DIM;
	if (p->mode == DISPLAY_DIM && now - p->last > 60000) return DISPLAY_OFF;
	/* /tmp/stay_alive inhibits stock's next stage. No automatic suspend exists
	 * here, so that stage is unconditionally inhibited pending qualification.
	 */
	return DISPLAY_QUERY;
}
static void idle_applied(struct idle_policy *p, int mode, int64_t now)
{
	if (mode != p->mode) {
		p->last = now;
		if (mode == DISPLAY_AWAKE) p->last_wake = now;
	}
	p->mode = mode;
	p->pending = DISPLAY_QUERY;
}
static int idle_gamepad_open(void)
{
	glob_t paths = { 0 };
	int found = -1;
	if (glob("/dev/input/event*", 0, NULL, &paths)) goto out;
	for (size_t i = 0; i < paths.gl_pathc; i++) {
		char name[80] = { 0 };
		struct stat st;
		int fd = open(paths.gl_pathv[i], O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
		if (fd < 0) continue;
		if (!fstat(fd, &st) && S_ISCHR(st.st_mode) &&
		    ioctl(fd, EVIOCGNAME(sizeof(name) - 1), name) >= 0 &&
		    !strcmp(name, "TrimUI Brick Gamepad")) { found = fd; break; }
		close(fd);
	}
out:
	globfree(&paths); return found;
}
