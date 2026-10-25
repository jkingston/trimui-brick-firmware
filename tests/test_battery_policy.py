from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BatteryPolicyTest(unittest.TestCase):
    def test_live_telemetry_and_inhibitors(self):
        source = ROOT / 'package/brick-power/brick-power-key.c'
        harness = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdlib.h>
#include <stdio.h>
#include <time.h>
#include <unistd.h>
static int clock_calls, delayed, fail_clock, read_calls, connect_after_read;
static const char *root;
static void write_attr(const char *, const char *, const char *);
static ssize_t test_read(int fd, void *buffer, size_t size)
{
    ssize_t n = read(fd, buffer, size);
    if (++read_calls == connect_after_read) write_attr("axp2202-usb", "online", "1\n");
    return n;
}
static int test_clock(clockid_t id, struct timespec *t)
{
    assert(id == CLOCK_BOOTTIME);
    if (fail_clock) return -1;
    t->tv_sec = clock_calls++ ? delayed : 0; t->tv_nsec = 0;
    return 0;
}
int forbidden_exec(const char *, const char *, ...);
#define main daemon_main
#define clock_gettime test_clock
#define read test_read
#define execl forbidden_exec
#include "SOURCE"
#undef main
#undef clock_gettime
#undef read
#undef execl
int forbidden_exec(const char *path, const char *arg, ...)
{
    (void)path; (void)arg; abort();
}
static void write_attr(const char *supply, const char *attribute, const char *text)
{
    char path[1024]; snprintf(path, sizeof(path), "%s/%s/%s", root, supply, attribute);
    FILE *f = fopen(path, "w"); assert(f); assert(fputs(text, f) >= 0); assert(!fclose(f));
}
static void sample(const char *capacity, const char *status, const char *online, bool expected)
{
    write_attr("axp2202-battery", "capacity", capacity);
    write_attr("axp2202-battery", "status", status);
    write_attr("axp2202-usb", "online", online);
    clock_calls = read_calls = 0;
    assert(battery_critical(root) == expected);
}
int main(int argc, char **argv)
{
    assert(argc == 2); root = argv[1];
    sample("0\n", "Unknown\n", "0\n", true);
    sample("1\n", "Discharging\n", "0\n", true);
    sample("2\n", "Unknown\n", "0\n", false);
    sample("100\n", "Unknown\n", "0\n", false);
    const char *invalid[] = { "", "1", "-1\n", "101\n", "1junk\n", " 1\n", "+1\n", "1\n2\n", "999999999999999999999\n" };
    for (size_t i = 0; i < sizeof(invalid)/sizeof(*invalid); i++) sample(invalid[i], "Unknown\n", "0\n", false);
    const char *states[] = { "Charging\n", "Full\n", "Not charging\n", "", "Unknown", "garbage\n" };
    for (size_t i = 0; i < sizeof(states)/sizeof(*states); i++) sample("0\n", states[i], "0\n", false);
    const char *inputs[] = { "1\n", "", "-1\n", "0", "2\n", "garbage\n" };
    for (size_t i = 0; i < sizeof(inputs)/sizeof(*inputs); i++) sample("0\n", "Unknown\n", inputs[i], false);
    delayed = 2; sample("0\n", "Unknown\n", "0\n", false); delayed = 0;
    fail_clock = 1; sample("0\n", "Unknown\n", "0\n", false); fail_clock = 0;
    connect_after_read = 4; sample("0\n", "Unknown\n", "0\n", false); connect_after_read = 0;
    sample("0\n", "Unknown\n", "0\n", true);
    char path[1024]; snprintf(path, sizeof(path), "%s/axp2202-battery/capacity", root); assert(!unlink(path));
    assert(!battery_critical(root)); /* Never reuse the preceding successful sample. */
    assert(!battery_critical("/nonexistent-brick-battery-test"));
    puts("PASS battery threshold, strict parsing, inhibitors, freshness and recheck; no shutdown executed");
}
'''.replace('"SOURCE"', '"' + str(source) + '"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'axp2202-battery').mkdir()
            (path / 'axp2202-usb').mkdir()
            (path / 'test.c').write_text(harness)
            subprocess.run(['cc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', str(path / 'test.c'), '-o', str(path / 'test')], check=True)
            subprocess.run([str(path / 'test'), directory], check=True, timeout=5)
