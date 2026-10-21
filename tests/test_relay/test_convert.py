"""Tests for the continuous audio converter and the splitter behind it."""

import asyncio
import json
import shutil
import subprocess

import pytest

from quadlink.relay.convert import AudioConverter, Splitter
from quadlink.relay.tsinfo import (
    _VIDEO_TYPES,
    PACKET,
    PTS_WRAP,
    _packets,
    _pes_starts,
    _pid,
    audio_pts,
    audio_sample_rate,
    first_video_pts,
)
from tests.test_relay import synth

# keyframes at 1.2s and 1.6s inside every 2s segment, as some Twitch channels have
_SPLIT = (1.2, 1.6)


def _video_pts(data: bytes) -> list[int]:
    pid = _pid(data, _VIDEO_TYPES)
    assert pid is not None
    return [pts for pts, _ in _pes_starts(data, pid) if pts is not None]


class _Outputs:
    """Collects on_output calls; await get(key) for one output."""

    def __init__(self) -> None:
        self.got: dict[int, bytes | None] = {}
        self.calls: list[int] = []
        self._changed = asyncio.Event()

    def __call__(self, key: int, data: bytes | None) -> None:
        self.calls.append(key)
        self.got[key] = data
        self._changed.set()

    async def get(self, key: int, timeout: float = 10) -> bytes | None:
        async with asyncio.timeout(timeout):
            while key not in self.got:
                self._changed.clear()
                await self._changed.wait()
        return self.got[key]


async def _run(ffmpeg: str, segs: list[bytes]) -> tuple[AudioConverter, _Outputs, list[int]]:
    outputs = _Outputs()
    conv = AudioConverter(ffmpeg, outputs)
    await conv.start()
    keys = []
    for seg in segs:
        key = conv.register(seg)
        assert key is not None
        keys.append(key)
        await conv.feed(seg)
    return conv, outputs, keys


@pytest.mark.asyncio
async def test_each_output_completes_once_the_next_segment_is_fed(ffmpeg, ts_run):
    segs = ts_run(44100, 4)
    outputs = _Outputs()
    conv = AudioConverter(ffmpeg, outputs)
    await conv.start()
    keys = []
    for i, seg in enumerate(segs):
        key = conv.register(seg)
        assert key == first_video_pts(seg)
        keys.append(key)
        await conv.feed(seg)
        if i:
            out = await outputs.get(keys[i - 1], timeout=3)
            assert out is not None
    # the last has no next keyframe to close it
    await asyncio.sleep(0.3)
    assert keys[-1] not in outputs.got
    await conv.finish()
    assert outputs.got[keys[-1]] is not None
    assert outputs.calls == keys
    await conv.close()


@pytest.mark.asyncio
async def test_every_video_frame_lands_in_its_own_segments_output(ffmpeg, ts_run):
    segs = ts_run(44100, 4, _SPLIT)
    conv, outputs, keys = await _run(ffmpeg, segs)
    await conv.finish()
    outs = [outputs.got[k] for k in keys]
    for seg, out in zip(segs, outs, strict=True):
        assert out is not None
        assert sorted(_video_pts(out)) == sorted(_video_pts(seg))
        assert audio_sample_rate(out) == 48000


@pytest.mark.asyncio
async def test_outputs_are_whole_packets_that_rebuild_the_stream(ffmpeg, ts_run):
    segs = ts_run(44100, 3, _SPLIT)
    conv, outputs, keys = await _run(ffmpeg, segs)
    await conv.finish()
    outs = [outputs.got[k] for k in keys]
    for out in outs:
        assert out is not None and len(out) % PACKET == 0
        # PAT, then PMT, before any media
        pids = [pid for pid, _, _ in _packets(out[: 3 * PACKET])]
        assert pids[0] in (0, 0x11) and 0 in pids[:2]
        assert any(pid != 0 for pid in pids[:3])


@pytest.mark.asyncio
async def test_each_output_decodes_alone(ffmpeg, ts_run):
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe not on PATH")
    segs = ts_run(44100, 3, _SPLIT)
    conv, outputs, keys = await _run(ffmpeg, segs)
    await conv.finish()
    for key in keys:
        out = outputs.got[key]
        assert out is not None
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-of", "json", "-i", "pipe:0"],
            input=out,
            capture_output=True,
            check=True,
        )
        assert probe.stderr == b""
        streams = json.loads(probe.stdout)["streams"]
        assert {s["codec_type"] for s in streams} == {"video", "audio"}
        assert [s["sample_rate"] for s in streams if s["codec_type"] == "audio"] == ["48000"]
        decode = subprocess.run(
            [ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "null", "-"],
            input=out,
            capture_output=True,
            check=True,
        )
        assert decode.stderr == b""


@pytest.mark.asyncio
async def test_audio_runs_on_across_outputs(ffmpeg, ts_run):
    segs = ts_run(44100, 4)
    conv, outputs, keys = await _run(ffmpeg, segs)
    await conv.finish()
    outs = [outputs.got[k] for k in keys]
    pts = [p for out in outs if out is not None for p in audio_pts(out)]
    diffs = [b - a for a, b in zip(pts, pts[1:], strict=False)]
    assert all(d > 0 for d in diffs)
    # the 44.1 kHz source's 1024-sample AAC frame duration isn't an integer number
    # of 90 kHz ticks, so its rounded PES timestamps carry a few ticks of noise
    # into the resampled output; tolerate that instead of requiring an exact multiple
    assert all(abs(d - round(d / 1920) * 1920) <= 8 for d in diffs)
    # per-step jitter is rounding noise from the 44.1 kHz source; across the run it must
    # cancel. The last output is left out: the frames ffmpeg flushes at EOF carry it
    head = [p for out in outs[:-1] if out is not None for p in audio_pts(out)]
    assert (head[-1] - head[0]) % 1920 == 0
    # every output holds the audio of its own stretch
    for i, out in enumerate(outs[:-1]):
        assert out is not None
        audio = audio_pts(out)
        # ffmpeg interleaves by DTS, and B frames put a keyframe's DTS ~100ms before
        # its PTS, so the audio just before a cut follows the keyframe into the next output
        assert -13500 <= audio[0] - keys[i] <= 0
        assert 0 < keys[i + 1] - audio[-1] <= 13500


@pytest.mark.asyncio
async def test_audio_stays_aligned_with_video(ffmpeg, ts_run):
    segs = ts_run(44100, 3)
    conv, outputs, keys = await _run(ffmpeg, segs)
    await conv.finish()
    out = [p for k in keys for p in audio_pts(outputs.got[k] or b"")]
    src = audio_pts(segs[0])
    # the encoder delay compensation lands the first frame where the source's starts
    assert abs(out[0] - src[0]) < 960, out[0] - src[0]


@pytest.mark.asyncio
async def test_fresh_run_lags_one_segment(ffmpeg, ts_run):
    """A new run's first output must not wait on the default stream probe."""
    segs = ts_run(44100, 2)
    conv, outputs, keys = await _run(ffmpeg, segs)
    out = await outputs.get(keys[0], timeout=2)
    assert out is not None and audio_sample_rate(out) == 48000
    await conv.close()


@pytest.mark.asyncio
async def test_segment_without_new_video_pts_has_no_output(ffmpeg, ts_run):
    segs = ts_run(44100, 2)
    conv = AudioConverter(ffmpeg, _Outputs())
    assert conv.register(segs[1]) is not None
    # not after the previous key: its output would never complete in order
    assert conv.register(segs[0]) is None
    assert conv.register(b"no video") is None


@pytest.mark.asyncio
async def test_crash_fails_pending_outputs(ffmpeg, ts_run):
    segs = ts_run(44100, 2)
    conv, outputs, keys = await _run(ffmpeg, segs)
    await outputs.get(keys[0])
    assert conv._proc is not None
    conv._proc.kill()
    assert await outputs.get(keys[1]) is None
    await conv.close()


@pytest.mark.asyncio
async def test_close_drops_pending_outputs(ffmpeg, ts_run):
    segs = ts_run(44100, 2)
    conv, outputs, keys = await _run(ffmpeg, segs)
    await outputs.get(keys[0])
    await conv.close()
    await asyncio.sleep(0.1)
    assert keys[1] not in outputs.got


def _stream(*frames: tuple[int, bool]) -> bytes:
    """PAT and PMT, then per (video PTS, headers first) one video and one audio packet."""
    data = synth.pat() + synth.pmt()
    for n, (pts, headers) in enumerate(frames):
        if headers:
            data += synth.pat(cc=n + 1) + synth.pmt(cc=n + 1)
        data += synth.pes(synth.VIDEO_PID, pts, cc=n) + synth.cont(synth.VIDEO_PID, cc=n)
        data += synth.pes(synth.AUDIO_PID, pts, cc=n)
    return data


def _split(keys: list[int], data: bytes, chunk: int = 1000) -> list[tuple[int, bytes]]:
    splitter = Splitter()
    for key in keys:
        splitter.expect(key)
    done = []
    for i in range(0, len(data), chunk):
        done += splitter.push(data[i : i + chunk])
    return done + splitter.end()


def test_splitter_cuts_at_each_keyed_frame_and_moves_headers():
    data = _stream((1000, False), (4000, False), (7000, True), (10000, False), (13000, True))
    done = _split([1000, 7000, 13000], data)
    assert [k for k, _ in done] == [1000, 7000, 13000]
    assert b"".join(out for _, out in done) == data
    for _, out in done:
        assert [pid for pid, _, _ in _packets(out[: 2 * PACKET])] == [0, synth.PMT_PID]


def test_splitter_repeats_headers_when_a_cut_has_none():
    data = _stream((1000, False), (4000, False))
    done = _split([1000, 4000], data)
    assert done[1][1][: 2 * PACKET] == data[: 2 * PACKET]
    assert done[1][1][2 * PACKET :] == data[len(done[0][1]) :]


def test_splitter_cuts_across_a_pts_wrap():
    near = PTS_WRAP - 3000
    data = _stream((near, False), (near + 1500, False), (0, True), (1500, False), (3000, True))
    done = _split([near, 0, 3000], data)
    assert [k for k, _ in done] == [near, 0, 3000]
    assert [sorted(_video_pts(out)) for _, out in done] == [[near, near + 1500], [0, 1500], [3000]]


def test_splitter_gives_a_skipped_key_no_output():
    data = _stream((1000, False), (7000, True))
    done = _split([1000, 4000, 7000], data)
    assert [(k, bool(out)) for k, out in done] == [(1000, True), (4000, False), (7000, True)]


def test_splitter_drops_output_ahead_of_any_key():
    splitter = Splitter()
    assert splitter.push(_stream((1000, False))) == []
    assert splitter._buf == bytearray()
