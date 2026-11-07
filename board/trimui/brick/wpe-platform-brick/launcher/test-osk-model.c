#include "brick-osk-model.h"

#include <glib.h>
#include <string.h>

static void test_navigation_and_commit(void)
{
    BrickOskModel model;
    brick_osk_model_init(&model, 0, 0);
    g_assert_cmpstr(brick_osk_model_layout_name(&model), ==, "Text keyboard");
    BrickOskOperation operation = brick_osk_model_handle(&model,
        BRICK_OSK_BUTTON_SELECT, TRUE);
    g_assert_cmpint(operation.kind, ==, BRICK_OSK_OPERATION_PREEDIT);
    g_assert_cmpstr(operation.text, ==, "q");
    operation = brick_osk_model_handle(&model, BRICK_OSK_BUTTON_SELECT, FALSE);
    g_assert_cmpint(operation.kind, ==, BRICK_OSK_OPERATION_COMMIT);
    g_assert_cmpstr(operation.text, ==, "q");
    operation = brick_osk_model_handle(&model, BRICK_OSK_BUTTON_RIGHT, TRUE);
    g_assert_cmpint(operation.kind, ==, BRICK_OSK_OPERATION_RENDER);
}

static void test_non_ascii_and_password(void)
{
    BrickOskModel model;
    brick_osk_model_init(&model, 6, 0);
    g_assert_cmpstr(brick_osk_model_layout_name(&model), ==,
        "Password keyboard");
    model.row = 2;
    model.column = 7;
    BrickOskOperation operation = brick_osk_model_handle(&model,
        BRICK_OSK_BUTTON_SELECT, TRUE);
    g_assert_cmpstr(operation.text, ==, "é");
    g_assert_true(g_utf8_validate(operation.text, -1, NULL));
    operation = brick_osk_model_handle(&model,
        BRICK_OSK_BUTTON_SELECT, FALSE);
    g_assert_cmpstr(operation.text, ==, "é");
}

static void test_purpose_actions_and_html(void)
{
    BrickOskModel model;
    brick_osk_model_init(&model, 1, 0);
    g_assert_cmpstr(brick_osk_model_layout_name(&model), ==,
        "Numeric keyboard");
    g_assert_cmpint(brick_osk_model_handle(&model,
        BRICK_OSK_BUTTON_BACKSPACE, TRUE).kind, ==,
        BRICK_OSK_OPERATION_DELETE);
    g_assert_cmpint(brick_osk_model_handle(&model,
        BRICK_OSK_BUTTON_CARET_LEFT, TRUE).kind, ==,
        BRICK_OSK_OPERATION_CARET_LEFT);
    g_assert_cmpint(brick_osk_model_handle(&model,
        BRICK_OSK_BUTTON_CARET_RIGHT, TRUE).kind, ==,
        BRICK_OSK_OPERATION_CARET_RIGHT);
    g_assert_cmpint(brick_osk_model_handle(&model,
        BRICK_OSK_BUTTON_CLOSE, TRUE).kind, ==,
        BRICK_OSK_OPERATION_HIDE);
    g_assert_cmpstr(brick_osk_model_key_label(&model, 0, 0), ==, "1");
}

static void test_shift_and_uppercase_hint(void)
{
    BrickOskModel model;
    brick_osk_model_init(&model, 0, 1u << 2);
    g_assert_cmpstr(brick_osk_model_layout_name(&model), ==,
        "Uppercase keyboard");
    BrickOskOperation operation = brick_osk_model_handle(&model,
        BRICK_OSK_BUTTON_SELECT, TRUE);
    g_assert_cmpstr(operation.text, ==, "Q");

    brick_osk_model_set_content_type(&model, 0, 0);
    model.row = 3;
    model.column = 5;
    operation = brick_osk_model_handle(&model, BRICK_OSK_BUTTON_SELECT, TRUE);
    g_assert_cmpint(operation.kind, ==, BRICK_OSK_OPERATION_RENDER);
    g_assert_cmpstr(brick_osk_model_layout_name(&model), ==,
        "Uppercase keyboard");
}

static void test_every_layout_label(void)
{
    BrickOskModel model;
    brick_osk_model_init(&model, 0, 0);
    for (guint layout = 0; layout < 3; layout++) {
        model.layout = layout;
        for (guint row = 0; row < 4; row++) {
            for (guint column = 0; column < 10; column++) {
                const char *label = brick_osk_model_key_label(&model, row,
                    column);
                g_assert_nonnull(label);
                g_assert_true(*label);
                g_assert_true(g_utf8_validate(label, -1, NULL));
            }
        }
    }
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/osk/navigation-commit", test_navigation_and_commit);
    g_test_add_func("/osk/non-ascii-password", test_non_ascii_and_password);
    g_test_add_func("/osk/purpose-actions-labels", test_purpose_actions_and_html);
    g_test_add_func("/osk/shift-uppercase-hint", test_shift_and_uppercase_hint);
    g_test_add_func("/osk/every-layout-label", test_every_layout_label);
    return g_test_run();
}
