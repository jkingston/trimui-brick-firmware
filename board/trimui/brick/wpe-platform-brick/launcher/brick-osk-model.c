#include "brick-osk-model.h"

#include <string.h>

#define ROWS 4
#define COLUMNS 10

typedef enum {
    KEY_TEXT,
    KEY_SPACE,
    KEY_BACKSPACE,
    KEY_LAYOUT,
    KEY_SHIFT,
    KEY_DONE,
} KeyKind;

typedef struct {
    const char *label;
    const char *text;
    KeyKind kind;
} Key;

#define T(value) { value, value, KEY_TEXT }
#define S(label, kind) { label, NULL, kind }

static const Key latin[ROWS][COLUMNS] = {
    { T("q"), T("w"), T("e"), T("r"), T("t"), T("y"), T("u"), T("i"), T("o"), T("p") },
    { T("a"), T("s"), T("d"), T("f"), T("g"), T("h"), T("j"), T("k"), T("l"), T("'") },
    { T("z"), T("x"), T("c"), T("v"), T("b"), T("n"), T("m"), T("é"), T("å"), T(".") },
    { S("123", KEY_LAYOUT), T("@"), T("-"), T("_"), T("/"), S("⇧", KEY_SHIFT), S("Space", KEY_SPACE), S("⌫", KEY_BACKSPACE), T("?"), S("Done", KEY_DONE) },
};

static const Key uppercase[ROWS][COLUMNS] = {
    { T("Q"), T("W"), T("E"), T("R"), T("T"), T("Y"), T("U"), T("I"), T("O"), T("P") },
    { T("A"), T("S"), T("D"), T("F"), T("G"), T("H"), T("J"), T("K"), T("L"), T("'") },
    { T("Z"), T("X"), T("C"), T("V"), T("B"), T("N"), T("M"), T("É"), T("Å"), T(".") },
    { S("123", KEY_LAYOUT), T("@"), T("-"), T("_"), T("/"), S("⇩", KEY_SHIFT), S("Space", KEY_SPACE), S("⌫", KEY_BACKSPACE), T("?"), S("Done", KEY_DONE) },
};

static const Key numeric[ROWS][COLUMNS] = {
    { T("1"), T("2"), T("3"), T("4"), T("5"), T("6"), T("7"), T("8"), T("9"), T("0") },
    { T("+"), T("-"), T("."), T(","), T("/"), T(":"), T("("), T(")"), T("#"), T("*") },
    { T("@"), T("_"), T("="), T("%"), T("&"), T("?"), T("!"), T("'"), T("\""), T(";") },
    { S("ABC", KEY_LAYOUT), T("é"), T("å"), T("$"), T("€"), S("Space", KEY_SPACE), T("."), S("⌫", KEY_BACKSPACE), T("/"), S("Done", KEY_DONE) },
};

static const Key *key_at(const BrickOskModel *model)
{
    if (model->layout == 1)
        return &numeric[model->row][model->column];
    if (model->layout == 2)
        return &uppercase[model->row][model->column];
    return &latin[model->row][model->column];
}

static guint default_layout(guint purpose, guint hints)
{
    /* WebKitInputPurpose DIGITS, NUMBER and PHONE are values 1..3. */
    if (purpose >= 1 && purpose <= 3)
        return 1;
    /* WebKitInputHint UPPERCASE_CHARS is bit 2. */
    return hints & (1u << 2) ? 2 : 0;
}

void brick_osk_model_init(BrickOskModel *model, guint purpose, guint hints)
{
    g_return_if_fail(model);
    memset(model, 0, sizeof(*model));
    model->purpose = purpose;
    model->hints = hints;
    model->layout = default_layout(purpose, hints);
}

void brick_osk_model_set_content_type(BrickOskModel *model, guint purpose,
    guint hints)
{
    g_return_if_fail(model);
    model->purpose = purpose;
    model->hints = hints;
    model->layout = default_layout(purpose, hints);
    model->row = 0;
    model->column = 0;
    model->selecting = FALSE;
    model->selected_text[0] = '\0';
}

BrickOskOperation brick_osk_model_handle(BrickOskModel *model,
    BrickOskButton button, gboolean pressed)
{
    g_return_val_if_fail(model,
        ((BrickOskOperation) { BRICK_OSK_OPERATION_NONE, NULL }));
    BrickOskOperation operation = { BRICK_OSK_OPERATION_NONE, NULL };

    if (button == BRICK_OSK_BUTTON_SELECT && !pressed) {
        if (!model->selecting)
            return operation;
        model->selecting = FALSE;
        operation.kind = BRICK_OSK_OPERATION_COMMIT;
        operation.text = model->selected_text;
        return operation;
    }
    if (!pressed)
        return operation;

    switch (button) {
    case BRICK_OSK_BUTTON_UP:
        model->row = (model->row + ROWS - 1) % ROWS;
        operation.kind = BRICK_OSK_OPERATION_RENDER;
        break;
    case BRICK_OSK_BUTTON_DOWN:
        model->row = (model->row + 1) % ROWS;
        operation.kind = BRICK_OSK_OPERATION_RENDER;
        break;
    case BRICK_OSK_BUTTON_LEFT:
        model->column = (model->column + COLUMNS - 1) % COLUMNS;
        operation.kind = BRICK_OSK_OPERATION_RENDER;
        break;
    case BRICK_OSK_BUTTON_RIGHT:
        model->column = (model->column + 1) % COLUMNS;
        operation.kind = BRICK_OSK_OPERATION_RENDER;
        break;
    case BRICK_OSK_BUTTON_BACKSPACE:
        operation.kind = BRICK_OSK_OPERATION_DELETE;
        break;
    case BRICK_OSK_BUTTON_CARET_LEFT:
        operation.kind = BRICK_OSK_OPERATION_CARET_LEFT;
        break;
    case BRICK_OSK_BUTTON_CARET_RIGHT:
        operation.kind = BRICK_OSK_OPERATION_CARET_RIGHT;
        break;
    case BRICK_OSK_BUTTON_CLOSE:
        operation.kind = BRICK_OSK_OPERATION_HIDE;
        break;
    case BRICK_OSK_BUTTON_SELECT: {
        const Key *key = key_at(model);
        switch (key->kind) {
        case KEY_TEXT:
        case KEY_SPACE:
            g_strlcpy(model->selected_text,
                key->kind == KEY_SPACE ? " " : key->text,
                sizeof(model->selected_text));
            model->selecting = TRUE;
            operation.kind = BRICK_OSK_OPERATION_PREEDIT;
            operation.text = model->selected_text;
            break;
        case KEY_BACKSPACE:
            operation.kind = BRICK_OSK_OPERATION_DELETE;
            break;
        case KEY_LAYOUT:
            model->layout = model->layout == 1 ? 0 : 1;
            model->row = 0;
            model->column = 0;
            operation.kind = BRICK_OSK_OPERATION_RENDER;
            break;
        case KEY_SHIFT:
            model->layout = model->layout == 2 ? 0 : 2;
            model->row = 0;
            model->column = 0;
            operation.kind = BRICK_OSK_OPERATION_RENDER;
            break;
        case KEY_DONE:
            operation.kind = BRICK_OSK_OPERATION_HIDE;
            break;
        }
        break;
    }
    }
    return operation;
}

const char *brick_osk_model_layout_name(const BrickOskModel *model)
{
    g_return_val_if_fail(model, "Keyboard");
    if (model->purpose == 6)
        return "Password keyboard";
    if (model->purpose == 4)
        return "URL keyboard";
    if (model->purpose == 5)
        return "Email keyboard";
    if (model->purpose == 7)
        return "Search keyboard";
    if (model->purpose >= 1 && model->purpose <= 3)
        return "Numeric keyboard";
    if (model->layout == 1)
        return "Symbols keyboard";
    if (model->layout == 2)
        return "Uppercase keyboard";
    return "Text keyboard";
}

const char *brick_osk_model_key_label(const BrickOskModel *model, guint row,
    guint column)
{
    g_return_val_if_fail(model, "");
    g_return_val_if_fail(row < ROWS && column < COLUMNS, "");
    if (model->layout == 1)
        return numeric[row][column].label;
    if (model->layout == 2)
        return uppercase[row][column].label;
    return latin[row][column].label;
}
