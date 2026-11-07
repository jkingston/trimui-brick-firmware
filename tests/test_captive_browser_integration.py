import http.cookiejar
import http.client
import hashlib
import os
import json
from pathlib import Path
import signal
import ssl
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = (ROOT / 'board/trimui/brick/wpe-platform-brick/integration')
SUPERVISOR = INTEGRATION / 'brick-captive-supervisor'
INPUT = INTEGRATION / 'brick-evdev-input.c'
SANDBOX = INTEGRATION / 'brick-sandbox-probe.c'
FIXTURE = INTEGRATION / 'captive-fixture.py'
WATCHDOG = INTEGRATION / 'brick-kms-watchdog.c'
LAUNCHER = ROOT / 'board/trimui/brick/wpe-platform-brick/launcher'
LIFECYCLE = INTEGRATION / 'brick-wpe-lifecycle-spike.py'
AUTHORITY_METRICS = INTEGRATION / 'brick-authority-metrics.c'
MEMORY_HOLD = INTEGRATION / 'brick-memory-hold.c'
CGROUP_MEMORY = INTEGRATION / 'brick-wpe-cgroup-memory-spike'
FRAME_QUALIFICATION = INTEGRATION / 'brick-wpe-frame-qualification.patch'
NETWORK_FAULT = INTEGRATION / 'brick-captive-network-fault-spike'
LONG_CYCLE = INTEGRATION / 'brick-captive-long-cycle-spike'
NETWORK_A = '0' * 64
NETWORK_B = '1' * 64


class SingleBrowserImplementationTest(unittest.TestCase):
    def test_product_selects_only_upstream_wpe_drm(self):
        config = (ROOT / 'configs/brick_defconfig').read_text().splitlines()
        self.assertIn('BR2_PACKAGE_BRICK_WPEWEBKIT=y', config)
        self.assertIn('# BR2_PACKAGE_BRICK_GPU is not set', config)
        self.assertNotIn('BR2_PACKAGE_WPEWEBKIT=y', config)
        self.assertNotIn('BR2_PACKAGE_NETSURF=y', config)

        package = (ROOT / 'package/brick-wpewebkit/brick-wpewebkit.mk').read_text()
        source_options = (ROOT / 'package/brick-wpewebkit/'
                          'source-cmake-options.txt').read_text()
        self.assertIn('brick-captive-supervisor', package)
        self.assertIn('/usr/libexec/brick-captive-supervisor', package)
        self.assertNotIn('LIBGL_ALWAYS_SOFTWARE', package)
        for option in (
                '-DENABLE_WPE_PLATFORM_DRM=ON',
                '-DENABLE_GPU_PROCESS=ON',
                '-DUSE_GBM=ON',
                '-DUSE_LIBDRM=ON',
                '-DENABLE_BUBBLEWRAP_SANDBOX=ON'):
            self.assertIn(option, source_options)
        for option in (
                '-DENABLE_WPE_PLATFORM_WAYLAND=OFF',
                '-DENABLE_WPE_PLATFORM_HEADLESS=OFF',
                '-DENABLE_WPE_LEGACY_API=OFF'):
            self.assertIn(option, source_options)
        self.assertIn('releases/download/wpewebkit-brick-2.54.1-r1', package)
        self.assertIn('$(generic-package)', package)

        self.assertFalse((ROOT / 'board/trimui/brick/patches/netsurf').exists())
        self.assertFalse((ROOT / 'board/trimui/brick/wpe-platform-brick/'
                          'brick-wpe-platform.c').exists())

    def test_qualification_overlays_are_single_path_and_bounded(self):
        frame_patch = FRAME_QUALIFICATION.read_text()
        for counter in ('render_submissions', 'shm_copies', 'atomic_commits',
                        'page_flip_events', 'completed_buffer_flips'):
            self.assertIn(counter, frame_patch)
        self.assertIn('WPEViewDRM.cpp', frame_patch)
        self.assertNotIn('brick-wpe-platform.c', frame_patch)

        self.assertIn('memory_high=$((352 * 1024 * 1024))',
                      SUPERVISOR.read_text())
        self.assertIn('memory_max=$((384 * 1024 * 1024))',
                      SUPERVISOR.read_text())
        launcher = (LAUNCHER / 'brick-captive-wpe.c').read_text()
        self.assertIn('wpe_view_set_visible(platform_view, TRUE)', launcher)
        self.assertIn('wpe_view_focus_in(platform_view)', launcher)

    def test_network_fault_spike_is_bounded_and_preserves_credentials(self):
        result = subprocess.run(['sh', '-n', str(NETWORK_FAULT)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = NETWORK_FAULT.read_text()
        self.assertIn('run_stale_case loss', source)
        self.assertIn('run_stale_case network-change', source)
        self.assertIn('run_retry loss', source)
        self.assertIn('run_retry network-change', source)
        self.assertIn('wait_outcome exit-1', source)
        self.assertIn('wait_outcome success', source)
        self.assertIn(' disconnect)', source)
        self.assertIn(' reconnect)', source)
        self.assertNotIn('remove_network', source)
        self.assertNotIn('select_network', source)
        self.assertNotIn('save_config', source)

    def test_long_cycle_spike_bounds_faults_soaks_and_control_retries(self):
        result = subprocess.run(['sh', '-n', str(LONG_CYCLE)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        source = LONG_CYCLE.read_text()
        self.assertIn('[ "$cycles" -le 1000 ]', source)
        self.assertIn('[ "$static_seconds" -le 600 ]', source)
        self.assertIn('[ "$animated_seconds" -le 600 ]', source)
        self.assertIn('control remained busy for 5 seconds', source)
        self.assertIn('kill -KILL "$1"', source)
        self.assertIn('cycle_kind=cancel', source)
        self.assertIn('cycle_kind=web-kill', source)
        self.assertIn('cmp "$root/drm.baseline" "$root/drm.current"', source)
        self.assertIn('run_soak static', source)
        self.assertIn('run_soak animated', source)


class CaptiveSupervisorTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profiles = self.root / 'profiles'
        self.runtime = self.root / 'runtime'
        self.cgroup = self.root / 'cgroup'
        self.cgroup.mkdir()
        self.events = self.root / 'events'
        self.hook = self.root / 'hook'
        self.hook.write_text(
            '#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$1" '
            '>> "$BRICK_TEST_EVENTS"\n')
        self.hook.chmod(0o755)
        self.browser = self.root / 'browser'
        self.browser.write_text(
            '#!/bin/sh\nprintf "browser %s | %s %s\\n" "$*" '
            '"$XDG_DATA_HOME" "$XDG_CACHE_HOME" >> "$BRICK_TEST_EVENTS"\n'
            'exit "${BRICK_TEST_BROWSER_STATUS:-0}"\n')
        self.browser.chmod(0o755)
        self.env = dict(
            os.environ,
            BRICK_CAPTIVE_PROFILE_ROOT=str(self.profiles),
            BRICK_CAPTIVE_RUNTIME_ROOT=str(self.runtime),
            BRICK_TEST_CGROUP_ROOT=str(self.cgroup),
            BRICK_DISPLAY_PAUSE_HOOK=str(self.hook),
            BRICK_DISPLAY_RESUME_HOOK=str(self.hook),
            BRICK_TEST_EVENTS=str(self.events),
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_session(self, key=NETWORK_A, status=0):
        env = dict(self.env, BRICK_TEST_BROWSER_STATUS=str(status))
        return subprocess.run(
            [str(SUPERVISOR), key, 'http://portal.test/', '--',
             str(self.browser)], env=env, capture_output=True, text=True,
            timeout=5)

    def test_crashed_browser_resumes_display_and_preserves_exit_status(self):
        result = self.run_session(status=71)
        self.assertEqual(result.returncode, 71, result.stderr)
        lines = self.events.read_text().splitlines()
        self.assertEqual(lines[0], f'hook {NETWORK_A}')
        self.assertIn('browser --uri http://portal.test/', lines[1])
        self.assertIn(f'--network-key {NETWORK_A}', lines[1])
        self.assertIn(f'--profile-root {self.profiles}', lines[1])
        self.assertEqual(lines[2], f'hook {NETWORK_A}')
        self.assertFalse((self.runtime / 'session').exists())

    def test_profiles_are_private_and_isolated_by_stable_network_key(self):
        self.assertEqual(self.run_session(NETWORK_A).returncode, 0)
        self.assertEqual(self.run_session(NETWORK_B).returncode, 0)
        for key in (NETWORK_A, NETWORK_B):
            profile = self.profiles / hashlib.sha256(key.encode()).hexdigest()
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            for child in ('cache', 'config', 'data'):
                self.assertEqual((profile / child).stat().st_mode & 0o777,
                                 0o700)
            self.assertFalse((self.profiles / key).exists())
        lines = [line for line in self.events.read_text().splitlines()
                 if line.startswith('browser ')]
        profile_a = hashlib.sha256(NETWORK_A.encode()).hexdigest()
        profile_b = hashlib.sha256(NETWORK_B.encode()).hexdigest()
        self.assertIn(f'/profiles/{profile_a}/data', lines[0])
        self.assertIn(f'/profiles/{profile_b}/data', lines[1])

    def test_launcher_binds_only_the_current_hashed_profile(self):
        launcher = (LAUNCHER / 'brick-captive-wpe.c').read_text()
        forget = (ROOT / 'board/trimui/brick/rootfs-overlay/usr/libexec/'
                  'brick-wifi-forget').read_text()
        supervisor = SUPERVISOR.read_text()
        production_root = '/data/wifi/captive-browser'
        self.assertIn(f'"{production_root}"', launcher)
        self.assertIn(f':-{production_root}}}', forget)
        self.assertIn(f':-{production_root}}}', supervisor)
        self.assertIn('g_path_get_dirname(data_path)', launcher)
        self.assertIn('g_setenv(CAPTIVE_PROFILE_ROOT_ENV, profile_base, TRUE)',
                      launcher)
        self.assertIn('webkit_network_session_get_cookie_manager(session)',
                      launcher)
        self.assertIn('WEBKIT_COOKIE_PERSISTENT_STORAGE_SQLITE', launcher)
        self.assertIn('PRAGMA quick_check(1)', launcher)
        self.assertIn('cookie database corrupt; resetting current profile',
                      launcher)
        self.assertLess(launcher.index('prepare_cookie_database(cookie_path'),
                        launcher.index('webkit_network_session_new(data_path'))
        self.assertIn('gio-2.0 sqlite3',
                      (ROOT / 'package/brick-wpewebkit/'
                       'brick-wpewebkit.mk').read_text())
        self.assertIn("profile_id=$(printf '%s' \"$network_key\" | sha256sum",
                      supervisor)

    def test_rejects_raw_or_path_like_network_identity(self):
        for key in ('hotel-wifi', '../escape', 'A' * 64, '0' * 63):
            result = self.run_session(key)
            self.assertEqual(result.returncode, 2)
        self.assertFalse(self.profiles.exists())

    def test_rejects_non_web_portal_scheme(self):
        result = subprocess.run(
            [str(SUPERVISOR), NETWORK_A, 'file:///etc/passwd', '--',
             str(self.browser)], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_supervisor_signal_terminates_browser_process_group(self):
        descendant_file = self.root / 'descendant'
        tree = self.root / 'browser-tree'
        tree.write_text(
            '#!/bin/sh\ntrap "" TERM\nsleep 60 &\nprintf "%s\\n" "$!" > '
            '"$BRICK_TEST_DESCENDANT"\nwait\n')
        tree.chmod(0o755)
        env = dict(self.env, BRICK_TEST_DESCENDANT=str(descendant_file),
                   BRICK_CAPTIVE_KILL_GRACE_TICKS='2')
        supervisor = subprocess.Popen(
            [str(SUPERVISOR), NETWORK_A, 'http://portal.test/', '--',
             str(tree)], env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            if descendant_file.exists():
                break
            time.sleep(0.01)
        self.assertTrue(descendant_file.exists())
        descendant = int(descendant_file.read_text())
        supervisor.terminate()
        self.assertEqual(supervisor.wait(timeout=5), 143)
        supervisor.stdout.close()
        supervisor.stderr.close()
        for _ in range(100):
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            self.fail('browser descendant survived supervisor cleanup')
        self.assertEqual(self.events.read_text().splitlines()[-1],
                         f'hook {NETWORK_A}')

    def test_memory_domain_policy_metrics_and_cleanup(self):
        result = self.run_session()
        self.assertEqual(result.returncode, 0, result.stderr)
        metrics = (self.runtime / 'last-memory').read_text()
        self.assertIn('high_bytes=369098752', metrics)
        self.assertIn('max_bytes=402653184', metrics)
        self.assertIn('oom_group_kill 0', metrics)
        self.assertIn('pids_events=[max 0 ', metrics)
        parent = self.cgroup / 'brick-captive'
        self.assertTrue(parent.is_dir())
        self.assertEqual(list(parent.glob('session-*')), [])
        self.assertIn('memory current_bytes=', result.stderr)

    def test_memory_policy_source_is_fail_closed_for_product_root(self):
        source = SUPERVISOR.read_text()
        self.assertIn('mount -t cgroup2 none "$cgroup_mount"', source)
        self.assertIn("printf '+memory +pids\\n'", source)
        self.assertIn('memory.oom.group', source)
        self.assertIn('cgroup.kill', source)
        self.assertIn('[ "$(id -u)" -ne 0 ]', source)
        self.assertNotIn('BRICK_CAPTIVE_MEMINFO', source)

    def test_memory_admission_denial_does_not_pause_display(self):
        meminfo = self.root / 'meminfo'
        meminfo.write_text('MemAvailable: 524287 kB\n')
        env = dict(self.env, BRICK_TEST_MEMINFO=str(meminfo))
        result = subprocess.run(
            [str(SUPERVISOR), NETWORK_A, 'http://portal.test/', '--',
             str(self.browser)], env=env, capture_output=True, text=True,
            timeout=5)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn('admission denied', result.stderr)
        self.assertFalse(self.events.exists())

    def test_oom_metrics_preserve_failure_and_restore_display(self):
        oom = self.root / 'browser-oom'
        oom.write_text(
            '#!/bin/sh\n'
            'printf "low 0\\nhigh 12\\nmax 3\\noom 1\\n'
            'oom_kill 2\\noom_group_kill 1\\n" '
            '> "$BRICK_CAPTIVE_CGROUP_PATH/memory.events"\n'
            'printf "402653184\\n" '
            '> "$BRICK_CAPTIVE_CGROUP_PATH/memory.peak"\n'
            'exit 137\n')
        oom.chmod(0o755)
        result = subprocess.run(
            [str(SUPERVISOR), NETWORK_A, 'http://portal.test/', '--',
             str(oom)], env=self.env, capture_output=True, text=True,
            timeout=5)
        self.assertEqual(result.returncode, 137, result.stderr)
        metrics = (self.runtime / 'last-memory').read_text()
        self.assertIn('peak_bytes=402653184', metrics)
        self.assertIn('oom_group_kill 1', metrics)
        self.assertEqual(self.events.read_text().splitlines()[-1],
                         f'hook {NETWORK_A}')

    def test_partial_pause_failure_still_runs_resume_hook(self):
        marker = self.root / 'pause-attempted'
        hook = self.root / 'partial-pause'
        hook.write_text(
            '#!/bin/sh\n'
            'printf "hook %s\\n" "$1" >> "$BRICK_TEST_EVENTS"\n'
            'if [ ! -e "$BRICK_TEST_PAUSE_MARKER" ]; then\n'
            '  : > "$BRICK_TEST_PAUSE_MARKER"\n'
            '  exit 9\n'
            'fi\n')
        hook.chmod(0o755)
        env = dict(self.env, BRICK_DISPLAY_PAUSE_HOOK=str(hook),
                   BRICK_DISPLAY_RESUME_HOOK=str(hook),
                   BRICK_TEST_PAUSE_MARKER=str(marker))
        result = subprocess.run(
            [str(SUPERVISOR), NETWORK_A, 'http://portal.test/', '--',
             str(self.browser)], env=env, capture_output=True, text=True,
            timeout=5)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.events.read_text().splitlines(),
                         [f'hook {NETWORK_A}', f'hook {NETWORK_A}'])
        self.assertEqual(list((self.cgroup / 'brick-captive').glob('session-*')),
                         [])

    def test_resume_failure_turns_success_into_cleanup_failure(self):
        marker = self.root / 'display-paused'
        hook = self.root / 'resume-fails'
        hook.write_text(
            '#!/bin/sh\n'
            'if [ -e "$BRICK_TEST_PAUSE_MARKER" ]; then exit 9; fi\n'
            ': > "$BRICK_TEST_PAUSE_MARKER"\n')
        hook.chmod(0o755)
        env = dict(self.env, BRICK_DISPLAY_PAUSE_HOOK=str(hook),
                   BRICK_DISPLAY_RESUME_HOOK=str(hook),
                   BRICK_TEST_PAUSE_MARKER=str(marker))
        result = subprocess.run(
            [str(SUPERVISOR), NETWORK_A, 'http://portal.test/', '--',
             str(self.browser)], env=env, capture_output=True, text=True,
            timeout=5)
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn('display resume hook failed', result.stderr)
        self.assertFalse((self.runtime / 'session').exists())


class EvdevInputHarnessTest(unittest.TestCase):
    def test_translation_repeat_and_drop_resynchronization(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            executable = directory / 'brick-evdev-input'
            subprocess.run([
                os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra',
                '-Werror', str(INPUT), '-o', str(executable),
            ], check=True)
            events = directory / 'events'
            # timeval(long,long), type(u16), code(u16), value(s32).
            def event(event_type, code, value):
                return struct.pack('@llHHi', 0, 0, event_type, code, value)
            events.write_bytes(b''.join((
                event(1, 0x220, 1),
                event(1, 0x223, 2),
                event(1, 0x131, 1),
                event(1, 0x131, 0),
                event(0, 3, 0),       # SYN_DROPPED
                event(1, 0x13c, 1),   # ignored while dropped
                event(0, 0, 0),       # SYN_REPORT / resync
                event(1, 0x2c1, 1),
            )))
            result = subprocess.run(
                [str(executable), '--no-grab', str(events)], check=True,
                capture_output=True, text=True)
            self.assertEqual(result.stdout.splitlines(), [
                'pointer-motion 0 -18',
                'pointer-motion 30 0',
                'pointer-button primary down',
                'pointer-button primary up',
                'resync begin',
                'resynced unavailable',
                'scroll 0 120',
            ])

    def test_launcher_adapter_has_bounded_balanced_wpe_events(self):
        source = (LAUNCHER / 'brick-controller.c').read_text()
        self.assertIn('O_NONBLOCK | O_CLOEXEC', source)
        self.assertIn('ioctl(self->fd, EVIOCGRAB, 1)', source)
        self.assertIn('event.code == SYN_DROPPED', source)
        self.assertIn('resync(self, event_time(&event))', source)
        self.assertIn('release_held(self,', source)
        self.assertIn('g_timeout_add_seconds(1, reconnect_controller', source)
        self.assertIn('CLAMP(self->x, 0, width - 1)', source)
        self.assertIn('wpe_view_event(self->view, event)', source)


class TrustedOskViewTest(unittest.TestCase):
    def test_launcher_owns_noncommunicating_same_view_osk_buffer(self):
        source = (LAUNCHER / 'brick-captive-wpe.c').read_text()
        self.assertGreaterEqual(source.count('FD_CLOEXEC'), 2)
        self.assertIn('"authority-fd"', source)
        self.assertNotIn('"osk-fd"', source)
        osk = (LAUNCHER / 'brick-osk-context.c').read_text()
        self.assertNotIn('g_message(', osk)
        self.assertNotIn('WEBKIT_TYPE_WEB_VIEW', osk)
        self.assertNotIn('webkit_web_context_new', osk)
        self.assertIn('wpe_buffer_shm_new(', osk)
        self.assertIn('wpe_view_render_buffer(self->portal_platform_view', osk)
        self.assertIn('WEBKIT_INPUT_HINT_INHIBIT_OSK', osk)
        self.assertIn('wpe_view_set_visible(self->portal_platform_view, FALSE)',
                      osk)
        self.assertIn('wpe_view_set_visible(self->portal_platform_view, TRUE)',
                      osk)
        self.assertIn('brick-trusted-osk-buffer', osk)
        self.assertIn('self->render_failures <= 3', osk)
        self.assertIn('wpe_view_set_visible(self->portal_platform_view, TRUE)',
                      osk)
        self.assertIn('g_signal_emit(self, failed_signal, 0)', osk)
        self.assertIn('portal producer did not quiesce; trusted OSK aborted',
                      osk)
        self.assertIn('self->state = BRICK_OSK_HIDDEN;', osk)
        self.assertIn('self->late_portal_frames = 0;', osk)
        self.assertNotIn(
            'self->osk_buffer_in_flight = FALSE;\n        '
            'self->late_portal_frames = 0;', osk)
        self.assertIn('authority_send(app, "OSK_FAILED", NULL)', source)
        for literal in ("case '@':", "case '-':", "case '_':", "case '/':",
                        "case '?':", "case '+':", "case '.':", "case ',':",
                        "case ':':", "case '(': return", "case ')': return",
                        "case '#':", "case '*':", "case '=':", "case '%':",
                        "case '&':", "case '!':", "case ';':", "case '$':"):
            self.assertIn(literal, osk)
        self.assertIn('return "EUR"', osk)
        self.assertIn('return blank;', osk)
        gate = (ROOT / 'package/brick-wpewebkit/source-patches/'
                '0004-WPE-drop-pending-web-frame-while-view-unmapped.patch')
        gate_source = gate.read_text()
        self.assertIn('!wpe_view_get_mapped(m_wpeView.get())', gate_source)
        self.assertIn('frameDone();', gate_source)
        self.assertIn('m_pendingBuffer = nullptr;', gate_source)
        self.assertIn('m_bufferIDs.contains(buffer)', gate_source)
        self.assertIn('bufferRendered(WPEBuffer*)', gate_source)
        model = (LAUNCHER / 'brick-osk-model.c').read_text()
        self.assertIn('brick_osk_model_key_label', model)
        self.assertIn('password', (LAUNCHER / 'README.md').read_text())

    def test_launcher_requires_gpu_dom_rendering_and_drops_privileges(self):
        launcher = (LAUNCHER / 'brick-captive-wpe.c').read_text()
        self.assertIn('UseGPUProcessForDOMRendering', launcher)
        self.assertIn('required GPU DOM rendering feature unavailable',
                      launcher)
        self.assertIn('g_setenv("WPE_PLATFORM", "drm", TRUE)', launcher)
        self.assertIn('g_setenv("GBM_ALWAYS_SOFTWARE", "true", TRUE)',
                      launcher)
        self.assertIn('g_setenv("GALLIUM_DRIVER", "softpipe", TRUE)',
                      launcher)
        self.assertIn('wpe_display_drm_connect_fd(', launcher)
        self.assertIn('g_unsetenv(CAPTIVE_DRM_FD_ENV)', launcher)
        self.assertIn('CAPTIVE_GPU_DRM_FD_ENV', launcher)
        self.assertIn('CAPTIVE_PROFILE_ROOT_ENV', launcher)

        runner = (LAUNCHER / 'brick-captive-run.c').read_text()
        self.assertIn('#define CAPTIVE_USER "brick-captive"', runner)
        self.assertIn('open(CAPTIVE_DRM_DEVICE, O_RDWR | O_CLOEXEC)', runner)
        self.assertIn('F_DUPFD_CLOEXEC', runner)
        self.assertIn('CAPTIVE_GPU_DRM_FD_ENV', runner)
        self.assertIn('setgroups(0, NULL)', runner)
        self.assertIn('getgroups(0, NULL) != 0', runner)
        self.assertIn('setresgid(', runner)
        self.assertIn('setresuid(', runner)
        self.assertIn('PR_SET_NO_NEW_PRIVS', runner)
        self.assertIn('unsetenv("WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS")',
                      runner)

    def test_drm_backend_imports_bounded_linear_shm_without_web_drm(self):
        patch = (ROOT / 'package' / 'brick-wpewebkit' / 'source-patches' /
                 '0003-WPEPlatformDRM-scan-out-shared-memory-buffers.patch').read_text()
        self.assertIn('WPE_IS_BUFFER_SHM(buffer)', patch)
        self.assertIn('WPE_PIXEL_FORMAT_ARGB8888', patch)
        self.assertIn('DRM_FORMAT_ARGB8888, DRM_FORMAT_MOD_LINEAR', patch)
        self.assertIn('auto rowBytes = static_cast<gsize>(width) * 4', patch)
        self.assertIn('sourceStride < rowBytes', patch)
        self.assertIn('height > G_MAXSIZE / sourceStride', patch)
        self.assertIn('sourceSize < static_cast<gsize>(sourceStride) * height',
                      patch)
        self.assertIn('GBM_BO_USE_SCANOUT | GBM_BO_USE_LINEAR', patch)
        self.assertIn('gbm_bo_map(', patch)
        self.assertIn('destinationStride < rowBytes', patch)
        self.assertIn('gbm_bo_unmap(drmBuffer.bufferObject(), mapData)', patch)
        self.assertIn('static bool drmBufferUpdateSHM(', patch)
        self.assertIn('WPE_IS_BUFFER_SHM(buffer) && '
                      '!drmBufferUpdateSHM(buffer, *drmBuffer, error)', patch)
        self.assertIn('refresh its pixels on every render submission', patch)
        self.assertNotIn('/dev/dri', patch)

        package = (ROOT / 'package/brick-wpewebkit/brick-wpewebkit.mk').read_text()
        self.assertNotIn(' - video Captive browser', package)
        sandbox_patch = (ROOT / 'package/brick-wpewebkit/source-patches/'
                         '0002-Bubblewrap-bind-graphics-devices-only-in-GPUProcess.patch').read_text()
        self.assertIn('ProcessType::GPU', sandbox_patch)
        self.assertIn('-        bindOpenGL(sandboxArgs);', sandbox_patch)
        self.assertIn('g_subprocess_launcher_take_fd', sandbox_patch)
        self.assertIn('g_subprocess_launcher_unsetenv', sandbox_patch)
        self.assertIn('gpuDRMTargetFD = 198', sandbox_patch)
        self.assertIn('Bubblewrap 0.13 passes unknown inherited fds',
                      sandbox_patch)
        self.assertIn('only its monitor and PID 1 close their copies',
                      sandbox_patch)
        self.assertIn('unsetenv("BRICK_CAPTIVE_GPU_DRM_FD")', sandbox_patch)
        self.assertIn('g_subprocess_launcher_unsetenv(launcher.get(), '
                      '"BRICK_CAPTIVE_PROFILE_ROOT")', sandbox_patch)
        self.assertIn('processType != ProcessLauncher::ProcessType::Web',
                      sandbox_patch)
        self.assertIn('processType != ProcessLauncher::ProcessType::Network',
                      sandbox_patch)
        self.assertIn('"--bind", profileRoot, profileRoot', sandbox_patch)


class SandboxProbeTest(unittest.TestCase):
    def test_probe_builds_and_seccomp_is_effective_on_build_host(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'brick-sandbox-probe'
            subprocess.run([
                os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra',
                '-Werror', str(SANDBOX), '-o', str(executable),
            ], check=True)
            result = subprocess.run([str(executable)], capture_output=True,
                                    text=True)
            # Namespace availability is host-policy dependent; the probe must
            # report each result, while seccomp is independently testable.
            self.assertIn('namespace_user=', result.stdout)
            self.assertIn('namespace_mount=', result.stdout)
            self.assertIn('namespace_pid=', result.stdout)
            self.assertIn('namespace_net=', result.stdout)
            self.assertIn('seccomp_filter=ok', result.stdout)


class KmsWatchdogRecoveryContractTest(unittest.TestCase):
    def test_drm_master_retry_is_bounded_and_ebusy_only(self):
        source = WATCHDOG.read_text()

        self.assertIn('#define DRM_MASTER_RETRY_INTERVAL_MS 20', source)
        self.assertIn('#define DRM_MASTER_RETRY_TIMEOUT_MS 3000', source)
        self.assertIn('master_error = errno;', source)
        self.assertIn('if (master_error != EBUSY || !retry_started_ms ||',
                      source)
        self.assertIn(
            'retry_now_ms - retry_started_ms >= '
            'DRM_MASTER_RETRY_TIMEOUT_MS', source)
        self.assertIn(
            '.tv_nsec = DRM_MASTER_RETRY_INTERVAL_MS * 1000 * 1000',
            source)
        self.assertIn('nanosleep(&retry_delay, NULL);', source)
        self.assertIn('master_attempts++;\n        if (drmSetMaster(fd) == 0)',
                      source)
        self.assertIn('acquired DRM master attempts=%u elapsed=%llu ms',
                      source)
        self.assertIn('DRM master unavailable attempts=%u elapsed=%llu ms',
                      source)


class WpeLifecycleHarnessTest(unittest.TestCase):
    def test_samples_authority_driven_sessions_and_injected_kill(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            browser = root / 'browser.py'
            browser.write_text('''#!/usr/bin/env python3
import argparse, socket
p=argparse.ArgumentParser()
p.add_argument('--uri'); p.add_argument('--network-key')
p.add_argument('--profile-root'); p.add_argument('--authority-fd', type=int)
a=p.parse_args(); s=socket.socket(fileno=a.authority_fd); f=s.makefile('rw', buffering=1)
f.write('READY\\n'); f.write('LOADED '+a.uri+'\\n')
for line in f:
    if line.strip() == 'SUCCESS': raise SystemExit(0)
''')
            browser.chmod(0o755)
            base = [str(LIFECYCLE), '--browser', str(browser), '--uri',
                    'http://portal.test/static', '--profile-root', str(root),
                    '--sessions', '3', '--timeout', '2']
            normal = subprocess.run(base, check=True, capture_output=True,
                                    text=True)
            report = json.loads(normal.stdout)
            self.assertEqual(report['implementation'],
                             'WPE WebKit DRM / Mesa softpipe GBM')
            self.assertEqual(report['loaded_sessions'], 3)
            self.assertEqual(report['timeouts'], 0)
            self.assertGreater(report['max_peak_fds'], 0)

            killed = subprocess.run(base + ['--kill-after-load'], check=True,
                                    capture_output=True, text=True)
            kill_report = json.loads(killed.stdout)
            self.assertTrue(all(item['injected_kill']
                                for item in kill_report['results']))

    def test_launcher_uses_supported_bounded_memory_pressure_api(self):
        launcher = (ROOT / 'board/trimui/brick/wpe-platform-brick/launcher/'
                    'brick-captive-wpe.c').read_text()
        self.assertIn('#define CAPTIVE_PROCESS_MEMORY_LIMIT_MB 192', launcher)
        self.assertIn('webkit_memory_pressure_settings_set_memory_limit(',
                      launcher)
        self.assertIn('webkit_memory_pressure_settings_set_poll_interval(',
                      launcher)
        self.assertIn('webkit_network_session_set_memory_pressure_settings(',
                      launcher)
        self.assertIn('"memory-pressure-settings", memory_pressure', launcher)
        self.assertIn('WEBKIT_CACHE_MODEL_DOCUMENT_BROWSER', launcher)
        self.assertNotIn('webkit_memory_pressure_settings_set_kill_threshold(',
                         launcher)

    def test_launcher_reports_a_real_first_frame_to_authority(self):
        launcher = (ROOT / 'board/trimui/brick/wpe-platform-brick/launcher/'
                    'brick-captive-wpe.c').read_text()
        self.assertIn('webkit_web_view_add_frame_displayed_callback(', launcher)
        self.assertIn('authority_send(app, "FRAME", NULL)', launcher)

    def test_authority_metrics_helper_builds_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([
                os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra',
                '-Werror', str(AUTHORITY_METRICS), '-o',
                str(Path(directory) / 'brick-authority-metrics')
            ], check=True, capture_output=True, text=True)

    def test_memory_domain_helpers_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run([
                os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra',
                '-Werror', str(MEMORY_HOLD), '-o',
                str(Path(directory) / 'brick-memory-hold')
            ], check=True, capture_output=True, text=True)
        source = CGROUP_MEMORY.read_text()
        for control in ('memory.high', 'memory.max', 'memory.oom.group',
                        'cgroup.kill', 'pids.max'):
            self.assertIn(control, source)
        self.assertIn('MemAvailable:', source)
        self.assertIn('leaked_processes=', source)
        self.assertIn('leaked_mounts=', source)


class CaptiveFixtureTest(unittest.TestCase):
    def setUp(self):
        self.server = subprocess.Popen(
            [os.environ.get('PYTHON', 'python3'), str(FIXTURE)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.base = self.server.stdout.readline().strip()
        if not self.base:
            error = self.server.stderr.read()
            self.server.wait(timeout=5)
            self.fail(f'captive fixture failed to start: {error}')
        self.addCleanup(self.stop_server)

    def stop_server(self):
        if self.server.poll() is None:
            self.server.send_signal(signal.SIGTERM)
            self.server.wait(timeout=5)
        self.server.stdout.close()
        self.server.stderr.close()

    def test_capport_and_form_cookie_javascript_chain(self):
        capport = json_load(urllib.request.urlopen(self.base + '/capport'))
        self.assertTrue(capport['captive'])
        self.assertEqual(capport['user-portal-url'], self.base + '/portal')

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar), NoRedirect())
        portal = urllib.request.urlopen(self.base + '/portal').read()
        self.assertIn(b'inputmode="numeric"', portal)
        body = urllib.parse.urlencode({
            'csrf': 'brick-fixture', 'code': '1234'}).encode()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(self.base + '/login', body)
        redirect = raised.exception
        self.assertEqual(redirect.code, 302)
        self.assertEqual(redirect.headers['Location'], '/javascript')
        redirect.close()
        javascript = opener.open(self.base + '/javascript').read()
        self.assertIn(b"location.replace('/done')", javascript)
        self.assertEqual(opener.open(self.base + '/done').read(),
                         b'FORM COOKIE JS OK')

    def test_osk_fixture_has_editable_focus_and_result_endpoint(self):
        osk = urllib.request.urlopen(self.base + '/osk').read()
        self.assertIn(b'inputmode="numeric"', osk)
        self.assertIn(b'type="password"', osk)
        self.assertIn(b'type="email"', osk)
        self.assertIn(b'type="url"', osk)
        self.assertIn(b'src="/osk-frame"', osk)
        self.assertIn(b'autofocus', osk)
        self.assertIn(b'/osk-result?length=', osk)
        self.assertIn(b'OSK OK',
                      urllib.request.urlopen(self.base +
                                             '/osk-result?length=4').read())

    def test_osk_fixture_can_target_a_distinct_iframe_origin(self):
        frame_origin = 'http://127.0.0.1:54321'
        query = urllib.parse.urlencode({
            'kind': 'iframe-cross-origin',
            'frame-origin': frame_origin,
        })
        page = urllib.request.urlopen(
            self.base + '/osk-qualify?' + query).read()
        self.assertIn((frame_origin +
                       '/osk-qualify-frame?kind=iframe-cross-origin').encode(),
                      page)

        frame = urllib.request.urlopen(
            self.base +
            '/osk-qualify-frame?kind=iframe-cross-origin').read()
        self.assertIn(b'/osk-result?kind=iframe-cross-origin&length=', frame)

        for unsafe_origin in ('javascript:alert(1)',
                              'http://user:pass@127.0.0.1:1',
                              'http://127.0.0.1:1/path'):
            with self.subTest(origin=unsafe_origin):
                unsafe = urllib.parse.urlencode({
                    'kind': 'iframe-cross-origin',
                    'frame-origin': unsafe_origin,
                })
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(
                        self.base + '/osk-qualify?' + unsafe)
                self.assertEqual(raised.exception.code, 400)
                raised.exception.close()

    def test_profile_fixture_seeds_and_checks_cookie_and_local_storage(self):
        seed = urllib.request.urlopen(self.base +
                                      '/profile-seed?marker=A')
        self.assertIn('brick_profile_marker=A', seed.headers['Set-Cookie'])
        self.assertIn('Max-Age=86400', seed.headers['Set-Cookie'])
        self.assertIn(b"localStorage.setItem('brick-profile-marker', 'A')",
                      seed.read())
        check = urllib.request.urlopen(
            self.base + '/profile-check?phase=a-reopen').read()
        self.assertIn(b"localStorage.getItem('brick-profile-marker')", check)
        recorded = urllib.request.urlopen(
            self.base +
            '/profile-result?phase=a-reopen&storage=missing').read()
        self.assertIn(b'PROFILE RESULT RECORDED', recorded)

        for path in ('/profile-seed?marker=secret',
                     '/profile-check?phase=../escape',
                     '/profile-result?phase=%0Asecret&storage=A'):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(self.base + path)
                self.assertEqual(raised.exception.code, 400)
                raised.exception.close()

    def test_redirect_loop_and_bounded_chain(self):
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(self.base + '/loop', timeout=2)
        self.assertEqual(raised.exception.code, 302)
        raised.exception.close()
        with urllib.request.urlopen(
                self.base + '/redirect-chain?n=5', timeout=2) as response:
            self.assertEqual(response.geturl(), self.base + '/static')
            self.assertIn(b'Static portal', response.read())

    def test_chunked_large_slow_and_connection_loss(self):
        self.assertEqual(
            urllib.request.urlopen(self.base + '/chunked', timeout=2).read(),
            b'chunk-one\nchunk-two\nchunk-three\n')
        large = urllib.request.urlopen(
            self.base + '/large?bytes=1048576', timeout=2).read()
        self.assertEqual(len(large), 1024 * 1024)
        started = time.monotonic()
        self.assertEqual(urllib.request.urlopen(
            self.base + '/slow?seconds=0.05', timeout=2).read(), b'SLOW OK')
        self.assertGreaterEqual(time.monotonic() - started, 0.04)
        with self.assertRaises((urllib.error.URLError,
                                http.client.RemoteDisconnected)):
            urllib.request.urlopen(self.base + '/drop', timeout=2).read()

    def test_terms_voucher_popup_iframe_idp_and_no_navigation(self):
        expected = {
            '/terms': b'Network terms',
            '/voucher': b'autocomplete="one-time-code"',
            '/popup': b'target="_blank"',
            '/iframe': b'<iframe',
            '/idp': b'type="password"',
            '/success-no-navigation': b'without top-level navigation',
        }
        for path, marker in expected.items():
            with self.subTest(path=path):
                self.assertIn(marker, urllib.request.urlopen(
                    self.base + path, timeout=2).read())

    def test_hostile_surfaces_and_authority_forgery_are_present(self):
        for path, marker in {
                '/forged-authority': b'CLOSED success',
                '/download': b'never save this',
                '/file-chooser': b'type="file"',
                '/permissions': b'getUserMedia',
                '/animate?frames=1000': b'n<1000',
                }.items():
            with self.subTest(path=path):
                self.assertIn(marker, urllib.request.urlopen(
                    self.base + path, timeout=2).read())

        launcher = (ROOT / 'board/trimui/brick/wpe-platform-brick/launcher/'
                    'brick-captive-wpe.c').read_text()
        self.assertIn('Only the external connectivity authority may send SUCCESS',
                      launcher)
        self.assertNotIn('webkit_web_view_get_title', launcher)
        self.assertNotIn('g_str_has_suffix', launcher)

    def test_full_frame_animation_fixture_reports_dom_completion(self):
        page = urllib.request.urlopen(
            self.base + '/animate-full?frames=1000&token=qualification',
            timeout=2).read()
        self.assertIn(b'surface.style.backgroundColor', page)
        self.assertIn(b'const timer=setInterval(draw,50)', page)
        self.assertEqual(json.loads(urllib.request.urlopen(
            self.base + '/animate-status?token=qualification',
            timeout=2).read()), {'frames': 0, 'elapsed_ms': 0})
        request = (self.base + '/animate-complete?token=qualification&'
                   'frames=1000&elapsed_ms=16667')
        self.assertEqual(urllib.request.urlopen(request, timeout=2).status, 204)
        self.assertEqual(json.loads(urllib.request.urlopen(
            self.base + '/animate-status?token=qualification',
            timeout=2).read()), {'frames': 1000, 'elapsed_ms': 16667})

    def test_stall_is_bounded_by_client_timeout(self):
        started = time.monotonic()
        with self.assertRaises((TimeoutError, urllib.error.URLError)):
            urllib.request.urlopen(self.base + '/stall?seconds=2',
                                   timeout=0.05).read()
        self.assertLess(time.monotonic() - started, 0.5)


class CaptiveTlsFixtureTest(unittest.TestCase):
    def make_certificate(self, directory, common_name, san):
        key = directory / f'{common_name}.key'
        cert = directory / f'{common_name}.pem'
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(key), '-out', str(cert), '-days', '1',
            '-subj', f'/CN={common_name}', '-addext',
            f'subjectAltName={san}',
        ], check=True, capture_output=True)
        return cert, key

    def make_expired_certificate(self, directory):
        directory.mkdir()
        ca_key = directory / 'ca.key'
        ca_cert = directory / 'ca.pem'
        key = directory / 'expired.key'
        request = directory / 'expired.csr'
        cert = directory / 'expired.pem'
        subprocess.run([
            'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(ca_key), '-out', str(ca_cert), '-days', '2',
            '-subj', '/CN=Brick fixture CA',
        ], check=True, capture_output=True)
        subprocess.run([
            'openssl', 'req', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(key), '-out', str(request),
            '-subj', '/CN=127.0.0.1', '-addext',
            'subjectAltName=IP:127.0.0.1',
        ], check=True, capture_output=True)
        (directory / 'newcerts').mkdir()
        (directory / 'index.txt').touch()
        (directory / 'serial').write_text('1000\n')
        config = directory / 'openssl.cnf'
        config.write_text(f'''[ca]
default_ca=fixture
[fixture]
dir={directory}
database=$dir/index.txt
new_certs_dir=$dir/newcerts
certificate=$dir/ca.pem
private_key=$dir/ca.key
serial=$dir/serial
default_md=sha256
policy=fixture_policy
copy_extensions=copy
[fixture_policy]
commonName=supplied
''')
        subprocess.run([
            'openssl', 'ca', '-batch', '-config', str(config),
            '-startdate', '20200101000000Z', '-enddate', '20200102000000Z',
            '-in', str(request), '-out', str(cert),
        ], check=True, capture_output=True)
        return cert, key, ca_cert

    def start_tls_fixture(self, cert, key):
        server = subprocess.Popen([
            os.environ.get('PYTHON', 'python3'), str(FIXTURE),
            '--tls-cert', str(cert), '--tls-key', str(key),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        base = server.stdout.readline().strip()
        if not base:
            error = server.stderr.read()
            server.wait(timeout=5)
            self.fail(f'TLS fixture failed to start: {error}')
        self.addCleanup(stop_process, server)
        return base

    def test_valid_trusted_self_signed_and_wrong_host_tls(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            valid_cert, valid_key = self.make_certificate(
                directory, 'brick-fixture', 'IP:127.0.0.1')
            valid_base = self.start_tls_fixture(valid_cert, valid_key)
            trusted = ssl.create_default_context(cafile=str(valid_cert))
            self.assertEqual(urllib.request.urlopen(
                valid_base + '/online', context=trusted, timeout=2).status, 204)
            with self.assertRaises(urllib.error.URLError) as untrusted:
                urllib.request.urlopen(valid_base + '/online', timeout=2)
            self.assertIsInstance(untrusted.exception.reason,
                                  ssl.SSLCertVerificationError)

            wrong_cert, wrong_key = self.make_certificate(
                directory, 'portal.invalid', 'DNS:portal.invalid')
            wrong_base = self.start_tls_fixture(wrong_cert, wrong_key)
            wrong_trust = ssl.create_default_context(cafile=str(wrong_cert))
            with self.assertRaises(urllib.error.URLError) as wrong_host:
                urllib.request.urlopen(wrong_base + '/online',
                                       context=wrong_trust, timeout=2)
            self.assertIsInstance(wrong_host.exception.reason,
                                  ssl.SSLCertVerificationError)

            expired_cert, expired_key, expired_ca = \
                self.make_expired_certificate(directory / 'expired')
            expired_base = self.start_tls_fixture(expired_cert, expired_key)
            expired_trust = ssl.create_default_context(cafile=str(expired_ca))
            with self.assertRaises(urllib.error.URLError) as expired:
                urllib.request.urlopen(expired_base + '/online',
                                       context=expired_trust, timeout=2)
            self.assertIsInstance(expired.exception.reason,
                                  ssl.SSLCertVerificationError)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def json_load(response):
    import json
    with response:
        return json.load(response)


def stop_process(process):
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
    process.stdout.close()
    process.stderr.close()


if __name__ == '__main__':
    unittest.main()
