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
# seconds ffmpeg may take to accept one segment; the segment's clients wait on it
FEED_WAIT = 3.0
# seconds without a new segment before a converting run is flushed. ffmpeg holds a
# run's last 1-2 outputs until more input or EOF, and the server lists filler (and
# with it those held-back segments) from STALL_AFTER (3.0) after the last arrival;
# a flush takes well under the 0.5s left, so the tail is converted by then
IDLE_FLUSH = 2.5
# seconds between ready() checks in wait_ready; small against a 2s segment
READY_POLL = 0.05


# identity equality: entries go in sets and are compared by object
@dataclass(eq=False)
class _Entry:
    seg: Segment
    raw: "asyncio.Future[bytes]"
    # set only when the segment went into a live converter and has a video PTS
    converter: AudioConverter | None = None
    pts: int | None = None
    converted: bytes | None = None


@dataclass
class _Slot:
    entries: "OrderedDict[int, _Entry]" = field(default_factory=OrderedDict)
    queue: "asyncio.Queue[_Entry]" = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None
    converter: AudioConverter | None = None
    run: int = 0
    # false until the next fetched segment decides whether its run converts
    decided: bool = False
    registered: float = 0.0
    # entries with a client in get(); harvested even once evicted from the cache
    waiting: "list[_Entry]" = field(default_factory=list)


def _mark_retrieved(fut: "asyncio.Future[bytes]") -> None:
    # a failed fetch nobody requested must not log "exception was never retrieved"
    if not fut.cancelled():
        fut.exception()


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
        self._retiring: set[asyncio.Task[None]] = set()

    def register(self, slot: int, seg: Segment) -> None:
        """Queue a newly listed segment for fetching (and converting) in order."""
        state = self._slots.setdefault(slot, _Slot())
        if seg.filler or seg.seq in state.entries:
            return
        entry = _Entry(seg, asyncio.get_running_loop().create_future())
        entry.raw.add_done_callback(_mark_retrieved)
        state.entries[seg.seq] = entry
        state.registered = entry.raw.get_loop().time()
        while len(state.entries) > self._keep:
            state.entries.popitem(last=False)
        state.queue.put_nowait(entry)
        if state.worker is None or state.worker.done():
            state.worker = asyncio.create_task(self._work(slot, state))

    async def _work(self, slot: int, state: _Slot) -> None:
        loop = asyncio.get_running_loop()
        while True:
            if state.queue.empty():
                if state.converter is None:
                    return
                idle = state.registered + IDLE_FLUSH - loop.time()
                try:
                    entry = await asyncio.wait_for(state.queue.get(), max(0.0, idle))
                except TimeoutError:
                    # register() saw this worker alive, so it must drain what came in
                    if not state.queue.empty():
                        continue
                    # a source stall or ad gap: flush the tail before filler lists it
                    self._retire(state, state.converter)
                    state.converter = None
                    state.decided = False
                    return
            else:
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
            try:
                await self._route(slot, state, entry, data)
            except Exception as e:
                logger.warning(
                    "relay conversion failed", slot=slot, seq=entry.seg.seq, error=str(e)
                )
            if not entry.raw.done():
                entry.raw.set_result(data)

    async def _route(self, slot: int, state: _Slot, entry: _Entry, data: bytes) -> None:
        ts = entry.seg.map_gen is None
        # each run decides conversion once, from its first segment's audio rate
        if entry.seg.discontinuity or not state.decided:
            if state.converter is not None:
                self._retire(state, state.converter)
                state.converter = None
            state.run += 1
            state.decided = True
            rate = audio_sample_rate(data) if ts else None
            if self._ffmpeg and rate is not None and rate != TARGET_RATE:
                new = AudioConverter(self._ffmpeg, self._workroot / f"slot{slot}-run{state.run}")
                # held before start so aclose() reaches it if cancelled mid-start
                state.converter = new
                try:
                    await new.start()
                except Exception as e:
                    logger.warning("relay conversion failed", slot=slot, error=str(e))
                    state.converter = None
                    await new.close()
                else:
                    logger.info("relay converting audio", slot=slot, rate=rate)
        conv = state.converter
        if conv is None or not ts:
            return
        try:
            # a hung ffmpeg must not hold this segment's clients past their buffer
            await asyncio.wait_for(conv.feed(data), FEED_WAIT)
        except Exception as e:
            # the rest of the run passes through raw
            logger.warning("relay conversion failed", slot=slot, seq=entry.seg.seq, error=repr(e))
            state.converter = None
            self._retire(state, conv)
            return
        # ffmpeg still gets every segment, so a segment without video only skips
        # conversion itself, keeping the encoder continuous
        entry.pts = first_video_pts(data)
        if entry.pts is not None:
            entry.converter = conv

    def _retire(self, state: _Slot, conv: AudioConverter) -> None:
        """Finish a run's converter in the background, keeping its outputs for cached entries."""
        task = asyncio.create_task(self._harvest(state, conv))
        self._retiring.add(task)
        task.add_done_callback(self._harvested)

    def _harvested(self, task: asyncio.Task[None]) -> None:
        self._retiring.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            logger.warning("relay conversion failed", error=repr(e))

    async def _harvest(self, state: _Slot, conv: AudioConverter) -> None:
        try:
            # EOF makes ffmpeg write the run's last segment, which no next keyframe closes
            await conv.finish()
        finally:
            for entry in {*state.entries.values(), *state.waiting}:
                if entry.converter is not conv:
                    continue
                if entry.converted is None and entry.pts is not None:
                    entry.converted = await conv.output_for(entry.pts, 0)
                entry.converter = None
            await conv.close()

    def ready(self, slot: int, seq: int) -> bool:
        """Whether a client asking for this segment now would be served at once.

        Converted segments count only once their output exists; others are never held
        back. An unfetched segment whose run has not decided yet counts as not ready.
        """
        state = self._slots.get(slot)
        entry = state.entries.get(seq) if state is not None else None
        if state is None or entry is None:
            return True
        if not entry.raw.done():
            if self._ffmpeg is None or entry.seg.map_gen is not None:
                return True
            if not state.decided or state.converter is not None:
                return False
            # a pending discontinuity at or before this segment starts a run not yet decided
            return not any(
                e.seg.discontinuity and not e.raw.done() and e.seg.seq <= seq
                for e in state.entries.values()
            )
        if entry.converted is not None or entry.converter is None:
            return True
        assert entry.pts is not None
        return entry.converter.has_output(entry.pts)

    async def wait_ready(self, slot: int, seq: int, timeout: float) -> bool:
        """Wait until ready(slot, seq) holds or `timeout` seconds pass; return ready()."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        # no event exists to await: ffmpeg signals a converted output only by writing a file
        while not self.ready(slot, seq):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(READY_POLL)
        return True

    async def get(self, slot: int, seq: int) -> bytes | None:
        state = self._slots.get(slot)
        entry = state.entries.get(seq) if state is not None else None
        if entry is None:
            return None
        try:
            # shielded: one client disconnecting must not cancel the fetch for all
            data = await asyncio.shield(entry.raw)
        except Exception:
            return None
        assert state is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CONVERT_WAIT
        state.waiting.append(entry)
        try:
            # re-read entry each pass: a retiring run moves its outputs into entry.converted
            while entry.converted is None:
                conv = entry.converter
                if conv is None:
                    return data
                assert entry.pts is not None
                out = await conv.output_for(entry.pts, 0)
                if out is not None:
                    entry.converted = out
                    break
                if loop.time() >= deadline:
                    logger.warning("relay conversion late, serving original", slot=slot, seq=seq)
                    return data
                await asyncio.sleep(0.1)
            return entry.converted
        finally:
            state.waiting.remove(entry)

    async def aclose(self) -> None:
        workers = [s.worker for s in self._slots.values() if s.worker is not None]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for state in self._slots.values():
            # queued entries can already be out of the cache; their clients still wait
            pending = list(state.entries.values())
            while not state.queue.empty():
                pending.append(state.queue.get_nowait())
            for entry in pending:
                if not entry.raw.done():
                    entry.raw.set_exception(RuntimeError("relay segment proxy closed"))
            if state.converter is not None:
                await state.converter.close()
                state.converter = None
        await asyncio.gather(*self._retiring, return_exceptions=True)
        self._slots.clear()
