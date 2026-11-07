#pragma once

#include <glib.h>

typedef enum {
    BRICK_OSK_BUTTON_UP,
    BRICK_OSK_BUTTON_DOWN,
    BRICK_OSK_BUTTON_LEFT,
    BRICK_OSK_BUTTON_RIGHT,
    BRICK_OSK_BUTTON_SELECT,
    BRICK_OSK_BUTTON_BACKSPACE,
    BRICK_OSK_BUTTON_CARET_LEFT,
    BRICK_OSK_BUTTON_CARET_RIGHT,
    BRICK_OSK_BUTTON_CLOSE,
} BrickOskButton;

typedef enum {
    BRICK_OSK_OPERATION_NONE,
    BRICK_OSK_OPERATION_RENDER,
    BRICK_OSK_OPERATION_PREEDIT,
    BRICK_OSK_OPERATION_COMMIT,
    BRICK_OSK_OPERATION_DELETE,
    BRICK_OSK_OPERATION_CARET_LEFT,
    BRICK_OSK_OPERATION_CARET_RIGHT,
    BRICK_OSK_OPERATION_HIDE,
} BrickOskOperationKind;

typedef struct {
    BrickOskOperationKind kind;
    const char *text;
} BrickOskOperation;

typedef struct {
    guint purpose;
    guint hints;
    guint layout;
    guint row;
    guint column;
    gboolean selecting;
    char selected_text[16];
} BrickOskModel;

void brick_osk_model_init(BrickOskModel *model, guint purpose, guint hints);
void brick_osk_model_set_content_type(BrickOskModel *model, guint purpose,
    guint hints);
BrickOskOperation brick_osk_model_handle(BrickOskModel *model,
    BrickOskButton button, gboolean pressed);
const char *brick_osk_model_layout_name(const BrickOskModel *model);
const char *brick_osk_model_key_label(const BrickOskModel *model, guint row,
    guint column);
