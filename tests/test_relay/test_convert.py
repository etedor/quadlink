"""Tests for the continuous audio converter."""

import pytest

from quadlink.relay.convert import AudioConverter
from quadlink.relay.tsinfo import audio_pts, audio_sample_rate, first_video_pts


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
