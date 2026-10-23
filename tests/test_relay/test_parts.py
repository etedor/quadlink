"""Tests for cutting a continuous MPEG-TS stream into LL-HLS parts."""

import pytest

from quadlink.relay.parts import PartCutter
from quadlink.relay.tsinfo import PACKET

from . import synth

FRAME = 3000  # 90 kHz ticks per frame at 30 fps


def _stream(frames: list[int], headers_at: set[int] = frozenset()) -> bytes:
    """PAT and PMT, then per frame one video and one audio packet; PSI before keyframes."""
    data = synth.pat() + synth.pmt()
    for n, pts in enumerate(frames):
        if n in headers_at:
            data += synth.pat(cc=n + 1) + synth.pmt(cc=n + 1)
        data += synth.pes(synth.VIDEO_PID, pts, cc=n) + synth.cont(synth.VIDEO_PID, cc=n)
        data += synth.pes(synth.AUDIO_PID, pts, cc=n)
    return data


def _cut(keys: list[int], data: bytes, chunk: int = 1000, end: bool = True):
    cutter = PartCutter()
    for key in keys:
        cutter.expect(key)
    parts = []
    for i in range(0, len(data), chunk):
        parts += cutter.push(data[i : i + chunk])
    return parts + (cutter.end() if end else [])


def _segments(count: int, start: int = 900000) -> tuple[list[int], list[int], set[int]]:
    """Frames of `count` 2s segments; keys and the frame index of each keyframe."""
    frames = [start + n * FRAME for n in range(60 * count)]
    keys = [frames[60 * s] for s in range(count)]
    return frames, keys, {60 * s for s in range(count)}


def test_parts_are_half_a_second_and_segments_two():
    frames, keys, heads = _segments(3)
    parts = _cut(keys, _stream(frames, heads))
    assert [p.duration for p in parts] == pytest.approx([0.5] * 12)
    assert [p.key for p in parts] == [k for k in keys for _ in range(4)]
    assert [p.independent for p in parts] == [True, False, False, False] * 3
    assert [p.last for p in parts] == [False, False, False, True] * 3


def test_parts_rebuild_the_stream_exactly():
    frames, keys, heads = _segments(2)
    data = _stream(frames, heads)
    parts = _cut(keys, data)
    assert b"".join(p.data for p in parts) == data
    assert all(len(p.data) % PACKET == 0 for p in parts)


def test_each_segment_starts_with_pat_and_pmt():
    frames, keys, heads = _segments(3)
    parts = _cut(keys, _stream(frames, heads))
    for part in parts:
        if part.independent:
            assert part.data[:3] == b"\x47\x40\x00"


def test_a_cut_without_its_own_headers_gets_copies():
    frames, keys, _ = _segments(2)
    parts = _cut(keys, _stream(frames))
    second = next(p for p in parts if p.key == keys[1])
    assert second.data[:3] == b"\x47\x40\x00"


def test_a_part_finishes_as_soon_as_the_next_one_starts():
    frames, keys, heads = _segments(2)
    data = _stream(frames, heads)
    # everything up to frame 16 (0.53 s): the first part is out, the segment is not
    upto = data.index(synth.pes(synth.VIDEO_PID, frames[16], cc=16))
    parts = _cut(keys, data[:upto], end=False)
    assert len(parts) == 1
    assert parts[0].duration == pytest.approx(0.5)


def test_parts_follow_decode_order_with_b_frames():
    """PTS jumps around with B-frames; parts are timed by DTS."""
    data = synth.pat() + synth.pmt()
    keys = [900000, 900000 + 60 * FRAME]
    for n in range(120):
        dts = 900000 + n * FRAME - FRAME
        # I P B B pattern: PTS runs up to two frames ahead of DTS
        pts = dts + (FRAME if n % 3 == 0 else 2 * FRAME if n % 3 == 1 else 0)
        if n == 60:
            pts = keys[1]
        data += synth.pes(synth.VIDEO_PID, pts, dts, cc=n) + synth.cont(synth.VIDEO_PID, cc=n)
    parts = _cut(keys, data)
    assert all(abs(p.duration - 0.5) < 0.04 for p in parts if not p.last or p.key == keys[0])


def test_output_before_the_first_key_is_dropped():
    frames, keys, heads = _segments(2)
    parts = _cut(keys[1:], _stream(frames, heads))
    assert {p.key for p in parts} == {keys[1]}
    assert parts[0].data[:3] == b"\x47\x40\x00"


def test_end_closes_the_open_part_as_the_segments_last():
    frames, keys, heads = _segments(1)
    parts = _cut(keys, _stream(frames[:40], heads))
    assert [p.duration for p in parts] == pytest.approx([0.5, 0.5, 40 / 30 - 1.0])
    assert parts[-1].last
