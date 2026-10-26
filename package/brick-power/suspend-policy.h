/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef BRICK_SUSPEND_POLICY_H
#define BRICK_SUSPEND_POLICY_H

static bool idle_suspend_due(const struct idle_policy *p, int64_t now,
			     int seconds, bool inhibited, bool inputs_ready)
{
	return seconds > 0 && !inhibited && inputs_ready &&
		p->mode == DISPLAY_OFF && p->pending == DISPLAY_QUERY &&
		!p->power_held && p->gamepad_ready && !p->gamepad_dropped &&
		!idle_has_keys(p) && now - p->last > (int64_t)seconds * 1000;
}

static void idle_suspend_finished(struct idle_policy *p, int64_t now)
{
	p->last = now;
	p->pending = DISPLAY_AWAKE;
}

#endif
