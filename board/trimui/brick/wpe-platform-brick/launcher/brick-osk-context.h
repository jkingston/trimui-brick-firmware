#pragma once
#include "brick-osk-model.h"
#include <wpe/webkit.h>
#include <wpe/wpe-platform.h>

#define BRICK_TYPE_OSK_CONTEXT (brick_osk_context_get_type())
G_DECLARE_FINAL_TYPE(BrickOskContext, brick_osk_context, BRICK, OSK_CONTEXT,
    WebKitInputMethodContext)

WebKitInputMethodContext *brick_osk_context_new(WebKitWebView *portal_view);
void brick_osk_context_set_preedit(BrickOskContext *context, const char *text);
void brick_osk_context_commit(BrickOskContext *context, const char *text);
gboolean brick_osk_context_handle_button(BrickOskContext *context,
    BrickOskButton button, gboolean pressed);
void brick_osk_context_dismiss(BrickOskContext *context);
gboolean brick_osk_context_is_visible(BrickOskContext *context);
#ifdef BRICK_OSK_QUALIFICATION
void brick_osk_context_qualification_focus_in(BrickOskContext *context);
#endif
