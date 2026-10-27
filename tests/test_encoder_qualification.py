"""Host-side validation tests for bounded encoder device evidence."""
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import encoder_qualification as qualification


def metadata_lines(frames, forced):
    lines = []
    states = qualification.expected_h264_states(frames, forced)
    buffers = min(frames, qualification.H264_BUFFERS)
    for frame, state in enumerate(states):
        key = state['key']
        nals = '7,8,5' if frame == 0 else '5' if key else '1'
        lines.append(
            f'frame={frame} index={frame % buffers} sequence={frame} '
            f'bytes={100 + frame} flags={"0x8" if key else "0x10"} '
            f'key={key} p={int(not key)} canary=1 nals={nals}')
    return lines


def h264_segment(frames, forced, generation=1):
    lines = []
    slots = {0: (0x220000, 0x220140), 1: (0x220200, 0x220340)}
    states = qualification.expected_h264_states(frames, forced)
    for frame, state in enumerate(states):
        if forced and frame == qualification.FORCE_FRAME:
            lines.append('cedrus: encoder qualification force requested=1 completed=0')
        reference = slots[state['reference']]
        reconstruction = slots[state['reconstruction']]
        lines.append(
            'cedrus: encoder qualification prepare codec=h264 '
            f'generation={generation + frame} frame={state["frame"]} '
            f'poc={state["poc"]} idr={state["key"]} '
            f'ref_slot={state["reference"]} '
            f'recon_slot={state["reconstruction"]} '
            f'ref_luma={reference[0]:#x} ref_chroma={reference[1]:#x} '
            f'recon_luma={reconstruction[0]:#x} '
            f'recon_chroma={reconstruction[1]:#x} '
            f'force_requested={state["requested"]} '
            f'force_completed={state["completed_prepare"]} '
            f'force_active={state["active"]}')
        lines.append(
            'cedrus: encoder qualification completion codec=h264 '
            f'generation={generation + frame} key={state["key"]} '
            f'promoted_reference={state["promoted"]} '
            f'next_frame={state["next_frame"]} '
            f'frames_since_idr={state["frames_since_idr"]} '
            f'payload={100 + frame} force_requested={state["requested"]} '
            f'force_completed={state["completed_terminal"]} '
            f'force_active={state["active"]}')
    return lines


def abort_segment():
    lines = h264_segment(1, False)
    lines += [
        'cedrus: encoder qualification force requested=1 completed=0',
        'cedrus: encoder qualification prepare codec=h264 generation=2 '
        'frame=0 poc=0 idr=1 ref_slot=0 recon_slot=1 '
        'ref_luma=0x220000 ref_chroma=0x220140 '
        'recon_luma=0x220200 recon_chroma=0x220340 '
        'force_requested=1 force_completed=0 force_active=1',
        'cedrus: encoder qualification abort codec=h264 reason=0 '
        'force_requested=1 force_completed=0 force_active=1',
    ]
    return lines


def retry_segment():
    return [
        'cedrus: encoder qualification prepare codec=h264 generation=3 '
        'frame=0 poc=0 idr=1 ref_slot=0 recon_slot=0 '
        'ref_luma=0x220000 ref_chroma=0x220140 '
        'recon_luma=0x220000 recon_chroma=0x220140 '
        'force_requested=1 force_completed=0 force_active=1',
        'cedrus: encoder qualification completion codec=h264 generation=3 '
        'key=1 promoted_reference=0 next_frame=1 frames_since_idr=1 '
        'payload=100 force_requested=1 force_completed=1 force_active=1',
    ]


def reset_lines():
    return [f'cedrus: {qualification.RESET_BEGIN}',
            f'cedrus: {qualification.RESET_COMPLETE}']


def fake_h264(nals, length):
    payload = bytearray()
    for nal in nals:
        payload += b'\x00\x00\x01' + bytes([nal]) + b'\x55'
    payload += b'\x55' * (length - len(payload))
    return bytes(payload)


def complete_dmesg():
    jpeg = []
    for generation in range(1, 19):
        jpeg += [
            'cedrus: encoder qualification prepare codec=jpeg '
            f'generation={generation} quality=95',
            'cedrus: encoder qualification completion codec=jpeg '
            f'generation={generation} payload=20000',
        ]
    lines = []
    for segment in (
            h264_segment(qualification.BASELINE_FRAMES, False),
            h264_segment(qualification.EXTENDED_FRAMES, True),
            abort_segment(), retry_segment(), jpeg, [],
            h264_segment(qualification.EXTENDED_FRAMES, True)):
        lines.extend(segment)
        lines.extend(reset_lines())
    return '\n'.join(lines)


class EncoderQualificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def metadata(self, frames=qualification.BASELINE_FRAMES, forced=False):
        path = self.root / 'session.meta'
        path.write_text('\n'.join(metadata_lines(frames, forced)) + '\n')
        return path

    def test_metadata_requires_fresh_then_rotated_capture_buffers(self):
        baseline = qualification.verify_metadata(
            self.metadata(), qualification.BASELINE_FRAMES, False)
        self.assertEqual([row[1] for row in baseline], list(range(6)))
        extended = qualification.verify_metadata(
            self.metadata(qualification.EXTENDED_FRAMES, True),
            qualification.EXTENDED_FRAMES, True)
        self.assertEqual([row[1] for row in extended[:10]],
                         list(range(8)) + [0, 1])
        path = self.metadata()
        path.write_text(path.read_text().replace('index=5', 'index=0'))
        with self.assertRaisesRegex(ValueError, 'capture-buffer rotation'):
            qualification.verify_metadata(
                path, qualification.BASELINE_FRAMES, False)

    def test_metadata_rejects_error_or_wrong_nal(self):
        path = self.metadata()
        path.write_text(path.read_text().replace('flags=0x10', 'flags=0x50', 1))
        with self.assertRaisesRegex(ValueError, 'error completion'):
            qualification.verify_metadata(
                path, qualification.BASELINE_FRAMES, False)
        path = self.metadata()
        path.write_text(path.read_text().replace('nals=1', 'nals=5', 1))
        with self.assertRaisesRegex(ValueError, 'NAL types'):
            qualification.verify_metadata(
                path, qualification.BASELINE_FRAMES, False)

    def test_session_payloads_match_metadata_and_concatenation(self):
        self.metadata().rename(self.root / 'session1.meta')
        frames = []
        for frame in range(qualification.BASELINE_FRAMES):
            nals = [7, 8, 5] if frame == 0 else [1]
            payload = fake_h264(nals, 100 + frame)
            frames.append(payload)
            (self.root / f'session1-frame{frame}.h264').write_bytes(payload)
        stream = self.root / 'session1.h264'
        stream.write_bytes(b''.join(frames))
        qualification.verify_session(
            self.root, 'session1', qualification.BASELINE_FRAMES, False)
        stream.write_bytes(b'wrong')
        with self.assertRaisesRegex(ValueError, 'concatenation'):
            qualification.verify_session(
                self.root, 'session1', qualification.BASELINE_FRAMES, False)

    def test_session_payloads_independently_validate_nal_types(self):
        self.metadata().rename(self.root / 'session1.meta')
        payloads = []
        for frame in range(qualification.BASELINE_FRAMES):
            nals = [7, 8, 5] if frame == 0 else [1]
            payload = fake_h264(nals, 100 + frame)
            payloads.append(payload)
            (self.root / f'session1-frame{frame}.h264').write_bytes(payload)
        (self.root / 'session1.h264').write_bytes(b''.join(payloads))
        wrong = fake_h264([5], 101)
        (self.root / 'session1-frame1.h264').write_bytes(wrong)
        (self.root / 'session1.h264').write_bytes(
            payloads[0] + wrong + b''.join(payloads[2:]))
        with self.assertRaisesRegex(ValueError, 'wrong NAL types'):
            qualification.verify_session(
                self.root, 'session1', qualification.BASELINE_FRAMES, False)

    def test_abort_payloads_match_metadata_and_nals(self):
        startup = fake_h264([7, 8, 5], 100)
        retry = fake_h264([7, 8, 5], 101)
        (self.root / 'h264-abort-startup.h264').write_bytes(startup)
        (self.root / 'h264-abort-retry.h264').write_bytes(retry)
        metadata = self.root / 'h264-abort-retry.meta'
        metadata.write_text(
            'phase=startup bytes=100 key=1 canary=1 nals=7,8,5\n'
            'phase=abort force=1 evidence_persisted=1 source_frame=1\n'
            'phase=retry bytes=101 key=1 canary=1 nals=7,8,5\n')
        qualification.verify_abort_payloads(self.root)
        (self.root / 'h264-abort-retry.h264').write_bytes(
            fake_h264([7, 8, 1], 101))
        with self.assertRaisesRegex(ValueError, 'wrong NAL types'):
            qualification.verify_abort_payloads(self.root)

    def test_dmesg_requires_force_gop_registers_and_abort_retry(self):
        text = complete_dmesg()
        qualification.verify_dmesg(text)
        with self.assertRaisesRegex(ValueError, 'force-request accounting'):
            qualification.verify_dmesg(
                text.replace('force requested=1 completed=0',
                             'force requested=2 completed=0', 1))
        with self.assertRaisesRegex(ValueError, 'reference registers'):
            qualification.verify_dmesg(
                text.replace('ref_luma=0x220200', 'ref_luma=0x220201', 1))
        with self.assertRaisesRegex(ValueError, 'Forced job completed'):
            marker = 'cedrus: encoder qualification abort codec=h264'
            extra = ('cedrus: encoder qualification completion codec=h264 '
                     'generation=2 key=1 promoted_reference=1 next_frame=1 '
                     'frames_since_idr=1 payload=100 force_requested=1 '
                     'force_completed=1 force_active=1\n')
            qualification.verify_dmesg(text.replace(marker, extra + marker))

    def test_dmesg_rejects_kernel_fault(self):
        with self.assertRaisesRegex(ValueError, 'iommu fault'):
            qualification.verify_dmesg(complete_dmesg() + '\nIOMMU fault')

    def test_checksums_cover_each_regular_evidence_file(self):
        evidence = self.root / 'evidence'
        evidence.mkdir()
        (evidence / 'one').write_bytes(b'one')
        (evidence / 'two').write_bytes(b'two')
        lines = [
            f'{hashlib.sha256((evidence / name).read_bytes()).hexdigest()}  ./{name}'
            for name in ('one', 'two')]
        (evidence / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
        qualification.verify_checksums(evidence)
        (evidence / 'three').write_bytes(b'uncovered')
        with self.assertRaisesRegex(ValueError, 'do not cover'):
            qualification.verify_checksums(evidence)

    def test_tar_extraction_rejects_links_and_traversal(self):
        def archive(member):
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode='w:gz') as output:
                output.addfile(
                    member, io.BytesIO(b'x') if member.isfile() else None)
            return payload.getvalue()

        safe = tarfile.TarInfo('boot-id/evidence')
        safe.size = 1
        qualification.safe_extract(archive(safe), self.root)
        self.assertEqual((self.root / 'boot-id/evidence').read_bytes(), b'x')
        traversal = tarfile.TarInfo('../outside')
        traversal.size = 1
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            qualification.safe_extract(archive(traversal), self.root)
        link = tarfile.TarInfo('boot-id/link')
        link.type = tarfile.SYMTYPE
        link.linkname = '/etc/passwd'
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            qualification.safe_extract(archive(link), self.root)

    def test_exact_baseline_input_generator_matches_stock_fixture_hash(self):
        data = b''.join(qualification.expected_source_frame(frame)
                        for frame in range(qualification.BASELINE_FRAMES))
        self.assertEqual(len(data), qualification.FRAME_BYTES * 6)
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            qualification.SOURCE_BASELINE_SHA256)

    def test_extended_decode_rejects_consistent_wrong_pair(self):
        first = self.root / 'first.yuv'
        second = self.root / 'second.yuv'
        wrong = bytes(qualification.FRAME_BYTES * qualification.EXTENDED_FRAMES)
        first.write_bytes(wrong)
        second.write_bytes(wrong)
        with self.assertRaisesRegex(ValueError, 'flat decoded plane'):
            qualification.verify_extended_decodes(first, second)

    def test_extended_decode_accepts_independent_source_oracle(self):
        first = self.root / 'first.yuv'
        second = self.root / 'second.yuv'
        expected = b''.join(
            qualification.expected_source_frame(frame)
            for frame in range(qualification.EXTENDED_FRAMES))
        first.write_bytes(expected)
        second.write_bytes(expected)
        metrics = self.root / 'mae.meta'
        extended = qualification.verify_extended_decodes(first, second)
        baseline = extended[:qualification.BASELINE_FRAMES]
        qualification.persist_source_metrics(metrics, baseline, extended)
        lines = metrics.read_text().splitlines()
        self.assertEqual(
            lines[:2],
            [f'source_baseline_sha256={qualification.SOURCE_BASELINE_SHA256}',
             'comparison=decoded-byte-mae-no-universal-limit'])
        self.assertEqual(len(lines), (qualification.BASELINE_FRAMES +
                                     qualification.EXTENDED_FRAMES + 3))
        self.assertEqual(
            lines[2], 'stock_baseline_max_y_mae=0.000000 '
            'stock_baseline_max_u_mae=0.000000 '
            'stock_baseline_max_v_mae=0.000000')
        self.assertEqual(
            lines[3], 'session=stock-baseline frame=0 y_mae=0.000000 '
            'u_mae=0.000000 v_mae=0.000000')

    def test_exact_nv12_input_matches_stock_fixture_hash(self):
        data = bytearray()
        square_x, square_y = 8, 16
        for row in range(240):
            for col in range(320):
                value = (col * 3 + row * 2) & 0xff
                if (square_x <= col < square_x + 24 and
                        square_y <= row < square_y + 24):
                    value = 235
                data.append(value)
        for row in range(120):
            for col in range(160):
                data.extend(((96 + col) & 0xff, (160 + row) & 0xff))
        self.assertEqual(
            hashlib.sha256(data).hexdigest(),
            'be7d6674d35e67c73a48838d4784d149d8210f632780e4ca900d3de7e60264d0')

    def test_jpeg_scan_rejects_trailing_or_missing_eoi(self):
        payload = b'\xff\xd8\xff\xda\x00\x02abc\xff\x00d\xff\xd9'
        self.assertEqual(qualification.jpeg_scan(payload), b'abc\xff\x00d')
        with self.assertRaisesRegex(ValueError, 'SOI/EOI'):
            qualification.jpeg_scan(payload + b'x')

    def test_jpeg_metadata_requires_three_recycled_buffers(self):
        path = self.root / 'jpeg.meta'
        path.write_text(''.join(
            f'frame={frame} index={frame % 3} sequence={frame} '
            f'bytes={20000 + frame} flags=0x8 key=1 eoi=1 canary=1 '
            'memory=dmabuf\n' for frame in range(6)))
        rows = qualification.verify_jpeg_metadata(path, 'dmabuf')
        self.assertEqual([row[1] for row in rows], [0, 1, 2, 0, 1, 2])
        path.write_text(path.read_text().replace('index=0', 'index=1', 1))
        with self.assertRaisesRegex(ValueError, 'three capture buffers'):
            qualification.verify_jpeg_metadata(path, 'dmabuf')

    def test_interrupt_total_counts_only_cpu_columns(self):
        path = self.root / 'interrupts'
        path.write_text('94: 3 4 5 6 GICv3 126 Level video-codec\n')
        self.assertEqual(qualification.interrupt_total(path), 18)


if __name__ == '__main__':
    unittest.main()
