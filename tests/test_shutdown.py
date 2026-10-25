"""Exercise shutdown process scanning without sending any process signals."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

OVERLAY = Path(__file__).resolve().parents[1] / 'board/trimui/brick/rootfs-overlay'
SCRIPT = OVERLAY / 'usr/libexec/brick-shutdown'


class ShutdownScanTests(unittest.TestCase):
    def setUp(self):
        self.busybox = shutil.which('busybox')
        if not self.busybox:
            self.skipTest('host BusyBox is required')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / 'awk').symlink_to(self.busybox)
        text = SCRIPT.read_text()
        self.function = text[text.index('remaining() {'):text.index('\nwait_empty()')]

    def scan(self, records):
        paths = []
        for index, record in enumerate(records):
            path = self.root / str(index)
            if record is not None:
                path.write_text(record)
            paths.append(str(path))
        # Only the production scan function is run, with fixture file operands.
        function = self.function.replace('/proc/[0-9]*/stat', ' '.join(paths))
        result = subprocess.run([self.busybox, 'sh', '-c',
                                 'session=123\n' + function + '\nremaining\n'],
                                env=dict(os.environ, PATH=str(self.root) + ':' + os.environ['PATH']),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_disappeared_first_file_does_not_hide_later_live_process(self):
        self.assertEqual(self.scan([None, '17 (name with (parentheses)) S 1 17 456 0\n']),
                         ['scan-raced', '17'])

    def test_malformed_stat_keeps_scan_nonempty_and_reads_later_process(self):
        self.assertEqual(self.scan(['broken\n', '18 (valid) S 1 18 456 0\n']),
                         ['scan-invalid', '18'])

    def test_init_own_session_kernel_threads_and_exited_tasks_excluded(self):
        self.assertEqual(self.scan([
            '1 (init) S 0 1 456 0\n',
            '18 (shutdown worker) S 1 18 123 0\n',
            '19 (kernel thread) S 2 0 0 0\n',
            '20 (zombie) Z 1 20 456 0\n',
            '21 (dead) X 1 21 456 0\n',
            '22 (live) S 1 22 456 0\n',
        ]), ['22'])

    def test_uninterruptible_task_is_not_treated_as_exited(self):
        self.assertEqual(self.scan(['23 (blocked writer) D 1 23 456 0\n']), ['23'])


if __name__ == '__main__':
    unittest.main()
