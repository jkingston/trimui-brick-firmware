#include "captive-policy.h"
#include <glib/gstdio.h>
#include <string.h>

static void test_urls(void)
{
    g_assert_true(brick_captive_uri_allowed("http://portal.test/login?x=1"));
    g_assert_true(brick_captive_uri_allowed("https://id.example.test/"));
    g_assert_true(brick_captive_uri_allowed("about:blank"));
    g_assert_false(brick_captive_uri_allowed("file:///etc/shadow"));
    g_assert_false(brick_captive_uri_allowed("data:text/html,pwn"));
    g_assert_false(brick_captive_uri_allowed("javascript:alert(1)"));
    g_assert_false(brick_captive_uri_allowed("https://user:pass@host/"));
}

static void test_profiles(void)
{
    g_autofree char *root = g_dir_make_tmp("brick-captive-test-XXXXXX", NULL);
    g_autofree char *a_data = NULL, *a_cache = NULL;
    g_autofree char *b_data = NULL, *b_cache = NULL;
    g_assert_true(brick_captive_profile_paths(root, "network-a", &a_data,
        &a_cache, NULL));
    g_assert_true(brick_captive_profile_paths(root, "network-b", &b_data,
        &b_cache, NULL));
    g_assert_cmpstr(a_data, !=, b_data);
    g_assert_false(strstr(a_data, "network-a") != NULL);
    g_assert_true(g_file_test(a_data, G_FILE_TEST_IS_DIR));
    g_assert_true(g_file_test(a_cache, G_FILE_TEST_IS_DIR));
    g_autofree char *a_base = g_path_get_dirname(a_data);
    g_autofree char *b_base = g_path_get_dirname(b_data);
    g_rmdir(a_data);
    g_rmdir(a_cache);
    g_rmdir(b_data);
    g_rmdir(b_cache);
    g_rmdir(a_base);
    g_rmdir(b_base);
    g_rmdir(root);
}

static void test_lifecycle(void)
{
    BrickCaptiveLifecycle state = BRICK_CAPTIVE_STARTING;
    g_assert_false(brick_captive_lifecycle_event(&state, "success"));
    g_assert_true(brick_captive_lifecycle_event(&state, "ready"));
    g_assert_true(brick_captive_lifecycle_event(&state, "success"));
    g_assert_true(brick_captive_lifecycle_event(&state, "close"));
    g_assert_false(brick_captive_lifecycle_event(&state, "ready"));
}

static void test_osk(void)
{
    BrickOskState state = BRICK_OSK_HIDDEN;
    state = brick_osk_transition(state, "focus-in");
    g_assert_cmpint(state, ==, BRICK_OSK_VISIBLE);
    state = brick_osk_transition(state, "preedit");
    g_assert_cmpint(state, ==, BRICK_OSK_PREEDIT);
    state = brick_osk_transition(state, "commit");
    g_assert_cmpint(state, ==, BRICK_OSK_VISIBLE);
    state = brick_osk_transition(state, "focus-out");
    g_assert_cmpint(state, ==, BRICK_OSK_HIDDEN);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/captive/url-policy", test_urls);
    g_test_add_func("/captive/profile-isolation", test_profiles);
    g_test_add_func("/captive/lifecycle", test_lifecycle);
    g_test_add_func("/captive/osk", test_osk);
    return g_test_run();
}
