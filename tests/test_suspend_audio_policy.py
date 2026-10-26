import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def item(source, marker):
    start = source.index(marker)
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


class SuspendAudioPolicyTest(unittest.TestCase):
    def test_gain_and_speaker_rollback_matches_driver_semantics(self):
        source = (ROOT / 'package/brick-power/brick-suspend.c').read_text()
        code = '\n'.join([
            item(source, 'struct audio_state') + ';',
            item(source, 'static int audio_integer_control'),
            item(source, 'static int audio_switch_control'),
            item(source, 'static int audio_open'),
            item(source, 'static int audio_quiesce'),
            item(source, 'static int audio_restore'),
        ])
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
typedef struct { int unused; } snd_ctl_t;
typedef struct { int unused; } snd_ctl_elem_id_t;
typedef struct { int unused; } snd_ctl_elem_info_t;
typedef struct { int unused; } snd_ctl_elem_value_t;
typedef struct { int unused; } snd_ctl_card_info_t;
#define SND_CTL_ELEM_IFACE_MIXER 1
#define SND_CTL_ELEM_TYPE_INTEGER 2
#define SND_CTL_ELEM_TYPE_BOOLEAN 3
#define AUDIO_POLICY_LOCK "/run/brick-audio-policy.lock"
static snd_ctl_t ctl_object;
static snd_ctl_elem_id_t id_object;
static snd_ctl_elem_info_t info_object;
static snd_ctl_elem_value_t value_object;
static snd_ctl_card_info_t card_object;
#define snd_ctl_elem_id_alloca(p) (*(p) = &id_object)
#define snd_ctl_elem_info_alloca(p) (*(p) = &info_object)
#define snd_ctl_elem_value_alloca(p) (*(p) = &value_object)
#define snd_ctl_card_info_alloca(p) (*(p) = &card_object)
static const char *control_name;
static long requested[2], physical[2], pending[2];
static bool speaker, pending_switch, driver_muted;
static int scenario, writes, reads;
static char order[16];
static int fake_open(const char *path, int flags, ...)
{ assert(!strcmp(path, AUDIO_POLICY_LOCK)); (void)flags; return 41; }
static int fake_flock(int fd, int operation)
{ assert(fd == 41); (void)operation; return 0; }
static int fake_close(int fd) { assert(fd == 41); return 0; }
static void snd_ctl_elem_id_set_interface(snd_ctl_elem_id_t *id, int type)
{ (void)id; assert(type == SND_CTL_ELEM_IFACE_MIXER); }
static void snd_ctl_elem_id_set_name(snd_ctl_elem_id_t *id, const char *name)
{ (void)id; control_name = name; }
static void snd_ctl_elem_info_set_id(snd_ctl_elem_info_t *info, snd_ctl_elem_id_t *id)
{ (void)info; (void)id; }
static int snd_ctl_elem_info(snd_ctl_t *ctl, snd_ctl_elem_info_t *info)
{ (void)ctl; (void)info; return 0; }
static int snd_ctl_elem_info_get_type(snd_ctl_elem_info_t *info)
{ (void)info; return !strcmp(control_name, "Speaker Switch") ?
    SND_CTL_ELEM_TYPE_BOOLEAN : SND_CTL_ELEM_TYPE_INTEGER; }
static unsigned snd_ctl_elem_info_get_count(snd_ctl_elem_info_t *info)
{ (void)info; return !strcmp(control_name, "Speaker Switch") ? 1 : 2; }
static long snd_ctl_elem_info_get_min(snd_ctl_elem_info_t *info)
{ (void)info; return 0; }
static long snd_ctl_elem_info_get_max(snd_ctl_elem_info_t *info)
{ (void)info; return 255; }
static void snd_ctl_elem_value_set_id(snd_ctl_elem_value_t *value,
                                      snd_ctl_elem_id_t *id)
{ (void)value; (void)id; }
static int snd_ctl_elem_read(snd_ctl_t *ctl, snd_ctl_elem_value_t *value)
{ (void)ctl; (void)value; reads++; return 0; }
static long snd_ctl_elem_value_get_integer(snd_ctl_elem_value_t *value,
                                           unsigned index)
{ (void)value; return requested[index]; }
static long snd_ctl_elem_value_get_boolean(snd_ctl_elem_value_t *value,
                                           unsigned index)
{ (void)value; assert(index == 0); return speaker; }
static void snd_ctl_elem_value_set_integer(snd_ctl_elem_value_t *value,
                                           unsigned index, long data)
{ (void)value; pending[index] = data; }
static void snd_ctl_elem_value_set_boolean(snd_ctl_elem_value_t *value,
                                           unsigned index, long data)
{ (void)value; assert(index == 0); pending_switch = !!data; }
static int snd_ctl_elem_write(snd_ctl_t *ctl, snd_ctl_elem_value_t *value)
{
    (void)ctl; (void)value;
    if (!strcmp(control_name, "Speaker Switch")) {
        order[writes++] = 's'; speaker = pending_switch;
        if (scenario == 3 && writes == 2) return -EIO;
        if (scenario == 5 && writes == 4) return -EIO;
    } else {
        order[writes++] = 'g';
        requested[0] = pending[0]; requested[1] = pending[1];
        if (!driver_muted) {
            physical[0] = pending[0]; physical[1] = pending[1];
        }
        if (scenario == 4 && writes == 1) return -EIO;
    }
    return 0;
}
static int snd_card_next(int *card)
{ *card = *card < 0 ? 0 : -1; return 0; }
static int snd_ctl_open(snd_ctl_t **ctl, const char *name, int mode)
{ assert(!strcmp(name, "hw:0")); (void)mode; *ctl = &ctl_object; return 0; }
static int snd_ctl_card_info(snd_ctl_t *ctl, snd_ctl_card_info_t *info)
{ (void)ctl; (void)info; return 0; }
static const char *snd_ctl_card_info_get_name(snd_ctl_card_info_t *info)
{ (void)info; return "A133 Audio Codec"; }
static int snd_ctl_close(snd_ctl_t *ctl)
{ assert(ctl == &ctl_object); return 0; }
#define open fake_open
#define flock fake_flock
#define close fake_close
CODE
#undef open
#undef flock
#undef close
int main(void)
{
    for (scenario = 0; scenario < 6; scenario++) {
        struct audio_state state = { .lock = -1 };
        long saved[2] = { 73, 121 };
        requested[0] = saved[0]; requested[1] = saved[1];
        driver_muted = scenario == 2;
        physical[0] = driver_muted ? 0 : saved[0];
        physical[1] = driver_muted ? 0 : saved[1];
        speaker = scenario == 1 ? false : true;
        bool saved_speaker = speaker;
        writes = reads = 0; memset(order, 0, sizeof(order));
        int quiesced = audio_quiesce(&state);
        if (scenario < 3 || scenario == 5) {
            assert(!quiesced);
            assert(requested[0] == 0 && requested[1] == 0 && !speaker);
            if (!driver_muted) assert(physical[0] == 0 && physical[1] == 0);
        } else {
            assert(quiesced < 0);
        }
        int restored = audio_restore(&state);
        if (scenario == 5) assert(restored < 0); else assert(!restored);
        assert(requested[0] == saved[0] && requested[1] == saved[1]);
        assert(speaker == saved_speaker);
        if (driver_muted) assert(physical[0] == 0 && physical[1] == 0);
        else assert(physical[0] == saved[0] && physical[1] == saved[1]);
        if (scenario < 3 || scenario == 5) assert(!strcmp(order, "gsgs"));
    }
    puts("PASS gain-first quiesce/restore, exact speaker state and muted-driver semantics");
}
'''.replace('CODE', code)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'test.c').write_text(harness)
            subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O2',
                            '-Wall', '-Wextra', '-Werror', str(directory / 'test.c'),
                            '-o', str(directory / 'test')], check=True)
            subprocess.run([str(directory / 'test')], check=True)

    def test_confirmed_bluez_absence_is_already_quiescent(self):
        source = (ROOT / 'package/brick-power/brick-suspend.c').read_text()
        transaction = item(source, 'static int transaction')
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
struct audio_state { int lock; };
struct led_policy_state { int lock; };
static char *owner;
static int present, get_calls, same_calls, wait_calls, freeze_calls;
static bool supports_freeze(void) { return true; }
static int audio_quiesce(struct audio_state *state) { (void)state; return 0; }
static int audio_restore(struct audio_state *state) { (void)state; return 0; }
static int led_policy_quiesce(struct led_policy_state *state) { (void)state; return 0; }
static int led_policy_restore(struct led_policy_state *state) { (void)state; return 0; }
static int bluez_present(void) { return present; }
static int get_power(bool *powered) { get_calls++; *powered = false; return 0; }
static int set_power(bool powered) { (void)powered; return 0; }
static bool same_owner(void) { same_calls++; return true; }
static int wait_power_off(void) { wait_calls++; return 0; }
static bool interrupted(void) { return false; }
static int freeze_once(void) { freeze_calls++; return 0; }
TRANSACTION
int main(void)
{
    owner = NULL; present = 0;
    assert(!transaction());
    assert(!get_calls && !same_calls && !wait_calls && freeze_calls == 1);

    freeze_calls = 0; present = 1;
    assert(transaction() < 0);
    assert(!get_calls && !freeze_calls);

    present = -1;
    assert(transaction() < 0);
    assert(!get_calls && !freeze_calls);

    owner = "org.bluez.owner"; present = 1;
    assert(!transaction());
    assert(get_calls == 1 && same_calls == 1 && wait_calls == 1 && freeze_calls == 1);
    puts("PASS confirmed BlueZ absence and exact-owner Bluetooth quiescence");
}
'''.replace('TRANSACTION', transaction)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / 'test.c').write_text(harness)
            subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-O2',
                            '-Wall', '-Wextra', '-Werror', str(directory / 'test.c'),
                            '-o', str(directory / 'test')], check=True)
            subprocess.run([str(directory / 'test')], check=True)


if __name__ == '__main__':
    unittest.main()
