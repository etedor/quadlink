"""Tests for the continuous audio converter and the splitter behind it."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from quadlink.relay import convert, mp4info
from quadlink.relay.convert import AudioConverter, Remuxer, Splitter, remux
from quadlink.relay.tsinfo import (
    _AAC_ADTS,
    _VIDEO_TYPES,
    PACKET,
    PTS_WRAP,
    _packets,
    _pes_starts,
    _pes_times,
    _pid,
    audio_pts,
    audio_sample_rate,
    before,
    continue_stream,
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
@pytest.mark.parametrize(
    "shape",
    [{"extra_keyframes": _SPLIT}, {"open_gop": True}],
    ids=["mid-segment-keyframes", "open-gop"],
)
async def test_every_video_frame_lands_in_its_own_segments_output(ffmpeg, ts_run, shape):
    segs = ts_run(44100, 4, **shape)
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
# the 33-bit PTS wraps at 2**33 / 90000 = 95443.7s. ffmpeg reads a stream that starts
# under 60s before the wrap as negative timestamps, one that starts earlier as growing
# past 2**33; both must come out as the input's PTS
@pytest.mark.parametrize("offset,count", [(95439, 4), (95375, 36)])
async def test_a_run_across_the_pts_wrap(ffmpeg, ts_run, offset, count):
    segs = ts_run(44100, count, offset=offset)
    starts = [first_video_pts(s) for s in segs]
    assert any(
        a is not None and b is not None and a > b for a, b in zip(starts, starts[1:], strict=False)
    )
    conv, outputs, keys = await _run(ffmpeg, segs)
    await conv.finish()
    assert outputs.calls == keys
    for seg, key in zip(segs, keys, strict=True):
        out = outputs.got[key]
        assert out is not None
        assert sorted(_video_pts(out)) == sorted(_video_pts(seg))
        assert audio_sample_rate(out) == 48000


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
async def test_output_with_no_cut_fails_the_run_at_once(ffmpeg, ts_run, monkeypatch):
    """Repeated input never closes the head output; its buffer must not grow unbounded."""
    segs = ts_run(44100, 2)
    monkeypatch.setattr(convert, "MAX_PENDING", 2 * max(map(len, segs)))
    conv, outputs, keys = await _run(ffmpeg, segs)
    assert await outputs.get(keys[0]) is not None
    async with asyncio.timeout(10):
        while not conv.failed:
            # not after the last key, so no output of its own and no cut
            assert conv.register(segs[0]) is None
            try:
                await conv.feed(segs[0])
            except (OSError, RuntimeError):
                # the reader killed ffmpeg during this feed
                break
    assert conv.failed
    assert await outputs.get(keys[1], timeout=1) is None
    assert not conv.continues(segs[1])
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(RuntimeError):
        await conv.feed(segs[0])
    assert loop.time() - start < 0.1
    assert conv._splitter.size == 0
    await conv.close()


@pytest.mark.asyncio
async def test_continues_only_with_a_later_video_pts(ffmpeg, ts_run):
    segs = ts_run(44100, 3)
    conv = AudioConverter(ffmpeg, _Outputs())
    assert conv.continues(segs[1])
    conv.register(segs[1])
    assert conv.continues(segs[2])
    assert not conv.continues(segs[1])
    assert not conv.continues(segs[0])
    assert conv.continues(b"no video")


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
    # only the PAT and PMT stay
    assert splitter.size == 2 * PACKET


def test_splitter_keeps_headers_for_the_first_key_after_dropped_output():
    data = _stream((1000, False), (4000, False), (7000, False))
    # PAT, PMT, then three packets per frame
    first = 5 * PACKET
    splitter = Splitter()
    assert splitter.push(data[:first]) == []
    splitter.expect(4000)
    splitter.expect(7000)
    done = splitter.push(data[first:]) + splitter.end()
    assert [k for k, _ in done] == [4000, 7000]
    assert done[0][1][: 2 * PACKET] == data[: 2 * PACKET]
    assert sorted(_video_pts(done[0][1])) == [4000]


def _decode_errors(ffmpeg: str, data: bytes) -> bytes:
    return subprocess.run(
        [ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "null", "-"],
        input=data,
        capture_output=True,
        check=True,
    ).stderr


def _first_video_packet(data: bytes) -> tuple[int, str]:
    """(PTS, ffprobe flags) of the first video packet in decode order."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("ffprobe not on PATH")
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "quiet",
            "-select_streams",
            "v",
            "-show_packets",
            "-of",
            "json",
            "-i",
            "pipe:0",
        ],
        input=data,
        capture_output=True,
        check=True,
    )
    first = json.loads(probe.stdout)["packets"][0]
    return int(first["pts"]), first["flags"]


def _fragments(seg: bytes) -> list[bytes]:
    """Each moof with the mdat after it."""
    boxes, pos = [], 0
    while pos < len(seg):
        size = int.from_bytes(seg[pos : pos + 4], "big")
        boxes.append(seg[pos : pos + size])
        pos += size
    return [a + b for a, b in zip(boxes[::2], boxes[1::2], strict=True)]


def _depth_change(fmp4_run, depths: tuple[int, int], mid: bool) -> tuple[bytes, list[bytes]]:
    """A negative-CTS run whose B-frame depth changes at segment 3, or inside it."""
    init, first = fmp4_run(44100, 6, negative_cts=True, bframes=depths[0])
    _, second = fmp4_run(44100, 6, negative_cts=True, bframes=depths[1])
    segs = first[:3] + second[3:]
    if mid:
        head, tail = _fragments(first[3]), _fragments(second[3])
        segs[3] = b"".join(head[:4] + tail[4:])
    return init, segs


async def _as_ts(ffmpeg: str, init: bytes, segs: list[bytes]) -> list[bytes]:
    remuxer = Remuxer(ffmpeg, init)
    out = []
    for seg in segs:
        ts = await remuxer.remux(seg, 10)
        assert ts is not None
        out.append(ts)
    return out


def _times(data: bytes, types: set[int] = _VIDEO_TYPES) -> list[tuple[int, int]]:
    pid = _pid(data, types)
    assert pid is not None
    return list(_pes_times(data, pid))


def _counter_jumps(data: bytes) -> int:
    last: dict[int, int] = {}
    jumps = 0
    for i in range(0, len(data), PACKET):
        pid = ((data[i + 1] & 0x1F) << 8) | data[i + 2]
        cc, payload = data[i + 3] & 0x0F, bool(data[i + 3] & 0x10)
        if pid in last and cc != ((last[pid] + 1) & 0x0F if payload else last[pid]):
            jumps += 1
        last[pid] = cc
    return jumps


_SHAPES: dict[str, dict[str, object]] = {
    "edit-list": {},
    "chunked": {"chunked": True},
    "negative-cts": {"negative_cts": True},
    "pts-wrap": {"offset": 95440},
}
_DEPTHS = {
    "shallower": ((3, 1), False),
    "deeper": ((1, 3), False),
    "deeper-mid-segment": ((1, 3), True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", [*_SHAPES, *_DEPTHS])
async def test_remuxed_segments_join_into_one_remux(ffmpeg, fmp4_run, shape):
    # a reader of the joined outputs sees what one mov demuxer over the whole stream
    # gives; its lift only rises, while a segment alone would start from its own
    if shape in _SHAPES:
        init, segs = fmp4_run(44100, 5, **_SHAPES[shape])
    else:
        init, segs = _depth_change(fmp4_run, *_DEPTHS[shape])
    ts = await _as_ts(ffmpeg, init, segs)
    whole = await remux(ffmpeg, init, b"".join(segs), 30)
    assert whole is not None
    assert [t for out in ts for t in _times(out)] == _times(whole)
    if shape in _SHAPES:
        # spliced encodes differ by a rounding tick at the splice
        assert [t for out in ts for t in _times(out, {_AAC_ADTS})] == _times(whole, {_AAC_ADTS})
    assert _counter_jumps(b"".join(ts)) == 0
    keys = [first_video_pts(out) for out in ts]
    for out, key in zip(ts[:-1], keys[1:], strict=True):
        assert key is not None
        assert all(before(pts, key) for pts, _ in _times(out))


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [44100, 48000])
@pytest.mark.parametrize("shape", ["edit-list", "chunked", "negative-cts"])
async def test_fmp4_run_converts_to_ts_frame_for_frame(ffmpeg, fmp4_run, rate, shape):
    init, segs = fmp4_run(rate, 4, **_SHAPES[shape])
    ts = await _as_ts(ffmpeg, init, segs)
    conv, outputs, keys = await _run(ffmpeg, ts)
    assert keys == [first_video_pts(t) for t in ts]
    # a run's first output completes once the next segment's keyframe is fed
    assert await outputs.get(keys[0], timeout=3) is not None
    await conv.finish()
    assert outputs.calls == keys
    for t, key in zip(ts, keys, strict=True):
        out = outputs.got[key]
        assert out is not None and len(out) % PACKET == 0
        assert first_video_pts(out) == key
        assert sorted(_video_pts(out)) == sorted(_video_pts(t))
        assert len(set(_video_pts(out))) == len(_video_pts(out)) == 60
        assert audio_sample_rate(out) == 48000
        assert _decode_errors(ffmpeg, out) == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", [*_DEPTHS])
async def test_fmp4_run_keeps_each_frame_in_its_output_as_b_frame_depth_changes(
    ffmpeg, fmp4_run, shape
):
    init, segs = _depth_change(fmp4_run, *_DEPTHS[shape])
    ts = await _as_ts(ffmpeg, init, segs)
    conv, outputs, keys = await _run(ffmpeg, ts)
    await conv.finish()
    for t, key in zip(ts, keys, strict=True):
        out = outputs.got[key]
        assert out is not None
        assert _first_video_packet(out) == (key, "K__")
        assert _times(out) == _times(t)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunked", [False, True], ids=["one-fragment", "chunked"])
async def test_fmp4_audio_runs_on_across_outputs(ffmpeg, fmp4_run, chunked):
    init, segs = fmp4_run(44100, 5, chunked=chunked)
    conv, outputs, keys = await _run(ffmpeg, await _as_ts(ffmpeg, init, segs))
    await conv.finish()
    # the chunked source's first AAC frame holds the encoder priming as a longer
    # duration, a gap in the source itself; the last output holds the EOF flush
    outs = [outputs.got[k] for k in keys][1:]
    keys = keys[1:]
    head = [p for out in outs[:-1] if out is not None for p in audio_pts(out)]
    diffs = [b - a for a, b in zip(head, head[1:], strict=False)]
    # a PES can carry several AAC frames
    assert all(d > 0 and abs(d - round(d / 1920) * 1920) <= 8 for d in diffs)
    assert (head[-1] - head[0]) % 1920 == 0
    for i, out in enumerate(outs[:-1]):
        assert out is not None
        audio = audio_pts(out)
        assert -13500 <= audio[0] - keys[i] <= 0
        assert 0 < keys[i + 1] - audio[-1] <= 13500


@pytest.mark.asyncio
async def test_fmp4_run_across_the_pts_wrap(ffmpeg, fmp4_run):
    init, segs = fmp4_run(44100, 4, offset=95440)
    conv, outputs, keys = await _run(ffmpeg, await _as_ts(ffmpeg, init, segs))
    assert any(a > b for a, b in zip(keys, keys[1:], strict=False))
    await conv.finish()
    assert outputs.calls == keys
    assert all(outputs.got[k] for k in keys)


@pytest.mark.asyncio
async def test_remux_fails_on_bad_input(ffmpeg, fmp4_run):
    init, segs = fmp4_run(44100, 2)
    assert await remux(ffmpeg, init, b"not a fragment", 10) is None
    remuxer = Remuxer(ffmpeg, init)
    first = await remuxer.remux(segs[0], 10)
    assert first is not None
    assert await remuxer.remux(b"not a fragment", 10) is None
    # a failed segment leaves the stream as it was: counters run on from the first
    second = await remuxer.remux(segs[1], 10)
    assert second is not None
    assert _counter_jumps(first + second) == 0


def _timescales(init: bytes) -> dict[int, int]:
    scales = {}
    moov = mp4info._first(init, 0, len(init), [b"moov"])
    assert moov is not None
    for lo, hi in mp4info._find(init, *moov, [b"trak"]):
        tkhd = mp4info._first(init, lo, hi, [b"tkhd"])
        mdhd = mp4info._first(init, lo, hi, [b"mdia", b"mdhd"])
        assert tkhd is not None and mdhd is not None and init[tkhd[0]] == init[mdhd[0]] == 0
        track = int.from_bytes(init[tkhd[0] + 12 : tkhd[0] + 16], "big")
        scales[track] = int.from_bytes(init[mdhd[0] + 12 : mdhd[0] + 16], "big")
    return scales


def _looped(init: bytes, segs: list[bytes], count: int) -> list[bytes]:
    """count segments repeating segs, each pass moved on in time like a live stream."""
    scales = _timescales(init)
    out = []
    for i in range(count):
        passes, seg = divmod(i, len(segs))
        data = segs[seg]
        buf = bytearray(data)
        for lo, hi in mp4info._find(data, 0, len(data), [b"moof", b"traf"]):
            tfhd = mp4info._first(data, lo, hi, [b"tfhd"])
            tfdt = mp4info._first(data, lo, hi, [b"tfdt"])
            assert tfhd is not None and tfdt is not None
            scale = scales[int.from_bytes(data[tfhd[0] + 4 : tfhd[0] + 8], "big")]
            width = 8 if data[tfdt[0]] == 1 else 4
            pos = tfdt[0] + 4
            value = int.from_bytes(data[pos : pos + width], "big")
            buf[pos : pos + width] = (value + passes * len(segs) * 2 * scale).to_bytes(width, "big")
        out.append(bytes(buf))
    return out


# the nix dev shell's ps on macOS lacks rss
_PS = "/bin/ps" if Path("/bin/ps").exists() else "ps"


def _rss_mib(pid: int) -> float:
    ps = subprocess.run([_PS, "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return int(ps.stdout) / 1024


@pytest.mark.asyncio
async def test_long_fmp4_run_keeps_the_converter_memory_flat(ffmpeg, fmp4_run):
    # a long-lived mov demuxer indexes every sample it reads: ~5 MiB over this run
    init, segs = fmp4_run(44100, 6, chunked=True)
    looped = _looped(init, segs, 600)
    conv, outputs, _ = await _run(ffmpeg, [])
    counters: dict[int, int] = {}
    rss = []
    for start in range(0, len(looped), 10):
        # remuxes run ahead in parallel; their outputs join in order
        batch = looped[start : start + 10]
        for out in await asyncio.gather(*(remux(ffmpeg, init, seg, 10) for seg in batch)):
            assert out is not None
            ts = continue_stream(out, counters)
            conv.register(ts)
            await conv.feed(ts)
        if start in (100, len(looped) - 10):
            await asyncio.sleep(0.2)
            assert conv._proc is not None
            rss.append(_rss_mib(conv._proc.pid))
    assert not conv.failed and len(outputs.got) > 500
    await conv.close()
    assert rss[1] - rss[0] < 1.5, rss
