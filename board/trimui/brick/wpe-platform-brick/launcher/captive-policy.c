#include "captive-policy.h"
#include <glib/gstdio.h>
#include <errno.h>
#include <string.h>

gboolean brick_captive_uri_allowed(const char *uri)
{
    g_autoptr(GUri) parsed = NULL;
    g_autoptr(GError) error = NULL;
    const char *scheme;

    if (!uri || !*uri)
        return FALSE;
    if (!g_strcmp0(uri, "about:blank"))
        return TRUE;
    parsed = g_uri_parse(uri, G_URI_FLAGS_PARSE_RELAXED, &error);
    if (!parsed)
        return FALSE;
    scheme = g_uri_get_scheme(parsed);
    if (g_strcmp0(scheme, "http") && g_strcmp0(scheme, "https"))
        return FALSE;
    return g_uri_get_host(parsed) && *g_uri_get_host(parsed) &&
        !g_uri_get_userinfo(parsed);
}

char *brick_captive_profile_id(const char *network_key)
{
    g_autoptr(GChecksum) checksum = g_checksum_new(G_CHECKSUM_SHA256);
    if (!network_key || !*network_key)
        return NULL;
    g_checksum_update(checksum, (const guchar *)network_key, strlen(network_key));
    return g_strdup(g_checksum_get_string(checksum));
}

static gboolean mkdir_private(const char *path, GError **error)
{
    if (!g_mkdir_with_parents(path, 0700)) {
        if (!g_chmod(path, 0700))
            return TRUE;
    }
    g_set_error(error, G_FILE_ERROR, g_file_error_from_errno(errno),
        "cannot create private profile directory %s: %s", path,
        g_strerror(errno));
    return FALSE;
}

gboolean brick_captive_profile_paths(const char *root, const char *network_key,
    char **data_path, char **cache_path, GError **error)
{
    g_autofree char *id = brick_captive_profile_id(network_key);
    g_autofree char *base = NULL;
    g_return_val_if_fail(data_path && cache_path, FALSE);
    *data_path = NULL;
    *cache_path = NULL;
    if (!root || !g_path_is_absolute(root) || !id) {
        g_set_error_literal(error, G_FILE_ERROR, G_FILE_ERROR_INVAL,
            "profile root must be absolute and network key non-empty");
        return FALSE;
    }
    base = g_build_filename(root, id, NULL);
    *data_path = g_build_filename(base, "data", NULL);
    *cache_path = g_build_filename(base, "cache", NULL);
    if (mkdir_private(*data_path, error) && mkdir_private(*cache_path, error))
        return TRUE;
    g_clear_pointer(data_path, g_free);
    g_clear_pointer(cache_path, g_free);
    return FALSE;
}

gboolean brick_captive_lifecycle_event(BrickCaptiveLifecycle *state,
    const char *event)
{
    g_return_val_if_fail(state && event, FALSE);
    if (*state == BRICK_CAPTIVE_STARTING && !strcmp(event, "ready"))
        *state = BRICK_CAPTIVE_BROWSING;
    else if (*state == BRICK_CAPTIVE_BROWSING && !strcmp(event, "success"))
        *state = BRICK_CAPTIVE_AUTHORIZED;
    else if ((*state == BRICK_CAPTIVE_STARTING ||
              *state == BRICK_CAPTIVE_BROWSING) && !strcmp(event, "failed"))
        *state = BRICK_CAPTIVE_FAILED;
    else if (*state != BRICK_CAPTIVE_CLOSED && !strcmp(event, "close"))
        *state = BRICK_CAPTIVE_CLOSED;
    else
        return FALSE;
    return TRUE;
}

BrickOskState brick_osk_transition(BrickOskState state, const char *event)
{
    if (!g_strcmp0(event, "focus-in"))
        return BRICK_OSK_VISIBLE;
    if (!g_strcmp0(event, "preedit") && state != BRICK_OSK_HIDDEN)
        return BRICK_OSK_PREEDIT;
    if (!g_strcmp0(event, "commit") && state == BRICK_OSK_PREEDIT)
        return BRICK_OSK_VISIBLE;
    if (!g_strcmp0(event, "focus-out") || !g_strcmp0(event, "reset"))
        return BRICK_OSK_HIDDEN;
    return state;
}
