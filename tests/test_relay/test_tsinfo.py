"""Tests for MPEG-TS header parsing."""

from quadlink.relay.tsinfo import audio_pts, audio_sample_rate, first_video_pts


def test_sample_rate_from_adts(ts_run):
    assert audio_sample_rate(ts_run(44100, 1)[0]) == 44100
    assert audio_sample_rate(ts_run(48000, 1)[0]) == 48000


def test_first_video_pts_is_continuous_across_segments(ts_run):
    segs = ts_run(48000, 3)
    starts = [first_video_pts(s) for s in segs]
    assert all(isinstance(s, int) for s in starts)
    assert starts[1] - starts[0] == 180000  # 2 s at 90 kHz
    assert starts[2] - starts[1] == 180000


def test_audio_pts_are_sorted_ticks(ts_run):
    pts = audio_pts(ts_run(48000, 1)[0])
    assert pts == sorted(pts)
    assert len(pts) > 10


def test_garbage_has_no_answers():
    assert first_video_pts(b"\x00" * 1880) is None
    assert audio_sample_rate(b"") is None
    assert audio_pts(b"\x47" + b"\x00" * 187) == []
