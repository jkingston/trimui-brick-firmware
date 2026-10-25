"""Exercise bounded shutdown with mocks and isolated test processes."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
import unittest

LIB = Path(__file__).resolve().parents[1] / 'board/trimui/brick/rootfs-overlay/usr/libexec'


class ServiceStopTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.mock = self.root / 'bin'
        self.mock.mkdir()
        self.state = self.root / 'state'
        self.state.write_text('persistent\n')
        self.log = self.root / 'calls'
        self.log.write_text('')
        self.helper = self.root / 'brick-stop-daemon'
        self.helper.write_text((LIB / self.helper.name).read_text())
        self.helper.chmod(0o755)
        self.originals = self.root / 'originals'
        self.originals.mkdir()
        self.pids = self.root / 'pids'
        self.pids.mkdir()
        for name in ('S30dbus-daemon', 'S40bluetoothd', 'S46bluealsa', 'S50dropbear'):
            script = self.originals / name
            script.write_text('#!/bin/sh\nprintf "original %s\\n" "$*" >> "$CALL_LOG"\n')
            script.chmod(0o755)
        wrapper = (LIB / 'brick-state-service').read_text()
        self.wrapper = self.root / 'wrapper'
        self.wrapper.write_text(wrapper.replace('/run/brick-data/state', str(self.state))
                                .replace('/usr/libexec/brick-stop-daemon', str(self.helper))
                                .replace('/usr/libexec/brick-init', str(self.originals))
                                .replace('/var/run/', str(self.pids) + '/')
                                .replace('/run/bluealsa.pid', str(self.pids / 'bluealsa.pid')))
        control = self.mock / 'start-stop-daemon'
        control.write_text('''#!/bin/sh
printf 'control %s\n' "$*" >> "$CALL_LOG"
case "$*" in
*--test*)
    case "$SCENARIO" in
    stuck|masked-signal-error) exit 0;;
    poll-error) exit 2;;
    delayed) [ "$(grep -c -- --test "$CALL_LOG")" -lt 3 ]; exit $?;;
    *) exit 1;;
    esac;;
*) [ "$SCENARIO" != command-error ] || exit 2;;
esac
''')
        control.chmod(0o755)
        sleep = self.mock / 'sleep'
        sleep.write_text('#!/bin/sh\nprintf "sleep %s\\n" "$*" >> "$CALL_LOG"\n')
        sleep.chmod(0o755)

    def run_service(self, service, action, scenario='exited'):
        env = dict(os.environ, PATH=str(self.mock) + ':/usr/bin:/bin',
                   CALL_LOG=str(self.log), SCENARIO=scenario)
        result = subprocess.run(['sh', str(self.wrapper), service, action],
                                env=env, capture_output=True, text=True, timeout=5)
        return result, self.log.read_text().splitlines()

    def test_failed_stop_retains_pid_and_never_starts(self):
        for service, pid in [('S30dbus-daemon', 'dbus-daemon.pid'),
                             ('S40bluetoothd', 'bluetoothd.pid'),
                             ('S46bluealsa', 'bluealsa.pid')]:
            for action in ('stop', 'restart'):
                for scenario, code in [('stuck', 1), ('command-error', 2), ('masked-signal-error', 1), ('poll-error', 2)]:
                    with self.subTest(service=service, action=action, scenario=scenario):
                        self.log.write_text('')
                        pidfile = self.pids / pid
                        pidfile.write_text('12345\n')
                        result, calls = self.run_service(service, action, scenario)
                        self.assertEqual(result.returncode, code)
                        self.assertTrue(pidfile.exists())
                        self.assertNotIn('OK', result.stdout)
                        self.assertFalse(any(c.startswith('original ') for c in calls))
                        if scenario in ('stuck', 'masked-signal-error'):
                            self.assertEqual(calls.count('sleep 0.1'), 50)

    def test_successful_stop_and_restart(self):
        for service, pid in [('S30dbus-daemon', 'dbus-daemon.pid'),
                             ('S40bluetoothd', 'bluetoothd.pid'),
                             ('S46bluealsa', 'bluealsa.pid')]:
            for action in ('stop', 'restart'):
                with self.subTest(service=service, action=action):
                    self.log.write_text('')
                    pidfile = self.pids / pid
                    pidfile.write_text('12345\n')
                    result, calls = self.run_service(service, action)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(pidfile.exists())
                    self.assertIn('OK', result.stdout)
                    self.assertEqual([c for c in calls if c.startswith('original ')],
                                     ['original start'] if action == 'restart' else [])

    def test_delayed_exit_is_observed_before_restart(self):
        for service in ('S30dbus-daemon', 'S40bluetoothd', 'S46bluealsa'):
            with self.subTest(service=service):
                self.log.write_text('')
                result, calls = self.run_service(service, 'restart', 'delayed')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls.count('sleep 0.1'), 2)
                self.assertEqual(calls[-1], 'original start')

    def test_failure_gate_allows_stop_but_not_restart(self):
        self.state.write_text('unavailable\n')
        for service in ('S30dbus-daemon', 'S40bluetoothd', 'S46bluealsa'):
            self.log.write_text('')
            result, calls = self.run_service(service, 'restart')
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(calls, [])
            result, calls = self.run_service(service, 'stop')
            self.assertEqual(result.returncode, 0)
            self.assertTrue(calls)

    def test_host_busybox_absent_pid_stop_and_restart(self):
        busybox = shutil.which('busybox')
        if not busybox:
            self.skipTest('host BusyBox unavailable')
        applets = subprocess.check_output([busybox, '--list'], text=True).splitlines()
        if 'start-stop-daemon' not in applets:
            self.skipTest('host BusyBox lacks start-stop-daemon')
        # A missing private PID file limits BusyBox lookup to no process.
        missing_pid = self.pids / 'never-created.pid'
        command = [busybox, 'start-stop-daemon', '--stop', '--quiet',
                   '--pidfile', str(missing_pid), '--exec', '/nonexistent/brick-test']
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run(command + ['--oknodo'],
                                        capture_output=True).returncode, 0)
        control = self.mock / 'start-stop-daemon'
        control.unlink()
        control.symlink_to(busybox)
        for service in ('S30dbus-daemon', 'S40bluetoothd', 'S46bluealsa'):
            for action in ('stop', 'restart'):
                with self.subTest(service=service, action=action):
                    self.log.write_text('')
                    self.assertEqual(list(self.pids.iterdir()), [])
                    result, calls = self.run_service(service, action)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('OK', result.stdout)
                    self.assertEqual(calls, ['original start'] if action == 'restart' else [])

    def test_real_daemon_pidfile_cleanup_and_zombie_exit(self):
        busybox = shutil.which('busybox')
        compiler = shutil.which('cc')
        if not busybox or not compiler:
            self.skipTest('host BusyBox and C compiler required')
        if 'start-stop-daemon' not in subprocess.check_output(
                [busybox, '--list'], text=True).splitlines():
            self.skipTest('host BusyBox lacks start-stop-daemon')
        source = self.root / 'daemon.c'
        binary = self.root / 'daemon'
        source.write_text(r'''#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
static volatile sig_atomic_t stopping;
static void stop(int sig) { (void)sig; stopping = 1; }
int main(int argc, char **argv) {
    (void)argc;
    signal(SIGTERM, stop);
    puts("ready"); fflush(stdout);
    while (!stopping) pause();
    unlink(argv[1]);
    if (!strcmp(argv[2], "replace")) {
        FILE *f = fopen(argv[1], "w");
        if (!f) return 2;
        fputs("99999999\n", f); fclose(f);
    }
    if (!strcmp(argv[2], "stuck")) for (;;) pause();
    usleep(300000);
    return 0;
}
''')
        subprocess.run([compiler, '-Wall', '-Wextra', '-Werror', str(source),
                        '-o', str(binary)], check=True, capture_output=True)
        control = self.mock / 'start-stop-daemon'
        control.unlink()
        control.symlink_to(busybox)
        (self.mock / 'sleep').unlink()
        env = dict(os.environ, PATH=str(self.mock) + ':/usr/bin:/bin')
        for scenario in ('unlink', 'replace', 'stuck'):
            with self.subTest(scenario=scenario):
                pidfile = self.pids / 'real.pid'
                daemon = subprocess.Popen([str(binary), str(pidfile), scenario],
                                          stdout=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(daemon.stdout.readline(), 'ready\n')
                    pidfile.write_text(str(daemon.pid) + '\n')
                    started = time.monotonic()
                    result = subprocess.run(['sh', str(self.helper), 'test',
                                             str(pidfile), str(binary)], env=env,
                                            capture_output=True, text=True, timeout=12)
                    elapsed = time.monotonic() - started
                    stat = Path('/proc') / str(daemon.pid) / 'stat'
                    # Parent deliberately has not reaped the child: successful
                    # termination must accept Z, but never an executing process.
                    state = stat.read_text().split(') ', 1)[1].split()[0]
                    if scenario == 'stuck':
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotEqual(state, 'Z')
                        self.assertGreaterEqual(elapsed, 4.9)
                        self.assertNotIn('OK', result.stdout)
                    else:
                        self.assertEqual(state, 'Z')
                        self.assertGreaterEqual(elapsed, .25)
                        if scenario == 'replace':
                            self.assertNotEqual(result.returncode, 0)
                            self.assertEqual(pidfile.read_text(), '99999999\n')
                        else:
                            self.assertEqual(result.returncode, 0, result.stderr)
                            self.assertFalse(pidfile.exists())
                finally:
                    if daemon.poll() is None:
                        daemon.kill()
                    daemon.wait(timeout=3)
                    daemon.stdout.close()
                    pidfile.unlink(missing_ok=True)

    def test_other_actions_and_services_keep_original_dispatch(self):
        for service, action in [('S30dbus-daemon', 'start'),
                                ('S40bluetoothd', 'reload'),
                                ('S50dropbear', 'restart'), ('S50dropbear', 'stop')]:
            with self.subTest(service=service, action=action):
                self.log.write_text('')
                result, calls = self.run_service(service, action)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(calls, ['original ' + action])


if __name__ == '__main__':
    unittest.main()
