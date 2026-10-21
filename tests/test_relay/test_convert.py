"""Tests for the continuous audio converter."""

import pytest

from quadlink.relay.convert import AudioConverter
from quadlink.relay.tsinfo import (
    _VIDEO_TYPES,
    _pes_starts,
    _pid,
    audio_pts,
    audio_sample_rate,
    first_video_pts,
)


def _video_pts(data: bytes) -> list[int]:
    pid = _pid(data, _VIDEO_TYPES)
    assert pid is not None
    return [pts for pts, _ in _pes_starts(data, pid) if pts is not None]


@pytest.mark.asyncio
async def test_converts_a_run_gaplessly_one_output_per_input(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 4)
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    for seg in segs:
        await conv.feed(seg)
    outs = []
    for seg in segs[:-1]:  # the last closes only when ffmpeg sees the next keyframe or EOF
        out = await conv.output_for(first_video_pts(seg), timeout=10)
        assert out is not None
        outs.append(out)
    await conv.close()

    for seg, out in zip(segs, outs, strict=False):  # outs is one shorter than segs
        assert first_video_pts(out) == first_video_pts(seg)
        assert audio_sample_rate(out) == 48000
    pts = [p for out in outs for p in audio_pts(out)]
    diffs = [b - a for a, b in zip(pts, pts[1:], strict=False)]
    assert all(d > 0 for d in diffs)
    # the 44.1 kHz source's 1024-sample AAC frame duration isn't an integer number
    # of 90 kHz ticks, so its rounded PES timestamps carry a few ticks of noise
    # into the resampled output; tolerate that instead of requiring an exact multiple
    assert all(abs(d - round(d / 1920) * 1920) <= 8 for d in diffs)
    # per-step jitter is rounding noise from the 44.1 kHz source; across the run it must cancel
    assert (pts[-1] - pts[0]) % 1920 == 0


@pytest.mark.asyncio
async def test_audio_stays_aligned_with_video(ffmpeg, ts_run, tmp_path):
    seg = ts_run(44100, 3)
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    for s in seg:
        await conv.feed(s)
    out = await conv.output_for(first_video_pts(seg[1]), timeout=10)
    await conv.close()
    src_offset = audio_pts(seg[1])[0] - first_video_pts(seg[1])
    out_offset = audio_pts(out)[0] - first_video_pts(out)
    assert abs(out_offset - src_offset) <= 1920  # within one 48 kHz AAC frame


@pytest.mark.asyncio
async def test_unknown_pts_times_out(ffmpeg, ts_run, tmp_path):
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    await conv.feed(ts_run(44100, 2)[0])
    assert await conv.output_for(123, timeout=0.5) is None
    await conv.close()


@pytest.mark.asyncio
async def test_has_output_reports_without_blocking(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 3)
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    pts0 = first_video_pts(segs[0])
    assert pts0 is not None
    assert not conv.has_output(pts0)
    for seg in segs:
        await conv.feed(seg)
    out = await conv.output_for(pts0, timeout=10)
    assert out is not None
    assert conv.has_output(pts0)
    await conv.close()


@pytest.mark.asyncio
async def test_finish_flushes_last_output_and_close_removes_it(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 2)
    conv = AudioConverter(ffmpeg, tmp_path / "w")
    await conv.start()
    for seg in segs:
        await conv.feed(seg)
    await conv.finish()
    last = first_video_pts(segs[-1])
    assert last is not None and conv.has_output(last)
    await conv.close()
    # a stale lookup after the workdir is gone reports no output instead of raising
    assert await conv.output_for(last, timeout=0) is None
    assert not conv.has_output(last + 1)


@pytest.mark.asyncio
async def test_fresh_run_lags_one_segment(ffmpeg, ts_run, tmp_path):
    """A new run's first output must not wait on the default stream probe."""
    segs = ts_run(44100, 2)
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    for seg in segs:
        await conv.feed(seg)
    out = await conv.output_for(first_video_pts(segs[0]), timeout=2)
    assert out is not None and audio_sample_rate(out) == 48000
    await conv.close()


@pytest.mark.asyncio
async def test_discard_deletes_output_even_before_it_is_written(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 3)
    conv = AudioConverter(ffmpeg, tmp_path / "w")
    await conv.start()
    pts = [first_video_pts(s) for s in segs]
    for seg in segs:
        await conv.feed(seg)
    # discarded before ffmpeg has written it: deleted once the scan finds it
    conv.discard(pts[0])
    assert await conv.output_for(pts[1], timeout=10) is not None
    assert not conv.has_output(pts[0])
    conv.discard(pts[1])
    assert not conv.has_output(pts[1])
    await conv.finish()
    assert conv.has_output(pts[2])
    assert sorted(p.name for p in (tmp_path / "w").glob("*.ts")) == ["o2.ts"]
    await conv.close()


@pytest.mark.asyncio
async def test_start_clears_a_stale_run_dir(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 2)
    work = tmp_path / "w"
    work.mkdir()
    # left by a crashed daemon: a listing whose o0.ts holds another run's segment
    (work / "out.m3u8").write_text("#EXTM3U\n#EXTINF:2.0,\no0.ts\n")
    (work / "o0.ts").write_bytes(segs[1])
    conv = AudioConverter(ffmpeg, work)
    await conv.start()
    assert not conv.has_output(first_video_pts(segs[1]))
    await conv.close()


# keyframes at 1.2s and 1.6s make ffmpeg cut three pieces per 2s input segment
_SPLIT = (1.2, 1.6)


@pytest.mark.asyncio
async def test_mid_segment_keyframes_serve_whole_segments(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 4, _SPLIT)
    conv = AudioConverter(ffmpeg, tmp_path / "w")
    await conv.start()
    pts = [await conv.feed(seg) for seg in segs]
    assert pts == [first_video_pts(s) for s in segs]
    outs = []
    # the last closes only at EOF
    for p in pts[:-1]:
        assert p is not None
        outs.append(await conv.output_for(p, timeout=10))
    await conv.finish()
    assert pts[-1] is not None and conv.has_output(pts[-1])
    outs.append(await conv.output_for(pts[-1], timeout=0))
    # every piece went into some output
    assert len(list((tmp_path / "w").glob("*.ts"))) == 3 * len(segs)
    await conv.close()

    for i, (seg, out) in enumerate(zip(segs, outs, strict=True)):
        assert out is not None
        assert sorted(_video_pts(out)) == sorted(_video_pts(seg))
        assert audio_sample_rate(out) == 48000
        audio = audio_pts(out)
        start = pts[i]
        assert start is not None
        assert audio[0] - start <= 9000
        # the last segment's tail audio may end slightly early at EOF
        if i + 1 < len(pts):
            nxt = pts[i + 1]
            assert nxt is not None
            assert nxt - audio[-1] <= 9000


@pytest.mark.asyncio
async def test_discard_deletes_every_piece(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 3, _SPLIT)
    conv = AudioConverter(ffmpeg, tmp_path / "w")
    await conv.start()
    pts = [await conv.feed(seg) for seg in segs]
    assert pts[0] is not None and pts[1] is not None
    # discarded before all its pieces exist: later pieces go once scanned
    conv.discard(pts[0])
    assert await conv.output_for(pts[1], timeout=10) is not None
    conv.discard(pts[1])
    await conv.finish()
    assert not conv.has_output(pts[0]) and not conv.has_output(pts[1])
    assert sorted(p.name for p in (tmp_path / "w").glob("*.ts")) == ["o6.ts", "o7.ts", "o8.ts"]
    await conv.close()


@pytest.mark.asyncio
async def test_segment_without_new_video_pts_has_no_output(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 2)
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    assert await conv.feed(segs[1]) is not None
    # not after the previous boundary: a range for it would never complete in order
    assert await conv.feed(segs[0]) is None
    await conv.close()
