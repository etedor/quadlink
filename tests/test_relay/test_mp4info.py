"""Tests for the fMP4 parsing behind fMP4 remuxing."""

import random

from quadlink.relay.mp4info import lift_offsets, video_track


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
    data, lift = lift_offsets(segs[0], video, 0)
    assert data is segs[0] and lift > 0
    # the same depth again adds nothing either
    assert lift_offsets(segs[1], video, lift) == (segs[1], lift)


def test_segment_after_a_deeper_one_gets_its_offsets_raised(fmp4_run):
    init, segs = fmp4_run(44100, 1, negative_cts=True, bframes=1)
    video = video_track(init)
    assert video is not None
    _, own = lift_offsets(segs[0], video, 0)
    data, lift = lift_offsets(segs[0], video, own + 512)
    assert lift == own + 512
    assert len(data) == len(segs[0]) and data != segs[0]
    # no offset is negative now, so the segment adds no lift of its own
    assert lift_offsets(data, video, 0) == (data, 0)


def test_segment_without_video_keeps_the_lift(fmp4_run):
    init, _ = fmp4_run(44100, 1)
    video = video_track(init)
    assert video is not None
    assert lift_offsets(b"not an fMP4 fragment", video, 7) == (b"not an fMP4 fragment", 7)


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
