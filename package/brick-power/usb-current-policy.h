// SPDX-License-Identifier: GPL-2.0-only
#ifndef BRICK_USB_CURRENT_POLICY_H
#define BRICK_USB_CURRENT_POLICY_H

#include <stdbool.h>
#include <stdint.h>

#define BRICK_USB_PC_CURRENT_UA 500000
#define BRICK_USB_ADAPTER_CURRENT_UA 2500000
#define BRICK_USB_BOOT_DELAY_MS 20000
#define BRICK_USB_ATTACH_DELAY_MS 5000

struct brick_usb_current_state {
	bool observed;
	bool attached;
	bool configured;
	uint64_t attached_ms;
	uint64_t delay_ms;
};

static inline int brick_usb_current(struct brick_usb_current_state *state,
				    bool online, bool configured,
				    uint64_t now_ms)
{
	if (!online) {
		state->observed = true;
		state->attached = false;
		state->configured = false;
		state->attached_ms = 0;
		state->delay_ms = 0;
		return 0;
	}

	if (!state->attached) {
		state->delay_ms = state->observed ? BRICK_USB_ATTACH_DELAY_MS :
						       BRICK_USB_BOOT_DELAY_MS;
		state->observed = true;
		state->attached = true;
		state->attached_ms = now_ms;
	}
	state->configured |= configured;

	/* A data connection stays on the stock-proven PC limit until detach. */
	if (state->configured || now_ms - state->attached_ms < state->delay_ms)
		return BRICK_USB_PC_CURRENT_UA;

	return BRICK_USB_ADAPTER_CURRENT_UA;
}

#endif
