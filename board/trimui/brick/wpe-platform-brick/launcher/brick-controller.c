#define _GNU_SOURCE
#include "brick-controller.h"
#include "brick-osk-context.h"

#include <errno.h>
#include <fcntl.h>
#include <glib-unix.h>
#include <linux/input.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <wpe/WPEKeysyms.h>

#define BRICK_BTN_SOUTH BTN_SOUTH
#define BRICK_BTN_EAST BTN_EAST
#define BRICK_BTN_MODE BTN_MODE
#define BRICK_BTN_VIEW 0x2c0
#define BRICK_BTN_FILTER 0x2c1

struct _BrickController {
    WPEView *view;
    WebKitWebView *web_view;
    int fd;
    guint source;
    guint reconnect_source;
    char *device;
    double x;
    double y;
    gboolean primary_down;
    gboolean enter_down;
    gboolean dropped;
    BrickOskContext *osk;
};

static gboolean reconnect_controller(gpointer user_data);

static guint32 event_time(const struct input_event *event)
{
    guint64 milliseconds = (guint64)event->time.tv_sec * 1000 +
        (guint64)event->time.tv_usec / 1000;
    return milliseconds ? (guint32)milliseconds :
        (guint32)(g_get_monotonic_time() / 1000);
}

static void send_pointer_button(BrickController *self, gboolean down,
    guint32 time)
{
    g_autoptr(WPEEvent) event = wpe_event_pointer_button_new(down ?
        WPE_EVENT_POINTER_DOWN : WPE_EVENT_POINTER_UP, self->view,
        WPE_INPUT_SOURCE_MOUSE, time, 0, WPE_BUTTON_PRIMARY,
        self->x, self->y, down ? 1 : 0);
    wpe_view_event(self->view, event);
    self->primary_down = down;
}

static void send_enter(BrickController *self, gboolean down, guint32 time)
{
    g_autoptr(WPEEvent) event = wpe_event_keyboard_new(down ?
        WPE_EVENT_KEYBOARD_KEY_DOWN : WPE_EVENT_KEYBOARD_KEY_UP, self->view,
        WPE_INPUT_SOURCE_KEYBOARD, time, 0, KEY_ENTER,
        WPE_KEY_Return);
    wpe_view_event(self->view, event);
    self->enter_down = down;
}

static void release_held(BrickController *self, guint32 time)
{
    if (self->primary_down)
        send_pointer_button(self, FALSE, time);
    if (self->enter_down)
        send_enter(self, FALSE, time);
}

static void resync(BrickController *self, guint32 time)
{
    unsigned long keys[(KEY_MAX + 8 * sizeof(long)) / (8 * sizeof(long))] = { 0 };
    gboolean primary;
    gboolean enter;

    if (ioctl(self->fd, EVIOCGKEY(sizeof(keys)), keys) < 0) {
        release_held(self, time);
        return;
    }
#define PRESSED(code) (keys[(code) / (8 * sizeof(long))] & \
    (1UL << ((code) % (8 * sizeof(long)))))
    primary = !!PRESSED(BRICK_BTN_EAST);
    enter = !!PRESSED(BRICK_BTN_MODE);
#undef PRESSED
    if (primary != self->primary_down)
        send_pointer_button(self, primary, time);
    if (enter != self->enter_down)
        send_enter(self, enter, time);
}

static void send_motion(BrickController *self, unsigned code, int value,
    guint32 time)
{
    int step = value == 2 ? 30 : 18;
    double old_x = self->x;
    double old_y = self->y;
    int width = MAX(wpe_view_get_width(self->view), 1);
    int height = MAX(wpe_view_get_height(self->view), 1);

    if (code == KEY_LEFT)
        self->x -= step;
    else if (code == KEY_RIGHT)
        self->x += step;
    else if (code == KEY_UP)
        self->y -= step;
    else
        self->y += step;
    self->x = CLAMP(self->x, 0, width - 1);
    self->y = CLAMP(self->y, 0, height - 1);
    g_autoptr(WPEEvent) event = wpe_event_pointer_move_new(
        WPE_EVENT_POINTER_MOVE, self->view, WPE_INPUT_SOURCE_MOUSE, time,
        0, self->x, self->y, self->x - old_x,
        self->y - old_y);
    wpe_view_event(self->view, event);
}

static void handle_key(BrickController *self, const struct input_event *event)
{
    guint32 time = event_time(event);
    gboolean down = event->value != 0;

    BrickOskButton osk_button;
    gboolean osk_mapped = TRUE;
    switch (event->code) {
    case KEY_UP: osk_button = BRICK_OSK_BUTTON_UP; break;
    case KEY_DOWN: osk_button = BRICK_OSK_BUTTON_DOWN; break;
    case KEY_LEFT: osk_button = BRICK_OSK_BUTTON_LEFT; break;
    case KEY_RIGHT: osk_button = BRICK_OSK_BUTTON_RIGHT; break;
    case BRICK_BTN_EAST: osk_button = BRICK_OSK_BUTTON_SELECT; break;
    case BRICK_BTN_SOUTH: osk_button = BRICK_OSK_BUTTON_BACKSPACE; break;
    case BRICK_BTN_VIEW: osk_button = BRICK_OSK_BUTTON_CARET_LEFT; break;
    case BRICK_BTN_FILTER: osk_button = BRICK_OSK_BUTTON_CARET_RIGHT; break;
    case BRICK_BTN_MODE: osk_button = BRICK_OSK_BUTTON_CLOSE; break;
    default: osk_mapped = FALSE; break;
    }
    if (osk_mapped && self->osk &&
        brick_osk_context_handle_button(self->osk, osk_button, down)) {
        /* The click which focused the field may still be physically held when
         * the trusted keyboard takes over. Balance portal events before
         * consuming subsequent controller input. */
        if (self->primary_down)
            send_pointer_button(self, FALSE, time);
        if (self->enter_down)
            send_enter(self, FALSE, time);
        return;
    }

    switch (event->code) {
    case BRICK_BTN_EAST:
        if (event->value != 2 && down != self->primary_down)
            send_pointer_button(self, down, time);
        break;
    case BRICK_BTN_MODE:
        if (event->value != 2 && down != self->enter_down)
            send_enter(self, down, time);
        break;
    case BRICK_BTN_SOUTH:
        if (event->value == 1 && webkit_web_view_can_go_back(self->web_view))
            webkit_web_view_go_back(self->web_view);
        break;
    case BRICK_BTN_VIEW:
    case BRICK_BTN_FILTER:
        if (down) {
            double dy = event->code == BRICK_BTN_VIEW ? -120 : 120;
            g_autoptr(WPEEvent) scroll = wpe_event_scroll_new(self->view,
                WPE_INPUT_SOURCE_MOUSE, time, 0, 0, dy,
                FALSE, FALSE, self->x, self->y);
            wpe_view_event(self->view, scroll);
        }
        break;
    case KEY_UP:
    case KEY_DOWN:
    case KEY_LEFT:
    case KEY_RIGHT:
        if (down)
            send_motion(self, event->code, event->value, time);
        break;
    default:
        break;
    }
}

void brick_controller_set_osk(BrickController *self, BrickOskContext *osk)
{
    g_return_if_fail(self);
    self->osk = osk;
}

static gboolean input_ready(int fd, GIOCondition condition, gpointer user_data)
{
    BrickController *self = user_data;
    struct input_event event;
    ssize_t size;


    if (condition & (G_IO_ERR | G_IO_HUP)) {
        release_held(self, (guint32)(g_get_monotonic_time() / 1000));
        self->source = 0;
        close(self->fd);
        self->fd = -1;
        self->reconnect_source = g_timeout_add_seconds(1,
            reconnect_controller, self);
        return G_SOURCE_REMOVE;
    }
    while ((size = read(fd, &event, sizeof(event))) == sizeof(event)) {
        if (event.type == EV_SYN && event.code == SYN_DROPPED) {
            self->dropped = TRUE;
            continue;
        }
        if (self->dropped) {
            if (event.type == EV_SYN && event.code == SYN_REPORT) {
                self->dropped = FALSE;
                resync(self, event_time(&event));
            }
            continue;
        }
        if (event.type == EV_KEY && event.value >= 0 && event.value <= 2)
            handle_key(self, &event);
    }
    if (size < 0 && (errno == EAGAIN || errno == EINTR))
        return G_SOURCE_CONTINUE;
    if (size == 0)
        return G_SOURCE_CONTINUE;
    release_held(self, (guint32)(g_get_monotonic_time() / 1000));
    self->source = 0;
    close(self->fd);
    self->fd = -1;
    self->reconnect_source = g_timeout_add_seconds(1, reconnect_controller,
        self);
    return G_SOURCE_REMOVE;
}

static gboolean reconnect_controller(gpointer user_data)
{
    BrickController *self = user_data;

    if (!self->device) {
        self->reconnect_source = 0;
        return G_SOURCE_REMOVE;
    }
    self->fd = open(self->device, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (self->fd < 0 || ioctl(self->fd, EVIOCGRAB, 1) < 0) {
        if (self->fd >= 0)
            close(self->fd);
        self->fd = -1;
        return G_SOURCE_CONTINUE;
    }
    self->dropped = FALSE;
    resync(self, (guint32)(g_get_monotonic_time() / 1000));
    self->source = g_unix_fd_add(self->fd, G_IO_IN | G_IO_ERR | G_IO_HUP,
        input_ready, self);
    self->reconnect_source = 0;
    return G_SOURCE_REMOVE;
}

BrickController *brick_controller_new(WPEView *view, WebKitWebView *web_view,
    const char *device, GError **error)
{
    g_return_val_if_fail(WPE_IS_VIEW(view), NULL);
    g_return_val_if_fail(WEBKIT_IS_WEB_VIEW(web_view), NULL);
    g_return_val_if_fail(device, NULL);
    BrickController *self = g_new0(BrickController, 1);
    self->view = view;
    self->web_view = web_view;
    self->device = g_strdup(device);
    self->fd = open(device, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (self->fd < 0 || ioctl(self->fd, EVIOCGRAB, 1) < 0) {
        int saved_errno = errno;
        if (self->fd >= 0)
            close(self->fd);
        g_set_error(error, G_FILE_ERROR, g_file_error_from_errno(saved_errno),
            "cannot open/grab controller %s: %s", device,
            g_strerror(saved_errno));
        g_free(self->device);
        g_free(self);
        return NULL;
    }
    self->x = MAX(wpe_view_get_width(view), 1) / 2.0;
    self->y = MAX(wpe_view_get_height(view), 1) / 2.0;
    resync(self, (guint32)(g_get_monotonic_time() / 1000));
    self->source = g_unix_fd_add(self->fd, G_IO_IN | G_IO_ERR | G_IO_HUP,
        input_ready, self);
    wpe_view_focus_in(view);
    return self;
}

BrickController *brick_controller_new_from_fd(WPEView *view,
    WebKitWebView *web_view, int fd, GError **error)
{
    g_return_val_if_fail(WPE_IS_VIEW(view), NULL);
    g_return_val_if_fail(WEBKIT_IS_WEB_VIEW(web_view), NULL);
    BrickController *self = g_new0(BrickController, 1);
    self->view = view;
    self->web_view = web_view;
    self->fd = fd;
    if (fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) < 0 ||
        fcntl(fd, F_SETFD, fcntl(fd, F_GETFD) | FD_CLOEXEC) < 0 ||
        ioctl(fd, EVIOCGRAB, 1) < 0) {
        int saved_errno = errno;
        g_set_error(error, G_FILE_ERROR, g_file_error_from_errno(saved_errno),
            "cannot configure/grab controller fd: %s", g_strerror(saved_errno));
        g_free(self);
        return NULL;
    }
    self->x = MAX(wpe_view_get_width(view), 1) / 2.0;
    self->y = MAX(wpe_view_get_height(view), 1) / 2.0;
    resync(self, (guint32)(g_get_monotonic_time() / 1000));
    self->source = g_unix_fd_add(fd, G_IO_IN | G_IO_ERR | G_IO_HUP,
        input_ready, self);
    wpe_view_focus_in(view);
    return self;
}

void brick_controller_free(BrickController *self)
{
    if (!self)
        return;
    release_held(self, (guint32)(g_get_monotonic_time() / 1000));
    wpe_view_focus_out(self->view);
    if (self->source)
        g_source_remove(self->source);
    if (self->reconnect_source)
        g_source_remove(self->reconnect_source);
    if (self->fd >= 0) {
        (void)ioctl(self->fd, EVIOCGRAB, 0);
        close(self->fd);
    }
    g_free(self->device);
    g_free(self);
}
