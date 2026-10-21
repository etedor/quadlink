"""Tests for MPEG-TS header parsing."""

from quadlink.relay.tsinfo import (
    _VIDEO_TYPES,
    PTS_WRAP,
    _pes_starts,
    _pid,
    audio_pts,
    audio_sample_rate,
    before,
    first_video_pts,
)
from tests.test_relay import synth


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


def test_first_video_pts_matches_the_lowest_of_every_frame(ts_run):
    for seg in ts_run(44100, 3, (1.2,)):
        pid = _pid(seg, _VIDEO_TYPES)
        assert pid is not None
        lowest = min(pts for pts, _ in _pes_starts(seg, pid) if pts is not None)
        assert first_video_pts(seg) == lowest


def test_first_video_pts_scans_until_dts_catches_up():
    # open GOP: two leading B frames follow the I frame but present before it
    data = synth.pat() + synth.pmt()
    data += synth.pes(synth.VIDEO_PID, 9000, 1500, cc=0)
    data += synth.pes(synth.VIDEO_PID, 6000, 3000, cc=1)
    data += synth.pes(synth.VIDEO_PID, 4500, cc=2)
    # DTS 4500 reached the lowest PTS, so no later frame can present earlier;
    # this impossible one shows the scan stopped
    data += synth.pes(synth.VIDEO_PID, 3000, cc=3)
    assert first_video_pts(data) == 4500


def test_first_video_pts_across_a_wrap():
    near = PTS_WRAP - 1500
    data = synth.pat() + synth.pmt()
    data += synth.pes(synth.VIDEO_PID, 1500, near, cc=0)
    data += synth.pes(synth.VIDEO_PID, near, cc=1)
    data += synth.pes(synth.VIDEO_PID, 3000, 1500, cc=2)
    assert first_video_pts(data) == near


def test_before_wraps():
    assert before(1, 2) and not before(2, 1) and not before(5, 5)
    assert before(PTS_WRAP - 10, 10) and not before(10, PTS_WRAP - 10)
