import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SuspendTargetTest(unittest.TestCase):
    @staticmethod
    def build_and_run(deep):
        source = (ROOT / 'package/brick-power/brick-suspend.c').read_text()
        start = source.index('static int write_value')
        end = source.index('static int transaction')
        implementation = source[start:end]
        harness = r'''
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define BRICK_POWER_STATE_PATH "/test/power/state"
#define BRICK_MEM_SLEEP_PATH "/test/power/mem_sleep"

static const char *power_states;
static const char *mem_states;
static const char *wakeup_count;
static bool ignore_selection, fail_wakeup_write, fail_state_write;
static bool fail_restore, was_interrupted;
static unsigned int mem_read_calls, fail_mem_read_call;
static char writes[32];
static size_t write_count;

static void reset(const char *power, const char *mem)
{
	power_states = power;
	mem_states = mem;
	wakeup_count = "17\n";
	ignore_selection = fail_wakeup_write = fail_state_write = false;
	fail_restore = was_interrupted = false;
	mem_read_calls = fail_mem_read_call = 0;
	memset(writes, 0, sizeof(writes));
	write_count = 0;
	errno = 0;
}

static int fake_open(const char *path, int flags, ...)
{
	(void)flags;
	if (!strcmp(path, BRICK_POWER_STATE_PATH)) return 10;
	if (!strcmp(path, BRICK_MEM_SLEEP_PATH)) return 11;
	if (!strcmp(path, "/sys/power/wakeup_count")) return 12;
	errno = ENOENT;
	return -1;
}

static ssize_t fake_read(int fd, void *buffer, size_t size)
{
	const char *value = fd == 10 ? power_states : fd == 11 ? mem_states :
		fd == 12 ? wakeup_count : NULL;
	size_t length;

	if (fd == 11 && ++mem_read_calls == fail_mem_read_call) {
		errno = EIO;
		return -1;
	}
	if (!value) { errno = EIO; return -1; }
	length = strlen(value);
	if (length > size) length = size;
	memcpy(buffer, value, length);
	return length;
}

static ssize_t fake_write(int fd, const void *buffer, size_t size)
{
	const char *value = buffer;

	if (write_count + 1 >= sizeof(writes)) abort();
	if (fd == 11 && size == 5 && !memcmp(value, "deep\n", 5)) {
		writes[write_count++] = 'd';
		if (!ignore_selection) mem_states = "s2idle [deep]\n";
		return size;
	}
	if (fd == 11 && size == 7 && !memcmp(value, "s2idle\n", 7)) {
		writes[write_count++] = 's';
		if (fail_restore) { errno = EIO; return -1; }
		mem_states = "[s2idle] deep\n";
		return size;
	}
	if (fd == 12) {
		writes[write_count++] = 'w';
		if (fail_wakeup_write) { errno = EBUSY; return -1; }
		return size;
	}
	if (fd == 10 && size == 4 && !memcmp(value, "mem\n", 4)) {
		writes[write_count++] = 'm';
		if (fail_state_write) { errno = EIO; return -1; }
		return size;
	}
	if (fd == 10 && size == 7 && !memcmp(value, "freeze\n", 7)) {
		writes[write_count++] = 'f';
		if (fail_state_write) { errno = EIO; return -1; }
		return size;
	}
	errno = EINVAL;
	return -1;
}

static int fake_close(int fd) { (void)fd; return 0; }
static unsigned int fake_alarm(unsigned int seconds) { (void)seconds; return 0; }
static bool interrupted(void) { return was_interrupted; }

#define open fake_open
#define read fake_read
#define write fake_write
#define close fake_close
#define alarm fake_alarm
IMPLEMENTATION
#undef open
#undef read
#undef write
#undef close
#undef alarm

int main(void)
{
	alarm_handler(0);
#ifdef BRICK_DEEP_SUSPEND
	reset("freeze mem\n", "[s2idle] deep\n");
	assert(supports_suspend());
	assert(!suspend_once());
	assert(!strcmp(writes, "dwms"));
	assert(!strcmp(mem_states, "[s2idle] deep\n"));

	reset("freeze mem\n", "s2idle [deep]\n");
	assert(supports_suspend());
	assert(!suspend_once());
	assert(!strcmp(writes, "wm"));

	reset("freeze mem\n", "[s2idle]\n");
	assert(!supports_suspend());
	assert(suspend_once() < 0);
	assert(!write_count);

	reset("freeze mem\n", "s2idle deep\n");
	assert(!supports_suspend());
	assert(suspend_once() < 0);
	assert(!write_count);

	reset("freeze mem\n", "[s2idle] [deep]\n");
	assert(!supports_suspend());
	assert(suspend_once() < 0);
	assert(!write_count);

	reset("freeze mem\n", "s2idle [0123456789abcdef]\n");
	assert(!supports_suspend());
	assert(errno == EINVAL);
	assert(suspend_once() < 0);
	assert(errno == EINVAL);
	assert(!write_count);

	reset("freeze mem\n", "[s2idle] deep\n");
	ignore_selection = true;
	assert(suspend_once() < 0);
	assert(!strcmp(writes, "d"));
	assert(!strcmp(mem_states, "[s2idle] deep\n"));

	reset("freeze mem\n", "[s2idle] deep\n");
	fail_mem_read_call = 2;
	assert(suspend_once() < 0);
	assert(!strcmp(writes, "ds"));
	assert(!strcmp(mem_states, "[s2idle] deep\n"));

	reset("freeze mem\n", "[s2idle] deep\n");
	fail_wakeup_write = true;
	assert(suspend_once() < 0);
	assert(!strcmp(writes, "dws"));
	assert(!strcmp(mem_states, "[s2idle] deep\n"));

	reset("freeze mem\n", "[s2idle] deep\n");
	fail_state_write = true;
	assert(suspend_once() < 0);
	assert(!strcmp(writes, "dwms"));
	assert(!strcmp(mem_states, "[s2idle] deep\n"));

	reset("freeze mem\n", "[s2idle] deep\n");
	fail_mem_read_call = 3;
	assert(!suspend_once());
	assert(!strcmp(writes, "dwms"));
	assert(!strcmp(mem_states, "[s2idle] deep\n"));

	reset("freeze mem\n", "[s2idle] deep\n");
	fail_restore = true;
	assert(suspend_once() < 0);
	assert(!strcmp(writes, "dwms"));
	assert(!strcmp(mem_states, "s2idle [deep]\n"));

	reset("freeze mem\n", "[s2idle] deep\n");
	was_interrupted = true;
	assert(suspend_once() < 0);
	assert(!strcmp(writes, "ds"));
	assert(!strcmp(mem_states, "[s2idle] deep\n"));
#else
	reset("freeze mem\n", "[s2idle] deep\n");
	assert(supports_suspend());
	assert(!suspend_once());
	assert(!strcmp(writes, "wf"));
	assert(!strcmp(mem_states, "[s2idle] deep\n"));

	reset("mem\n", "[s2idle] deep\n");
	assert(!supports_suspend());
#endif
	puts("PASS profile-selected suspend target and rollback");
	return 0;
}
'''.replace('IMPLEMENTATION', implementation)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source_path = directory / 'test.c'
            binary = directory / 'test'
            source_path.write_text(harness)
            command = [os.environ.get('CC', 'cc'), '-std=gnu11', '-O2',
                       '-Wall', '-Wextra', '-Werror']
            if deep:
                command.append('-DBRICK_DEEP_SUSPEND=1')
            subprocess.run(command + [str(source_path), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_s2idle_build_never_selects_mem(self):
        self.build_and_run(False)

    def test_deep_build_selects_and_restores_mem_sleep(self):
        self.build_and_run(True)

    def test_build_flag_is_tied_to_complete_scp_profile(self):
        makefile = (ROOT / 'package/brick-power/brick-power.mk').read_text()
        self.assertIn('ifeq ($(BR2_PACKAGE_BRICK_SCP_SYSTEM_SUSPEND),y)\n'
                      'BRICK_POWER_SUSPEND_CFLAGS = -DBRICK_DEEP_SUSPEND=1\n'
                      'endif', makefile)
        self.assertIn('$(BRICK_POWER_SUSPEND_CFLAGS) \\\n', makefile)


if __name__ == '__main__':
    unittest.main()
