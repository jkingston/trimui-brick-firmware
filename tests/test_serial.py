"""Serial protocol tests use local sockets/mocks; no USB devices are opened."""
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import socket
import pty
import termios
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('brick_serial', Path(__file__).resolve().parents[1] / 'tools/serial_console.py')
serial = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serial)


class SerialTests(unittest.TestCase):
    def test_disconnect_during_cleanup_preserves_command_error(self):
        master, slave = pty.openpty()
        console = serial.Console(os.ttyname(slave))
        try:
            with self.assertRaisesRegex(serial.ProtocolError, 'reboot'):
                with console:
                    os.close(master)
                    master = None
                    raise serial.ProtocolError('reboot disconnected USB')
            with self.assertRaises(OSError):
                os.fstat(console.fd)
        finally:
            if master is not None:
                os.close(master)
            os.close(slave)


    def test_fragmented_markers_and_partial_writes(self):
        left, right = socket.socketpair()
        left.setblocking(False)
        payload, start, done = serial.frame('true')
        # Derive actual token independently from the transmitted command.
        token = re.search(rb'BRICK_[a-f0-9]+', payload).group()
        received = bytearray()
        def peer():
            while len(received) < len(payload):
                received.extend(right.recv(19))
            response = payload + b'\r\n' + token + b'_START\r\nhello\r\n\r\n' + token + b'_END:7\r\n'
            for i in range(0, len(response), 3):
                os.write(right.fileno(), response[i:i+3])
        worker = threading.Thread(target=peer)
        worker.start()
        real_write = os.write
        try:
            with mock.patch.object(serial.os, 'write', side_effect=lambda fd, b: real_write(fd, b[:7])):
                data = serial.Transport(left.fileno()).exchange(payload, done, 3)
            self.assertEqual(bytes(received), payload)
            self.assertEqual(serial.result(data, start, done), (b'hello\n', 7))
        finally:
            worker.join(3)
            left.close(); right.close()

    def test_timeout_without_completion(self):
        left, right = socket.socketpair()
        left.setblocking(False)
        try:
            with self.assertRaises(TimeoutError):
                serial.Transport(left.fileno()).exchange(b'', re.compile(b'never'), 0.01)
        finally:
            left.close(); right.close()

    def test_upload_paths_cannot_escape_ram_directory(self):
        for path in ('/mnt/SDCARD/module.ko', '/tmp/../etc/file', '/tmp/nested/file', '/tmp/', 'relative', '/tmp//file'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                serial.remote_destination(path)
        self.assertEqual(serial.remote_destination('/tmp/module.ko'), '/tmp/module.ko')

    def test_seven_megabyte_upload_protocol_and_restore(self):
        content = bytes(range(256)) * (7 * 4096)
        console = serial.Console('unused')
        commands = []
        uploaded = bytearray()
        frames = []
        def checked(command, timeout=None):
            commands.append(command)
            if command.endswith('stty -g'): return b'1:2:3:4\n'
            if 'sha256sum' in command: return hashlib.sha256(content).hexdigest().encode()
            if ' wc -c ' in command: return str(len(content)).encode()
            return b''
        console.checked = checked
        def exchange(payload, completion, timeout):
            token = re.search(rb'BRICK_[a-f0-9]+', payload).group()
            opening = re.search(rb"<<'(BRICK_DATA_[a-f0-9]+)'\n", payload)
            delimiter = opening.group(1)
            encoded = payload[opening.end():payload.index(b'\n' + delimiter, opening.end())]
            self.assertLessEqual(max(map(len, encoded.splitlines())), 256)
            frames.append(payload)
            self.assertLessEqual(len(payload), 2048)
            self.assertIn(b'wc -c < ', payload)
            self.assertRegex(payload, rb'wc -c < /tmp/[^ ]+/chunk')
            self.assertNotRegex(payload, rb'wc -c < /tmp/[^ ]+/payload')
            self.assertIn(b' cat ', payload)
            chunk = base64.b64decode(encoded)
            self.assertLessEqual(len(chunk),1024)
            uploaded.extend(chunk)
            return b'\n' + token + b'_START\n\n' + token + b'_END:0\n'
        console.transport = mock.Mock(exchange=exchange)
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'module.ko'
            local.write_bytes(content)
            checksum = console.upload(local, '/tmp/module.ko')
        self.assertEqual(checksum, hashlib.sha256(content).hexdigest())
        self.assertEqual(bytes(uploaded),content)
        self.assertEqual(len(frames),len(content)//1024)
        self.assertIn('stty -echo', commands[1])
        self.assertTrue(any('tmpfs' in c for c in commands))
        self.assertTrue(any('sha256sum' in c for c in commands))
        self.assertTrue(any(' mv ' in c for c in commands))
        self.assertTrue(commands[-1].endswith('stty 1:2:3:4'))

    def test_explicit_helper_and_restore_after_lost_echo_ack(self):
        console = serial.Console('unused')
        calls = []
        def checked(command, timeout=None):
            calls.append(command)
            if command == '/tmp/stty-tiny -g': return b'abcd1234\n'
            if command == '/tmp/stty-tiny -echo': raise TimeoutError('lost ack')
            return b''
        console.checked = checked
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'data'; local.write_bytes(b'x')
            with self.assertRaises(TimeoutError):
                console.upload(local, '/tmp/data', stty_helper='/tmp/stty-tiny')
        self.assertTrue(calls[-1].endswith('/tmp/stty-tiny abcd1234'))
        self.assertFalse(any('/bin/busybox stty' in c for c in calls))

    def test_explicit_helper_failure_does_not_fallback(self):
        console = serial.Console('unused')
        console.checked = mock.Mock(side_effect=serial.ProtocolError('missing helper'))
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'data'; local.write_bytes(b'x')
            with self.assertRaises(serial.ProtocolError):
                console.upload(local, '/tmp/data', stty_helper='/tmp/missing')
        console.checked.assert_called_once_with('/tmp/missing -g')

    def test_native_pty_bulk_is_not_usb_gadget_proof(self):
        busybox = shutil.which('busybox')
        if not busybox: self.skipTest('native BusyBox required')
        with tempfile.TemporaryDirectory() as directory:
            master, slave = pty.openpty()
            mode = termios.tcgetattr(slave)
            self.assertTrue(mode[3] & termios.ICANON)
            mode[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(slave, termios.TCSANOW, mode)
            peer = subprocess.Popen([busybox, 'sh'], stdin=slave, stdout=slave, stderr=slave)
            os.set_blocking(master, False)
            try:
                content = bytes(range(256)) * 32768  # 8 MiB, far beyond the canonical queue.
                encoded = base64.b64encode(content)
                lines = b'\n'.join(encoded[i:i+256] for i in range(0,len(encoded),256))
                target = Path(directory) / 'payload'
                command = f"sleep 0.2; {busybox} base64 -d > {target} <<'END_DATA'\n".encode()
                payload, start, done = serial.frame((command + lines + b'\nEND_DATA\n').decode())
                response = serial.Transport(master).exchange(payload, done, 20)
                self.assertEqual(serial.result(response,start,done),(b'',0))
                self.assertEqual(target.read_bytes(),content)
            finally:
                peer.kill();peer.wait(timeout=2)
                os.close(master);os.close(slave)

    def test_bounded_upload_actual_busybox_pty(self):
        busybox = shutil.which('busybox')
        if not busybox: self.skipTest('native BusyBox required')
        with tempfile.TemporaryDirectory(dir='/tmp') as directory:
            master, slave = pty.openpty()
            mode = termios.tcgetattr(slave); mode[3] &= ~(termios.ECHO|termios.ECHONL)
            termios.tcsetattr(slave,termios.TCSANOW,mode)
            peer = subprocess.Popen([busybox,'sh'],stdin=slave,stdout=slave,stderr=slave)
            os.set_blocking(master,False)
            destination = '/tmp/brick-bounded-test-' + Path(directory).name
            try:
                content = bytes(range(256))*1024
                local=Path(directory)/'source';local.write_bytes(content)
                console=serial.Console('unused');console.transport=serial.Transport(master)
                original=console.checked
                def checked(command,timeout=None):
                    if 'stty -g' in command:return b'1:2:3'
                    if 'stty -echo' in command:return b''
                    if '/proc/mounts' in command:return b'' # Test owns private local staging.
                    command=command.replace('; '+busybox+' stty 1:2:3','')
                    return original(command,timeout)
                console.checked=checked
                with mock.patch.object(serial,'BB',busybox):
                    checksum=console.upload(local,destination,timeout=20)
                self.assertEqual(Path(destination).read_bytes(),content)
                self.assertEqual(checksum,hashlib.sha256(content).hexdigest())
                # A valid but short base64 chunk must fail its bounded byte
                # check before append/rename, preserving an existing target.
                transport=console.transport
                class ShortChunk:
                    count=0
                    def exchange(self,payload,completion,timeout):
                        opening=re.search(rb"<<'(BRICK_DATA_[a-f0-9]+)'\n",payload)
                        if opening:
                            self.count+=1
                            end=payload.index(b'\n'+opening.group(1),opening.end())
                            data=base64.b64decode(payload[opening.end():end])[:-1]
                            encoded=base64.b64encode(data)
                            lines=b'\n'.join(encoded[i:i+256] for i in range(0,len(encoded),256))
                            payload=payload[:opening.end()]+lines+payload[end:]
                        return transport.exchange(payload,completion,timeout)
                shortened=ShortChunk();console.transport=shortened
                with mock.patch.object(serial,'BB',busybox):
                    with self.assertRaisesRegex(serial.ProtocolError,'acknowledgement failed at 0'):
                        console.upload(local,destination,timeout=20)
                self.assertEqual(shortened.count,1)
                self.assertEqual(Path(destination).read_bytes(),content)
                class FailedAppend:
                    count=0
                    def exchange(self,payload,completion,timeout):
                        if b'BRICK_DATA_' in payload:
                            self.count+=1
                            payload=payload.replace(b'/payload\n',b'/absent/payload\n',1)
                        return transport.exchange(payload,completion,timeout)
                failed=FailedAppend();console.transport=failed
                with mock.patch.object(serial,'BB',busybox):
                    with self.assertRaisesRegex(serial.ProtocolError,'acknowledgement failed at 0'):
                        console.upload(local,destination,timeout=20)
                self.assertEqual(failed.count,1)
                self.assertEqual(Path(destination).read_bytes(),content)
            finally:
                Path(destination).unlink(missing_ok=True)
                peer.kill();peer.wait(timeout=2);os.close(master);os.close(slave)

    def test_failed_chunk_ack_aborts_before_next_chunk_or_rename(self):
        console=serial.Console('unused');commands=[];frames=[]
        def checked(command,timeout=None):
            commands.append(command)
            return b'1:2:3' if command.endswith('stty -g') else b''
        def exchange(payload,completion,timeout):
            frames.append(payload)
            self.assertIn(b'brick_decode=$?',payload)
            self.assertIn(b'wc -c',payload)
            token=re.search(rb'BRICK_[a-f0-9]+',payload).group()
            return b'\n'+token+b'_START\n\n'+token+b'_END:1\n'
        console.checked=checked;console.transport=mock.Mock(exchange=exchange)
        with tempfile.TemporaryDirectory() as directory:
            local=Path(directory)/'source';local.write_bytes(b'x'*3000)
            with self.assertRaisesRegex(serial.ProtocolError,'acknowledgement failed at 0'):
                console.upload(local,'/tmp/failure-test')
        self.assertEqual(len(frames),1)
        self.assertFalse(any(' mv ' in c for c in commands))
        self.assertTrue(commands[-1].endswith('stty 1:2:3'))

    def test_lost_echo_ack_still_restores_mode(self):
        console = serial.Console('unused')
        calls = []
        def checked(command, timeout=None):
            calls.append(command)
            if command.endswith('stty -g'): return b'1:2:3\n'
            if command.endswith('stty -echo'): raise TimeoutError('lost ack')
            return b''
        console.checked = checked
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'module.ko'; local.write_bytes(b'x')
            with self.assertRaises(TimeoutError): console.upload(local, '/tmp/module.ko')
        self.assertTrue(calls[-1].endswith('stty 1:2:3'))

    def test_missing_stty_uses_acknowledged_chunks_without_terminal_changes(self):
        console = serial.Console('unused')
        commands = []
        uploaded = bytearray()
        content = bytes(range(256)) * 2500 + b'last'
        def checked(command, timeout=None):
            commands.append(command)
            if command.endswith('stty -g'):
                raise serial.ProtocolError('stty: applet not found')
            wire, _, _ = serial.frame(serial.BB + ' sh -c ' + serial.shlex.quote(command))
            self.assertLess(max(map(len, wire.splitlines())), 512)
            if command.startswith("printf '%s'"):
                encoded = re.search(r"printf '%s' '([^']*)'", command).group(1)
                chunk = base64.b64decode(encoded)
                self.assertLessEqual(len(chunk), 128)
                uploaded.extend(chunk)
            if 'sha256sum' in command:
                self.assertEqual(bytes(uploaded), content)
                return hashlib.sha256(uploaded).hexdigest().encode()
            if ' wc -c ' in command: return str(len(uploaded)).encode()
            return b''
        console.checked = checked
        console.transport = mock.Mock()
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'probe'; local.write_bytes(content)
            checksum = console.upload(local, '/tmp/probe')
        self.assertEqual(checksum, hashlib.sha256(content).hexdigest())
        self.assertEqual([c for c in commands if 'stty' in c], [serial.BB + ' stty -g'])
        self.assertIn('rm -rf /tmp/.brick-upload-', commands[-1])
        console.transport.exchange.assert_not_called()

    def test_partial_transfer_timeout_sends_delimiter_and_restore(self):
        console = serial.Console('unused')
        console.checked = lambda command, timeout=None: b'1:2:3\n' if command.endswith('stty -g') else b''
        transmissions = []
        def exchange(payload, completion, timeout):
            transmissions.append(payload)
            if len(transmissions) == 1:
                raise TimeoutError('partial transfer')
            original_delimiter = re.search(rb"<<'(BRICK_DATA_[a-f0-9]+)'", transmissions[0]).group(1)
            self.assertTrue(payload.startswith(b'\n' + original_delimiter + b'\n);\n'))
            self.assertIn(b'stty 1:2:3', payload)
            token = re.search(rb'BRICK_[a-f0-9]+', payload).group()
            return b'\n' + token + b'_START\n\n' + token + b'_END:0\n'
        console.transport = mock.Mock(exchange=exchange)
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / 'module.ko'; local.write_bytes(b'x')
            with self.assertRaises(TimeoutError): console.upload(local, '/tmp/module.ko')
        self.assertEqual(len(transmissions), 2)

    def test_checksum_failure_reports_actual_size_and_hash(self):
        console = serial.Console('unused')
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            local = directory / 'local'; local.write_bytes(b'expected')
            (directory / 'payload').write_bytes(b'actual')
            def checked(command, timeout=None):
                if command.endswith('stty -g'): return b'1:2:3\n'
                if 'sha256sum' in command or ' wc -c ' in command:
                    command = re.sub(r'/tmp/\.brick-upload-[a-f0-9]+', str(directory), command)
                    command = command.replace(serial.BB + ' ', '')
                    completed = subprocess.run(['sh', '-c', command], capture_output=True)
                    self.assertEqual(completed.returncode, 0)
                    return completed.stdout
                return b''
            def exchange(payload, completion, timeout):
                token = re.search(rb'BRICK_[a-f0-9]+', payload).group()
                return b'\n' + token + b'_START\n\n' + token + b'_END:0\n'
            console.checked = checked
            console.transport = mock.Mock(exchange=exchange)
            with self.assertRaises(serial.ProtocolError) as failure:
                console.upload(local, '/tmp/unused')
            self.assertIn('expected_bytes=8 actual_bytes=6', str(failure.exception))
            self.assertIn('actual_sha256=' + hashlib.sha256(b'actual').hexdigest(), str(failure.exception))


if __name__ == '__main__':
    unittest.main()
