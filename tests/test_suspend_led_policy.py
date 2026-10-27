import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SuspendLedPolicyTest(unittest.TestCase):
    def test_exact_standard_led_state_restored_after_success_and_partial_failure(self):
        header = ROOT / 'package/brick-power/led-policy.h'
        harness = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
static char root[1024], lock_path[1024];
static int write_count, fail_write;
static int test_open(const char *path, int flags, ...)
{
    mode_t mode = 0;
    if (flags & O_CREAT) {
        va_list args;
        va_start(args, flags); mode = va_arg(args, int); va_end(args);
    }
    if ((flags & O_WRONLY) && strstr(path, "/brightness"))
        flags |= O_TRUNC;
    return open(path, flags, mode);
}
static ssize_t test_write(int fd, const void *buffer, size_t size)
{
    ssize_t result = write(fd, buffer, size);
    write_count++;
    if (write_count == fail_write) { errno = EIO; return -1; }
    return result;
}
#define LED_CLASS_PATH root
#define BRICK_LED_LOCK lock_path
#define open test_open
#define write test_write
#include "HEADER"
#undef write
#undef open
static void put(const char *path, int value)
{
    FILE *file = fopen(path, "w");
    assert(file && fprintf(file, "%d\n", value) > 0 && !fclose(file));
}
static int get(unsigned index)
{
    char path[1200];
    snprintf(path, sizeof(path), "%s/rgb:indicator-%u/brightness", root, index);
    FILE *file = fopen(path, "r");
    int value = -1;
    assert(file && fscanf(file, "%d", &value) == 1 && !fclose(file));
    return value;
}
static void reset_values(void)
{
    char path[1200];
    for (unsigned i = 0; i < BRICK_LED_COUNT; i++) {
        snprintf(path, sizeof(path), "%s/rgb:indicator-%u/brightness", root, i);
        put(path, i ? (int)(i * 7 % 256) : 0);
    }
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    snprintf(root, sizeof(root), "%s/leds", argv[1]);
    snprintf(lock_path, sizeof(lock_path), "%s/lock", argv[1]);
    assert(!mkdir(root, 0700));
    char path[1200];
    for (unsigned i = 0; i < BRICK_LED_COUNT; i++) {
        snprintf(path, sizeof(path), "%s/rgb:indicator-%u", root, i);
        assert(!mkdir(path, 0700));
        size_t length = strlen(path);
        strcpy(path + length, "/max_brightness"); put(path, 255);
        strcpy(path + length, "/brightness"); put(path, i ? (int)(i * 7 % 256) : 0);
        strcpy(path + length, "/trigger");
        FILE *trigger = fopen(path, "w");
        assert(trigger && fputs("[none] timer heartbeat\n", trigger) >= 0 && !fclose(trigger));
    }
    struct led_policy_state state = { .lock = -1 };
    assert(!led_policy_quiesce(&state));
    for (unsigned i = 0; i < BRICK_LED_COUNT; i++) assert(get(i) == 0);
    assert(!led_policy_restore(&state));
    for (unsigned i = 0; i < BRICK_LED_COUNT; i++)
        assert(get(i) == (i ? (int)(i * 7 % 256) : 0));

    reset_values(); write_count = 0;
    state = (struct led_policy_state){ .lock = -1 };
    assert(!led_policy_quiesce(&state));
    snprintf(path, sizeof(path), "%s/rgb:indicator-7/trigger", root);
    FILE *trigger = fopen(path, "w");
    assert(trigger && fputs("none timer [heartbeat]\n", trigger) >= 0 && !fclose(trigger));
    assert(led_policy_restore(&state) < 0);
    assert(get(7) == 0); /* trigger ownership wins over the stale snapshot */
    trigger = fopen(path, "w");
    assert(trigger && fputs("[none] timer heartbeat\n", trigger) >= 0 && !fclose(trigger));

    reset_values(); write_count = 0; fail_write = 4;
    state = (struct led_policy_state){ .lock = -1 };
    assert(led_policy_quiesce(&state) < 0); /* write reached hardware, then errored */
    fail_write = 0;
    assert(!led_policy_restore(&state));
    for (unsigned i = 0; i < BRICK_LED_COUNT; i++)
        assert(get(i) == (i ? (int)(i * 7 % 256) : 0));

    snprintf(path, sizeof(path), "%s/rgb:indicator-22/max_brightness", root);
    put(path, 127); reset_values(); write_count = 0;
    state = (struct led_policy_state){ .lock = -1 };
    assert(led_policy_quiesce(&state) < 0);
    assert(!led_policy_restore(&state));
    assert(write_count == 0); /* validate all 23 devices before changing one */

    put(path, 255);
    snprintf(path, sizeof(path), "%s/rgb:indicator-7/trigger", root);
    trigger = fopen(path, "w");
    assert(trigger && fputs("none timer [heartbeat]\n", trigger) >= 0 && !fclose(trigger));
    state = (struct led_policy_state){ .lock = -1 };
    assert(led_policy_quiesce(&state) < 0);
    assert(!led_policy_restore(&state));
    assert(write_count == 0); /* never detach an application-owned trigger */
    puts("PASS exact 23-device LED quiesce and wake/abort restoration");
}
'''.replace('"HEADER"', '"' + str(header) + '"')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'test.c'
            binary = Path(directory) / 'test'
            source.write_text(harness)
            subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O2',
                            '-Wall', '-Wextra', '-Werror', str(source), '-o', str(binary)],
                           check=True)
            subprocess.run([str(binary), directory], check=True, timeout=5)

    def test_automatic_policy_is_opt_in_and_reuses_explicit_transaction(self):
        daemon = (ROOT / 'package/brick-power/brick-power-key.c').read_text()
        suspend = (ROOT / 'package/brick-power/brick-suspend.c').read_text()
        init = (ROOT / 'board/trimui/brick/rootfs-overlay/etc/init.d/S49powerkey').read_text()
        self.assertIn('BRICK_SUSPEND_SECONDS=${BRICK_SUSPEND_SECONDS:-0}', init)
        self.assertIn('execl("/usr/sbin/brick-suspend", "brick-suspend",', daemon)
        self.assertIn('led_policy_quiesce(&leds)', suspend)
        self.assertGreaterEqual(suspend.count('led_policy_restore(&leds)'), 2)
        self.assertIn('#ifdef BRICK_DEEP_SUSPEND', suspend)
        self.assertIn('write_value(BRICK_POWER_STATE_PATH, "mem\\n")', suspend)


if __name__ == '__main__':
    unittest.main()
