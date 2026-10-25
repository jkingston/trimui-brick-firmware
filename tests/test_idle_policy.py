from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IdlePolicyTest(unittest.TestCase):
    def test_state_timing_and_brightness_ownership(self):
        source = ROOT / 'package/brick-power/brick-power-key.c'
        harness = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
#include <poll.h>
#include <errno.h>
#include <string.h>
static int fail_write;
static char brightness[1024], maximum[1024], lockpath[1024];
#define DISPLAY_BRIGHTNESS brightness
#define DISPLAY_MAXIMUM maximum
#define DISPLAY_LOCK lockpath
/* Regular fixtures emulate sysfs's replace-value write semantics. */
static int test_open(const char *path, int flags, ...)
{
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list args; va_start(args, flags); mode = va_arg(args, int); va_end(args); }
    if (!strcmp(path, brightness) && (flags & O_WRONLY)) {
        if (fail_write) { errno = EIO; return -1; }
        flags |= O_TRUNC;
    }
    return open(path, flags, mode);
}
#define open test_open
#define main unused_daemon_main
#include "SOURCE"
#undef main
#undef open
static void put(const char *path, const char *value)
{ FILE *f = fopen(path, "w"); assert(f); assert(fputs(value, f) >= 0); assert(!fclose(f)); }
int main(int argc, char **argv)
{
    assert(argc == 2);
    snprintf(brightness, sizeof(brightness), "%s/brightness", argv[1]);
    snprintf(maximum, sizeof(maximum), "%s/max", argv[1]);
    snprintf(lockpath, sizeof(lockpath), "%s/lock", argv[1]);
    put(maximum, "255\n"); put(brightness, "176\n");
    struct display_state d;
    assert(!display_change(DISPLAY_QUERY, 0, &d) && d.desired == 176);
    assert(!display_change(DISPLAY_DIM, 0, &d) && d.desired == 176 && display_read(brightness) == 1);
    assert(!display_change(DISPLAY_OFF, 0, &d) && d.desired == 176 && display_read(brightness) == 0);
    assert(!display_change(DISPLAY_QUERY, 0, &d) && d.mode == DISPLAY_OFF && d.desired == 176);
    assert(!display_change(DISPLAY_DOWN, 0, &d) && d.desired == 158 && d.mode == DISPLAY_AWAKE);
    assert(!display_change(DISPLAY_DIM, 0, &d));
    assert(!display_change(DISPLAY_SET, 90, &d)); /* cooperating frontend changes desired value */
    assert(!display_change(DISPLAY_AWAKE, 0, &d) && display_read(brightness) == 90);
    assert(!display_change(DISPLAY_OFF, 0, &d));
    put(brightness, "73\n"); /* out-of-band writer detected, not overwritten on restore */
    assert(!display_change(DISPLAY_AWAKE, 0, &d) && d.desired == 73 && display_read(brightness) == 73);
    struct display_state observed = d;
    assert(!display_change(DISPLAY_SET, 73, &d)); /* same-value activity still changes generation */
    assert(display_change_if(DISPLAY_OFF, 0, &d, &observed) == 1);
    assert(d.mode == DISPLAY_AWAKE && display_read(brightness) == 73);
    assert(!display_change(DISPLAY_SET, 0, &d));
    assert(!display_change(DISPLAY_DIM, 0, &d) && display_read(brightness) == 0);
    assert(!display_change(DISPLAY_SET, 90, &d));
    fail_write = 1; assert(display_change(DISPLAY_OFF, 0, &d) < 0); fail_write = 0;
    assert(!display_change(DISPLAY_QUERY, 0, &d) && d.desired == 90 && d.mode == DISPLAY_AWAKE);
    int held = open(lockpath, O_RDWR); assert(held >= 0 && !flock(held, LOCK_EX));
    int pipefd[2]; assert(!pipe(pipefd));
    pid_t child = fork(); assert(child >= 0);
    if (!child) {
        close(held); close(pipefd[0]);
        assert(!display_change(DISPLAY_SET, 123, &d));
        assert(write(pipefd[1], "x", 1) == 1); _exit(0);
    }
    close(pipefd[1]); struct pollfd ready = { pipefd[0], POLLIN, 0 };
    assert(poll(&ready, 1, 50) == 0); /* shared writer cannot pass held lock */
    assert(!flock(held, LOCK_UN)); close(held);
    int status; assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    close(pipefd[0]);
    assert(!display_change(DISPLAY_AWAKE, 0, &d) && d.desired == 123 && display_read(brightness) == 123);
    put(maximum, "7\n"); assert(display_change(DISPLAY_SET, 176, &d) < 0);

    struct idle_policy p = { .pending = DISPLAY_QUERY, .gamepad_ready = true };
    assert(idle_timeout(&p, 300000, 300, false) == DISPLAY_QUERY);
    assert(idle_timeout(&p, 300001, 300, false) == DISPLAY_DIM);
    assert(idle_timeout(&p, 999999, 0, false) == DISPLAY_QUERY);
    assert(idle_timeout(&p, 999999, 300, true) == DISPLAY_QUERY);
    idle_applied(&p, DISPLAY_DIM, 300001);
    assert(idle_timeout(&p, 360001, 300, false) == DISPLAY_QUERY);
    assert(idle_timeout(&p, 360002, 300, false) == DISPLAY_OFF);
    idle_activity(&p, 330000); assert(p.pending == DISPLAY_AWAKE);
    idle_applied(&p, DISPLAY_OFF, 360002);
    assert(idle_timeout(&p, 99999999, 300, false) == DISPLAY_QUERY); /* no autosuspend */
    idle_power_event(&p, 1, 370000); idle_power_event(&p, 0, 370010);
    assert(p.pending == DISPLAY_AWAKE); idle_applied(&p, DISPLAY_AWAKE, 370010);
    idle_power_event(&p, 1, 370020); idle_power_event(&p, 0, 370030);
    assert(p.pending == DISPLAY_QUERY); /* wake bounce cannot switch back off */
    idle_power_event(&p, 1, 371000); idle_power_event(&p, 0, 371100);
    assert(p.pending == DISPLAY_OFF); idle_applied(&p, DISPLAY_AWAKE, 372000);
    idle_power_sync(&p, true, 373000); idle_power_event(&p, 0, 373100);
    assert(p.pending == DISPLAY_QUERY); /* held at reconnect has no short press */
    idle_power_event(&p, 1, 374000); idle_power_event(&p, 0, 380000);
    assert(p.pending == DISPLAY_QUERY); /* long hold does not also toggle */
    p.keys[BTN_A/8] |= 1U << (BTN_A%8);
    assert(idle_timeout(&p, 999999, 300, false) == DISPLAY_QUERY);
    p.keys[BTN_A/8] = 0; p.gamepad_ready = false;
    assert(idle_timeout(&p, 999999, 300, false) == DISPLAY_QUERY);
    struct idle_policy volume = { .gamepad_ready = true }, fn = { .gamepad_ready = true };
    struct input_event e = { .type = EV_KEY, .code = KEY_VOLUMEUP, .value = 1 };
    assert(!idle_gamepad_event(-1, &volume, &e, 400000) && idle_has_keys(&volume));
    e.value = 0; assert(!idle_gamepad_event(-1, &volume, &e, 400100) && !idle_has_keys(&volume));
    e.code = KEY_FN; e.value = 1;
    assert(!idle_gamepad_event(-1, &fn, &e, 400200) && fn.last == 400200);
    assert(!idle_has_keys(&volume)); /* latched Fn is distinct from momentary volume holds */
    e.type = EV_SYN; e.code = SYN_DROPPED;
    assert(!idle_gamepad_event(-1, &volume, &e, 400300) && !volume.gamepad_ready);
    e.code = SYN_REPORT;
    assert(idle_gamepad_event(-1, &volume, &e, 400400) < 0 && !volume.gamepad_ready);
    puts("PASS actual idle deadlines, inhibitors, held/reconnect/wake guards and shared desired brightness");
}
'''.replace('"SOURCE"', '"' + str(source) + '"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.c').write_text(harness)
            subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
            subprocess.run([str(path / 'test'), directory], check=True, timeout=5)
