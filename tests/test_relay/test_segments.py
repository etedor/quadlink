"""Tests for the fetch-once segment proxy."""

import asyncio

import pytest

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
