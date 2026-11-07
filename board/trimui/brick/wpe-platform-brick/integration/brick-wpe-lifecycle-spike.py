#!/usr/bin/env python3
"""Measure repeated real brick-captive-wpe process lifecycles on the Brick.

The display must already be handed to this process by the qualified broker or
watchdog.  This tool never selects another browser or display implementation.
"""
import argparse
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import time


def descendants(root):
    parents = {}
    for item in Path('/proc').iterdir():
        if not item.name.isdigit():
            continue
        try:
            fields = (item / 'stat').read_text().split()
            parents[int(item.name)] = int(fields[3])
        except (OSError, ValueError, IndexError):
            pass
    result = {root}
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in result and pid not in result:
                result.add(pid)
                changed = True
    return result


def sample_tree(root):
    rss_kib = 0
    fds = 0
    processes = 0
    for pid in descendants(root):
        try:
            status = (Path('/proc') / str(pid) / 'status').read_text()
            rss_line = next((line for line in status.splitlines()
                             if line.startswith('VmRSS:')), 'VmRSS: 0 kB')
            rss_kib += int(rss_line.split()[1])
            fds += len(list((Path('/proc') / str(pid) / 'fd').iterdir()))
            processes += 1
        except (OSError, ValueError):
            pass
    return rss_kib, fds, processes


def one_session(args, index):
    authority, child_authority = socket.socketpair()
    authority.setblocking(False)
    command = [args.browser, '--uri', args.uri, '--network-key',
               args.network_key, '--profile-root', args.profile_root,
               '--authority-fd', str(child_authority.fileno())]
    started = time.monotonic()
    process = subprocess.Popen(command, pass_fds=(child_authority.fileno(),),
                               start_new_session=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
    child_authority.close()
    selector = selectors.DefaultSelector()
    selector.register(authority, selectors.EVENT_READ)
    received = b''
    ready_ms = None
    loaded_ms = None
    frame_ms = None
    peak_rss_kib = peak_fds = peak_processes = 0
    timed_out = False
    killed = False
    while process.poll() is None:
        now = time.monotonic()
        rss, fds, count = sample_tree(process.pid)
        peak_rss_kib = max(peak_rss_kib, rss)
        peak_fds = max(peak_fds, fds)
        peak_processes = max(peak_processes, count)
        for key, _ in selector.select(0.02):
            try:
                received += key.fileobj.recv(65536)
            except BlockingIOError:
                pass
        lines = received.split(b'\n')
        received = lines.pop()
        for raw in lines:
            line = raw.decode('utf-8', 'replace')
            if line == 'READY' and ready_ms is None:
                ready_ms = round((now - started) * 1000, 3)
            if line.startswith('LOADED ') and loaded_ms is None:
                loaded_ms = round((now - started) * 1000, 3)
                if args.kill_after_load and not args.wait_for_frame:
                    os.killpg(process.pid, signal.SIGKILL)
                    killed = True
                elif not args.wait_for_frame:
                    authority.sendall(b'SUCCESS\n')
            if line == 'FRAME' and frame_ms is None:
                frame_ms = round((now - started) * 1000, 3)
                if args.kill_after_load:
                    os.killpg(process.pid, signal.SIGKILL)
                    killed = True
                else:
                    authority.sendall(b'SUCCESS\n')
        if now - started >= args.timeout:
            os.killpg(process.pid, signal.SIGKILL)
            timed_out = True
            break
    try:
        status = process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        status = process.wait(timeout=5)
    stderr = process.stderr.read()
    process.stderr.close()
    authority.close()
    selector.close()
    return {
        'session': index,
        'status': status,
        'ready_ms': ready_ms,
        'loaded_ms': loaded_ms,
        'first_frame_ms': frame_ms,
        'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
        'peak_rss_kib': peak_rss_kib,
        'peak_fds': peak_fds,
        'peak_processes': peak_processes,
        'timed_out': timed_out,
        'injected_kill': killed,
        'stderr_tail': stderr[-1000:],
    }


def percentile(values, percentile):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1,
                      int((len(values) - 1) * percentile))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', default='/usr/bin/brick-captive-wpe')
    parser.add_argument('--uri', required=True)
    parser.add_argument('--network-key', default='0' * 64)
    parser.add_argument('--profile-root', default='/tmp/brick-wpe-lifecycle')
    parser.add_argument('--sessions', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=15)
    parser.add_argument('--kill-after-load', action='store_true')
    parser.add_argument('--wait-for-frame', action='store_true')
    args = parser.parse_args()
    if args.sessions < 1 or args.sessions > 1000 or args.timeout <= 0:
        parser.error('sessions must be 1..1000 and timeout must be positive')
    results = [one_session(args, index + 1) for index in range(args.sessions)]
    loads = [item['loaded_ms'] for item in results
             if item['loaded_ms'] is not None]
    frames = [item['first_frame_ms'] for item in results
              if item['first_frame_ms'] is not None]
    output = {
        'implementation': 'WPE WebKit DRM / Mesa softpipe GBM',
        'sessions': len(results),
        'loaded_sessions': len(loads),
        'p95_loaded_ms': percentile(loads, 0.95),
        'p95_first_frame_ms': percentile(frames, 0.95),
        'max_peak_rss_kib': max(item['peak_rss_kib'] for item in results),
        'max_peak_fds': max(item['peak_fds'] for item in results),
        'timeouts': sum(item['timed_out'] for item in results),
        'results': results,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    expected = -signal.SIGKILL if args.kill_after_load else 0
    return 0 if all(item['status'] == expected for item in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
