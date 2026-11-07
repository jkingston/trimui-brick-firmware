#define _GNU_SOURCE
#include "brick-controller.h"
#include "brick-osk-context.h"
#include "captive-policy.h"
#include <errno.h>
#include <fcntl.h>
#include <glib-unix.h>
#include <limits.h>
#include <signal.h>
#include <sqlite3.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wpe/drm/wpe-drm.h>
#include <wpe/webkit.h>
#include <wpe/wpe-platform.h>

#define CAPTIVE_PROCESS_MEMORY_LIMIT_MB 192
#define CAPTIVE_MEMORY_POLL_SECONDS 1.0
#define CAPTIVE_DRM_FD_ENV "BRICK_CAPTIVE_DRM_FD"
#define CAPTIVE_GPU_DRM_FD_ENV "BRICK_CAPTIVE_GPU_DRM_FD"
#define CAPTIVE_PROFILE_ROOT_ENV "BRICK_CAPTIVE_PROFILE_ROOT"
#define CAPTIVE_DRM_DEVICE "/dev/dri/card0"

typedef struct {
    GMainLoop *loop;
    WebKitWebView *view;
    int authority_fd;
    BrickCaptiveLifecycle lifecycle;
    gboolean first_frame_sent;
    BrickOskContext *osk;
#ifdef BRICK_OSK_QUALIFICATION
    guint qualification_step;
    guint qualification_focus_ticks;
    gboolean qualification_clicked;
    guint qualification_sessions;
    gboolean qualification_waiting_for_hide;
#endif
} App;

static gboolean remove_cookie_database(const char *path, GError **error)
{
    const char *suffixes[] = { "", "-wal", "-shm" };
    for (gsize i = 0; i < G_N_ELEMENTS(suffixes); i++) {
        g_autofree char *candidate = g_strconcat(path, suffixes[i], NULL);
        if (g_unlink(candidate) && errno != ENOENT) {
            g_set_error(error, G_FILE_ERROR, g_file_error_from_errno(errno),
                "cannot reset corrupt cookie database: %s", g_strerror(errno));
            return FALSE;
        }
    }
    return TRUE;
}

static gboolean prepare_cookie_database(const char *path, GError **error)
{
    sqlite3 *database = NULL;
    sqlite3_stmt *statement = NULL;
    gboolean corrupt = FALSE;
    if (g_access(path, F_OK) && errno == ENOENT)
        return TRUE;
    int result = sqlite3_open_v2(path, &database,
        SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX, NULL);
    if (result == SQLITE_CORRUPT || result == SQLITE_NOTADB)
        corrupt = TRUE;
    else if (result != SQLITE_OK) {
        g_set_error(error, G_FILE_ERROR, G_FILE_ERROR_FAILED,
            "cannot inspect cookie database: SQLite status %d", result);
        sqlite3_close(database);
        return FALSE;
    }
    if (!corrupt) {
        result = sqlite3_prepare_v2(database, "PRAGMA quick_check(1)", -1,
            &statement, NULL);
        if (result == SQLITE_CORRUPT || result == SQLITE_NOTADB)
            corrupt = TRUE;
        else if (result != SQLITE_OK) {
            g_set_error(error, G_FILE_ERROR, G_FILE_ERROR_FAILED,
                "cannot check cookie database: SQLite status %d", result);
            sqlite3_close(database);
            return FALSE;
        }
    }
    if (statement) {
        result = sqlite3_step(statement);
        if (result == SQLITE_ROW) {
            const unsigned char *check = sqlite3_column_text(statement, 0);
            corrupt = !check || strcmp((const char *)check, "ok");
        } else if (result == SQLITE_CORRUPT || result == SQLITE_NOTADB)
            corrupt = TRUE;
        else {
            g_set_error(error, G_FILE_ERROR, G_FILE_ERROR_FAILED,
                "cannot read cookie integrity result: SQLite status %d",
                result);
            sqlite3_finalize(statement);
            sqlite3_close(database);
            return FALSE;
        }
        sqlite3_finalize(statement);
    }
    sqlite3_close(database);
    if (!corrupt)
        return TRUE;
    g_printerr("cookie database corrupt; resetting current profile\n");
    return remove_cookie_database(path, error);
}

#ifdef BRICK_OSK_QUALIFICATION
typedef struct {
    BrickOskButton button;
    gboolean pressed;
} QualificationAction;

#define Q_PRESS(button) { button, TRUE }
#define Q_SELECT Q_PRESS(BRICK_OSK_BUTTON_SELECT), \
    { BRICK_OSK_BUTTON_SELECT, FALSE }

static const QualificationAction qualify_basic[] = { Q_SELECT };
static const QualificationAction qualify_utf8[] = {
    Q_PRESS(BRICK_OSK_BUTTON_DOWN), Q_PRESS(BRICK_OSK_BUTTON_DOWN),
    Q_PRESS(BRICK_OSK_BUTTON_RIGHT), Q_PRESS(BRICK_OSK_BUTTON_RIGHT),
    Q_PRESS(BRICK_OSK_BUTTON_RIGHT), Q_PRESS(BRICK_OSK_BUTTON_RIGHT),
    Q_PRESS(BRICK_OSK_BUTTON_RIGHT), Q_PRESS(BRICK_OSK_BUTTON_RIGHT),
    Q_PRESS(BRICK_OSK_BUTTON_RIGHT), Q_SELECT,
};
static const QualificationAction qualify_uppercase[] = {
    Q_PRESS(BRICK_OSK_BUTTON_DOWN), Q_PRESS(BRICK_OSK_BUTTON_DOWN),
    Q_PRESS(BRICK_OSK_BUTTON_DOWN), Q_PRESS(BRICK_OSK_BUTTON_RIGHT),
    Q_PRESS(BRICK_OSK_BUTTON_RIGHT), Q_PRESS(BRICK_OSK_BUTTON_RIGHT),
    Q_PRESS(BRICK_OSK_BUTTON_RIGHT), Q_PRESS(BRICK_OSK_BUTTON_RIGHT),
    Q_SELECT, Q_SELECT,
};
static const QualificationAction qualify_symbols[] = {
    Q_PRESS(BRICK_OSK_BUTTON_DOWN), Q_PRESS(BRICK_OSK_BUTTON_DOWN),
    Q_PRESS(BRICK_OSK_BUTTON_DOWN), Q_SELECT, Q_SELECT,
};

static gboolean qualification_step(gpointer user_data)
{
    App *app = user_data;
    if (!app->osk)
        return G_SOURCE_CONTINUE;
    const char *uri = webkit_web_view_get_uri(app->view);
    gboolean session_stress = uri && strstr(uri, "kind=sessions");
    if (session_stress && app->qualification_waiting_for_hide) {
        /* Keep the portal mapped for 500 ms between ownership sessions. This
         * gives its repaint/page flip time to complete before the qualifier
         * requests the next trusted handoff. */
        if (app->qualification_focus_ticks++ < 10)
            return G_SOURCE_CONTINUE;
        if (brick_osk_context_is_visible(app->osk)) {
            g_printerr("OSK_SESSION_STRESS_FAILED visible-after-dismiss\n");
            g_main_loop_quit(app->loop);
            return G_SOURCE_REMOVE;
        }
        g_printerr("OSK_SESSION_HIDDEN %u\n", app->qualification_sessions);
        if (app->qualification_sessions >= 100) {
            g_printerr("OSK_SESSION_STRESS_PASS %u\n",
                app->qualification_sessions);
            g_main_loop_quit(app->loop);
            return G_SOURCE_REMOVE;
        }
        app->qualification_waiting_for_hide = FALSE;
        app->qualification_focus_ticks = 0;
    }
    if (!brick_osk_context_is_visible(app->osk)) {
        if (!webkit_web_view_is_loading(app->view)) {
            WPEView *view = webkit_web_view_get_wpe_view(app->view);
            if (!app->qualification_clicked) {
                double x = MAX(wpe_view_get_width(view), 1) / 2.0;
                double y = MAX(wpe_view_get_height(view), 1) / 2.0;
                guint32 time = (guint32)(g_get_monotonic_time() / 1000);
                wpe_view_focus_in(view);
                g_autoptr(WPEEvent) move = wpe_event_pointer_move_new(
                    WPE_EVENT_POINTER_MOVE, view, WPE_INPUT_SOURCE_MOUSE,
                    time, 0, x, y, 0, 0);
                g_autoptr(WPEEvent) down = wpe_event_pointer_button_new(
                    WPE_EVENT_POINTER_DOWN, view, WPE_INPUT_SOURCE_MOUSE,
                    time, 0, WPE_BUTTON_PRIMARY, x, y, 1);
                g_autoptr(WPEEvent) up = wpe_event_pointer_button_new(
                    WPE_EVENT_POINTER_UP, view, WPE_INPUT_SOURCE_MOUSE,
                    time, 0, WPE_BUTTON_PRIMARY, x, y, 0);
                wpe_view_event(view, move);
                wpe_view_event(view, down);
                wpe_view_event(view, up);
                /* Test-only seam: the fixture is controlled and the stock
                 * kernel has no uinput, so focus its same-origin editor. */
                webkit_web_view_evaluate_javascript(app->view,
                    "(document.querySelector('input')||"
                    "document.querySelector('iframe')?.contentDocument?."
                    "querySelector('input'))?.focus()", -1, NULL,
                    "brick-osk-qualification", NULL, NULL, NULL);
                app->qualification_clicked = TRUE;
            }
            /* WebKit asynchronously applies INHIBIT_OSK to programmatic
             * focus. Reassert only in this build-time qualification seam
             * after that state is stable; production always honors it. */
            brick_osk_context_qualification_focus_in(app->osk);
            g_printerr("OSK_QUALIFIER_FOCUS_REQUESTED\n");
        }
        return G_SOURCE_CONTINUE;
    }
    /* Let the async fixture focus and the 100 ms producer handoff complete
     * before exercising the real controller/OSK action path. */
    if (app->qualification_focus_ticks++ < 10)
        return G_SOURCE_CONTINUE;
    if (app->qualification_focus_ticks == 11)
        g_printerr("OSK_QUALIFIER_VISIBLE\n");
    if (session_stress) {
        g_printerr("OSK_SESSION_VISIBLE %u\n",
            app->qualification_sessions + 1);
        brick_osk_context_dismiss(app->osk);
        app->qualification_sessions++;
        app->qualification_waiting_for_hide = TRUE;
        app->qualification_focus_ticks = 0;
        return G_SOURCE_CONTINUE;
    }
    if (uri && strstr(uri, "kind=hammer")) {
        if (app->qualification_step >= 100)
            return G_SOURCE_REMOVE;
        BrickOskButton button = app->qualification_step % 2 ?
            BRICK_OSK_BUTTON_RIGHT : BRICK_OSK_BUTTON_LEFT;
        app->qualification_step++;
        brick_osk_context_handle_button(app->osk, button, TRUE);
        return G_SOURCE_CONTINUE;
    }
    const QualificationAction *actions = qualify_basic;
    gsize length = G_N_ELEMENTS(qualify_basic);
    if (uri && strstr(uri, "kind=utf8")) {
        actions = qualify_utf8;
        length = G_N_ELEMENTS(qualify_utf8);
    } else if (uri && strstr(uri, "kind=uppercase")) {
        actions = qualify_uppercase;
        length = G_N_ELEMENTS(qualify_uppercase);
    } else if (uri && strstr(uri, "kind=symbols")) {
        actions = qualify_symbols;
        length = G_N_ELEMENTS(qualify_symbols);
    }
    if (app->qualification_step >= length)
        return G_SOURCE_REMOVE;
    const QualificationAction *action = &actions[app->qualification_step++];
    g_printerr("OSK_QUALIFIER_ACTION %u\n", app->qualification_step);
    brick_osk_context_handle_button(app->osk, action->button,
        action->pressed);
    return G_SOURCE_CONTINUE;
}
#endif

static gboolean enable_required_feature(WebKitSettings *settings,
    const char *identifier)
{
    g_autoptr(WebKitFeatureList) features =
        webkit_settings_get_all_features();
    gsize length = webkit_feature_list_get_length(features);
    for (gsize i = 0; i < length; i++) {
        WebKitFeature *feature = webkit_feature_list_get(features, i);
        if (!g_strcmp0(webkit_feature_get_identifier(feature), identifier)) {
            webkit_settings_set_feature_enabled(settings, feature, TRUE);
            return webkit_settings_get_feature_enabled(settings, feature);
        }
    }
    return FALSE;
}

static void authority_send(App *app, const char *event, const char *value)
{
    if (app->authority_fd < 0)
        return;
    dprintf(app->authority_fd, "%s%s%s\n", event, value ? " " : "",
        value ? value : "");
}

static gboolean authority_input(GIOChannel *channel, GIOCondition condition,
    gpointer user_data)
{
    App *app = user_data;
    g_autofree char *line = NULL;
    gsize length = 0;
    if (condition & G_IO_IN) {
        if (g_io_channel_read_line(channel, &line, &length, NULL, NULL) ==
            G_IO_STATUS_NORMAL) {
            g_strstrip(line);
            if (!g_ascii_strcasecmp(line, "SUCCESS")) {
                brick_captive_lifecycle_event(&app->lifecycle, "success");
                g_main_loop_quit(app->loop);
                return G_SOURCE_REMOVE;
            }
            if (!g_ascii_strcasecmp(line, "FAILED") ||
                !g_ascii_strcasecmp(line, "CANCEL")) {
                brick_captive_lifecycle_event(&app->lifecycle, "failed");
                g_main_loop_quit(app->loop);
                return G_SOURCE_REMOVE;
            }
        }
    }
    if (condition & (G_IO_HUP | G_IO_ERR)) {
        brick_captive_lifecycle_event(&app->lifecycle, "failed");
        g_main_loop_quit(app->loop);
        return G_SOURCE_REMOVE;
    }
    return G_SOURCE_CONTINUE;
}

static gboolean decide_policy(WebKitWebView *view, WebKitPolicyDecision *decision,
    WebKitPolicyDecisionType type, gpointer user_data)
{
    (void)view;
    (void)user_data;
    if (type == WEBKIT_POLICY_DECISION_TYPE_NAVIGATION_ACTION ||
        type == WEBKIT_POLICY_DECISION_TYPE_NEW_WINDOW_ACTION) {
        WebKitNavigationAction *action =
            webkit_navigation_policy_decision_get_navigation_action(
                WEBKIT_NAVIGATION_POLICY_DECISION(decision));
        const char *uri = webkit_uri_request_get_uri(
            webkit_navigation_action_get_request(action));
        if (!brick_captive_uri_allowed(uri)) {
            webkit_policy_decision_ignore(decision);
        } else if (type == WEBKIT_POLICY_DECISION_TYPE_NEW_WINDOW_ACTION) {
            /* Captive portals commonly use target=_blank for an IdP. Keep it
             * in the single trusted view instead of creating another view. */
            webkit_web_view_load_uri(view, uri);
            webkit_policy_decision_ignore(decision);
        } else
            webkit_policy_decision_use(decision);
        return TRUE;
    }
    if (type == WEBKIT_POLICY_DECISION_TYPE_RESPONSE &&
        !webkit_response_policy_decision_is_mime_type_supported(
            WEBKIT_RESPONSE_POLICY_DECISION(decision))) {
        webkit_policy_decision_ignore(decision);
        return TRUE;
    }
    return FALSE;
}

static WebKitWebView *deny_popup(WebKitWebView *view,
    WebKitNavigationAction *action, gpointer user_data)
{
    (void)view;
    (void)action;
    (void)user_data;
    return NULL;
}

static gboolean deny_permission(WebKitWebView *view,
    WebKitPermissionRequest *request, gpointer user_data)
{
    (void)view;
    (void)user_data;
    webkit_permission_request_deny(request);
    return TRUE;
}

static gboolean deny_file_chooser(WebKitWebView *view,
    WebKitFileChooserRequest *request, gpointer user_data)
{
    (void)view;
    (void)user_data;
    webkit_file_chooser_request_cancel(request);
    return TRUE;
}

static void cancel_download(WebKitNetworkSession *session,
    WebKitDownload *download, gpointer user_data)
{
    (void)session;
    (void)user_data;
    webkit_download_cancel(download);
}

static void load_changed(WebKitWebView *view, WebKitLoadEvent event,
    gpointer user_data)
{
    App *app = user_data;
    if (event == WEBKIT_LOAD_STARTED) {
        if (app->osk)
            brick_osk_context_dismiss(app->osk);
        authority_send(app, "PAGE", webkit_web_view_get_uri(view));
    } else if (event == WEBKIT_LOAD_FINISHED)
        authority_send(app, "LOADED", webkit_web_view_get_uri(view));
    /* Only the external connectivity authority may send SUCCESS. A page load,
     * HTTP status, URL or DOM observation never declares network access. */
}

static void frame_displayed(WebKitWebView *view, gpointer user_data)
{
    App *app = user_data;
    (void)view;
    if (app->first_frame_sent)
        return;
    app->first_frame_sent = TRUE;
    authority_send(app, "FRAME", NULL);
}

static void view_closed(WebKitWebView *view, gpointer user_data)
{
    App *app = user_data;
    (void)view;
    brick_captive_lifecycle_event(&app->lifecycle, "failed");
    g_main_loop_quit(app->loop);
}

static void osk_failed(WebKitInputMethodContext *context, gpointer user_data)
{
    App *app = user_data;
    (void)context;
    authority_send(app, "OSK_FAILED", NULL);
    brick_captive_lifecycle_event(&app->lifecycle, "failed");
    g_main_loop_quit(app->loop);
}

int main(int argc, char **argv)
{
    g_autoptr(GError) error = NULL;
    g_autofree char *data_path = NULL;
    g_autofree char *cache_path = NULL;
    g_autofree char *cookie_path = NULL;
    g_autoptr(WebKitNetworkSession) session = NULL;
    g_autoptr(WebKitWebContext) context = NULL;
    g_autoptr(WebKitSettings) settings = NULL;
    g_autoptr(WebKitInputMethodContext) input = NULL;
    g_autoptr(WebKitMemoryPressureSettings) memory_pressure = NULL;
    g_autoptr(WPEDisplay) display = NULL;
    const char *uri = NULL;
    const char *network_key = NULL;
    const char *profile_root = "/data/wifi/captive-browser";
    const char *input_device = NULL;
    int input_fd = -1;
    int authority_fd = -1;
    int drm_fd = -1;
    int gpu_drm_fd = -1;
#ifdef BRICK_OSK_QUALIFICATION
    gboolean qualify_osk = FALSE;
#endif
    GOptionEntry options[] = {
        { "uri", 0, 0, G_OPTION_ARG_STRING, &uri, "Initial portal URI", "URI" },
        { "network-key", 0, 0, G_OPTION_ARG_STRING, &network_key,
          "Opaque stable per-network key", "KEY" },
        { "profile-root", 0, 0, G_OPTION_ARG_STRING, &profile_root,
          "Absolute profile root", "PATH" },
        { "authority-fd", 0, 0, G_OPTION_ARG_INT, &authority_fd,
          "Bidirectional line-protocol connectivity authority", "FD" },
        { "input-device", 0, 0, G_OPTION_ARG_FILENAME, &input_device,
          "Exclusive Brick controller evdev node", "PATH" },
        { "input-fd", 0, 0, G_OPTION_ARG_INT, &input_fd,
          "Broker-opened exclusive Brick controller", "FD" },
#ifdef BRICK_OSK_QUALIFICATION
        { "qualify-osk", 0, 0, G_OPTION_ARG_NONE, &qualify_osk,
          "TEST BUILD ONLY: drive the trusted OSK", NULL },
#endif
        { NULL }
    };
    g_autoptr(GOptionContext) options_context =
        g_option_context_new("- Brick captive login browser");
    signal(SIGPIPE, SIG_IGN);
    g_option_context_add_main_entries(options_context, options, NULL);
    if (!g_option_context_parse(options_context, &argc, &argv, &error))
        return g_printerr("%s\n", error->message), 2;
    if (!brick_captive_uri_allowed(uri) || !network_key || authority_fd < 0)
        return g_printerr("valid --uri, --network-key and --authority-fd are required\n"), 2;
    if (!brick_captive_profile_paths(profile_root, network_key, &data_path,
            &cache_path, &error))
        return g_printerr("%s\n", error->message), 1;
    g_autofree char *profile_base = g_path_get_dirname(data_path);
    /* NetworkProcess receives only this network's directory in its writable
     * Bubblewrap bind, never the parent containing other network profiles. */
    g_setenv(CAPTIVE_PROFILE_ROOT_ENV, profile_base, TRUE);
    if (fcntl(authority_fd, F_SETFD,
            fcntl(authority_fd, F_GETFD) | FD_CLOEXEC) < 0)
        return g_printerr("cannot protect authority fd: %s\n",
            g_strerror(errno)), 1;
    if (input_fd >= 0 && fcntl(input_fd, F_SETFD,
            fcntl(input_fd, F_GETFD) | FD_CLOEXEC) < 0)
        return g_printerr("cannot protect controller fd: %s\n",
            g_strerror(errno)), 1;
    if (input_device && input_fd >= 0)
        return g_printerr("choose only one controller source\n"), 2;

    /* Product invariant: there is one qualified display path. Do not inherit a
     * supervisor environment that silently selects another WPE extension. */
    g_setenv("WPE_PLATFORM", "drm", TRUE);
    /* The stock PowerVR userspace cannot back this upstream WPE path. Keep the
     * device-qualified Mesa software renderer while retaining GBM/DMA-BUF
     * transport into the DRM presenter. These are runtime requirements, not
     * merely build-environment hints. */
    g_setenv("GBM_ALWAYS_SOFTWARE", "true", TRUE);
    g_setenv("GALLIUM_DRIVER", "softpipe", TRUE);
    const char *drm_fd_string = g_getenv(CAPTIVE_DRM_FD_ENV);
    char *drm_fd_end = NULL;
    errno = 0;
    long drm_fd_value = drm_fd_string ?
        strtol(drm_fd_string, &drm_fd_end, 10) : -1;
    if (errno || drm_fd_value < 0 || drm_fd_value > INT_MAX ||
            !drm_fd_end || *drm_fd_end)
        return g_printerr("trusted DRM fd is required\n"), 1;
    drm_fd = (int)drm_fd_value;
    if (fcntl(drm_fd, F_GETFD) < 0 ||
            fcntl(drm_fd, F_SETFD, fcntl(drm_fd, F_GETFD) | FD_CLOEXEC) < 0)
        return g_printerr("invalid trusted DRM fd: %s\n",
            g_strerror(errno)), 1;
    const char *gpu_drm_fd_string = g_getenv(CAPTIVE_GPU_DRM_FD_ENV);
    char *gpu_drm_fd_end = NULL;
    errno = 0;
    long gpu_drm_fd_value = gpu_drm_fd_string ?
        strtol(gpu_drm_fd_string, &gpu_drm_fd_end, 10) : -1;
    if (errno || gpu_drm_fd_value < 0 || gpu_drm_fd_value > INT_MAX ||
            !gpu_drm_fd_end || *gpu_drm_fd_end)
        return g_printerr("trusted GPU DRM fd is required\n"), 1;
    gpu_drm_fd = (int)gpu_drm_fd_value;
    if (fcntl(gpu_drm_fd, F_GETFD) < 0 ||
            fcntl(gpu_drm_fd, F_SETFD,
                fcntl(gpu_drm_fd, F_GETFD) | FD_CLOEXEC) < 0)
        return g_printerr("invalid trusted GPU DRM fd: %s\n",
            g_strerror(errno)), 1;
    display = wpe_display_drm_new();
    if (!wpe_display_drm_connect_fd(WPE_DISPLAY_DRM(display), drm_fd,
            CAPTIVE_DRM_DEVICE, &error))
        return g_printerr("WPE DRM display unavailable: %s\n",
            error->message), 1;
    close(drm_fd);
    drm_fd = -1;
    g_unsetenv(CAPTIVE_DRM_FD_ENV);
    wpe_display_set_primary(display);
    /* WPE defaults each child to the system RAM size (up to 3 GiB), which is
     * inappropriate on this 1 GiB appliance. This supported public API makes
     * Web/Network children scavenge at 33/50% of 192 MiB; it is not an OOM
     * limit and deliberately has no kill threshold. */
    memory_pressure = webkit_memory_pressure_settings_new();
    webkit_memory_pressure_settings_set_memory_limit(memory_pressure,
        CAPTIVE_PROCESS_MEMORY_LIMIT_MB);
    webkit_memory_pressure_settings_set_poll_interval(memory_pressure,
        CAPTIVE_MEMORY_POLL_SECONDS);
    webkit_network_session_set_memory_pressure_settings(memory_pressure);
    cookie_path = g_build_filename(data_path, "cookies.sqlite", NULL);
    if (!prepare_cookie_database(cookie_path, &error))
        return g_printerr("%s\n", error->message), 1;
    session = webkit_network_session_new(data_path, cache_path);
    webkit_cookie_manager_set_persistent_storage(
        webkit_network_session_get_cookie_manager(session), cookie_path,
        WEBKIT_COOKIE_PERSISTENT_STORAGE_SQLITE);
    webkit_network_session_set_tls_errors_policy(session,
        WEBKIT_TLS_ERRORS_POLICY_FAIL);
    settings = webkit_settings_new_with_settings(
        "enable-developer-extras", FALSE,
        "enable-webgl", FALSE,
        "enable-media-stream", FALSE,
        "enable-webrtc", FALSE,
        NULL);
    if (!enable_required_feature(settings,
            "UseGPUProcessForDOMRendering"))
        return g_printerr("required GPU DOM rendering feature unavailable\n"), 1;
    context = WEBKIT_WEB_CONTEXT(g_object_new(WEBKIT_TYPE_WEB_CONTEXT,
        "memory-pressure-settings", memory_pressure, NULL));
    /* A captive flow navigates a small related document set. Keep shared IdP
     * resources without the unbounded history cache of a general browser. */
    webkit_web_context_set_cache_model(context,
        WEBKIT_CACHE_MODEL_DOCUMENT_BROWSER);
    App app = { .loop = g_main_loop_new(NULL, FALSE),
        .authority_fd = authority_fd, .lifecycle = BRICK_CAPTIVE_STARTING };
    app.view = WEBKIT_WEB_VIEW(g_object_new(WEBKIT_TYPE_WEB_VIEW,
        "web-context", context, "network-session", session,
        "settings", settings, "display", display, NULL));
    WPEView *platform_view = webkit_web_view_get_wpe_view(app.view);
    if (!platform_view)
        return g_printerr("WPE platform view unavailable\n"), 1;
    wpe_toplevel_fullscreen(wpe_view_get_toplevel(platform_view));
    /* A newly-created WPEView is not visible by default. The first buffer can
     * still be produced while unmapped, which hid this omission for static
     * portals, but WebKit then throttles page timers and animation callbacks.
     * Mapping is part of the public WPEPlatform lifecycle contract and keeps
     * the portal active until the trusted OSK deliberately unmaps it. */
    wpe_view_set_visible(platform_view, TRUE);
    /* This is the sole fullscreen application while it owns KMS. Focus it
     * even when a qualification run has no controller fd; the controller
     * path repeats this idempotently after installing its event source. */
    wpe_view_focus_in(platform_view);
    BrickController *controller = NULL;
    if (input_device || input_fd >= 0) {
        controller = input_fd >= 0 ?
            brick_controller_new_from_fd(platform_view, app.view, input_fd,
                &error) :
            brick_controller_new(platform_view, app.view, input_device,
                &error);
        if (!controller)
            return g_printerr("%s\n", error->message), 1;
    }
    input = brick_osk_context_new(app.view);
    if (!input)
        return g_printerr("trusted OSK view unavailable\n"), 1;
    app.osk = BRICK_OSK_CONTEXT(input);
    webkit_web_view_set_input_method_context(app.view, input);
    g_signal_connect(input, "failed", G_CALLBACK(osk_failed), &app);
    if (controller)
        brick_controller_set_osk(controller, app.osk);
    g_signal_connect(app.view, "decide-policy", G_CALLBACK(decide_policy), &app);
    g_signal_connect(app.view, "create", G_CALLBACK(deny_popup), &app);
    g_signal_connect(app.view, "permission-request",
        G_CALLBACK(deny_permission), &app);
    g_signal_connect(app.view, "run-file-chooser",
        G_CALLBACK(deny_file_chooser), &app);
    g_signal_connect(app.view, "load-changed", G_CALLBACK(load_changed), &app);
    webkit_web_view_add_frame_displayed_callback(app.view, frame_displayed,
        &app, NULL);
    g_signal_connect(app.view, "close", G_CALLBACK(view_closed), &app);
    g_signal_connect(session, "download-started", G_CALLBACK(cancel_download),
        &app);
    GIOChannel *channel = g_io_channel_unix_new(authority_fd);
    g_io_add_watch(channel, G_IO_IN | G_IO_HUP | G_IO_ERR,
        authority_input, &app);
    g_io_channel_unref(channel);
    brick_captive_lifecycle_event(&app.lifecycle, "ready");
    authority_send(&app, "READY", NULL);
    webkit_web_view_load_uri(app.view, uri);
#ifdef BRICK_OSK_QUALIFICATION
    if (qualify_osk)
        g_timeout_add(50, qualification_step, &app);
#endif
    g_main_loop_run(app.loop);
    authority_send(&app, "CLOSED", app.lifecycle == BRICK_CAPTIVE_AUTHORIZED ?
        "success" : "failed");
    brick_controller_free(controller);
    g_object_unref(app.view);
    g_main_loop_unref(app.loop);
    return app.lifecycle == BRICK_CAPTIVE_AUTHORIZED ? 0 : 1;
}
