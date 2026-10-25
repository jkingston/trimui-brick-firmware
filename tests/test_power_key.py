from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PowerKeyTest(unittest.TestCase):
    def test_events_deadlines_and_resynchronization(self):
        source = ROOT / 'package/brick-power/brick-power-key.c'
        harness = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdarg.h>
#include <stdlib.h>
#define ioctl test_ioctl
#define execl forbidden_exec
#define main daemon_main
#include "SOURCE"
#undef main
#undef execl
#undef ioctl
static int physical;
int test_ioctl(int fd, unsigned long request, ...)
{
    (void)fd;
    assert(request == EVIOCGKEY((KEY_MAX + 8) / 8));
    if (physical < 0) { errno = ENODEV; return -1; }
    va_list ap; va_start(ap, request); unsigned char *bits = va_arg(ap, unsigned char *); va_end(ap);
    memset(bits, 0, (KEY_MAX + 8) / 8);
    if (physical) bits[KEY_POWER / 8] |= 1U << (KEY_POWER % 8);
    return 0;
}
int forbidden_exec(const char *path, const char *arg, ...)
{
    (void)path; (void)arg;
    abort(); /* Tests must never request actual shutdown. */
}
static void event(struct key_state *key, int type, int code, int value, int64_t now)
{
    struct input_event ev = { .type = type, .code = code, .value = value };
    assert(!consume_event(1, key, &ev, now));
}
int main(void)
{
    struct key_state key = { 0 };
    assert(!hold_due(&key, 999999));
    event(&key, EV_KEY, KEY_POWER, 1, 100);
    assert(!hold_due(&key, 6099) && hold_due(&key, 6100));
    event(&key, EV_KEY, KEY_POWER, 2, 5000);
    event(&key, EV_KEY, KEY_POWER, 1, 6000);
    assert(key.deadline == 6100); /* duplicates/repeat do not restart hold */
    event(&key, EV_KEY, KEY_POWER, 0, 6099);
    assert(!hold_due(&key, 999999));
    event(&key, EV_KEY, KEY_POWER, 2, 999999);
    event(&key, EV_KEY, KEY_VOLUMEUP, 1, 999999);
    event(&key, EV_KEY, KEY_POWER, 3, 999999);
    assert(!hold_due(&key, 999999));
    event(&key, EV_KEY, KEY_POWER, 1, 10000);
    key.fired = true;
    assert(!hold_due(&key, 20000));
    event(&key, EV_KEY, KEY_POWER, 0, 20000);
    event(&key, EV_KEY, KEY_POWER, 1, 21000);
    assert(!key.fired && hold_due(&key, 27000));
    event(&key, EV_SYN, SYN_DROPPED, 0, 26999);
    assert(key.dropped && !hold_due(&key, 999999));
    event(&key, EV_KEY, KEY_POWER, 1, 27000);
    assert(!key.down);
    physical = 1;
    event(&key, EV_SYN, SYN_REPORT, 0, 28000);
    assert(!key.dropped && key.down && key.deadline == 34000);
    physical = 0;
    event(&key, EV_SYN, SYN_DROPPED, 0, 33999);
    event(&key, EV_SYN, SYN_REPORT, 0, 34000);
    assert(!key.down && !hold_due(&key, 999999));
    physical = -1;
    key.down = true; key.deadline = 1;
    assert(synchronize(1, &key, 100) == -1 && !hold_due(&key, 999999));
    physical = 1;
    assert(!synchronize(1, &key, 50000));
    assert(!hold_due(&key, 55999) && hold_due(&key, 56000));
    cancel_hold(&key); /* disconnect cancels, reconnect earns no elapsed credit */
    assert(!hold_due(&key, 999999));
    assert(!synchronize(1, &key, 100000) && key.deadline == 106000);

    int fds[2]; assert(!pipe2(fds, O_NONBLOCK | O_CLOEXEC));
    struct input_event events[] = {
        { .type = EV_KEY, .code = KEY_POWER, .value = 1 },
        { .type = EV_KEY, .code = KEY_POWER, .value = 0 }
    };
    assert(write(fds[1], events, sizeof(events)) == sizeof(events));
    assert(drain_events(fds[0], &key, 107000) == 1);
    assert(!hold_due(&key, 107000)); /* queued release wins over expired timer */
    for (int i = 0; i < 257; i++) assert(write(fds[1], events, sizeof(events[0])) == sizeof(events[0]));
    assert(drain_events(fds[0], &key, 110000) == 0); /* do not fire with backlog */
    assert(drain_events(fds[0], &key, 110000) == 1);
    close(fds[1]); assert(drain_events(fds[0], &key, 116000) == -1); close(fds[0]);
    puts("PASS actual power-key event/deadline/reconnect/drop/backlog cases; no shutdown executed");
}
'''.replace('"SOURCE"', '"' + str(source) + '"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.c'
            executable = Path(directory) / 'test'
            path.write_text(harness)
            subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', str(path), '-o', str(executable)], check=True)
            subprocess.run([str(executable)], check=True, timeout=5)
