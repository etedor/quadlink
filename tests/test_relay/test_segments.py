"""Tests for the fetch-once segment proxy."""

import asyncio
import gc
import subprocess

import pytest
import structlog.testing

from quadlink.relay import convert, segments
from quadlink.relay.convert import AudioConverter, remux
from quadlink.relay.playlist import Segment
from quadlink.relay.segments import SegmentProxy
from quadlink.relay.tsinfo import _VIDEO_TYPES, _pes_starts, _pid, audio_sample_rate


def _seg(seq: int, uri: str, disc: bool = False) -> Segment:
    return Segment(
        seq=seq, uri=uri, duration=2.0, program_date_time=None, discontinuity=disc, map_gen=None
    )


def _video_frames(data: bytes) -> int:
    pid = _pid(data, _VIDEO_TYPES)
    return 0 if pid is None else sum(1 for _ in _pes_starts(data, pid))


def _source(segs: list[bytes]):
    calls: list[str] = []

    async def fetch(uri: str) -> bytes:
        calls.append(uri)
        return segs[int(uri)]

    return fetch, calls


@pytest.mark.asyncio
async def test_48k_passes_through_and_is_fetched_once(ffmpeg, ts_run):
    segs = ts_run(48000, 2)
    fetch, calls = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "0"))
    first = await proxy.get(1, 0)
    again = await asyncio.gather(*(proxy.get(1, 0) for _ in range(5)))
    assert first == segs[0]
    assert all(b == segs[0] for b in again)
    assert calls == ["0"]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_441k_run_is_served_converted(ffmpeg, ts_run):
    segs = ts_run(44100, 3)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    for i in range(3):
        proxy.register(1, _seg(i, str(i)))
    out = await proxy.get(1, 0)
    assert out is not None and audio_sample_rate(out) == 48000
    assert proxy.ready(1, 0)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_without_ffmpeg_441k_passes_through(ts_run):
    segs = ts_run(44100, 1)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, None)
    proxy.register(1, _seg(0, "0"))
    assert await proxy.get(1, 0) == segs[0]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_wait_ready_returns_once_ready_or_at_timeout(ffmpeg, ts_run):
    segs = ts_run(48000, 2)
    gate = asyncio.Event()

    async def fetch(uri: str) -> bytes:
        await gate.wait()
        return segs[int(uri)]

    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    assert not await proxy.wait_ready(1, 0, 0.1)
    gate.set()
    assert await proxy.wait_ready(1, 0, 2.0)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_unknown_segment_is_none():
    async def fetch(uri: str) -> bytes:
        raise AssertionError("not fetched")

    proxy = SegmentProxy(fetch, None)
    assert await proxy.get(1, 99) is None
    await proxy.aclose()


@pytest.mark.asyncio
async def test_converter_crash_falls_back_to_raw(ffmpeg, ts_run, monkeypatch):
    """ffmpeg dying mid-feed must not wedge the slot: the segment is served raw."""
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)

    async def boom(self: AudioConverter, data: bytes) -> None:
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(AudioConverter, "feed", boom)

    proxy.register(1, _seg(0, "0"))
    out = await proxy.get(1, 0)
    assert out == segs[0]

    # the run keeps going unconverted after the crash, not stuck forever
    proxy.register(1, _seg(1, "1"))
    out1 = await proxy.get(1, 1)
    assert out1 == segs[1]
    await proxy.aclose()


def _fmp4(seq: int, uri: str, disc: bool = False, map_gen: int = 1) -> Segment:
    return Segment(
        seq=seq,
        uri=uri,
        duration=2.0,
        program_date_time=None,
        discontinuity=disc,
        map_gen=map_gen,
        map_uri=f"init{map_gen}",
    )


def _fmp4_source(init: bytes, segs: list[bytes], ts: list[bytes] | None = None):
    """A fetch serving fMP4 segments at "0".., TS ones at "ts0".., the init at "init*"."""
    calls: list[str] = []

    async def fetch(uri: str) -> bytes:
        calls.append(uri)
        if uri.startswith("init"):
            return init
        if uri.startswith("ts"):
            assert ts is not None
            return ts[int(uri[2:])]
        return segs[int(uri)]

    return fetch, calls


def _gated(segs: list[bytes]):
    """A fetch that holds each URI until its gate opens."""
    gates = {str(i): asyncio.Event() for i in range(len(segs))}

    async def fetch(uri: str) -> bytes:
        await gates[uri].wait()
        return segs[int(uri)]

    return fetch, gates


async def _fetched(proxy: SegmentProxy, slot: int, seq: int) -> None:
    async with asyncio.timeout(10):
        while not proxy._slots[slot].entries[seq].raw.done():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_start_failure_serves_raw(ffmpeg, ts_run, monkeypatch):
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)

    async def boom(self: AudioConverter) -> None:
        raise OSError("no ffmpeg")

    monkeypatch.setattr(AudioConverter, "start", boom)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    assert await proxy.get(1, 0) == segs[0]
    assert await proxy.get(1, 1) == segs[1]
    assert proxy.ready(1, 0) and proxy.ready(1, 1)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_hung_feed_times_out_to_raw(ffmpeg, ts_run, monkeypatch):
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    monkeypatch.setattr("quadlink.relay.segments.FEED_WAIT", 0.2)

    async def hang(self: AudioConverter, data: bytes) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(AudioConverter, "feed", hang)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    async with asyncio.timeout(5):
        assert await proxy.get(1, 0) == segs[0]
        assert await proxy.get(1, 1) == segs[1]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_discontinuity_keeps_old_run_served_converted(ffmpeg, ts_run):
    """The old run's outputs, including its last (only flushed at EOF), outlive its ffmpeg."""
    old = ts_run(44100, 3)
    new = ts_run(48000, 1)
    segs = [*old, *new]
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    for i in range(3):
        proxy.register(1, _seg(i, str(i)))
    proxy.register(1, _seg(3, "3", disc=True))
    assert await proxy.get(1, 3) == new[0]
    async with asyncio.timeout(10):
        while not proxy.ready(1, 2):
            await asyncio.sleep(0.05)
    for i in range(3):
        out = await proxy.get(1, i)
        assert out is not None and out != old[i]
        assert audio_sample_rate(out) == 48000
    async with asyncio.timeout(10):
        while proxy._retiring:
            await asyncio.sleep(0.05)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_discontinuity_starts_new_converter(ffmpeg, ts_run):
    first = ts_run(44100, 2)
    # the new run's first output completes only once its next segment is in
    second = ts_run(44100, 3)
    segs = [*first, *second]
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    proxy.register(1, _seg(2, "2", disc=True))
    proxy.register(1, _seg(3, "3"))
    proxy.register(1, _seg(4, "4"))
    for i in range(3):
        out = await proxy.get(1, i)
        assert out is not None and audio_sample_rate(out) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_pts_step_back_without_discontinuity_starts_a_new_run(ffmpeg, ts_run):
    """A repeated stretch must not pile up behind the old run's head output."""
    segs = ts_run(44100, 4)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    order = [0, 1, 2, 3] * 3
    for seq, i in enumerate(order):
        proxy.register(1, _seg(seq, str(i)))
    loop = asyncio.get_running_loop()
    # the last has no next segment or flush to close it
    for seq in range(len(order) - 1):
        start = loop.time()
        out = await proxy.get(1, seq)
        assert out is not None and audio_sample_rate(out) == 48000, seq
        assert loop.time() - start < 3, seq
    state = proxy._slots[1]
    assert state.run == 3
    assert state.converter is not None and state.converter._splitter.size < 4 << 20
    await proxy.aclose()


@pytest.mark.asyncio
async def test_without_ffmpeg_fmp4_passes_through():
    fetch, calls = _fmp4_source(b"init", [b"fmp4 bytes"])
    proxy = SegmentProxy(fetch, None)
    assert not proxy.converts_fmp4
    proxy.register(1, _fmp4(0, "0"))
    assert await proxy.get(1, 0) == b"fmp4 bytes"
    assert calls == ["0"]
    await proxy.aclose()


def _ts_frames(data: bytes) -> list[int]:
    pid = _pid(data, _VIDEO_TYPES)
    assert pid is not None
    return sorted(pts for pts, _ in _pes_starts(data, pid) if pts is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [44100, 48000])
async def test_fmp4_run_is_served_converted_ts(ffmpeg, fmp4_run, rate):
    init, segs = fmp4_run(rate, 3, chunked=True)
    fetch, calls = _fmp4_source(init, segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    assert proxy.converts_fmp4
    for i in range(3):
        proxy.register(1, _fmp4(i, str(i)))
    # the run's last output needs EOF
    proxy.flush(1)
    outs = await asyncio.gather(*(proxy.get(1, i) for i in range(3)))
    for seg, out in zip(segs, outs, strict=True):
        assert out is not None and out[0] == 0x47
        assert audio_sample_rate(out) == 48000
        alone = await remux(ffmpeg, init, seg, 10)
        assert alone is not None and _ts_frames(out) == _ts_frames(alone)
    # the init is fetched once for its generation
    assert calls.count("init1") == 1
    assert proxy._slots[1].run == 1
    await proxy.aclose()


@pytest.mark.asyncio
async def test_switch_between_ts_and_fmp4_starts_a_new_run(ffmpeg, ts_run, fmp4_run):
    ts = ts_run(44100, 2)
    init, segs = fmp4_run(44100, 2, offset=100)
    fetch, _ = _fmp4_source(init, segs, ts)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "ts0"))
    proxy.register(1, _seg(1, "ts1"))
    proxy.register(1, _fmp4(2, "0", disc=True))
    proxy.register(1, _fmp4(3, "1"))
    proxy.register(1, _seg(4, "ts0", disc=True))
    proxy.flush(1)
    outs = await asyncio.gather(*(proxy.get(1, i) for i in range(5)))
    assert all(out is not None and audio_sample_rate(out) == 48000 for out in outs)
    assert proxy._slots[1].run == 3
    await proxy.aclose()


@pytest.mark.asyncio
async def test_failed_fmp4_conversion_serves_a_ts_remux(ffmpeg, fmp4_run, monkeypatch):
    init, segs = fmp4_run(44100, 2)
    fetch, _ = _fmp4_source(init, segs)

    async def boom(self: AudioConverter, data: bytes) -> None:
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(AudioConverter, "feed", boom)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _fmp4(0, "0"))
    proxy.register(1, _fmp4(1, "1"))
    with structlog.testing.capture_logs() as logs:
        outs = await asyncio.gather(proxy.get(1, 0), proxy.get(1, 0), proxy.get(1, 1))
    for seg, out in zip([segs[0], segs[0], segs[1]], outs, strict=True):
        assert out is not None and out[0] == 0x47
        # streams copied: still the source rate, but decodable TS
        assert audio_sample_rate(out) == 44100
        alone = await remux(ffmpeg, init, seg, 10)
        assert alone is not None and _ts_frames(out) == _ts_frames(alone)
        decode = subprocess.run(
            [ffmpeg, "-v", "error", "-i", "pipe:0", "-f", "null", "-"],
            input=out,
            capture_output=True,
            check=True,
        )
        assert decode.stderr == b""
    assert not any("filler" in log["event"] for log in logs)
    await proxy.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("hang", [False, True], ids=["fails", "hangs"])
async def test_fmp4_remux_failure_asks_for_filler(ffmpeg, fmp4_run, monkeypatch, hang):
    init, segs = fmp4_run(44100, 2)
    fetch, _ = _fmp4_source(init, segs)
    real_remux = convert.remux

    async def bad_remux(ffmpeg: str, init: bytes, data: bytes, timeout: float) -> bytes | None:
        if data != segs[0]:
            return await real_remux(ffmpeg, init, data, timeout)
        if hang:
            await asyncio.sleep(60)
        return None

    monkeypatch.setattr(segments, "REMUX_WAIT", 0.3)
    monkeypatch.setattr(convert, "remux", bad_remux)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _fmp4(0, "0"))
    proxy.register(1, _fmp4(1, "1"))
    proxy.flush(1)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with structlog.testing.capture_logs() as logs:
        assert await proxy.get(1, 0) == b""
    # bounded by REMUX_WAIT, so the tile gets filler before its buffer runs out
    assert loop.time() - start < 1
    assert any("filler" in log["event"] for log in logs)
    # the next segment starts a new run and is converted
    out = await proxy.get(1, 1)
    assert out is not None and audio_sample_rate(out) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_fmp4_without_its_init_asks_for_filler(ffmpeg, fmp4_run):
    init, segs = fmp4_run(44100, 2)
    fetch, calls = _fmp4_source(init, segs)

    async def no_init(uri: str) -> bytes:
        if uri.startswith("init"):
            calls.append(uri)
            raise RuntimeError("403")
        return await fetch(uri)

    proxy = SegmentProxy(no_init, ffmpeg)
    proxy.register(1, _fmp4(0, "0"))
    proxy.register(1, _fmp4(1, "1"))
    assert await proxy.get(1, 0) == b""
    assert await proxy.get(1, 1) == b""
    # a failed init fetch is tried twice; the next segment, within INIT_BACKOFF, skips it
    assert calls.count("init1") == 2
    await proxy.aclose()


@pytest.mark.asyncio
async def test_ready_without_ffmpeg_never_holds_back(ts_run):
    segs = ts_run(44100, 1)
    fetch, gates = _gated(segs)
    proxy = SegmentProxy(fetch, None)
    proxy.register(1, _seg(0, "0"))
    assert proxy.ready(1, 0)
    gates["0"].set()
    await _fetched(proxy, 1, 0)
    assert proxy.ready(1, 0)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_ready_fmp4_without_ffmpeg_is_not_held_back():
    fetch, gates = _gated([b"fmp4 bytes"])
    proxy = SegmentProxy(fetch, None)
    proxy.register(1, _fmp4(0, "0"))
    assert proxy.ready(1, 0)
    gates["0"].set()
    await _fetched(proxy, 1, 0)
    assert proxy.ready(1, 0)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_ready_converting_fmp4_run(ffmpeg, fmp4_run):
    init, segs = fmp4_run(44100, 3)
    fetch, _ = _fmp4_source(init, segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _fmp4(0, "0"))
    # undecided: it may convert
    assert not proxy.ready(1, 0)
    proxy.register(1, _fmp4(1, "1"))
    assert await proxy.wait_ready(1, 0, 5)
    assert not proxy.ready(1, 1)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_ready_converting_run(ffmpeg, ts_run):
    segs = ts_run(44100, 3)
    fetch, gates = _gated(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    for i in range(3):
        proxy.register(1, _seg(i, str(i)))
    # unfetched, first of run: conversion not decided yet
    assert not proxy.ready(1, 0)
    gates["0"].set()
    await _fetched(proxy, 1, 0)
    # fetched and converting, but ffmpeg closes an output only at the next keyframe
    assert not proxy.ready(1, 0)
    # unfetched, mid-run of a converting run
    assert not proxy.ready(1, 1)
    gates["1"].set()
    gates["2"].set()
    async with asyncio.timeout(10):
        while not proxy.ready(1, 0):
            await asyncio.sleep(0.05)
    out = await proxy.get(1, 0)
    assert out is not None and audio_sample_rate(out) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_native_48k_run_is_reencoded(ffmpeg, ts_run):
    segs = ts_run(48000, 3)

    async def fetch(uri: str) -> bytes:
        return segs[int(uri)]

    proxy = SegmentProxy(fetch, ffmpeg)
    for n in range(3):
        proxy.register(1, _seg(n, str(n)))
    data = await proxy.get(1, 0)
    assert data is not None and data != segs[0]
    assert audio_sample_rate(data) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_aclose_fails_pending_requests():
    async def fetch(uri: str) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError

    proxy = SegmentProxy(fetch, None)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    waiting = asyncio.create_task(proxy.get(1, 0))
    await asyncio.sleep(0)
    await proxy.aclose()
    async with asyncio.timeout(1):
        assert await waiting is None


@pytest.mark.asyncio
async def test_unrequested_fetch_failure_is_quiet():
    reported: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: reported.append(ctx))

    async def fetch(uri: str) -> bytes:
        raise OSError("gone")

    proxy = SegmentProxy(fetch, None)
    proxy.register(1, _seg(0, "0"))
    async with asyncio.timeout(1):
        while not proxy._slots[1].entries[0].raw.done():
            await asyncio.sleep(0.01)
    await proxy.aclose()
    gc.collect()
    assert reported == []


@pytest.mark.asyncio
async def test_spaced_registrations_keep_one_converter(ffmpeg, ts_run, monkeypatch):
    """Client polls leave seconds between registrations; the run must not restart."""
    loop = asyncio.get_running_loop()
    real_time = loop.time
    skew = [0.0]
    # a fake clock: jumping it fires any loop timer, as real waiting would
    monkeypatch.setattr(loop, "time", lambda: real_time() + skew[0])
    starts: list[AudioConverter] = []
    real_start = AudioConverter.start

    async def counted(self: AudioConverter) -> None:
        starts.append(self)
        await real_start(self)

    monkeypatch.setattr(AudioConverter, "start", counted)
    segs = ts_run(44100, 4)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    for i in range(4):
        proxy.register(1, _seg(i, str(i)))
        await _fetched(proxy, 1, i)
        skew[0] += 3.0
        await asyncio.sleep(0.05)
    assert len(starts) == 1
    assert proxy._slots[1].converter is starts[0]
    for i in range(2):
        out = await proxy.get(1, i)
        assert out is not None and audio_sample_rate(out) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_flush_converts_the_tail_promptly(ffmpeg, ts_run):
    """Filler lists a run's tail, which ffmpeg holds until EOF; flush must release it."""
    segs = ts_run(44100, 3)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    for i in range(2):
        proxy.register(1, _seg(i, str(i)))
    await _fetched(proxy, 1, 1)
    # two segments in: ffmpeg holds both outputs until more input or EOF
    assert not proxy.ready(1, 1)
    proxy.flush(1)
    loop = asyncio.get_running_loop()
    start = loop.time()
    out = await proxy.get(1, 1)
    assert loop.time() - start < 2.0
    assert out is not None and out != segs[1]
    assert audio_sample_rate(out) == 48000
    async with asyncio.timeout(3):
        while not (proxy.ready(1, 0) and proxy.ready(1, 1)):
            await asyncio.sleep(0.05)
    out = await proxy.get(1, 0)
    assert out is not None and audio_sample_rate(out) == 48000
    # the next segment starts a fresh converter, undecided until it is fetched
    proxy.register(1, _seg(2, "2"))
    assert not proxy.ready(1, 2)
    await _fetched(proxy, 1, 2)
    assert proxy._slots[1].converter is not None
    await proxy.aclose()


@pytest.mark.asyncio
async def test_flush_during_first_fetch_still_ends_the_run(ffmpeg, ts_run):
    """A flush while the run's first segment is still fetching must reach its converter."""
    segs = ts_run(44100, 1)

    async def fetch(uri: str) -> bytes:
        await asyncio.sleep(0.2)
        return segs[int(uri)]

    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "0"))
    await asyncio.sleep(0.05)
    proxy.flush(1)
    async with asyncio.timeout(3):
        while not proxy.ready(1, 0):
            await asyncio.sleep(0.05)
    out = await proxy.get(1, 0)
    assert out is not None and audio_sample_rate(out) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_run_start_is_served_converted_once_next_segment_arrives(ffmpeg, ts_run):
    """The playlist lists a run's first segment before its output exists; get() waits."""
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "0"))
    await _fetched(proxy, 1, 0)
    request = asyncio.create_task(proxy.get(1, 0))
    await asyncio.sleep(0.3)
    assert not request.done()
    loop = asyncio.get_running_loop()
    start = loop.time()
    proxy.register(1, _seg(1, "1"))
    out = await request
    assert loop.time() - start < 2.0
    assert out is not None and audio_sample_rate(out) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_flush_without_converter_is_noop(ts_run):
    segs = ts_run(44100, 1)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, None)
    proxy.flush(1)
    proxy.register(1, _seg(0, "0"))
    await _fetched(proxy, 1, 0)
    proxy.flush(1)
    assert await proxy.get(1, 0) == segs[0]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_evicted_entry_still_gets_harvested_output(ffmpeg, ts_run):
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, keep=2)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    await _fetched(proxy, 1, 1)
    waiting = asyncio.create_task(proxy.get(1, 0))
    await asyncio.sleep(0.05)
    # evicted while its client waits on the converter
    proxy._slots[1].entries.popitem(last=False)
    proxy.flush(1)
    async with asyncio.timeout(3):
        out = await waiting
    assert out is not None and audio_sample_rate(out) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_harvest_failure_is_logged(ffmpeg, ts_run, monkeypatch):
    reported: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: reported.append(ctx))

    async def boom(self: AudioConverter) -> None:
        raise RuntimeError("wedged")

    monkeypatch.setattr(AudioConverter, "finish", boom)
    segs = ts_run(44100, 1)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "0"))
    await _fetched(proxy, 1, 0)
    proxy.flush(1)
    with structlog.testing.capture_logs() as logs:
        async with asyncio.timeout(3):
            while not any(e["event"] == "relay conversion failed" for e in logs):
                await asyncio.sleep(0.05)
    await proxy.aclose()
    gc.collect()
    assert reported == []


@pytest.mark.asyncio
async def test_long_run_keeps_outputs_bounded(ffmpeg, ts_run):
    segs = ts_run(44100, 16)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, keep=4)
    for i in range(len(segs)):
        proxy.register(1, _seg(i, str(i)))
        await _fetched(proxy, 1, i)
        if i:
            assert await proxy.get(1, i - 1) is not None
    conv = proxy._slots[1].converter
    assert conv is not None
    # only the newest output is still being written; the rest live in the entries
    assert len(conv._splitter.pending) == 1
    assert len(conv._splitter._buf) < 2 * max(len(s) for s in segs)
    assert sum(e.converted is not None for e in proxy._slots[1].entries.values()) <= 4
    await proxy.aclose()


@pytest.mark.asyncio
async def test_long_run_with_mid_segment_keyframes_stays_bounded(ffmpeg, ts_run):
    """Extra keyframes split each segment into several outputs; all are served and deleted."""
    segs = ts_run(44100, 16, (1.2, 1.6))
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, keep=4)
    for i in range(len(segs)):
        proxy.register(1, _seg(i, str(i)))
        await _fetched(proxy, 1, i)
        if i:
            out = await proxy.get(1, i - 1)
            assert out is not None and audio_sample_rate(out) == 48000
            # every video frame of the input, not just those up to the first extra keyframe
            assert _video_frames(out) == _video_frames(segs[i - 1])
    conv = proxy._slots[1].converter
    assert conv is not None
    assert len(conv._splitter.pending) == 1
    assert len(conv._splitter._buf) < 2 * max(len(s) for s in segs)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_converted_entry_releases_its_raw_bytes(ffmpeg, ts_run):
    segs = ts_run(44100, 3)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    for i in range(3):
        proxy.register(1, _seg(i, str(i)))
    out = await proxy.get(1, 0)
    assert out is not None and audio_sample_rate(out) == 48000
    assert proxy._slots[1].entries[0].raw.result() == b""
    assert await proxy.get(1, 0) == out
    await proxy.aclose()


@pytest.mark.asyncio
async def test_evicted_unrequested_output_is_dropped(ffmpeg, ts_run):
    segs = ts_run(44100, 4)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, keep=2)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    state = proxy._slots[1]
    evicted = list(state.entries.values())
    proxy.register(1, _seg(2, "2"))
    proxy.register(1, _seg(3, "3"))
    await _fetched(proxy, 1, 3)
    assert list(state.entries) == [2, 3]
    out = await proxy.get(1, 2)
    assert out is not None and audio_sample_rate(out) == 48000
    # nothing outside the cache takes a converted output
    assert all(e.converted is None and e.converter is None for e in evicted)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_waiting_client_wakes_as_soon_as_output_completes(ffmpeg, ts_run):
    segs = ts_run(44100, 2)
    fetch, gates = _gated(segs)
    proxy = SegmentProxy(fetch, ffmpeg)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    gates["0"].set()
    await _fetched(proxy, 1, 0)
    request = asyncio.create_task(proxy.get(1, 0))
    ready = asyncio.create_task(proxy.wait_ready(1, 0, 5.0))
    await asyncio.sleep(0.2)
    assert not request.done() and not ready.done()
    loop = asyncio.get_running_loop()
    start = loop.time()
    gates["1"].set()
    out = await request
    assert await ready
    # the next segment's keyframe completes the output as soon as ffmpeg reads it
    assert loop.time() - start < 1.0
    assert out is not None and audio_sample_rate(out) == 48000
    await proxy.aclose()


@pytest.mark.asyncio
async def test_empty_output_serves_raw():
    async def fetch(uri: str) -> bytes:
        return b"raw"

    proxy = SegmentProxy(fetch, None)
    proxy.register(1, _seg(0, "0"))
    await _fetched(proxy, 1, 0)
    state = proxy._slots[1]
    entry = state.entries[0]
    conv = object()
    entry.converter, entry.pts = conv, 5  # type: ignore[assignment]
    SegmentProxy._deliver(state, conv, 5, b"")  # type: ignore[arg-type]
    assert await proxy.get(1, 0) == b"raw"
    await proxy.aclose()


def _init_seg(seq: int, map_gen: int, map_uri: str) -> Segment:
    return Segment(
        seq=seq,
        uri=str(seq),
        duration=2.0,
        program_date_time=None,
        discontinuity=False,
        map_gen=map_gen,
        map_uri=map_uri,
    )


def _init_source(fail: set[str]):
    calls: list[str] = []

    async def fetch(uri: str) -> bytes:
        calls.append(uri)
        if uri in fail:
            raise RuntimeError("403")
        return f"init:{uri}".encode()

    return fetch, calls


@pytest.mark.asyncio
async def test_init_fetch_failure_reuses_the_init_at_the_same_path():
    fetch, calls = _init_source({"https://cdn/init.mp4?t=2", "https://cdn/other.mp4?t=3"})
    proxy = SegmentProxy(fetch, None)
    state = segments._Slot()
    first = await proxy._init(1, state, _init_seg(0, 1, "https://cdn/init.mp4?t=1"))
    assert first == b"init:https://cdn/init.mp4?t=1"
    # a new generation at the same path, its fetch failing twice
    assert await proxy._init(1, state, _init_seg(1, 2, "https://cdn/init.mp4?t=2")) is first
    assert calls.count("https://cdn/init.mp4?t=2") == 2
    # cached for the generation: no more fetches
    assert await proxy._init(1, state, _init_seg(2, 2, "https://cdn/init.mp4?t=2")) is first
    assert calls.count("https://cdn/init.mp4?t=2") == 2
    # another path is a different init, so nothing is reused
    assert await proxy._init(1, state, _init_seg(3, 3, "https://cdn/other.mp4?t=3")) is None


@pytest.mark.asyncio
async def test_init_retry_after_one_failure():
    failed: list[str] = []

    async def fetch(uri: str) -> bytes:
        if not failed:
            failed.append(uri)
            raise RuntimeError("timeout")
        return b"init"

    proxy = SegmentProxy(fetch, None)
    state = segments._Slot()
    assert await proxy._init(1, state, _init_seg(0, 1, "https://cdn/init.mp4")) == b"init"


@pytest.mark.asyncio
async def test_new_init_path_within_a_generation_is_fetched():
    fetch, calls = _init_source(set())
    proxy = SegmentProxy(fetch, None)
    state = segments._Slot()
    a = await proxy._init(1, state, _init_seg(0, 1, "https://cdn/a.mp4?t=1"))
    # token churn alone keeps the cached init
    assert await proxy._init(1, state, _init_seg(1, 1, "https://cdn/a.mp4?t=2")) is a
    b = await proxy._init(1, state, _init_seg(2, 1, "https://cdn/b.mp4?t=2"))
    assert b == b"init:https://cdn/b.mp4?t=2"
    assert calls == ["https://cdn/a.mp4?t=1", "https://cdn/b.mp4?t=2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("changed", "runs"), [(False, 1), (True, 2)], ids=["same", "changed"])
async def test_new_init_path_restarts_the_run_only_for_new_bytes(ffmpeg, fmp4_run, changed, runs):
    init, segs = fmp4_run(44100, 3, chunked=True)
    # a top-level free box: the same media, different bytes
    moved = init + b"\x00\x00\x00\x08free" if changed else init

    async def fetch(uri: str) -> bytes:
        # a fresh object per fetch, as from the network
        if uri.startswith("init"):
            return bytes(bytearray(moved if uri == "init-moved" else init))
        return segs[int(uri)]

    proxy = SegmentProxy(fetch, ffmpeg)
    seg0, seg1, seg2 = (_fmp4(i, str(i)) for i in range(3))
    seg2.map_uri = "init-moved"
    for seg in (seg0, seg1, seg2):
        proxy.register(1, seg)
    proxy.flush(1)
    outs = await asyncio.gather(*(proxy.get(1, i) for i in range(3)))
    assert all(out is not None and audio_sample_rate(out) == 48000 for out in outs)
    assert proxy._slots[1].run == runs
    await proxy.aclose()


@pytest.mark.asyncio
async def test_init_attempts_share_one_deadline(monkeypatch):
    monkeypatch.setattr(segments, "INIT_WAIT", 0.3)
    calls: list[str] = []

    async def hang(uri: str) -> bytes:
        calls.append(uri)
        await asyncio.sleep(10)
        return b"init"

    proxy = SegmentProxy(hang, None)
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await proxy._init(1, segments._Slot(), _init_seg(0, 1, "https://cdn/init.mp4")) is None
    assert loop.time() - start == pytest.approx(0.3, abs=0.1)
    # a slow failure gets no retry
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_slow_init_failure_is_not_retried(monkeypatch):
    monkeypatch.setattr(segments, "INIT_RETRY_UNDER", 0.05)
    calls: list[str] = []

    async def slow_fail(uri: str) -> bytes:
        calls.append(uri)
        await asyncio.sleep(0.1)
        raise RuntimeError("503")

    proxy = SegmentProxy(slow_fail, None)
    assert await proxy._init(1, segments._Slot(), _init_seg(0, 1, "https://cdn/init.mp4")) is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_init_is_retried_only_after_the_backoff(monkeypatch):
    monkeypatch.setattr(segments, "INIT_BACKOFF", 0.2)
    fetch, calls = _init_source({"https://cdn/init.mp4"})
    proxy = SegmentProxy(fetch, None)
    state = segments._Slot()
    assert await proxy._init(1, state, _init_seg(0, 1, "https://cdn/init.mp4")) is None
    assert len(calls) == 2
    assert await proxy._init(1, state, _init_seg(1, 1, "https://cdn/init.mp4")) is None
    assert len(calls) == 2
    # another generation is a different init, so it gets its own attempts
    assert await proxy._init(1, state, _init_seg(2, 2, "https://cdn/init.mp4")) is None
    assert len(calls) == 4
    await asyncio.sleep(0.25)
    assert await proxy._init(1, state, _init_seg(3, 2, "https://cdn/init.mp4")) is None
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_late_converter_output_never_replaces_served_bytes():
    async def fetch(uri: str) -> bytes:
        return b"raw"

    proxy = SegmentProxy(fetch, None)
    proxy.register(1, _seg(0, "0"))
    await _fetched(proxy, 1, 0)
    state = proxy._slots[1]
    entry = state.entries[0]
    conv = object()
    entry.converter, entry.pts = conv, 5  # type: ignore[assignment]
    entry.set_converted(b"remuxed")
    SegmentProxy._deliver(state, conv, 5, b"late")  # type: ignore[arg-type]
    assert await proxy.get(1, 0) == b"remuxed"
    await proxy.aclose()


@pytest.mark.asyncio
async def test_late_fmp4_conversion_and_remux_fit_convert_wait(monkeypatch):
    monkeypatch.setattr(segments, "CONVERT_WAIT", 0.6)
    monkeypatch.setattr(segments, "REMUX_WAIT", 0.4)
    loop = asyncio.get_running_loop()

    async def slow_remux(ffmpeg: str, init: bytes, data: bytes, timeout: float) -> bytes:
        await asyncio.sleep(0.3)
        return b"ts"

    async def nothing(*args: object) -> None:
        pass

    # a converter that takes the segment and never delivers
    monkeypatch.setattr(convert, "remux", slow_remux)
    monkeypatch.setattr(segments, "audio_sample_rate", lambda data: 44100)
    monkeypatch.setattr(AudioConverter, "start", nothing)
    monkeypatch.setattr(AudioConverter, "feed", nothing)
    monkeypatch.setattr(AudioConverter, "register", lambda self, data: 5)

    async def fetch(uri: str) -> bytes:
        return b"init" if uri.startswith("init") else b"fmp4"

    proxy = SegmentProxy(fetch, "ffmpeg")
    start = loop.time()
    proxy.register(1, _fmp4(0, "0"))
    assert await proxy.get(1, 0) == b"ts"
    # the remux, then CONVERT_WAIT less the REMUX_WAIT it may take
    assert loop.time() - start == pytest.approx(0.5, abs=0.1)
    await proxy.aclose()
