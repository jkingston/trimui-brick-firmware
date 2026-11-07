#pragma once
#include <glib.h>

typedef enum {
    BRICK_CAPTIVE_STARTING,
    BRICK_CAPTIVE_BROWSING,
    BRICK_CAPTIVE_AUTHORIZED,
    BRICK_CAPTIVE_FAILED,
    BRICK_CAPTIVE_CLOSED,
} BrickCaptiveLifecycle;

typedef enum {
    BRICK_OSK_HIDDEN,
    BRICK_OSK_VISIBLE,
    BRICK_OSK_PREEDIT,
} BrickOskState;

gboolean brick_captive_uri_allowed(const char *uri);
char *brick_captive_profile_id(const char *network_key);
gboolean brick_captive_profile_paths(const char *root, const char *network_key,
    char **data_path, char **cache_path, GError **error);
gboolean brick_captive_lifecycle_event(BrickCaptiveLifecycle *state,
    const char *event);
BrickOskState brick_osk_transition(BrickOskState state, const char *event);

