#!/usr/bin/env python3
"""Run and validate the isolated Brick H.264/JPEG encoder qualification."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from serial_console import Console, DEFAULT_PORT, ProtocolError


ENCODED_SHA256 = 'fbb1290e520b94a56af5b626f64000ec030124288e06d75661b8e999bc80f872'
DECODED_SHA256 = 'e4b027c7c450592bb2c3ba9083159ff154d21f830d4b3656d0a206dac5c9385b'
SOURCE_BASELINE_SHA256 = '9063c4925bdebf1a6bfd310a2b0657cc40d029e506bb9b25fe1846b26059ce8f'
FRAME_BYTES = 320 * 240 * 3 // 2
BASELINE_FRAMES = 6
EXTENDED_FRAMES = 36
H264_BUFFERS = 8
FORCE_FRAME = 5
GOP_SIZE = 30
META = re.compile(
    r'^frame=(\d+) index=(\d+) sequence=(\d+) bytes=(\d+) '
    r'flags=(0x[0-9a-f]+) key=([01]) p=([01]) canary=1 '
    r'nals=([0-9,]+)$')
PREPARE = re.compile(
    r'encoder qualification prepare codec=h264 generation=(\d+) '
    r'frame=(\d+) poc=(\d+) idr=([01]) ref_slot=(\d+) recon_slot=(\d+) '
    r'ref_luma=(0x[0-9a-f]+) ref_chroma=(0x[0-9a-f]+) '
    r'recon_luma=(0x[0-9a-f]+) recon_chroma=(0x[0-9a-f]+) '
    r'force_requested=(\d+) force_completed=(\d+) force_active=(\d+)')
COMPLETION = re.compile(
    r'encoder qualification completion codec=h264 generation=(\d+) '
    r'key=([01]) promoted_reference=(\d+) next_frame=(\d+) '
    r'frames_since_idr=(\d+) payload=(\d+) force_requested=(\d+) '
    r'force_completed=(\d+) force_active=(\d+)')
FORCE = re.compile(
    r'encoder qualification force requested=(\d+) completed=(\d+)')
ABORT = re.compile(
    r'encoder qualification abort codec=h264 reason=(\d+) '
    r'force_requested=(\d+) force_completed=(\d+) force_active=(\d+)')
ABORT_META = re.compile(
    r'^phase=(startup|retry) bytes=(\d+) key=1 canary=1 '
    r'nals=(7,8,5)$')
RESET_BEGIN = 'encoder qualification streamoff reset begin'
RESET_COMPLETE = 'encoder qualification streamoff reset complete'
V4L2_BUF_FLAG_ERROR = 0x0040
JPEG_SCAN_SHA256 = 'e3ef44fe90066acf1de51bf4fd487544e66ebfbd57d8c7726c72115e4a3c7172'
JPEG_DECODED_SHA256 = '98a229c16ddcd5cec5042fb63d292ff1ae2d584175d8e06b96c5fb42a14ff008'
JPEG_META = re.compile(
    r'^frame=(\d+) index=(\d+) sequence=(\d+) bytes=(\d+) '
    r'flags=(0x[0-9a-f]+) key=1 eoi=1 canary=1 memory=(mmap|dmabuf)$')
JPEG_PREPARE = re.compile(
    r'encoder qualification prepare codec=jpeg generation=(\d+) quality=95')
JPEG_COMPLETE = re.compile(
    r'encoder qualification completion codec=jpeg generation=(\d+) payload=(\d+)')


def sha256(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def safe_extract(payload, destination):
    """Extract device-created evidence without links or path traversal."""
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        members = archive.getmembers()
        if not members:
            raise ValueError('Device evidence archive is empty')
        for member in members:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or member.issym() or
                    member.islnk() or not (member.isdir() or member.isfile())):
                raise ValueError(f'Unsafe evidence archive member: {member.name}')
        archive.extractall(destination, filter='data')


def verify_checksums(directory):
    entries = (directory / 'SHA256SUMS').read_text().splitlines()
    if not entries:
        raise ValueError('Missing evidence checksums')
    names = set()
    for entry in entries:
        match = re.fullmatch(r'([0-9a-f]{64})  (\./)?([^/]+)', entry)
        if not match:
            raise ValueError(f'Invalid evidence checksum line: {entry}')
        expected, name = match.group(1), match.group(3)
        if name == 'SHA256SUMS' or name in names:
            raise ValueError(f'Invalid evidence checksum target: {name}')
        names.add(name)
        path = directory / name
        if not path.is_file() or sha256(path) != expected:
            raise ValueError(f'Evidence checksum mismatch: {name}')
    actual = {path.name for path in directory.iterdir()
              if path.is_file() and path.name != 'SHA256SUMS'}
    if names != actual:
        raise ValueError('Evidence checksums do not cover every regular file')


def expected_h264_states(frames, forced):
    states = []
    reference = 0
    next_frame = 0
    frames_since_idr = 0
    started = False
    requested = 0
    completed = 0
    for frame in range(frames):
        if forced and frame == FORCE_FRAME:
            requested = 1
        force_active = requested != completed
        key = not started or force_active or frames_since_idr >= GOP_SIZE
        reconstruction = 0 if not started else reference ^ 1
        states.append({
            'frame': 0 if key else next_frame,
            'poc': 0 if key else next_frame * 2,
            'key': int(key),
            'reference': reference if started else 0,
            'reconstruction': reconstruction,
            'requested': requested,
            'completed_prepare': completed,
            'active': requested if force_active else 0,
        })
        reference = reconstruction
        if key:
            next_frame = 1
            frames_since_idr = 1
            if force_active:
                completed = requested
        else:
            next_frame = (next_frame + 1) & 0xff
            frames_since_idr += 1
        started = True
        states[-1].update({
            'promoted': reference,
            'next_frame': next_frame,
            'frames_since_idr': frames_since_idr,
            'completed_terminal': completed,
        })
    return states


def verify_metadata(path, frames, forced):
    rows = []
    states = expected_h264_states(frames, forced)
    for line in path.read_text().splitlines():
        match = META.fullmatch(line)
        if not match:
            raise ValueError(f'Invalid metadata in {path.name}: {line}')
        frame, index, sequence, length = map(int, match.groups()[:4])
        flags = int(match.group(5), 16)
        key, pframe = map(int, match.groups()[5:7])
        nals = match.group(8)
        rows.append((frame, index, sequence, length, flags, key, pframe, nals))
    if len(rows) != frames:
        raise ValueError(f'{path.name} does not describe {frames} frames')
    if ([row[0] for row in rows] != list(range(frames)) or
            [row[2] for row in rows] != list(range(frames))):
        raise ValueError(f'{path.name} has an unexpected frame sequence')
    if [row[1] for row in rows] != [frame % min(frames, H264_BUFFERS)
                                    for frame in range(frames)]:
        raise ValueError(f'{path.name} has unexpected capture-buffer rotation')
    if any(row[3] <= 0 or row[3] > 65536 for row in rows):
        raise ValueError(f'{path.name} has an invalid payload size')
    if any(row[4] & V4L2_BUF_FLAG_ERROR for row in rows):
        raise ValueError(f'{path.name} contains an error completion')
    expected_types = [(state['key'], 0 if state['key'] else 1,
                       '7,8,5' if frame == 0 else
                       '5' if state['key'] else '1')
                      for frame, state in enumerate(states)]
    if [(row[5], row[6], row[7]) for row in rows] != expected_types:
        raise ValueError(f'{path.name} has incorrect flags or NAL types')
    return rows


def prepare_entries(text):
    entries = []
    for match in PREPARE.finditer(text):
        values = [int(value, 16) if value.startswith('0x') else int(value)
                  for value in match.groups()]
        entries.append((match.start(), *values))
    return entries


def completion_entries(text):
    return [(match.start(), *(int(value) for value in match.groups()))
            for match in COMPLETION.finditer(text)]


def verify_h264_segment(text, frames, forced, label):
    prepares = prepare_entries(text)
    completions = completion_entries(text)
    states = expected_h264_states(frames, forced)
    if len(prepares) != frames or len(completions) != frames:
        raise ValueError(f'{label} lacks {frames} prepare/completion pairs')
    generations = [entry[1] for entry in prepares]
    if generations != list(range(generations[0], generations[0] + frames)):
        raise ValueError(f'{label} generation sequence is not contiguous')
    if [entry[1] for entry in completions] != generations:
        raise ValueError(f'{label} terminal generations do not match prepares')
    slots = {}
    for frame, (prepare, completion, state) in enumerate(
            zip(prepares, completions, states, strict=True)):
        (_, _, frame_num, poc, key, ref_slot, recon_slot,
         ref_luma, ref_chroma, recon_luma, recon_chroma,
         requested, completed, active) = prepare
        (_, _, terminal_key, promoted, next_frame, frames_since_idr,
         payload, terminal_requested, terminal_completed,
         terminal_active) = completion
        if (frame_num, poc, key, ref_slot, recon_slot, requested,
                completed, active) != (
                    state['frame'], state['poc'], state['key'],
                    state['reference'], state['reconstruction'],
                    state['requested'], state['completed_prepare'],
                    state['active']):
            raise ValueError(f'{label} frame {frame} state is incorrect')
        if (terminal_key, promoted, next_frame, frames_since_idr,
                terminal_requested, terminal_completed, terminal_active) != (
                    state['key'], state['promoted'], state['next_frame'],
                    state['frames_since_idr'], state['requested'],
                    state['completed_terminal'], state['active']):
            raise ValueError(f'{label} frame {frame} terminal state is incorrect')
        if payload <= 0 or payload > 65536:
            raise ValueError(f'{label} frame {frame} has invalid payload')
        if recon_slot in slots and slots[recon_slot] != (recon_luma, recon_chroma):
            raise ValueError(f'{label} reconstruction registers changed for a slot')
        slots[recon_slot] = (recon_luma, recon_chroma)
        if ref_slot in slots and slots[ref_slot] != (ref_luma, ref_chroma):
            raise ValueError(f'{label} reference registers do not match the slot')
        if not all((ref_luma, ref_chroma, recon_luma, recon_chroma)):
            raise ValueError(f'{label} contains a zero reference register')
    if len(slots) != 2 or slots[0] == slots[1]:
        raise ValueError(f'{label} does not prove two reference slots')
    forces = [(int(match.group(1)), int(match.group(2)))
              for match in FORCE.finditer(text)]
    if forces != ([(1, 0)] if forced else []):
        raise ValueError(f'{label} has incorrect force-request accounting')
    return [entry[6] for entry in completions]


def verify_abort_retry(segments):
    abort_index = next((index for index, segment in enumerate(segments)
                        if ABORT.search(segment)), None)
    if abort_index is None:
        raise ValueError('Missing active H.264 abort evidence')
    abort_segment = segments[abort_index]
    prepares = prepare_entries(abort_segment)
    completions = completion_entries(abort_segment)
    aborts = [tuple(map(int, match.groups()))
              for match in ABORT.finditer(abort_segment)]
    if (len(prepares) != 2 or
            [row[2] for row in prepares] != [0, 0] or
            [row[4] for row in prepares] != [1, 1]):
        raise ValueError('Abort segment lacks startup and forced IDR prepares')
    if (prepares[1][11], prepares[1][12], prepares[1][13]) != (1, 0, 1):
        raise ValueError('Aborted IDR lacks a pending force serial')
    if len(completions) != 1 or completions[0][2] != 1:
        raise ValueError('Forced job completed instead of being aborted')
    if len(aborts) != 1 or aborts[0][1:] != (1, 0, 1):
        raise ValueError('Abort did not preserve the pending force request')
    retry = next((segment for segment in segments[abort_index + 1:]
                  if prepare_entries(segment)), None)
    if retry is None:
        raise ValueError('Missing post-reset H.264 force retry')
    prepares = prepare_entries(retry)
    completions = completion_entries(retry)
    if len(prepares) != 1 or len(completions) != 1:
        raise ValueError('Force retry is not a single terminal job')
    prepare = prepares[0]
    completion = completions[0]
    if (prepare[2], prepare[3], prepare[4], prepare[11], prepare[12],
            prepare[13]) != (0, 0, 1, 1, 0, 1):
        raise ValueError('Force retry did not restart as the pending IDR')
    if (completion[2], completion[7], completion[8], completion[9]) != \
            (1, 1, 1, 1):
        raise ValueError('Force retry did not consume the request on success')


def verify_dmesg(text):
    lowered = text.lower()
    for forbidden in ('iommu fault', 'frame processing timed out', 'oops',
                      'panic', 'reset failed'):
        if forbidden in lowered:
            raise ValueError(f'Kernel evidence contains {forbidden}')
    if text.count(RESET_BEGIN) < 6 or text.count(RESET_COMPLETE) < 6:
        raise ValueError('Missing ordered streamoff reset evidence')
    segments = text.split(RESET_COMPLETE)
    h264_segments = [segment for segment in segments
                     if prepare_entries(segment)]
    baseline = [segment for segment in h264_segments
                if len(prepare_entries(segment)) == BASELINE_FRAMES]
    extended = [segment for segment in h264_segments
                if len(prepare_entries(segment)) == EXTENDED_FRAMES]
    if len(baseline) != 1 or len(extended) != 2:
        raise ValueError('Missing baseline or restarted extended H.264 sessions')
    verify_h264_segment(baseline[0], BASELINE_FRAMES, False,
                        'H.264 exact baseline')
    payloads = [verify_h264_segment(segment, EXTENDED_FRAMES, True,
                                    f'H.264 extended session {index}')
                for index, segment in enumerate(extended)]
    if payloads[0] != payloads[1]:
        raise ValueError('Restarted H.264 payload sizes differ')
    verify_abort_retry(segments)
    jpeg_prepare = [int(match.group(1)) for match in JPEG_PREPARE.finditer(text)]
    jpeg_complete = [(int(match.group(1)), int(match.group(2)))
                     for match in JPEG_COMPLETE.finditer(text)]
    if len(jpeg_prepare) != 18 or len(jpeg_complete) != 18:
        raise ValueError('Missing 18 JPEG prepare/completion pairs')
    if jpeg_prepare != [row[0] for row in jpeg_complete]:
        raise ValueError('JPEG terminal generations do not match prepares')
    if any(length <= 2 or length > 4 * 1024 * 1024
           for _, length in jpeg_complete):
        raise ValueError('JPEG terminal payload is outside the bounded buffer')


def verify_session(directory, session, frames, forced):
    rows = verify_metadata(directory / f'{session}.meta', frames, forced)
    payloads = [directory / f'{session}-frame{frame}.h264'
                for frame in range(frames)]
    for path, row in zip(payloads, rows, strict=True):
        payload = path.read_bytes()
        if len(payload) != row[3]:
            raise ValueError(f'{session} frame payload does not match metadata')
        nals = ','.join(map(str, h264_nal_types(payload)))
        if nals != row[7]:
            raise ValueError(f'{session} frame payload has wrong NAL types')
    concatenated = b''.join(path.read_bytes() for path in payloads)
    if concatenated != (directory / f'{session}.h264').read_bytes():
        raise ValueError(f'{session} concatenation does not match frame payloads')
    return rows


def h264_nal_types(data):
    types = []
    offset = 0
    while offset + 3 < len(data):
        if data[offset:offset + 3] == b'\x00\x00\x01':
            header = offset + 3
        elif data[offset:offset + 4] == b'\x00\x00\x00\x01':
            header = offset + 4
        else:
            offset += 1
            continue
        if header >= len(data):
            break
        types.append(data[header] & 0x1f)
        offset = header + 1
    if not types:
        raise ValueError('H.264 payload has no Annex-B NAL unit')
    return types


def verify_abort_payloads(directory):
    lines = (directory / 'h264-abort-retry.meta').read_text().splitlines()
    if (len(lines) != 3 or
            lines[1] !=
            'phase=abort force=1 evidence_persisted=1 source_frame=1'):
        raise ValueError('Abort/retry evidence is incomplete')
    for phase, line in zip(('startup', 'retry'), (lines[0], lines[2]),
                           strict=True):
        match = ABORT_META.fullmatch(line)
        if not match or match.group(1) != phase:
            raise ValueError('Abort/retry evidence is incomplete')
        payload = (directory / f'h264-abort-{phase}.h264').read_bytes()
        if len(payload) != int(match.group(2)):
            raise ValueError(f'Abort {phase} payload does not match metadata')
        if h264_nal_types(payload) != [7, 8, 5]:
            raise ValueError(f'Abort {phase} payload has wrong NAL types')


def decode_h264(path, ffmpeg, temporary, frames):
    decoded = temporary / (path.name + '.yuv')
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', str(path),
                    '-f', 'rawvideo', '-pix_fmt', 'yuv420p', str(decoded)],
                   check=True)
    if decoded.stat().st_size != FRAME_BYTES * frames:
        raise ValueError(f'{path.name} decoded to the wrong frame count')
    return decoded


def expected_source_frame(frame):
    data = bytearray()
    square_x = 8 + frame * 13
    square_y = 16 + frame * 7
    for row in range(240):
        for col in range(320):
            value = (col * 3 + row * 2 + frame * 17) & 0xff
            if (square_x <= col < square_x + 24 and
                    square_y <= row < square_y + 24):
                value = (235 - frame * 9) & 0xff
            data.append(value)
    for row in range(120):
        data.extend((96 + col + frame * 11) & 0xff
                    for col in range(160))
    for row in range(120):
        data.extend([(160 + row - frame * 7) & 0xff] * 160)
    return bytes(data)


def decoded_source_metrics(path, frames):
    decoded = path.read_bytes()
    expected = b''.join(expected_source_frame(frame)
                        for frame in range(frames))
    if len(decoded) != len(expected):
        raise ValueError(f'{path.name} decoded to the wrong frame count')
    if frames >= BASELINE_FRAMES and sha256_bytes(
            expected[:FRAME_BYTES * BASELINE_FRAMES]) != SOURCE_BASELINE_SHA256:
        raise ValueError('Independent H.264 source oracle changed')
    plane_sizes = (320 * 240, 160 * 120, 160 * 120)
    metrics = []
    decoded_frames = []
    for frame in range(frames):
        offset = frame * FRAME_BYTES
        decoded_frames.append(decoded[offset:offset + FRAME_BYTES])
        frame_metrics = []
        for size in plane_sizes:
            actual_plane = decoded[offset:offset + size]
            expected_plane = expected[offset:offset + size]
            if len(set(actual_plane)) < 2:
                raise ValueError(
                    f'{path.name} frame {frame} has a flat decoded plane')
            error = sum(abs(actual - source) for actual, source in
                        zip(actual_plane, expected_plane, strict=True))
            frame_metrics.append(error / size)
            offset += size
        metrics.append(tuple(frame_metrics))
    if len(set(decoded_frames)) != frames:
        raise ValueError(f'{path.name} repeats a decoded source frame')
    return metrics


def verify_extended_decodes(first, second):
    if first.read_bytes() != second.read_bytes():
        raise ValueError('Restarted extended H.264 decodes differently')
    return decoded_source_metrics(first, EXTENDED_FRAMES)


def persist_source_metrics(path, baseline, extended):
    maxima = tuple(max(row[plane] for row in baseline)
                   for plane in range(3))
    with path.open('x', encoding='ascii') as output:
        output.write(
            f'source_baseline_sha256={SOURCE_BASELINE_SHA256}\n'
            'comparison=decoded-byte-mae-no-universal-limit\n'
            f'stock_baseline_max_y_mae={maxima[0]:.6f} '
            f'stock_baseline_max_u_mae={maxima[1]:.6f} '
            f'stock_baseline_max_v_mae={maxima[2]:.6f}\n')
        for session, rows in (('stock-baseline', baseline),
                              ('extended', extended)):
            for frame, (y_mae, u_mae, v_mae) in enumerate(rows):
                output.write(
                    f'session={session} frame={frame} y_mae={y_mae:.6f} '
                    f'u_mae={u_mae:.6f} v_mae={v_mae:.6f}\n')


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def jpeg_scan(data):
    """Return the entropy-coded scan from a single-image baseline JPEG."""
    if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise ValueError('JPEG lacks exact SOI/EOI boundaries')
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xff:
            raise ValueError('JPEG marker stream is malformed')
        while offset < len(data) and data[offset] == 0xff:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker == 0xda:
            length = int.from_bytes(data[offset:offset + 2], 'big')
            start = offset + length
            cursor = start
            while cursor + 1 < len(data):
                if data[cursor] != 0xff:
                    cursor += 1
                    continue
                following = data[cursor + 1]
                if following == 0x00 or 0xd0 <= following <= 0xd7:
                    cursor += 2
                    continue
                if following == 0xd9:
                    return data[start:cursor]
                raise ValueError('JPEG scan contains an unexpected marker')
            break
        if marker == 0xd9:
            break
        if marker == 0x01 or 0xd0 <= marker <= 0xd7:
            continue
        length = int.from_bytes(data[offset:offset + 2], 'big')
        if length < 2 or offset + length > len(data):
            raise ValueError('JPEG segment length is invalid')
        offset += length
    raise ValueError('JPEG has no complete entropy-coded scan')


def verify_jpeg_metadata(path, memory):
    rows = []
    for line in path.read_text().splitlines():
        match = JPEG_META.fullmatch(line)
        if not match:
            raise ValueError(f'Invalid JPEG metadata in {path.name}: {line}')
        frame, index, sequence, length = map(int, match.groups()[:4])
        flags = int(match.group(5), 16)
        if match.group(6) != memory:
            raise ValueError(f'{path.name} records the wrong memory type')
        rows.append((frame, index, sequence, length, flags))
    if len(rows) != 6 or [row[0] for row in rows] != list(range(6)) or \
            [row[2] for row in rows] != list(range(6)):
        raise ValueError(f'{path.name} has an unexpected JPEG frame sequence')
    if sorted(row[1] for row in rows) != [0, 0, 1, 1, 2, 2]:
        raise ValueError(f'{path.name} does not recycle three capture buffers')
    if any(row[3] <= 2 or row[3] > 4 * 1024 * 1024 for row in rows):
        raise ValueError(f'{path.name} has an invalid JPEG payload size')
    if any(row[4] & V4L2_BUF_FLAG_ERROR for row in rows):
        raise ValueError(f'{path.name} contains an error completion')
    return rows


def verify_jpeg_session(directory, session, memory):
    rows = verify_jpeg_metadata(directory / f'{session}.meta', memory)
    payloads = []
    for frame, row in enumerate(rows):
        path = directory / f'{session}-frame{frame}.jpg'
        data = path.read_bytes()
        if len(data) != row[3]:
            raise ValueError(f'{path.name} size does not match metadata')
        jpeg_scan(data)
        payloads.append(data)
    return rows, payloads


def decode_jpeg(path, ffmpeg, temporary):
    decoded = temporary / (path.name + '.yuv')
    subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', str(path),
                    '-f', 'rawvideo', '-pix_fmt', 'yuv420p', str(decoded)],
                   check=True)
    if decoded.stat().st_size != 320 * 240 * 3 // 2:
        raise ValueError(f'{path.name} decoded to the wrong geometry')
    return decoded.read_bytes()


def interrupt_total(path):
    total = 0
    for line in path.read_text().splitlines():
        if ':' not in line:
            continue
        fields = line.split(':', 1)[1].split()
        for field in fields:
            if not field.isdecimal():
                break
            total += int(field)
    return total


def validate_evidence(directory, ffmpeg='ffmpeg', metrics_path=None):
    verify_checksums(directory)
    manifest = json.loads((directory / 'brick-build.json').read_text())
    if manifest.get('ve_qualification') is not True:
        raise ValueError('Evidence is not from the video-engine qualification image')
    status = (directory / 'STATUS').read_text()
    if 'stage=runner result=pass code=0' not in status or 'kernel_fault=0' not in status:
        raise ValueError(f'Device qualification failed closed: {status.strip()}')
    iommu_state = (directory / 'iommu-state').read_text()
    if '/sys/kernel/iommu_groups/' not in iommu_state:
        raise ValueError('Encoder device lacks a captured IOMMU group')
    baseline = directory / 'h264-baseline.h264'
    session1 = directory / 'h264-before.h264'
    session2 = directory / 'h264-after.h264'
    if sha256(baseline) != ENCODED_SHA256:
        raise ValueError('Baseline H.264 does not match the exact stock stream')
    if session1.read_bytes() != session2.read_bytes():
        raise ValueError('Extended H.264 streams differ after codec transitions')
    verify_session(directory, 'h264-baseline', BASELINE_FRAMES, False)
    rows1 = verify_session(directory, 'h264-before', EXTENDED_FRAMES, True)
    rows2 = verify_session(directory, 'h264-after', EXTENDED_FRAMES, True)
    if [row[3:] for row in rows1] != [row[3:] for row in rows2]:
        raise ValueError('Restarted stream metadata differs from the first stream')
    verify_abort_payloads(directory)
    verify_dmesg((directory / 'dmesg-delta').read_text(errors='replace'))
    if interrupt_total(directory / 'interrupts-after') <= \
            interrupt_total(directory / 'interrupts-before'):
        raise ValueError('Video-engine interrupt count did not increase')
    if 'S264' not in (directory / 'decoder-probe').read_text():
        raise ValueError('Decoder transition probe lacks H.264 slice support')
    suspend = (directory / 'suspend.meta').read_text()
    if ('mode=idle-streaming-no-job' not in suspend or
            'wake=rtc' not in suspend or
            'active_job=not-run-no-deterministic-inflight-window' not in suspend):
        raise ValueError('Suspend evidence lacks the bounded idle contract')
    mmap1_rows, mmap1 = verify_jpeg_session(directory, 'jpeg-mmap1', 'mmap')
    mmap2_rows, mmap2 = verify_jpeg_session(directory, 'jpeg-mmap2', 'mmap')
    dmabuf_rows, dmabuf = verify_jpeg_session(directory, 'jpeg-dmabuf', 'dmabuf')
    if mmap1 != mmap2 or mmap1 != dmabuf:
        raise ValueError('JPEG payloads differ across restart or DMA-BUF import')
    if [row[3] for row in mmap1_rows] != [row[3] for row in mmap2_rows] or \
            [row[3] for row in mmap1_rows] != [row[3] for row in dmabuf_rows]:
        raise ValueError('JPEG metadata differs across memory modes')
    first_scan = jpeg_scan(mmap1[0])
    if len(first_scan) != 20852 or hashlib.sha256(first_scan).hexdigest() != JPEG_SCAN_SHA256:
        raise ValueError('First JPEG entropy scan does not match exact stock evidence')
    with tempfile.TemporaryDirectory(prefix='brick-encoder-decode-') as work:
        decoded_baseline = decode_h264(baseline, ffmpeg, Path(work),
                                       BASELINE_FRAMES)
        if sha256(decoded_baseline) != DECODED_SHA256:
            raise ValueError('Baseline H.264 does not decode to stock pixels')
        baseline_metrics = decoded_source_metrics(
            decoded_baseline, BASELINE_FRAMES)
        decoded1 = decode_h264(session1, ffmpeg, Path(work), EXTENDED_FRAMES)
        decoded2 = decode_h264(session2, ffmpeg, Path(work), EXTENDED_FRAMES)
        extended_metrics = verify_extended_decodes(decoded1, decoded2)
        if metrics_path is not None:
            persist_source_metrics(
                metrics_path, baseline_metrics, extended_metrics)
        decoded = [decode_jpeg(directory / f'jpeg-mmap1-frame{frame}.jpg',
                               ffmpeg, Path(work)) for frame in range(6)]
        if hashlib.sha256(decoded[0]).hexdigest() != JPEG_DECODED_SHA256:
            raise ValueError('First JPEG does not decode to exact stock pixels')
        for session in ('jpeg-mmap2', 'jpeg-dmabuf'):
            for frame in range(6):
                candidate = decode_jpeg(directory / f'{session}-frame{frame}.jpg',
                                        ffmpeg, Path(work))
                if candidate != decoded[frame]:
                    raise ValueError(f'{session} frame {frame} decodes differently')


def retrieve(console, remote, destination):
    name = PurePosixPath(remote).name
    if (not re.fullmatch(r'[0-9a-f-]{36}', name) or
            remote != f'/data/encoder-qualification/{name}'):
        raise ProtocolError('Runner returned an invalid evidence path')
    encoded = console.checked(
        f'/bin/busybox tar -cz -C /data/encoder-qualification {name} | '
        '/bin/busybox base64', timeout=120)
    try:
        payload = base64.b64decode(b''.join(encoded.split()), validate=True)
    except ValueError as error:
        raise ProtocolError('Device returned invalid base64 evidence') from error
    safe_extract(payload, destination)
    extracted = destination / name
    if not extracted.is_dir():
        raise ProtocolError('Device evidence archive lacks its named directory')
    return extracted


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default=DEFAULT_PORT)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        raise ValueError('Output path already exists')
    if not shutil.which(args.ffmpeg):
        raise ValueError(f'ffmpeg is unavailable: {args.ffmpeg}')
    args.output.mkdir(parents=True)
    try:
        with Console(args.port, timeout=args.timeout) as console:
            manifest = json.loads(console.checked(
                '/bin/busybox cat /etc/brick-build.json'))
            if manifest.get('ve_qualification') is not True:
                raise ProtocolError(
                    'Running image is not the video-engine qualification profile')
            output = console.checked(
                '/usr/libexec/run-encoder-qualification',
                timeout=max(args.timeout, 240)).decode()
            matches = re.findall(
                r'^EVIDENCE=(/data/encoder-qualification/[0-9a-f-]{36})$',
                output, re.MULTILINE)
            if len(matches) != 1:
                raise ProtocolError(
                    'Qualification did not return one evidence path')
            evidence = retrieve(console, matches[0], args.output)
        validate_evidence(
            evidence, args.ffmpeg,
            args.output / 'host-h264-source-mae.meta')
        print(f'QUALIFICATION_PASS {evidence}')
        return 0
    except BaseException:
        if args.output.exists() and not any(args.output.iterdir()):
            args.output.rmdir()
        raise


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, TimeoutError, ProtocolError, tarfile.TarError,
            subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f'Encoder qualification failed: {error}', file=sys.stderr)
        sys.exit(1)
