"""Tests for the fMP4 parsing behind fMP4 remuxing."""

import random
import struct

from quadlink.relay.mp4info import lift_offsets, video_track


def _trun(flags: int, offsets: list[int] | int) -> bytes:
    """A trun box; `offsets` is per-sample composition offsets, or a bare count
    when `flags` carries no composition-offset field."""
    count = len(offsets) if isinstance(offsets, list) else offsets
    body = bytes([0]) + flags.to_bytes(3, "big") + count.to_bytes(4, "big")
    if isinstance(offsets, list):
        for off in offsets:
            body += struct.pack(">i", off)
    return struct.pack(">I", 8 + len(body)) + b"trun" + body


def _tfhd(track_id: int) -> bytes:
    body = bytes(4) + struct.pack(">I", track_id)
    return struct.pack(">I", 8 + len(body)) + b"tfhd" + body


def _moof(track_id: int, trun: bytes) -> bytes:
    traf_body = _tfhd(track_id) + trun
    traf = struct.pack(">I", 8 + len(traf_body)) + b"traf" + traf_body
    return struct.pack(">I", 8 + len(traf)) + b"moof" + traf


def test_video_track_has_a_timescale(fmp4_run):
    init, _ = fmp4_run(44100, 1)
    video = video_track(init)
    assert video is not None and video.timescale > 0


def test_video_track_without_moov():
    assert video_track(b"") is None
    assert video_track(b"\x00\x00\x00\x08free") is None


def test_segment_needing_no_more_lift_is_unchanged(fmp4_run):
    init, segs = fmp4_run(44100, 2, negative_cts=True)
    video = video_track(init)
    assert video is not None
    data, lift, broke = lift_offsets(segs[0], video, 0)
    assert data is segs[0] and lift > 0 and not broke
    # the same depth again adds nothing either
    assert lift_offsets(segs[1], video, lift) == (segs[1], lift, False)


def test_segment_after_a_deeper_one_gets_its_offsets_raised(fmp4_run):
    init, segs = fmp4_run(44100, 1, negative_cts=True, bframes=1)
    video = video_track(init)
    assert video is not None
    _, own, _ = lift_offsets(segs[0], video, 0)
    data, lift, broke = lift_offsets(segs[0], video, own + 512)
    assert lift == own + 512 and not broke
    assert len(data) == len(segs[0]) and data != segs[0]
    # no offset is negative now, so the segment adds no lift of its own
    assert lift_offsets(data, video, 0) == (data, 0, False)


def test_segment_without_video_keeps_the_lift(fmp4_run):
    init, _ = fmp4_run(44100, 1)
    video = video_track(init)
    assert video is not None
    assert lift_offsets(b"not an fMP4 fragment", video, 7) == (b"not an fMP4 fragment", 7, False)


def test_v0_trun_offsets_are_read_signed(fmp4_run):
    """ffmpeg reads composition offsets as signed in trun v0 too, not only v1."""
    init, _ = fmp4_run(44100, 1)
    video = video_track(init)
    assert video is not None
    seg = _moof(video.track_id, _trun(0x800, [-1024]))
    assert lift_offsets(seg, video, 0) == (seg, 1024, False)


def test_segment_without_a_composition_offset_field_breaks_its_run(fmp4_run):
    """A lift it cannot carry must not let this segment's PTS sit behind the depth
    already reached; the caller breaks the run here instead."""
    init, _ = fmp4_run(44100, 1)
    video = video_track(init)
    assert video is not None
    seg = _moof(video.track_id, _trun(0, 3))
    data, lift, broke = lift_offsets(seg, video, 512)
    assert data == seg and lift == 0 and broke
    # with nothing yet to lift, the same segment needs no break
    assert lift_offsets(seg, video, 0) == (seg, 0, False)


def test_corrupt_boxes_never_raise(fmp4_run):
    init, segs = fmp4_run(44100, 1, negative_cts=True)
    video = video_track(init)
    assert video is not None
    rng = random.Random(7)
    for _ in range(300):
        bad_init, bad_seg = bytearray(init), bytearray(segs[0][:4096])
        for buf in (bad_init, bad_seg):
            for _ in range(4):
                buf[rng.randrange(len(buf))] = rng.randrange(256)
        video_track(bytes(bad_init))
        lift_offsets(bytes(bad_seg), video, 10**6)
