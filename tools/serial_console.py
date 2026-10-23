#!/usr/bin/env python3
"""Run commands or upload a file to the Brick's RAM over its USB console."""
import argparse
import base64
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import select
import shlex
import sys
import termios
import time
import uuid

DEFAULT_PORT = '/dev/serial/by-id/usb-Brick_development_TrimUI_Brick_Linux_console_brick-dev-if00'
BB = '/bin/busybox'


class ProtocolError(RuntimeError):
    pass


class Transport:
    def __init__(self, fd):
        self.fd = fd

    def exchange(self, payload, completion, timeout):
        """Pump reads while writing, including partial writes/backpressure."""
        pending = memoryview(payload)
        received = bytearray()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Serial completion marker not received before deadline')
            ready, writable, _ = select.select([self.fd], [self.fd] if pending else [], [], remaining)
            if ready:
                try:
                    chunk = os.read(self.fd, 65536)
                except BlockingIOError:
                    chunk = None
                if chunk == b'':
                    raise ProtocolError('USB console disconnected')
                if chunk:
                    received.extend(chunk)
                    if len(received) > 32 * 1024 * 1024:
                        raise ProtocolError('Console output exceeded 32 MiB')
                    if completion.search(received) and not pending:
                        return bytes(received)
            if writable:
                try:
                    count = os.write(self.fd, pending[:65536])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise ProtocolError('Serial write made no progress')
                pending = pending[count:]
            if not pending and completion.search(received):
                return bytes(received)

    def exchange_after_marker(self, prefix, payload, ready, completion, timeout):
        """Send a command, wait until its reader is live, then stream raw bytes."""
        pending = memoryview(prefix)
        received = bytearray()
        payload_started = False
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Raw serial transfer did not complete before deadline')
            readable, writable, _ = select.select([self.fd], [self.fd] if pending else [], [], remaining)
            if readable:
                try:
                    chunk = os.read(self.fd, 65536)
                except BlockingIOError:
                    chunk = None
                if chunk == b'':
                    raise ProtocolError('USB console disconnected')
                if chunk:
                    received.extend(chunk)
                    if len(received) > 1024 * 1024:
                        raise ProtocolError('Raw transfer console output exceeded 1 MiB')
                    if not payload_started and ready.search(received):
                        payload_started = True
                        pending = memoryview(payload)
                    if payload_started and not pending and completion.search(received):
                        return bytes(received)
            if writable:
                try:
                    count = os.write(self.fd, pending[:65536])
                except BlockingIOError:
                    continue
                if count <= 0:
                    raise ProtocolError('Serial write made no progress')
                pending = pending[count:]


def frame(command):
    token = 'BRICK_' + uuid.uuid4().hex
    start, end = token + '_START', token + '_END'
    payload = (f"printf '\\n{start}\\n'; (\n{command}\n); "
               f"brick_rc=$?; printf '\\n{end}:%s\\n' \"$brick_rc\"\n").encode()
    completion = re.compile(rb'(?:^|\n)' + end.encode() + rb':([0-9]+)\r?\n')
    return payload, start.encode(), completion


def result(data, start, completion):
    data = data.replace(b'\r\n', b'\n')
    first = re.search(rb'(?:^|\n)' + start + rb'\r?\n', data)
    last = completion.search(data)
    if not first or not last or first.end() > last.start():
        raise ProtocolError('Missing or out-of-order command markers')
    return data[first.end():last.start()], int(last.group(1))


def remote_destination(path):
    candidate = PurePosixPath(path)
    # Direct child only: cannot traverse a directory symlink to an SD mount.
    if candidate.parent != PurePosixPath('/tmp') or candidate.name in ('', '.', '..') or str(candidate) != path:
        raise ValueError('Upload destination must be a single file directly under /tmp')
    return path


class Console:
    def __init__(self, port, timeout=30):
        self.port, self.timeout = port, timeout
        self.fd = None
        self.original = None

    def __enter__(self):
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self.original = termios.tcgetattr(self.fd)
            mode = termios.tcgetattr(self.fd)
            mode[0] = mode[1] = mode[3] = 0
            mode[2] |= termios.CLOCAL | termios.CREAD
            mode[6][termios.VMIN] = mode[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, mode)
            self.transport = Transport(self.fd)
            return self
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def __exit__(self, *_):
        try:
            if self.original is not None:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.original)
        finally:
            os.close(self.fd)

    def execute(self, command, timeout=None):
        # Shell quoting protects embedded quotes/newlines in the command.
        quoted = f'{BB} sh -c {shlex.quote(command)}'
        if any(len(line.encode()) >= 512 for line in quoted.splitlines()):
            raise ValueError('Command line too long for terminal; upload a script instead')
        payload, start, completion = frame(quoted)
        return result(self.transport.exchange(payload, completion, timeout or self.timeout), start, completion)

    def checked(self, command, timeout=None):
        output, code = self.execute(command, timeout)
        if code:
            raise ProtocolError(f'Remote exit {code}: {output.decode(errors="replace").strip()}')
        return output

    def upload(self, local, destination, mode='0600', timeout=180, stty_helper=None, raw=False):
        if raw:
            return upload_raw(self, local, destination, mode, timeout)
        destination = remote_destination(destination)
        if not re.fullmatch(r'0?[0-7]{3}', mode):
            raise ValueError('Mode must be three octal permission digits, optionally prefixed by zero')
        content = Path(local).read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        work = '/tmp/.brick-upload-' + uuid.uuid4().hex
        delimiter = 'BRICK_DATA_' + uuid.uuid4().hex
        stty = shlex.quote(remote_destination(stty_helper)) if stty_helper else f'{BB} stty'
        try:
            old_mode = self.checked(f'{stty} -g').decode().strip()
        except ProtocolError:
            if stty_helper:
                raise  # Explicit helper failure must not silently change protocol.
            old_mode = None
            print('Remote stty unavailable; using acknowledged 128-byte base64 chunks.', file=sys.stderr)
        if old_mode is not None and not re.fullmatch(r'[0-9a-fA-F:]+', old_mode):
            raise ProtocolError('Unrecognized remote terminal mode; upload not started')
        echo_disabled = False
        cleanup_needed = False
        transfer_started = False
        transfer_finished = False
        raw_transfer = False
        try:
            # Restore even if the mode changed but its acknowledgement was lost.
            if old_mode is not None:
                echo_disabled = True
                self.checked(f'{stty} -echo')
            cleanup_needed = True
            # Only /tmp as a tmpfs mount is accepted. No persistent destination.
            self.checked(f"{BB} awk '$2 == \"/tmp\" && $3 == \"tmpfs\" {{ found=1 }} END {{ exit !found }}' /proc/mounts")
            self.checked(f'[ ! -d {shlex.quote(destination)} ] && {BB} mkdir -m 700 {work} && : > {work}/payload')
            if old_mode is None:
                deadline = time.monotonic() + timeout
                for offset in range(0, len(content), 128):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'Upload deadline exceeded after {offset}/{len(content)} acknowledged bytes')
                    encoded = base64.b64encode(content[offset:offset+128]).decode('ascii')
                    # Each complete command is below 512 bytes and waits for its
                    # marker before sending more; no overflowing terminal input.
                    try:
                        self.checked(f"printf '%s' '{encoded}' | {BB} base64 -d >> {work}/payload",
                                     timeout=min(self.timeout, remaining))
                    except (ProtocolError, TimeoutError) as error:
                        raise ProtocolError(f'Upload failed at offset {offset}/{len(content)}; '
                                            f'chunk may be partial: {error}') from error
            else:
                # USB gadget canonical queues dropped data with unbounded
                # heredocs, despite native PTY success. Bound the entire frame
                # below 2 KiB and wait for decode/size/append acknowledgement.
                # Count only a bounded temporary chunk: repeatedly scanning the
                # growing payload is quadratic on stock BusyBox wc.
                deadline = time.monotonic() + timeout
                next_report = 1024 * 1024
                for offset in range(0, len(content), 1024):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'Upload deadline exceeded after {offset}/{len(content)} acknowledged bytes')
                    chunk = content[offset:offset + 1024]
                    encoded = base64.b64encode(chunk)
                    lines = b'\n'.join(encoded[i:i + 256] for i in range(0, len(encoded), 256))
                    command = f"{BB} base64 -d > {work}/chunk <<'{delimiter}'\n".encode() + lines + f'\n{delimiter}\n'.encode()
                    command += f'brick_decode=$?; [ "$brick_decode" = 0 ] && [ "$({BB} wc -c < {work}/chunk)" = "{len(chunk)}" ] && {BB} cat {work}/chunk >> {work}/payload\n'.encode()
                    payload, start, completion = frame(command.decode('ascii'))
                    if len(payload) > 2048:
                        raise ProtocolError('Bounded upload frame exceeds 2048 bytes')
                    transfer_started = True
                    transfer_finished = False
                    data = self.transport.exchange(payload, completion, min(self.timeout, remaining))
                    output, code = result(data, start, completion)
                    transfer_finished = True
                    if code:
                        raise ProtocolError(f'Chunk decode/size/append acknowledgement failed at {offset}: {output.decode(errors="replace")}')
                    if offset + len(chunk) >= next_report:
                        print(f'UPLOAD_PROGRESS {offset + len(chunk)}/{len(content)} acknowledged bytes', file=sys.stderr, flush=True)
                        next_report += 1024 * 1024
            actual_hash = self.checked(f'{BB} sha256sum {work}/payload').decode().split()
            actual_bytes = self.checked(f'{BB} wc -c < {work}/payload').decode().strip()
            actual_hash = actual_hash[0] if actual_hash else '<missing>'
            if actual_hash != checksum or actual_bytes != str(len(content)):
                raise ProtocolError(f'UPLOAD_CHECK expected_bytes={len(content)} actual_bytes={actual_bytes} '
                                    f'expected_sha256={checksum} actual_sha256={actual_hash}')
            self.checked(f'{BB} chmod {mode} {work}/payload')
            self.checked(f'[ ! -d {shlex.quote(destination)} ] && {BB} mv {work}/payload {shlex.quote(destination)}')
            return checksum
        finally:
            # A timeout may leave the shell reading the heredoc. Terminate it
            # before requesting cleanup; partial data never reaches destination.
            if cleanup_needed or echo_disabled:
                try:
                    cleanup = f'{BB} rm -rf {work}'
                    if echo_disabled:
                        cleanup += f'; {stty} {shlex.quote(old_mode)}'
                    if transfer_started and not transfer_finished and not raw_transfer:
                        payload, start, completion = frame(f'{BB} sh -c {shlex.quote(cleanup)}')
                        payload = f'\n{delimiter}\n);\n'.encode() + payload
                        data = self.transport.exchange(payload, completion, 10)
                        _, code = result(data, start, completion)
                        if code:
                            raise ProtocolError(f'Remote cleanup returned {code}')
                    else:
                        self.checked(cleanup, timeout=10)
                except (OSError, TimeoutError, ProtocolError) as error:
                    print(f'Could not restore remote terminal/clean RAM staging: {error}', file=sys.stderr)


def transfer_raw(console, content, receiver, temporary, timeout):
    command = f'{shlex.quote(receiver)} {shlex.quote(temporary)} {len(content)} {int(timeout)}\n'.encode()
    def marker(name, count):
        return re.compile(rb'(?:^|\n)' + name.encode() + b' ' + f'{count:08x}'.encode() + rb'\r?\n')
    console.transport.exchange(command, marker('BRX_READY', len(content)), 10)
    deadline = time.monotonic() + timeout
    for offset in range(0, len(content), 4096):
        chunk = content[offset:offset + 4096]
        count = offset + len(chunk)
        expected = 'BRX_DONE' if count == len(content) else 'BRX_ACK'
        completion = re.compile(marker(expected, count).pattern + rb'|(?:^|\n)BRX_FAIL [0-9a-f]+\r?\n')
        response = console.transport.exchange(chunk, completion, max(0.01, deadline - time.monotonic() + 2))
        if b'BRX_FAIL ' in response:
            raise ProtocolError('Target receiver failed; partial upload removed')
        # Bound bursts into the gadget TTY input queue.
        time.sleep(0.001)


def upload_raw(console, local, destination, mode='0600', timeout=180):
    """Transfer bounded blocks to the foreground receiver, then verify content."""
    destination = remote_destination(destination)
    if not re.fullmatch(r'0?[0-7]{3}', mode):
        raise ValueError('Invalid file mode')
    if not 1 <= timeout <= 600:
        raise ValueError('Timeout must be between 1 and 600 seconds')
    content = Path(local).read_bytes()
    if not 1 <= len(content) <= 64 * 1024 * 1024:
        raise ValueError('Raw upload size must be 1 byte to 64 MiB')
    temporary = '/tmp/brx-' + uuid.uuid4().hex
    receiver = '/usr/bin/brick-receive'
    console.checked(f'test -x {receiver} && test ! -e {shlex.quote(destination)} && test ! -L {shlex.quote(destination)}')
    transfer_raw(console, content, receiver, temporary, timeout)
    digest = hashlib.sha256(content).hexdigest()
    output = console.checked(f'{BB} wc -c < {temporary}; {BB} sha256sum {temporary}').decode().split()
    if len(output) < 2 or output[0] != str(len(content)) or output[1] != digest:
        console.checked(f'{BB} rm -f {temporary}')
        raise ProtocolError('Raw upload size or SHA256 mismatch')
    console.checked(f'{BB} chmod {mode} {temporary}; {BB} ln -T {temporary} {shlex.quote(destination)} && {BB} rm {temporary}')
    return digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=DEFAULT_PORT)
    parser.add_argument('--timeout', type=float, default=30)
    sub = parser.add_subparsers(dest='action', required=True)
    execute = sub.add_parser('exec')
    execute.add_argument('command')
    upload = sub.add_parser('upload')
    upload.add_argument('local', type=Path)
    upload.add_argument('destination')
    upload.add_argument('--raw', action='store_true', help='Use bounded binary transfer (requires target stty)')
    upload.add_argument('--mode', default='0600')
    upload.add_argument('--transfer-timeout', type=float, default=180)
    args = parser.parse_args(argv)
    with Console(args.port, args.timeout) as console:
        if args.action == 'exec':
            output, code = console.execute(args.command)
            sys.stdout.buffer.write(output)
            return code
        checksum = console.upload(args.local, args.destination, args.mode, args.transfer_timeout, raw=args.raw)
        print(f'UPLOAD_VERIFIED {checksum} {args.destination}')
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, TimeoutError, ProtocolError) as error:
        print(f'Serial operation failed: {error}', file=sys.stderr)
        sys.exit(1)
