from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PowerPolicyLoopTest(unittest.TestCase):
    def test_slow_battery_and_discovery_do_not_credit_key_hold(self):
        source = ROOT / 'package/brick-power/brick-power-key.c'
        # Compile the actual main loop with bounded fake I/O; never run a daemon.
        main = source.read_text().split('int main(int argc, char **argv)', 1)[1]
        harness = r'''
#define main unused_daemon_main
#include "SOURCE"
#undef main
#include <assert.h>
#include <stdarg.h>
static int64_t fake_now, armed;
static int turns;
static int64_t fake_clock(void) { fake_now += 1000; return fake_now; }
static bool fake_battery(const char *root) { (void)root; fake_now += 7000; return false; }
static int fake_discover(void) { fake_now += 8000; return 77; }
static int fake_sync(int fd, struct key_state *key, int64_t now)
{
    assert(fd == 77); assert(now == fake_now);
    key->down = true; key->deadline = now + HOLD_MS; armed = now;
    return 0;
}
static int fake_poll(struct pollfd *p, nfds_t n, int timeout)
{
    (void)p; assert(n == 2); assert(timeout > 0);
    assert(fake_now >= armed && fake_now < armed + HOLD_MS);
    turns++; stopped = 1; return 0;
}
static int fake_open(const char *path, int flags, ...) { (void)path; (void)flags; return 78; }
static int fake_flock(int fd, int operation) { assert(fd == 78); (void)operation; return 0; }
static int fake_close(int fd) { assert(fd == 77 || fd == 78); return 0; }
static int fake_signal(int signum, const struct sigaction *a, struct sigaction *old)
{ (void)signum; (void)a; (void)old; return 0; }
static int forbidden_poweroff(const char *reason) { (void)reason; abort(); }
static int fake_display(int op, int value, struct display_state *out)
{ (void)op; (void)value; (void)out; return -1; }
#define display_change fake_display
#define monotonic_ms fake_clock
#define battery_critical fake_battery
#define discover fake_discover
#define synchronize fake_sync
#define poll fake_poll
#define open fake_open
#define flock fake_flock
#define close fake_close
#define sigaction fake_signal
#define request_poweroff forbidden_poweroff
int tested_main(int argc, char **argv) MAIN
#undef sigaction
int main(void)
{
    assert(!tested_main(1, NULL)); assert(turns == 1);
    assert(armed >= 17000);
    puts("PASS actual loop gives no hold credit for slow battery reads or discovery");
}
'''.replace('"SOURCE"', '"' + str(source) + '"').replace(' MAIN\n', main + '\n')
        # Function-like macro preserves the struct sigaction type.
        harness = harness.replace('#define sigaction fake_signal', '#define sigaction(...) fake_signal(__VA_ARGS__)')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'test.c').write_text(harness)
            subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
            subprocess.run([str(path / 'test')], check=True, timeout=5)
