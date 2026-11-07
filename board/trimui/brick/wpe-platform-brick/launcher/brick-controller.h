#pragma once

#include <wpe/webkit.h>
#include <wpe/wpe-platform.h>

typedef struct _BrickController BrickController;
typedef struct _BrickOskContext BrickOskContext;

BrickController *brick_controller_new(WPEView *view, WebKitWebView *web_view,
    const char *device, GError **error);
BrickController *brick_controller_new_from_fd(WPEView *view,
    WebKitWebView *web_view, int fd, GError **error);
void brick_controller_set_osk(BrickController *controller,
    BrickOskContext *osk);
void brick_controller_free(BrickController *controller);
