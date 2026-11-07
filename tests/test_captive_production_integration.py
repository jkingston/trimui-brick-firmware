import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / 'board/trimui/brick/wpe-platform-brick/integration'
DISPLAY = INTEGRATION / 'brick-captive-display'
CONTROL = INTEGRATION / 'brick-captive-control'
BROKER = INTEGRATION / 'brick-captive-broker'
AUTHORITY_SOURCE = INTEGRATION / 'brick-captive-authority.c'
NETWORK_KEY = '7' * 64


def wait_for(path, predicate=lambda value: True, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = path.read_text()
            if predicate(value):
                return value
        time.sleep(0.02)
    raise AssertionError(f'timed out waiting for {path}')


class DisplayLeaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'shell-state'
        self.state.write_text('running\n')
        self.events = self.root / 'events'
        self.shell = self.root / 'shell-init'
        self.shell.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$1" >> "$TEST_EVENTS"\n'
            'case $1 in\n'
            ' stop) [ "${TEST_IGNORE_STOP:-0}" = 1 ] || '
            'printf "stopped\\n" > "$TEST_STATE" ;;\n'
            ' start) [ "${TEST_IGNORE_START:-0}" = 1 ] || '
            'printf "running\\n" > "$TEST_STATE" ;;\n'
            'esac\n')
        self.shell.chmod(0o755)
        self.ssd = self.root / 'start-stop-daemon'
        self.ssd.write_text(
            '#!/bin/sh\n'
            '[ "$(cat "$TEST_STATE")" = running ]\n')
        self.ssd.chmod(0o755)
        self.env = dict(
            os.environ, BRICK_SHELL_INIT=str(self.shell),
            BRICK_SHELL_DAEMON='/usr/bin/brick-shell',
            BRICK_START_STOP_DAEMON=str(self.ssd),
            TEST_EVENTS=str(self.events), TEST_STATE=str(self.state))

    def tearDown(self):
        self.temp.cleanup()

    def run_hook(self, action, **extra):
        return subprocess.run(
            [str(DISPLAY), action], env=dict(self.env, **extra),
            capture_output=True, text=True)

    def test_pause_and_resume_delegate_to_shell_service_and_verify_state(self):
        self.assertEqual(self.run_hook('pause').returncode, 0)
        self.assertEqual(self.state.read_text().strip(), 'stopped')
        self.assertEqual(self.run_hook('resume').returncode, 0)
        self.assertEqual(self.state.read_text().strip(), 'running')
        self.assertEqual(self.run_hook('resume').returncode, 0)
        self.assertEqual(self.events.read_text().splitlines(), ['stop', 'start'])

    def test_pause_fails_closed_and_attempts_recovery_if_shell_remains(self):
        result = self.run_hook('pause', TEST_IGNORE_STOP='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events.read_text().splitlines(), ['stop', 'start'])

    def test_resume_fails_if_shell_service_does_not_become_active(self):
        self.state.write_text('stopped\n')
        result = self.run_hook('resume', TEST_IGNORE_START='1')
        self.assertNotEqual(result.returncode, 0)


class ControlAndBrokerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'
        self.wifi = self.root / 'wifi'
        self.wifi.mkdir()
        (self.wifi / 'connectivity').write_text(
            'state=captive\nportal_url=http://probe.test/generate_204\n')
        self.sys_input = self.root / 'sys-input'
        name = self.sys_input / 'event2/device/name'
        name.parent.mkdir(parents=True)
        name.write_text('TrimUI Brick Gamepad\n')
        self.dev_input = self.root / 'dev-input'
        self.dev_input.mkdir()
        self.wpa = self.root / 'wpa-cli'
        self.wpa.write_text(
            '#!/bin/sh\nprintf "id=%s\\nwpa_state=%s\\n" '
            '"${TEST_WPA_ID:-7}" "${TEST_WPA_STATE:-COMPLETED}"\n')
        self.wpa.chmod(0o755)
        self.identity = self.root / 'identity'
        self.identity.write_text(
            '#!/bin/sh\n[ "$1" = key ] && [ "$2" = 7 ] || exit 1\n'
            f'printf "%s\\n" {NETWORK_KEY}\n')
        self.identity.chmod(0o755)
        self.connectivity = self.root / 'connectivity-check'
        self.connectivity.write_text(
            '#!/bin/sh\nprintf "checked\\n" > "$TEST_CONNECTIVITY_CHECKED"\n')
        self.connectivity.chmod(0o755)
        self.connectivity_checked = self.root / 'connectivity-checked'
        self.env = dict(
            os.environ, BRICK_CAPTIVE_RUNTIME_ROOT=str(self.runtime),
            WIFI_STATE_DIR=str(self.wifi), WIFI_WPA_CLI=str(self.wpa),
            WIFI_PRIVATE_MAC=str(self.identity),
            WIFI_CONNECTIVITY=str(self.connectivity),
            TEST_CONNECTIVITY_CHECKED=str(self.connectivity_checked),
            BRICK_INPUT_SYSFS=str(self.sys_input),
            BRICK_INPUT_DEV=str(self.dev_input))

    def tearDown(self):
        self.temp.cleanup()

    def run_control(self, *args, **env):
        return subprocess.run(
            [str(CONTROL), *args], env=dict(self.env, **env),
            capture_output=True, text=True)

    def test_control_queues_only_valid_current_captive_network(self):
        result = self.run_control('open', '7')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.runtime / 'request').read_text().splitlines(),
            ['action=open', 'generation=1', 'network_id=7',
             f'network_key={NETWORK_KEY}',
             'url=http://probe.test/generate_204',
             f'input={self.dev_input}/event2'])

    def test_shell_actions_are_generation_safe_idempotent_and_have_fallback(self):
        self.runtime.mkdir()
        (self.runtime / 'status').write_text(
            'state=idle\ngeneration=0\n')
        status = self.run_control('status')
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn('generation=0\n', status.stdout)
        self.assertIn('ux_state=sign-in-required\n', status.stdout)
        self.assertIn('primary=sign-in\n', status.stdout)
        self.assertIn(
            'qr_payload=http://probe.test/generate_204\n', status.stdout)

        self.assertEqual(self.run_control('sign-in', '0').returncode, 0)
        request = (self.runtime / 'request').read_text()
        self.assertIn('generation=1\n', request)
        # Replayed intent is idempotent while the exact request is pending.
        self.assertEqual(self.run_control('sign-in', '1').returncode, 0)
        self.assertEqual((self.runtime / 'request').read_text(), request)
        stale = self.run_control('cancel', '0')
        self.assertEqual(stale.returncode, 3)
        self.assertEqual((self.runtime / 'request').read_text(), request)
        fallback = self.run_control('fallback', '1')
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        self.assertEqual(
            fallback.stdout.splitlines(),
            ['portal_url=http://probe.test/generate_204',
             'qr_payload=http://probe.test/generate_204'])

    def test_cancel_is_idempotent_and_stale_broker_cancel_cannot_mutate(self):
        self.runtime.mkdir()
        (self.runtime / 'status').write_text(
            'state=running\ngeneration=8\nnetwork_id=7\n')
        self.assertEqual(self.run_control('cancel', '8').returncode, 0)
        self.assertEqual(
            (self.runtime / 'request').read_text().splitlines(),
            ['action=cancel', 'generation=8'])
        (self.runtime / 'status').write_text('state=idle\ngeneration=8\n')
        self.assertEqual(self.run_control('cancel', '8').returncode, 0)
        self.assertFalse((self.runtime / 'request').exists())

    def test_control_rejects_stale_network_and_non_captive_authority(self):
        result = self.run_control('open', '7', TEST_WPA_ID='9')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.runtime / 'request').exists())

    def test_no_internet_retry_rechecks_connectivity_without_opening_browser(self):
        self.runtime.mkdir()
        (self.runtime / 'status').write_text('state=idle\ngeneration=4\n')
        (self.runtime / 'generation').write_text('4\n')
        (self.wifi / 'connectivity').write_text('state=limited\nportal_url=\n')
        result = self.run_control('retry', '4')
        self.assertEqual(result.returncode, 0, result.stderr)
        wait_for(self.connectivity_checked)
        self.assertFalse((self.runtime / 'request').exists())

    def test_control_bounds_stalled_wifi_helper(self):
        stalled = self.root / 'stalled-wpa'
        stalled.write_text('#!/bin/sh\nsleep 5\n')
        stalled.chmod(0o755)
        self.runtime.mkdir()
        (self.runtime / 'status').write_text('state=idle\ngeneration=0\n')
        started = time.monotonic()
        result = self.run_control(
            'sign-in', '0', WIFI_WPA_CLI=str(stalled), WIFI_COMMAND_TIMEOUT='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 2.5)
        (self.wifi / 'connectivity').write_text(
            'state=online\nportal_url=http://probe.test/generate_204\n')
        result = self.run_control('open', '7')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.runtime / 'request').exists())

    def test_control_rejects_open_while_session_is_active(self):
        self.runtime.mkdir()
        (self.runtime / 'status').write_text('state=running\n')
        result = self.run_control('open', '7')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.runtime / 'request').exists())

    def start_broker(self, supervisor):
        env = dict(
            self.env, BRICK_CAPTIVE_SUPERVISOR=str(supervisor),
            BRICK_CAPTIVE_AUTHORITY='/usr/libexec/brick-captive-authority',
            BRICK_CAPTIVE_BROWSER='/usr/bin/brick-captive-wpe',
            BRICK_DISPLAY_PAUSE_HOOK='/usr/libexec/brick-captive-display-pause',
            BRICK_DISPLAY_RESUME_HOOK='/usr/libexec/brick-captive-display-resume')
        process = subprocess.Popen(
            [str(BROKER), 'daemon'], env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True)
        wait_for(self.runtime / 'status', lambda value: 'state=idle' in value)
        return process

    def test_broker_invokes_the_single_supervisor_authority_browser_chain(self):
        invocation = self.root / 'invocation'
        supervisor = self.root / 'supervisor'
        supervisor.write_text(
            '#!/bin/sh\n'
            'printf "pause=%s\\nresume=%s\\nnetwork_id=%s\\nwatchdog=%s\\nargs=%s\\n" '
            '"$BRICK_DISPLAY_PAUSE_HOOK" "$BRICK_DISPLAY_RESUME_HOOK" '
            '"$BRICK_CAPTIVE_WPA_NETWORK_ID" "$BRICK_CAPTIVE_WATCHDOG" '
            '"$*" > "$TEST_INVOCATION"\n')
        supervisor.chmod(0o755)
        self.env['TEST_INVOCATION'] = str(invocation)
        broker = self.start_broker(supervisor)
        try:
            result = self.run_control('open', '7')
            self.assertEqual(result.returncode, 0, result.stderr)
            status = wait_for(
                self.runtime / 'status',
                lambda value: 'result=success' in value)
            self.assertIn('state=idle', status)
            called = invocation.read_text()
            self.assertIn('brick-captive-display-pause', called)
            self.assertIn('brick-captive-display-resume', called)
            self.assertIn('network_id=7', called)
            self.assertIn('watchdog=/usr/libexec/brick-captive-watchdog', called)
            self.assertIn(
                f'args={NETWORK_KEY} http://probe.test/generate_204 -- '
                '/usr/libexec/brick-captive-authority '
                '/usr/bin/brick-captive-wpe --input-device '
                f'{self.dev_input}/event2', called)
        finally:
            broker.terminate()
            broker.wait(timeout=3)

    def test_cancel_publishes_terminal_outcome_and_returns_broker_idle(self):
        supervisor_started = self.root / 'supervisor-started'
        supervisor = self.root / 'supervisor'
        supervisor.write_text(
            '#!/bin/sh\n'
            'touch "$TEST_SUPERVISOR_STARTED"\n'
            'trap "exit 143" HUP INT TERM\n'
            'while :; do sleep 1; done\n')
        supervisor.chmod(0o755)
        self.env['TEST_SUPERVISOR_STARTED'] = str(supervisor_started)
        broker = self.start_broker(supervisor)
        try:
            self.assertEqual(self.run_control('open', '7').returncode, 0)
            wait_for(supervisor_started)
            self.assertEqual(self.run_control('cancel').returncode, 0)
            status = wait_for(
                self.runtime / 'status',
                lambda value: 'result=exit-143' in value)
            self.assertIn('state=idle', status)
            self.assertIn('generation=1', status)
        finally:
            broker.terminate()
            broker.wait(timeout=3)

    def test_broker_rejects_malformed_request_without_running_supervisor(self):
        invocation = self.root / 'invocation'
        supervisor = self.root / 'supervisor'
        supervisor.write_text(
            '#!/bin/sh\ntouch "$TEST_INVOCATION"\n')
        supervisor.chmod(0o755)
        self.env['TEST_INVOCATION'] = str(invocation)
        broker = self.start_broker(supervisor)
        try:
            (self.runtime / 'request').write_text(
                'action=open\nnetwork_key=raw-ssid\n'
                'url=file:///etc/passwd\ninput=/dev/input/event0\n')
            status = wait_for(
                self.runtime / 'status',
                lambda value: 'result=invalid-request' in value)
            self.assertIn('state=failed', status)
            self.assertFalse(invocation.exists())
        finally:
            broker.terminate()
            broker.wait(timeout=3)

    def test_broker_ignores_stale_cancel_and_publishes_current_cancel(self):
        supervisor_pid = self.root / 'cancel-supervisor-pid'
        supervisor = self.root / 'cancel-supervisor'
        supervisor.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$$" > "$TEST_SUPERVISOR_PID"\n'
            'trap "exit 143" HUP INT TERM\n'
            'while :; do sleep 1; done\n')
        supervisor.chmod(0o755)
        self.env['TEST_SUPERVISOR_PID'] = str(supervisor_pid)
        broker = self.start_broker(supervisor)
        try:
            self.assertEqual(self.run_control('open', '7').returncode, 0)
            wait_for(self.runtime / 'status', lambda value: 'state=running' in value)
            pid = int(wait_for(supervisor_pid).strip())
            (self.runtime / 'request').write_text(
                'action=cancel\ngeneration=0\n')
            time.sleep(0.25)
            self.assertIn('state=running', (self.runtime / 'status').read_text())
            os.kill(pid, 0)
            self.assertEqual(self.run_control('cancel', '1').returncode, 0)
            status = wait_for(
                self.runtime / 'status',
                lambda value: 'state=idle' in value and 'result=exit-143' in value)
            self.assertIn('generation=1', status)
        finally:
            broker.terminate()
            broker.wait(timeout=3)

    def test_broker_sigkill_releases_singleton_lock_for_init_recovery(self):
        supervisor_pid = self.root / 'supervisor-pid'
        supervisor = self.root / 'supervisor'
        supervisor.write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$$" > "$TEST_SUPERVISOR_PID"\n'
            'trap "exit 143" HUP INT TERM\n'
            'while :; do sleep 1; done\n')
        supervisor.chmod(0o755)
        self.env['TEST_SUPERVISOR_PID'] = str(supervisor_pid)
        first = self.start_broker(supervisor)
        second = None
        orphan = None
        try:
            self.assertEqual(self.run_control('open', '7').returncode, 0)
            orphan = int(wait_for(supervisor_pid).strip())
            old_broker = int((self.runtime / 'broker.pid').read_text())
            first.kill()
            first.wait(timeout=3)
            env = dict(
                self.env, BRICK_CAPTIVE_SUPERVISOR=str(supervisor),
                BRICK_CAPTIVE_AUTHORITY='/usr/libexec/brick-captive-authority',
                BRICK_CAPTIVE_BROWSER='/usr/bin/brick-captive-wpe',
                BRICK_DISPLAY_PAUSE_HOOK='/usr/libexec/brick-captive-display-pause',
                BRICK_DISPLAY_RESUME_HOOK='/usr/libexec/brick-captive-display-resume')
            for _ in range(20):
                candidate = subprocess.Popen(
                    [str(BROKER), 'daemon'], env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    text=True)
                time.sleep(0.05)
                if candidate.poll() is None:
                    second = candidate
                    break
                candidate.wait(timeout=1)
            self.assertIsNotNone(second)
            wait_for(self.runtime / 'status', lambda value: 'state=idle' in value)
            new_broker = int((self.runtime / 'broker.pid').read_text())
            self.assertNotEqual(new_broker, old_broker)
        finally:
            if orphan:
                try:
                    os.kill(orphan, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if second and second.poll() is None:
                second.terminate()
                second.wait(timeout=3)
            if first.poll() is None:
                first.kill()
                first.wait(timeout=3)


class ConnectivityAuthorityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.authority = Path(cls.build.name) / 'brick-captive-authority'
        subprocess.run(
            ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
             str(AUTHORITY_SOURCE), '-o', str(cls.authority)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.wifi = self.root / 'wifi'
        self.wifi.mkdir()
        self.received = self.root / 'received'
        self.browser = self.root / 'browser.py'
        self.browser.write_text(
            '#!/usr/bin/env python3\n'
            'import os, sys\n'
            'channel = os.fdopen(3, "r+b", buffering=0)\n'
            'channel.write(b"FRAME\\nPAGE success\\nLOADED http://portal/\\n")\n'
            'line = channel.readline().decode().strip()\n'
            'open(os.environ["TEST_RECEIVED"], "w").write(line + "\\n")\n'
            'sys.exit(0 if line == "SUCCESS" else 1)\n')
        self.browser.chmod(0o755)
        self.checker = self.root / 'checker'
        self.checker.write_text(
            '#!/bin/sh\n'
            'printf "state=%s\\n" "${TEST_CONNECTIVITY_STATE:-captive}" '
            '> "$WIFI_STATE_DIR/connectivity"\n')
        self.checker.chmod(0o755)
        self.wpa = self.root / 'wpa-cli'
        self.wpa.write_text(
            '#!/bin/sh\nprintf "id=%s\\nwpa_state=%s\\n" '
            '"${TEST_WPA_ID:-7}" "${TEST_WPA_STATE:-COMPLETED}"\n')
        self.wpa.chmod(0o755)
        self.env = dict(
            os.environ, WIFI_STATE_DIR=str(self.wifi),
            BRICK_CONNECTIVITY_CHECKER=str(self.checker),
            WIFI_WPA_CLI=str(self.wpa), BRICK_CAPTIVE_WPA_NETWORK_ID='7',
            BRICK_AUTHORITY_CHECK_INTERVAL_MS='100',
            BRICK_AUTHORITY_SESSION_TIMEOUT_MS='1000',
            TEST_RECEIVED=str(self.received))

    def tearDown(self):
        self.temp.cleanup()

    def test_only_fresh_connectivity_authority_online_result_succeeds(self):
        result = subprocess.run(
            [str(self.authority), str(self.browser)],
            env=dict(self.env, TEST_CONNECTIVITY_STATE='online'),
            capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.received.read_text(), 'SUCCESS\n')

    def test_authorized_closed_event_allows_controller_cleanup_to_finish(self):
        browser = self.root / 'slow-controller-cleanup.py'
        browser.write_text(
            '#!/usr/bin/env python3\n'
            'import os, time\n'
            'channel = os.fdopen(3, "r+b", buffering=0)\n'
            'channel.write(b"LOADED http://portal/\\n")\n'
            'assert channel.readline().strip() == b"SUCCESS"\n'
            'channel.write(b"CLOSED success\\n")\n'
            'time.sleep(0.25)\n')
        browser.chmod(0o755)
        result = subprocess.run(
            [str(self.authority), str(browser)],
            env=dict(self.env, TEST_CONNECTIVITY_STATE='online'),
            capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_claims_cannot_authorize_a_captive_connection(self):
        result = subprocess.run(
            [str(self.authority), str(self.browser)], env=self.env,
            capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        if self.received.exists():
            self.assertNotEqual(self.received.read_text().strip(), 'SUCCESS')

    def test_network_change_cannot_authorize_online_connectivity(self):
        result = subprocess.run(
            [str(self.authority), str(self.browser)],
            env=dict(self.env, TEST_CONNECTIVITY_STATE='online', TEST_WPA_ID='9'),
            capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        if self.received.exists():
            self.assertNotEqual(self.received.read_text().strip(), 'SUCCESS')

    def test_wifi_loss_cannot_authorize_online_connectivity(self):
        result = subprocess.run(
            [str(self.authority), str(self.browser)],
            env=dict(self.env, TEST_CONNECTIVITY_STATE='online',
                     TEST_WPA_STATE='DISCONNECTED'),
            capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        if self.received.exists():
            self.assertNotEqual(self.received.read_text().strip(), 'SUCCESS')

    def test_network_change_during_probe_cannot_authorize(self):
        counter = self.root / 'wpa-count'
        changing_wpa = self.root / 'changing-wpa'
        changing_wpa.write_text(
            '#!/bin/sh\n'
            'count=$(cat "$TEST_WPA_COUNT" 2>/dev/null || printf 0)\n'
            'count=$((count + 1))\nprintf "%s\\n" "$count" > "$TEST_WPA_COUNT"\n'
            'if [ "$count" -eq 1 ]; then id=7; else id=9; fi\n'
            'printf "id=%s\\nwpa_state=COMPLETED\\n" "$id"\n')
        changing_wpa.chmod(0o755)
        result = subprocess.run(
            [str(self.authority), str(self.browser)],
            env=dict(self.env, TEST_CONNECTIVITY_STATE='online',
                     TEST_WPA_COUNT=str(counter), WIFI_WPA_CLI=str(changing_wpa)),
            capture_output=True, text=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertGreaterEqual(int(counter.read_text()), 2)
        if self.received.exists():
            self.assertNotEqual(self.received.read_text().strip(), 'SUCCESS')


class ImageIntegrationContractTest(unittest.TestCase):
    def test_package_and_post_build_install_complete_production_chain(self):
        package = (ROOT / 'package/brick-wpewebkit/brick-wpewebkit.mk').read_text()
        self.assertIn('BRICK_WPEWEBKIT_INSTALL_PREBUILT_STAGING', package)
        self.assertIn('BRICK_WPEWEBKIT_INSTALL_STAGING_CMDS', package)
        self.assertNotIn('BRICK_WPEWEBKIT_POST_BUILD_HOOKS', package)
        for path in (
                '/usr/libexec/brick-captive-authority',
                '/usr/libexec/brick-captive-supervisor',
                '/usr/libexec/brick-captive-broker',
                '/usr/libexec/brick-captive-watchdog',
                '/usr/libexec/brick-captive-display',
                '/usr/bin/brick-captive-control'):
            self.assertIn(path, package)
        post_build = (ROOT / 'board/trimui/brick/post-build.sh').read_text()
        self.assertIn('BR2_PACKAGE_BRICK_WPEWEBKIT=y', post_build)
        self.assertIn(
            '::respawn:/usr/libexec/brick-captive-broker daemon', post_build)
        inittab = (ROOT / 'board/trimui/brick/rootfs-overlay/etc/inittab').read_text()
        self.assertNotIn('brick-captive-broker', inittab)

    def test_production_chain_requires_external_exact_kms_recovery(self):
        broker = BROKER.read_text()
        supervisor = (INTEGRATION / 'brick-captive-supervisor').read_text()
        watchdog = (INTEGRATION / 'brick-kms-watchdog.c').read_text()
        self.assertIn('BRICK_CAPTIVE_WATCHDOG="$watchdog"', broker)
        self.assertIn('exec 9>&-', broker)
        self.assertIn('flock -x 8', broker)
        self.assertIn('resume_display 8>&-', broker)
        self.assertIn('rmdir "$cgroup" 2>/dev/null || return 1', broker)
        self.assertIn('"$resume_hook" "$network_key" 9>&-', supervisor)
        self.assertIn('--restore-only --child-cgroup "$cgroup_session"',
                      supervisor)
        self.assertIn('--pause-hook "$pause_hook"', supervisor)
        self.assertIn('drmModeSetCrtc(fd, saved->crtc_id, saved->fb_id,',
                      watchdog)
        self.assertIn('if (!restart_shell)\n        return rc;', watchdog)

    def test_maintained_shell_uses_only_control_api_and_installs_qr_encoder(self):
        shell = ROOT.parent / 'custom-firmware'
        captive = (shell / 'src/captive.zig').read_text()
        backend = (shell / 'src/linux_backend.zig').read_text()
        model = (shell / 'src/model.zig').read_text()
        config = (shell / 'package/brick-shell/Config.in').read_text()
        self.assertIn('/usr/bin/brick-captive-control', captive)
        self.assertNotIn('brick-captive-broker', captive)
        self.assertNotIn('brick-captive-supervisor', captive)
        self.assertIn('StaleGeneration', captive)
        self.assertIn('wifi_fallback', model)
        self.assertIn('select BR2_PACKAGE_LIBQRENCODE_TOOLS', config)
        # Captive state is user-facing, not a permanent background wakeup.
        self.assertIn('state.page == .wifi or state.page == .wifi_fallback',
                      backend)
        self.assertIn('captive_deadline = nowMilliseconds(io) + 5000',
                      backend)
        self.assertNotIn('else => 10000', backend)


if __name__ == '__main__':
    unittest.main()
