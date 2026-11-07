#define _GNU_SOURCE
#include "brick-osk-context.h"
#include "brick-osk-model.h"
#include "captive-policy.h"

#include <linux/input.h>
#include <string.h>
#include <wpe/WPEKeysyms.h>

#define OSK_ROWS 4
#define OSK_COLUMNS 10

struct _BrickOskContext {
    WebKitInputMethodContext parent;
    BrickOskState state;
    BrickOskModel model;
    gboolean preedit_enabled;
    char *preedit;
    WebKitWebView *portal_view;
    WPEView *portal_platform_view;
    guint render_source;
    guint late_portal_frames;
    guint render_failures;
    gboolean render_dirty;
    gboolean osk_buffer_in_flight;
    gboolean hide_pending;
    gboolean handoff_active;
    gboolean dismiss_requested;
};

G_DEFINE_TYPE(BrickOskContext, brick_osk_context,
    WEBKIT_TYPE_INPUT_METHOD_CONTEXT)

static guint failed_signal;

static const guint8 *glyph_rows(char character)
{
    static const guint8 glyphs[][7] = {
        {14,17,17,31,17,17,17}, {30,17,17,30,17,17,30},
        {14,17,16,16,16,17,14}, {30,17,17,17,17,17,30},
        {31,16,16,30,16,16,31}, {31,16,16,30,16,16,16},
        {14,17,16,23,17,17,15}, {17,17,17,31,17,17,17},
        {14,4,4,4,4,4,14}, {7,2,2,2,2,18,12},
        {17,18,20,24,20,18,17}, {16,16,16,16,16,16,31},
        {17,27,21,21,17,17,17}, {17,25,21,19,17,17,17},
        {14,17,17,17,17,17,14}, {30,17,17,30,16,16,16},
        {14,17,17,17,21,18,13}, {30,17,17,30,20,18,17},
        {15,16,16,14,1,1,30}, {31,4,4,4,4,4,4},
        {17,17,17,17,17,17,14}, {17,17,17,17,17,10,4},
        {17,17,17,21,21,21,10}, {17,17,10,4,10,17,17},
        {17,17,10,4,4,4,4}, {31,1,2,4,8,16,31},
        {14,17,19,21,25,17,14}, {4,12,4,4,4,4,14},
        {14,17,1,2,4,8,31}, {30,1,1,14,1,1,30},
        {2,6,10,18,31,2,2}, {31,16,16,30,1,1,30},
        {14,16,16,30,17,17,14}, {31,1,2,4,8,8,8},
        {14,17,17,14,17,17,14}, {14,17,17,15,1,1,14},
    };
    static const guint8 blank[7] = { 0 };
    static const guint8 at[7] = {14,17,23,21,23,16,14};
    static const guint8 dash[7] = {0,0,0,31,0,0,0};
    static const guint8 underscore[7] = {0,0,0,0,0,0,31};
    static const guint8 slash[7] = {1,2,2,4,8,8,16};
    static const guint8 question[7] = {14,17,1,2,4,0,4};
    static const guint8 plus[7] = {0,4,4,31,4,4,0};
    static const guint8 period[7] = {0,0,0,0,0,12,12};
    static const guint8 comma[7] = {0,0,0,0,0,12,8};
    static const guint8 colon[7] = {0,12,12,0,12,12,0};
    static const guint8 left_paren[7] = {2,4,8,8,8,4,2};
    static const guint8 right_paren[7] = {8,4,2,2,2,4,8};
    static const guint8 hash[7] = {10,31,10,10,31,10,0};
    static const guint8 star[7] = {0,21,14,31,14,21,0};
    static const guint8 equals[7] = {0,31,0,31,0,0,0};
    static const guint8 percent[7] = {17,2,4,8,17,0,0};
    static const guint8 ampersand[7] = {12,18,20,8,21,18,13};
    static const guint8 exclamation[7] = {4,4,4,4,4,0,4};
    static const guint8 apostrophe[7] = {4,4,2,0,0,0,0};
    static const guint8 quote[7] = {10,10,5,0,0,0,0};
    static const guint8 semicolon[7] = {0,12,12,0,12,8,0};
    static const guint8 dollar[7] = {4,15,20,14,5,30,4};
    if (character >= 'a' && character <= 'z')
        character -= 'a' - 'A';
    if (character >= 'A' && character <= 'Z')
        return glyphs[character - 'A'];
    if (character >= '0' && character <= '9')
        return glyphs[26 + character - '0'];
    switch (character) {
    case ' ': return blank;
    case '@': return at;
    case '-': return dash;
    case '_': return underscore;
    case '/': return slash;
    case '?': return question;
    case '+': return plus;
    case '.': return period;
    case ',': return comma;
    case ':': return colon;
    case '(': return left_paren;
    case ')': return right_paren;
    case '#': return hash;
    case '*': return star;
    case '=': return equals;
    case '%': return percent;
    case '&': return ampersand;
    case '!': return exclamation;
    case '\'': return apostrophe;
    case '"': return quote;
    case ';': return semicolon;
    case '$': return dollar;
    default: break;
    }
    return NULL;
}

static void fill_rect(guint32 *pixels, guint width, guint height, guint x,
    guint y, guint rect_width, guint rect_height, guint32 color)
{
    if (x >= width || y >= height)
        return;
    rect_width = MIN(rect_width, width - x);
    rect_height = MIN(rect_height, height - y);
    for (guint row = 0; row < rect_height; row++)
        for (guint column = 0; column < rect_width; column++)
            pixels[(y + row) * width + x + column] = color;
}

static void draw_character(guint32 *pixels, guint width, guint height, guint x,
    guint y, char character, guint scale, guint32 color)
{
    const guint8 *rows = glyph_rows(character);
    if (!rows) {
        fill_rect(pixels, width, height, x, y, 5 * scale, scale, color);
        fill_rect(pixels, width, height, x, y + 6 * scale, 5 * scale, scale,
            color);
        fill_rect(pixels, width, height, x, y, scale, 7 * scale, color);
        fill_rect(pixels, width, height, x + 4 * scale, y, scale, 7 * scale,
            color);
        return;
    }
    for (guint row = 0; row < 7; row++)
        for (guint column = 0; column < 5; column++)
            if (rows[row] & (1u << (4 - column)))
                fill_rect(pixels, width, height, x + column * scale,
                    y + row * scale, scale, scale, color);
}

static const char *display_label(const char *label)
{
    if (!g_strcmp0(label, "⇧") || !g_strcmp0(label, "⇩"))
        return "SHIFT";
    if (!g_strcmp0(label, "⌫"))
        return "DEL";
    if (!g_strcmp0(label, "é") || !g_strcmp0(label, "É"))
        return "E*";
    if (!g_strcmp0(label, "å") || !g_strcmp0(label, "Å"))
        return "A*";
    if (!g_strcmp0(label, "€"))
        return "EUR";
    return label;
}

static void draw_text(guint32 *pixels, guint width, guint height, guint x,
    guint y, const char *text, guint scale, guint32 color)
{
    for (const char *cursor = text; *cursor; cursor++) {
        draw_character(pixels, width, height, x, y, *cursor, scale, color);
        x += 6 * scale;
    }
}

static gboolean render_keyboard(BrickOskContext *self)
{
    guint width = wpe_view_get_width(self->portal_platform_view);
    guint height = wpe_view_get_height(self->portal_platform_view);
    if (!width || !height || width > G_MAXUINT / 4 ||
            height > G_MAXSIZE / ((gsize)width * 4))
        return FALSE;
    gsize size = (gsize)width * height * 4;
    guint32 *pixels = g_malloc0(size);
    fill_rect(pixels, width, height, 0, 0, width, height, 0xff10141c);
    guint title_height = MAX(height / 7, 48u);
    fill_rect(pixels, width, height, 0, 0, width, title_height, 0xff172a3d);
    const char *title = brick_osk_model_layout_name(&self->model);
    guint title_scale = MAX(MIN(width / 220, title_height / 12), 2u);
    draw_text(pixels, width, height, 24, (title_height - 7 * title_scale) / 2,
        title, title_scale, 0xffffffff);

    guint gap = MAX(width / 170, 4u);
    guint key_width = (width - gap * (OSK_COLUMNS + 1)) / OSK_COLUMNS;
    guint key_height = (height - title_height - gap * (OSK_ROWS + 1)) /
        OSK_ROWS;
    for (guint row = 0; row < OSK_ROWS; row++) {
        for (guint column = 0; column < OSK_COLUMNS; column++) {
            guint x = gap + column * (key_width + gap);
            guint y = title_height + gap + row * (key_height + gap);
            gboolean selected = row == self->model.row &&
                column == self->model.column;
            fill_rect(pixels, width, height, x, y, key_width, key_height,
                selected ? 0xff43d9bd : 0xff253449);
            const char *label = display_label(brick_osk_model_key_label(
                &self->model, row, column));
            guint length = strlen(label);
            guint scale = length > 4 ? 2 : MAX(MIN(key_width / 12,
                key_height / 11), 2u);
            guint text_width = length * 6 * scale;
            draw_text(pixels, width, height,
                x + (key_width > text_width ? (key_width - text_width) / 2 : 1),
                y + (key_height - 7 * scale) / 2, label, scale,
                selected ? 0xff07120f : 0xffffffff);
        }
    }

    g_autoptr(GBytes) bytes = g_bytes_new_take(pixels, size);
    g_autoptr(WPEBufferSHM) buffer = wpe_buffer_shm_new(
        wpe_view_get_display(self->portal_platform_view), width, height,
        WPE_PIXEL_FORMAT_ARGB8888, bytes, width * 4);
    g_object_set_data(G_OBJECT(buffer), "brick-trusted-osk-buffer",
        GINT_TO_POINTER(1));
    g_autoptr(GError) error = NULL;
    if (!wpe_view_render_buffer(self->portal_platform_view,
            WPE_BUFFER(buffer), NULL, 0, &error)) {
        g_warning("trusted OSK render failed: %s", error->message);
        return FALSE;
    }
    return TRUE;
}

static gboolean render_keyboard_idle(gpointer user_data)
{
    BrickOskContext *self = user_data;
    self->render_source = 0;
    if (self->state == BRICK_OSK_HIDDEN || self->osk_buffer_in_flight)
        return G_SOURCE_REMOVE;
    self->render_dirty = FALSE;
    if (render_keyboard(self)) {
        self->osk_buffer_in_flight = TRUE;
        self->render_failures = 0;
    } else {
        self->render_failures++;
        if (self->render_failures <= 3) {
            self->render_dirty = TRUE;
            self->render_source = g_timeout_add_full(G_PRIORITY_DEFAULT, 100,
                render_keyboard_idle, g_object_ref(self), g_object_unref);
        } else {
            g_warning("trusted OSK render failed permanently");
            self->state = BRICK_OSK_HIDDEN;
            self->handoff_active = FALSE;
            self->hide_pending = FALSE;
            wpe_view_set_visible(self->portal_platform_view, TRUE);
            g_signal_emit(self, failed_signal, 0);
        }
    }
    return G_SOURCE_REMOVE;
}

static void schedule_keyboard_render(BrickOskContext *self, guint delay_ms)
{
    self->render_dirty = TRUE;
    if (!self->render_source && !self->osk_buffer_in_flight)
        self->render_source = g_timeout_add_full(G_PRIORITY_DEFAULT,
            delay_ms,
            render_keyboard_idle, g_object_ref(self), g_object_unref);
}

static void buffer_rendered(WPEView *view, WPEBuffer *buffer,
    gpointer user_data)
{
    BrickOskContext *self = user_data;
    (void)view;
    if (g_object_get_data(G_OBJECT(buffer), "brick-trusted-osk-buffer")) {
        self->osk_buffer_in_flight = FALSE;
        if (self->hide_pending || self->state == BRICK_OSK_HIDDEN) {
            self->hide_pending = FALSE;
            wpe_view_set_visible(self->portal_platform_view, TRUE);
        } else if (self->render_dirty)
            schedule_keyboard_render(self, 0);
        return;
    }
    if (self->state == BRICK_OSK_HIDDEN)
        return;
    self->late_portal_frames++;
    g_warning("portal frame arrived after trusted OSK handoff (%u)",
        self->late_portal_frames);
    /* One final frame may already be pending when mapped becomes false. Submit
     * again after that flip; persistent replacements are a backend contract
     * failure surfaced by the warning rather than silently showing the page. */
    if (self->render_source) {
        g_source_remove(self->render_source);
        self->render_source = 0;
    }
    if (self->late_portal_frames <= 3) {
        schedule_keyboard_render(self, 100);
        return;
    }
    g_warning("portal producer did not quiesce; trusted OSK aborted");
    self->state = BRICK_OSK_HIDDEN;
    self->handoff_active = FALSE;
    self->render_dirty = FALSE;
    self->hide_pending = FALSE;
    wpe_view_set_visible(self->portal_platform_view, TRUE);
    g_signal_emit(self, failed_signal, 0);
}

static void show_keyboard(BrickOskContext *self)
{
    if (!self->portal_platform_view)
        return;
    if (webkit_input_method_context_get_input_hints(
            WEBKIT_INPUT_METHOD_CONTEXT(self)) &
            WEBKIT_INPUT_HINT_INHIBIT_OSK)
        return;
    /* Unmapping tells WebKit to stop portal frames. The trusted UI process
     * then submits a WPE SHM buffer to the same DRM view: one toplevel, one
     * primary plane and no second WebProcess or page-visible DOM. */
    self->handoff_active = TRUE;
    self->late_portal_frames = 0;
    wpe_view_set_visible(self->portal_platform_view, FALSE);
    self->hide_pending = FALSE;
    self->render_failures = 0;
    /* Visibility reaches the WebProcess asynchronously. Wait two-plus refresh
     * periods; any late portal buffer-rendered signal resets this barrier. */
    schedule_keyboard_render(self, 100);
}

static void hide_keyboard(BrickOskContext *self)
{
    self->handoff_active = FALSE;
    if (self->render_source) {
        g_source_remove(self->render_source);
        self->render_source = 0;
    }
    self->render_dirty = FALSE;
    if (self->osk_buffer_in_flight) {
        self->hide_pending = TRUE;
        return;
    }
    if (self->portal_platform_view)
        wpe_view_set_visible(self->portal_platform_view, TRUE);
}

static void set_state(BrickOskContext *self, const char *event)
{
    BrickOskState next = brick_osk_transition(self->state, event);
    if (next == self->state)
        return;
    self->state = next;
    if (next == BRICK_OSK_HIDDEN)
        hide_keyboard(self);
    else
        show_keyboard(self);
}

static void input_properties_changed(GObject *object, GParamSpec *spec,
    gpointer user_data)
{
    BrickOskContext *self = BRICK_OSK_CONTEXT(object);
    if (self->render_source) {
        g_source_remove(self->render_source);
        self->render_source = 0;
    }
    (void)spec;
    (void)user_data;
    guint hints = webkit_input_method_context_get_input_hints(
        WEBKIT_INPUT_METHOD_CONTEXT(self));
    brick_osk_model_set_content_type(&self->model,
        webkit_input_method_context_get_input_purpose(
            WEBKIT_INPUT_METHOD_CONTEXT(self)), hints);
    if (hints & WEBKIT_INPUT_HINT_INHIBIT_OSK) {
        self->state = BRICK_OSK_HIDDEN;
        hide_keyboard(self);
        return;
    }
    if (self->state != BRICK_OSK_HIDDEN)
        show_keyboard(self);
}

static void send_key(BrickOskContext *self, guint key_code, guint key_value)
{
    guint32 time = (guint32)(g_get_monotonic_time() / 1000);
    g_autoptr(WPEEvent) down = wpe_event_keyboard_new(
        WPE_EVENT_KEYBOARD_KEY_DOWN, self->portal_platform_view,
        WPE_INPUT_SOURCE_KEYBOARD, time, 0, key_code, key_value);
    g_autoptr(WPEEvent) up = wpe_event_keyboard_new(
        WPE_EVENT_KEYBOARD_KEY_UP, self->portal_platform_view,
        WPE_INPUT_SOURCE_KEYBOARD, time, 0, key_code, key_value);
    wpe_view_event(self->portal_platform_view, down);
    wpe_view_event(self->portal_platform_view, up);
}

static void brick_osk_set_enable_preedit(WebKitInputMethodContext *context,
    gboolean enabled)
{
    BRICK_OSK_CONTEXT(context)->preedit_enabled = enabled;
}

static void brick_osk_get_preedit(WebKitInputMethodContext *context, char **text,
    GList **underlines, guint *cursor_offset)
{
    BrickOskContext *self = BRICK_OSK_CONTEXT(context);
    *text = g_strdup(self->preedit ? self->preedit : "");
    *underlines = NULL;
    *cursor_offset = self->preedit ? g_utf8_strlen(self->preedit, -1) : 0;
}

static gboolean brick_osk_filter_key(WebKitInputMethodContext *context,
    gpointer key_event)
{
    (void)context;
    (void)key_event;
    return FALSE;
}

static void brick_osk_focus_in(WebKitInputMethodContext *context)
{
    BrickOskContext *self = BRICK_OSK_CONTEXT(context);
    guint hints = webkit_input_method_context_get_input_hints(context);
    brick_osk_model_set_content_type(&self->model,
        webkit_input_method_context_get_input_purpose(context),
        hints);
    if (hints & WEBKIT_INPUT_HINT_INHIBIT_OSK) {
        self->state = BRICK_OSK_HIDDEN;
        hide_keyboard(self);
        return;
    }
    set_state(self, "focus-in");
}

static void brick_osk_focus_out(WebKitInputMethodContext *context)
{
    BrickOskContext *self = BRICK_OSK_CONTEXT(context);
    if (self->handoff_active)
        return;
    set_state(self, "focus-out");
}

#ifdef BRICK_OSK_QUALIFICATION
void brick_osk_context_qualification_focus_in(BrickOskContext *self)
{
    g_return_if_fail(BRICK_IS_OSK_CONTEXT(self));
    guint hints = webkit_input_method_context_get_input_hints(
        WEBKIT_INPUT_METHOD_CONTEXT(self)) & ~WEBKIT_INPUT_HINT_INHIBIT_OSK;
    brick_osk_model_set_content_type(&self->model,
        webkit_input_method_context_get_input_purpose(
            WEBKIT_INPUT_METHOD_CONTEXT(self)), hints);
    set_state(self, "focus-in");
}
#endif

static void brick_osk_reset(WebKitInputMethodContext *context)
{
    BrickOskContext *self = BRICK_OSK_CONTEXT(context);
    if (self->handoff_active && !self->dismiss_requested)
        return;
    self->dismiss_requested = FALSE;
    if (self->preedit && *self->preedit)
        g_signal_emit_by_name(self, "preedit-finished");
    g_clear_pointer(&self->preedit, g_free);
    set_state(self, "reset");
}

static void brick_osk_dispose(GObject *object)
{
    BrickOskContext *self = BRICK_OSK_CONTEXT(object);
    if (self->render_source) {
        g_source_remove(self->render_source);
        self->render_source = 0;
    }
    self->state = BRICK_OSK_HIDDEN;
    self->handoff_active = FALSE;
    self->dismiss_requested = FALSE;
    self->hide_pending = FALSE;
    self->osk_buffer_in_flight = FALSE;
    if (self->portal_platform_view)
        wpe_view_set_visible(self->portal_platform_view, TRUE);
    if (self->portal_platform_view)
        g_signal_handlers_disconnect_by_data(self->portal_platform_view, self);
    self->portal_platform_view = NULL;
    g_clear_object(&self->portal_view);
    G_OBJECT_CLASS(brick_osk_context_parent_class)->dispose(object);
}

static void brick_osk_finalize(GObject *object)
{
    BrickOskContext *self = BRICK_OSK_CONTEXT(object);
    g_free(self->preedit);
    G_OBJECT_CLASS(brick_osk_context_parent_class)->finalize(object);
}

static void brick_osk_context_class_init(BrickOskContextClass *klass)
{
    GObjectClass *object_class = G_OBJECT_CLASS(klass);
    WebKitInputMethodContextClass *input_class =
        WEBKIT_INPUT_METHOD_CONTEXT_CLASS(klass);
    object_class->dispose = brick_osk_dispose;
    object_class->finalize = brick_osk_finalize;
    input_class->set_enable_preedit = brick_osk_set_enable_preedit;
    input_class->get_preedit = brick_osk_get_preedit;
    input_class->filter_key_event = brick_osk_filter_key;
    input_class->notify_focus_in = brick_osk_focus_in;
    input_class->notify_focus_out = brick_osk_focus_out;
    input_class->reset = brick_osk_reset;
    failed_signal = g_signal_new("failed", G_TYPE_FROM_CLASS(klass),
        G_SIGNAL_RUN_LAST, 0, NULL, NULL, NULL, G_TYPE_NONE, 0);
}

static void brick_osk_context_init(BrickOskContext *self)
{
    self->state = BRICK_OSK_HIDDEN;
    brick_osk_model_init(&self->model, WEBKIT_INPUT_PURPOSE_FREE_FORM,
        WEBKIT_INPUT_HINT_NONE);
    g_signal_connect(self, "notify::input-purpose",
        G_CALLBACK(input_properties_changed), NULL);
    g_signal_connect(self, "notify::input-hints",
        G_CALLBACK(input_properties_changed), NULL);
}

WebKitInputMethodContext *brick_osk_context_new(WebKitWebView *portal_view)
{
    g_return_val_if_fail(WEBKIT_IS_WEB_VIEW(portal_view), NULL);
    BrickOskContext *self = g_object_new(BRICK_TYPE_OSK_CONTEXT, NULL);
    self->portal_view = g_object_ref(portal_view);
    self->portal_platform_view = webkit_web_view_get_wpe_view(portal_view);

    if (!self->portal_platform_view) {
        g_object_unref(self);
        return NULL;
    }
    g_signal_connect_object(self->portal_platform_view, "buffer-rendered",
        G_CALLBACK(buffer_rendered), self, 0);
    return WEBKIT_INPUT_METHOD_CONTEXT(self);
}

void brick_osk_context_set_preedit(BrickOskContext *self, const char *text)
{
    g_return_if_fail(BRICK_IS_OSK_CONTEXT(self));
    g_return_if_fail(text);
    if (!self->preedit_enabled)
        return;
    gboolean was_active = self->preedit && *self->preedit;
    g_free(self->preedit);
    self->preedit = g_strdup(text);
    if (!was_active && *text)
        g_signal_emit_by_name(self, "preedit-started");
    g_signal_emit_by_name(self, "preedit-changed");
    if (*text)
        set_state(self, "preedit");
    else {
        if (was_active)
            g_signal_emit_by_name(self, "preedit-finished");
        set_state(self, "commit");
    }
}

void brick_osk_context_commit(BrickOskContext *self, const char *text)
{
    g_return_if_fail(BRICK_IS_OSK_CONTEXT(self));
    g_return_if_fail(text);
    if (self->preedit && *self->preedit)
        g_signal_emit_by_name(self, "preedit-finished");
    g_clear_pointer(&self->preedit, g_free);
    g_signal_emit_by_name(self, "committed", text);
    set_state(self, "commit");
}

gboolean brick_osk_context_handle_button(BrickOskContext *self,
    BrickOskButton button, gboolean pressed)
{
    g_return_val_if_fail(BRICK_IS_OSK_CONTEXT(self), FALSE);
    if (self->state == BRICK_OSK_HIDDEN)
        return FALSE;
    BrickOskOperation operation = brick_osk_model_handle(&self->model,
        button, pressed);
    switch (operation.kind) {
    case BRICK_OSK_OPERATION_RENDER:
        schedule_keyboard_render(self, 0);
        break;
    case BRICK_OSK_OPERATION_PREEDIT:
        brick_osk_context_set_preedit(self, operation.text);
        break;
    case BRICK_OSK_OPERATION_COMMIT:
        brick_osk_context_commit(self, operation.text);
        break;
    case BRICK_OSK_OPERATION_DELETE:
        g_signal_emit_by_name(self, "delete-surrounding", -1, 1u);
        break;
    case BRICK_OSK_OPERATION_CARET_LEFT:
        send_key(self, KEY_LEFT, WPE_KEY_Left);
        break;
    case BRICK_OSK_OPERATION_CARET_RIGHT:
        send_key(self, KEY_RIGHT, WPE_KEY_Right);
        break;
    case BRICK_OSK_OPERATION_HIDE:
        brick_osk_context_dismiss(self);
        break;
    case BRICK_OSK_OPERATION_NONE:
        break;
    }
    return TRUE;
}

void brick_osk_context_dismiss(BrickOskContext *self)
{
    g_return_if_fail(BRICK_IS_OSK_CONTEXT(self));
    self->dismiss_requested = TRUE;
    webkit_input_method_context_reset(WEBKIT_INPUT_METHOD_CONTEXT(self));
}

gboolean brick_osk_context_is_visible(BrickOskContext *self)
{
    g_return_val_if_fail(BRICK_IS_OSK_CONTEXT(self), FALSE);
    return self->state != BRICK_OSK_HIDDEN;
}
