import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ControlsTest(unittest.TestCase):
    def test_input_loss_resynchronization_and_repeat(self):
        source = (ROOT / 'package/brick-controls/brick-controls.c').read_text()
        function = source[source.index('static void input_event('):source.index('int main(')]
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <linux/input.h>
#include <sys/types.h>
struct input { int fd; bool dropped; dev_t device; };
static int physical;
static int fn_state(int fd) { (void)fd; return physical; }
static double now(void) { return 10; }
'''
        harness += function
        harness += r'''
int main(void)
{
    struct input in = { .fd = 1 };
    int volume = 10;
    bool mute = false, changed = false, dirty = false;
    double save = 0;
#define EVENT(slot,t,c,v) do { struct input_event e = { .type=t, .code=c, .value=v }; \
    input_event(&in,slot,&e,&volume,&mute,&changed,&dirty,&save); } while(0)
    EVENT(0,EV_KEY,KEY_VOLUMEUP,0);
    assert(volume == 10 && !dirty && !changed);
    EVENT(0,EV_KEY,KEY_VOLUMEUP,1);
    assert(volume == 11 && dirty && changed && save == 12);
    for (int i = 0; i < 40; i++) EVENT(0,EV_KEY,KEY_VOLUMEUP,2);
    assert(volume == 20);
    for (int i = 0; i < 40; i++) EVENT(0,EV_KEY,KEY_VOLUMEDOWN,2);
    assert(volume == 0);
    dirty = changed = false;
    EVENT(1,EV_KEY,KEY_FN,1);
    assert(mute && changed && !dirty);
    changed = false;
    EVENT(1,EV_SYN,SYN_DROPPED,0);
    EVENT(1,EV_KEY,KEY_FN,0);
    assert(mute && !changed && in.dropped);
    physical = 0;
    EVENT(1,EV_SYN,SYN_REPORT,0);
    assert(!mute && changed && !dirty && !in.dropped);
    changed = false;
    physical = 1;
    EVENT(1,EV_SYN,SYN_DROPPED,0);
    EVENT(1,EV_SYN,SYN_REPORT,0);
    assert(mute && changed && !dirty);
    changed = false;
    physical = -1;
    EVENT(1,EV_SYN,SYN_DROPPED,0);
    EVENT(1,EV_SYN,SYN_REPORT,0);
    assert(mute && !changed && !dirty);
    EVENT(0,EV_SYN,SYN_DROPPED,0);
    EVENT(0,EV_KEY,KEY_VOLUMEUP,1);
    EVENT(0,EV_SYN,SYN_REPORT,0);
    assert(volume == 0 && !dirty);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'test.c').write_text(harness)
            subprocess.run([os.environ.get('CC', 'cc'), '-Wall', '-Wextra', '-Werror',
                            str(directory / 'test.c'), '-o', str(directory / 'test')], check=True)
            subprocess.run([str(directory / 'test')], cwd=directory, check=True)

    def test_mute_failure_order_and_persistent_state(self):
        source = (ROOT / 'package/brick-controls/brick-controls.c').read_text()
        functions = source[source.index('static int attenuation('):source.index('static int control(')]
        functions += source[source.index('static int apply_volume('):source.index('static int fn_state(')]
        harness = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
typedef void snd_ctl_t;
static int calls, failure;
static long levels[3];
static char order[4];
static int control(snd_ctl_t *ctl, const char *name, long *v, unsigned count, bool write)
{
    (void)ctl;
    assert(write && calls < 3);
    order[calls] = count == 2 ? 'G' : 'A';
    assert(!strcmp(name, count == 2 ? "DAC Digital Playback Volume" : "DAC Playback Volume"));
    if (count == 2) assert(v[0] == v[1]);
    levels[calls++] = v[0];
    return calls == failure ? -EIO : 0;
}
'''
        harness += functions
        harness += r'''
int main(void)
{
    assert(attenuation(0) == 0 && attenuation(20) == 63);
    for (int v = 1; v <= 20; v++) assert(attenuation(v) > attenuation(v - 1));
    for (int mute = 0; mute <= 1; mute++) {
        for (int v = 0; v <= 20; v++) {
            for (int fail = 0; fail <= 2; fail++) {
                calls = 0; failure = fail; memset(order, 0, sizeof(order));
                int result = apply_volume(NULL, v, mute);
                assert(result == (fail ? -EIO : 0));
                assert(calls == (fail ? fail : 2));
                assert(order[0] == ((!v || mute) ? 'G' : 'A'));
                if (!v || mute) assert(levels[0] == 0);
                if (!fail && v && !mute) assert(levels[1] == 160);
            }
        }
    }
    const char *path = "state";
    assert(read_state(path, 20) == -ENOENT);
    for (int v = 0; v <= 20; v++) {
        assert(save_state(path, v) == 0);
        assert(read_state(path, 20) == v);
        struct stat st; assert(!stat(path, &st)); assert((st.st_mode & 0777) == 0600);
    }
    const char *bad[] = {"", "2 4\n", "1 -1\n", "1 21\n", "1 5 junk\n",
        "1 5                              hidden", "1 4294967296\n", "4294967297 20\n"};
    for (unsigned i = 0; i < sizeof(bad)/sizeof(bad[0]); i++) {
        FILE *f = fopen(path, "w"); assert(f); fputs(bad[i], f); fclose(f);
        assert(read_state(path, 20) == -EINVAL);
    }
    FILE *f = fopen(path, "w"); assert(f);
    const char nul[] = "1 20\0junk";
    assert(fwrite(nul, 1, sizeof(nul) - 1, f) == sizeof(nul) - 1); fclose(f);
    assert(read_state(path, 20) == -EINVAL);
    assert(!symlink(path, "link"));
    assert(read_state("link", 20) == -ELOOP);
    assert(save_state("missing/state", 10) < 0);
    puts("controls policy and persistence checks passed");
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'test.c').write_text(harness)
            (directory / 'settings.h').write_bytes(
                (ROOT / 'package/brick-controls/settings.h').read_bytes())
            subprocess.run([os.environ.get('CC', 'cc'), '-Wall', '-Wextra', '-Werror',
                            str(directory / 'test.c'), '-o', str(directory / 'test')], check=True)
            subprocess.run([str(directory / 'test')], cwd=directory, check=True)
