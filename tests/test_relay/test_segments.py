"""Tests for the fetch-once segment proxy."""

import asyncio
import gc

import pytest
import structlog.testing

from quadlink.relay.convert import AudioConverter
from quadlink.relay.playlist import Segment
from quadlink.relay.segments import SegmentProxy
from quadlink.relay.tsinfo import audio_sample_rate


def _seg(seq: int, uri: str, disc: bool = False) -> Segment:
    return Segment(
        seq=seq, uri=uri, duration=2.0, program_date_time=None, discontinuity=disc, map_gen=None
    )


def _source(segs: list[bytes]):
    calls: list[str] = []

    async def fetch(uri: str) -> bytes:
        calls.append(uri)
        return segs[int(uri)]

    return fetch, calls


@pytest.mark.asyncio
async def test_48k_passes_through_and_is_fetched_once(ffmpeg, ts_run, tmp_path):
    segs = ts_run(48000, 2)
    fetch, calls = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
    proxy.register(1, _seg(0, "0"))
    first = await proxy.get(1, 0)
    again = await asyncio.gather(*(proxy.get(1, 0) for _ in range(5)))
    assert first == segs[0]
    assert all(b == segs[0] for b in again)
    assert calls == ["0"]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_441k_run_is_served_converted(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 3)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
    for i in range(3):
        proxy.register(1, _seg(i, str(i)))
    out = await proxy.get(1, 0)
    assert out is not None and audio_sample_rate(out) == 48000
    assert proxy.ready(1, 0)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_without_ffmpeg_441k_passes_through(ts_run, tmp_path):
    segs = ts_run(44100, 1)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, None, tmp_path)
    proxy.register(1, _seg(0, "0"))
    assert await proxy.get(1, 0) == segs[0]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_wait_ready_returns_once_ready_or_at_timeout(ffmpeg, ts_run, tmp_path):
    segs = ts_run(48000, 1)
    gate = asyncio.Event()

    async def fetch(uri: str) -> bytes:
        await gate.wait()
        return segs[int(uri)]

    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
    proxy.register(1, _seg(0, "0"))
    assert not await proxy.wait_ready(1, 0, 0.1)
    gate.set()
    assert await proxy.wait_ready(1, 0, 2.0)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_unknown_segment_is_none(tmp_path):
    async def fetch(uri: str) -> bytes:
        raise AssertionError("not fetched")

    proxy = SegmentProxy(fetch, None, tmp_path)
    assert await proxy.get(1, 99) is None
    await proxy.aclose()


@pytest.mark.asyncio
async def test_converter_crash_falls_back_to_raw(ffmpeg, ts_run, tmp_path, monkeypatch):
    """ffmpeg dying mid-feed must not wedge the slot: the segment is served raw."""
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)

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


def _fmp4(seq: int, uri: str) -> Segment:
    return Segment(
        seq=seq, uri=uri, duration=2.0, program_date_time=None, discontinuity=False, map_gen=0
    )


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
async def test_start_failure_serves_raw(ffmpeg, ts_run, tmp_path, monkeypatch):
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)

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
async def test_hung_feed_times_out_to_raw(ffmpeg, ts_run, tmp_path, monkeypatch):
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    monkeypatch.setattr("quadlink.relay.segments.FEED_WAIT", 0.2)

    async def hang(self: AudioConverter, data: bytes) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(AudioConverter, "feed", hang)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    async with asyncio.timeout(5):
        assert await proxy.get(1, 0) == segs[0]
        assert await proxy.get(1, 1) == segs[1]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_discontinuity_keeps_old_run_served_converted(ffmpeg, ts_run, tmp_path):
    """The old run's outputs, including its last (only flushed at EOF), outlive its ffmpeg."""
    old = ts_run(44100, 3)
    new = ts_run(48000, 1)
    segs = [*old, *new]
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
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
        while (tmp_path / "slot1-run1").exists():
            await asyncio.sleep(0.05)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_discontinuity_starts_new_converter(ffmpeg, ts_run, tmp_path):
    first = ts_run(44100, 2)
    # ffmpeg closes an output once two more segments are in, so the new run needs three
    second = ts_run(44100, 3)
    segs = [*first, *second]
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
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
async def test_fmp4_is_never_fed(ffmpeg, ts_run, tmp_path, monkeypatch):
    segs = [*ts_run(44100, 1), b"fmp4 bytes"]
    fetch, _ = _source(segs)
    fed: list[bytes] = []
    real_feed = AudioConverter.feed

    async def record(self: AudioConverter, data: bytes) -> None:
        fed.append(data)
        await real_feed(self, data)

    monkeypatch.setattr(AudioConverter, "feed", record)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _fmp4(1, "1"))
    assert await proxy.get(1, 1) == segs[1]
    assert proxy.ready(1, 1)
    assert fed == [segs[0]]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_ready_without_ffmpeg_never_holds_back(ts_run, tmp_path):
    segs = ts_run(44100, 1)
    fetch, gates = _gated(segs)
    proxy = SegmentProxy(fetch, None, tmp_path)
    proxy.register(1, _seg(0, "0"))
    assert proxy.ready(1, 0)
    gates["0"].set()
    await _fetched(proxy, 1, 0)
    assert proxy.ready(1, 0)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_ready_fmp4_first_of_run_is_not_held_back(ffmpeg, tmp_path):
    fetch, gates = _gated([b"fmp4 bytes"])
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
    proxy.register(1, _fmp4(0, "0"))
    assert proxy.ready(1, 0)
    gates["0"].set()
    await _fetched(proxy, 1, 0)
    assert proxy.ready(1, 0)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_ready_converting_run(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 3)
    fetch, gates = _gated(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
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
async def test_ready_passthrough_run(ffmpeg, ts_run, tmp_path):
    segs = [*ts_run(48000, 2), *ts_run(44100, 1)]
    fetch, gates = _gated(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    proxy.register(1, _seg(2, "2", disc=True))
    assert not proxy.ready(1, 0)
    gates["0"].set()
    await _fetched(proxy, 1, 0)
    # fetched, not converting
    assert proxy.ready(1, 0)
    # unfetched, mid-run of a run decided not to convert
    assert proxy.ready(1, 1)
    # unfetched, first of a new run
    assert not proxy.ready(1, 2)
    await proxy.aclose()


@pytest.mark.asyncio
async def test_aclose_fails_pending_requests(tmp_path):
    async def fetch(uri: str) -> bytes:
        await asyncio.Event().wait()
        raise AssertionError

    proxy = SegmentProxy(fetch, None, tmp_path)
    proxy.register(1, _seg(0, "0"))
    proxy.register(1, _seg(1, "1"))
    waiting = asyncio.create_task(proxy.get(1, 0))
    await asyncio.sleep(0)
    await proxy.aclose()
    async with asyncio.timeout(1):
        assert await waiting is None


@pytest.mark.asyncio
async def test_unrequested_fetch_failure_is_quiet(tmp_path):
    reported: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: reported.append(ctx))

    async def fetch(uri: str) -> bytes:
        raise OSError("gone")

    proxy = SegmentProxy(fetch, None, tmp_path)
    proxy.register(1, _seg(0, "0"))
    async with asyncio.timeout(1):
        while not proxy._slots[1].entries[0].raw.done():
            await asyncio.sleep(0.01)
    await proxy.aclose()
    gc.collect()
    assert reported == []


@pytest.mark.asyncio
async def test_spaced_registrations_keep_one_converter(ffmpeg, ts_run, tmp_path, monkeypatch):
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
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
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
async def test_flush_converts_the_tail_promptly(ffmpeg, ts_run, tmp_path):
    """Filler lists a run's tail, which ffmpeg holds until EOF; flush must release it."""
    segs = ts_run(44100, 3)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
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
async def test_flush_without_converter_is_noop(ts_run, tmp_path):
    segs = ts_run(44100, 1)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, None, tmp_path)
    proxy.flush(1)
    proxy.register(1, _seg(0, "0"))
    await _fetched(proxy, 1, 0)
    proxy.flush(1)
    assert await proxy.get(1, 0) == segs[0]
    await proxy.aclose()


@pytest.mark.asyncio
async def test_evicted_entry_still_gets_harvested_output(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 2)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path, keep=2)
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
async def test_harvest_failure_is_logged(ffmpeg, ts_run, tmp_path, monkeypatch):
    reported: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: reported.append(ctx))

    async def boom(self: AudioConverter) -> None:
        raise RuntimeError("wedged")

    monkeypatch.setattr(AudioConverter, "finish", boom)
    segs = ts_run(44100, 1)
    fetch, _ = _source(segs)
    proxy = SegmentProxy(fetch, ffmpeg, tmp_path)
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
