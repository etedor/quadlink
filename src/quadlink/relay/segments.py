"""Fetch each relay segment from Twitch once and serve it to every client on the LAN.

Segments are processed per slot in playlist order, because a converting run must
feed ffmpeg its input in sequence. A run is a stretch without a discontinuity.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from quadlink.relay.convert import TARGET_RATE, AudioConverter
from quadlink.relay.playlist import Segment
from quadlink.relay.tsinfo import audio_sample_rate, first_video_pts

logger = structlog.get_logger()

# seconds a client request waits for a converted segment before getting the original
CONVERT_WAIT = 6.0


@dataclass
class _Entry:
    seg: Segment
    raw: "asyncio.Future[bytes]"
    converter: AudioConverter | None = None
    converted: bytes | None = None


@dataclass
class _Slot:
    entries: "OrderedDict[int, _Entry]" = field(default_factory=OrderedDict)
    queue: "asyncio.Queue[_Entry]" = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None
    converter: AudioConverter | None = None
    run: int = 0


class SegmentProxy:
    def __init__(
        self,
        fetch: Callable[[str], Awaitable[bytes]],
        ffmpeg: str | None,
        workroot: Path,
        keep: int = 30,
    ) -> None:
        self._fetch = fetch
        self._ffmpeg = ffmpeg
        self._workroot = workroot
        self._keep = keep
        self._slots: dict[int, _Slot] = {}

    def register(self, slot: int, seg: Segment) -> None:
        """Queue a newly listed segment for fetching (and converting) in order."""
        state = self._slots.setdefault(slot, _Slot())
        if seg.filler or seg.seq in state.entries:
            return
        entry = _Entry(seg, asyncio.get_running_loop().create_future())
        state.entries[seg.seq] = entry
        while len(state.entries) > self._keep:
            state.entries.popitem(last=False)
        state.queue.put_nowait(entry)
        if state.worker is None or state.worker.done():
            state.worker = asyncio.create_task(self._work(slot, state))

    async def _work(self, slot: int, state: _Slot) -> None:
        while not state.queue.empty():
            entry = state.queue.get_nowait()
            try:
                data = await self._fetch(entry.seg.uri)
            except Exception as e:
                logger.warning(
                    "relay segment fetch failed", slot=slot, seq=entry.seg.seq, error=str(e)
                )
                if not entry.raw.done():
                    entry.raw.set_exception(e)
                continue
            # route decides entry.converter before raw is visible to get(), so a
            # waiting client never observes an entry mid-decision as unconverted
            await self._route(slot, state, entry, data)
            entry.raw.set_result(data)

    async def _route(self, slot: int, state: _Slot, entry: _Entry, data: bytes) -> None:
        ts = entry.seg.map_gen is None
        # each run decides conversion once, from its first segment's audio rate
        if entry.seg.discontinuity or state.run == 0:
            if state.converter is not None:
                await state.converter.close()
                state.converter = None
            state.run += 1
            rate = audio_sample_rate(data) if ts else None
            if self._ffmpeg and rate is not None and rate != TARGET_RATE:
                conv = AudioConverter(self._ffmpeg, self._workroot / f"slot{slot}-run{state.run}")
                try:
                    await conv.start()
                except Exception as e:
                    logger.warning("relay conversion failed", slot=slot, error=str(e))
                    await conv.close()
                else:
                    state.converter = conv
                    logger.info("relay converting audio", slot=slot, rate=rate)
        if state.converter is not None and ts:
            try:
                await state.converter.feed(data)
            except Exception as e:
                # ffmpeg can die mid-run (crash, broken pipe); never let that wedge
                # the slot's worker, so later segments of the run pass through raw
                logger.warning(
                    "relay conversion failed", slot=slot, seq=entry.seg.seq, error=str(e)
                )
                await state.converter.close()
                state.converter = None
            else:
                entry.converter = state.converter

    def ready(self, slot: int, seq: int) -> bool:
        """Whether a client asking for this segment now would be served at once.

        An unfetched segment counts as ready unless its slot is converting: a raw
        fetch takes well under a buffer, but a conversion waits for the next keyframe.
        """
        state = self._slots.get(slot)
        entry = state.entries.get(seq) if state is not None else None
        if entry is None:
            return True
        if not entry.raw.done():
            return state is None or state.converter is None
        return entry.converter is None or entry.converted is not None or self._has_output(entry)

    def _has_output(self, entry: _Entry) -> bool:
        assert entry.converter is not None
        pts = first_video_pts(entry.raw.result())
        return pts is not None and entry.converter.has_output(pts)

    async def get(self, slot: int, seq: int) -> bytes | None:
        entry = self._slots.get(slot, _Slot()).entries.get(seq)
        if entry is None:
            return None
        try:
            data = await entry.raw
        except Exception:
            return None
        if entry.converter is None:
            return data
        if entry.converted is None:
            pts = first_video_pts(data)
            out = await entry.converter.output_for(pts, CONVERT_WAIT) if pts is not None else None
            if out is None:
                logger.warning("relay conversion late, serving original", slot=slot, seq=seq)
                return data
            entry.converted = out
        return entry.converted

    async def aclose(self) -> None:
        for state in self._slots.values():
            if state.worker is not None:
                state.worker.cancel()
            if state.converter is not None:
                await state.converter.close()
        self._slots.clear()
