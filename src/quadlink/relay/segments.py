"""Fetch each relay segment from Twitch once and serve it to every client on the LAN.

Segments are processed per slot in playlist order, because a converting run must
feed ffmpeg its input in sequence. A run is a stretch without a discontinuity.
"""

import asyncio
import shutil
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from quadlink.relay.convert import AudioConverter
from quadlink.relay.playlist import Segment
from quadlink.relay.tsinfo import audio_sample_rate, first_video_pts

logger = structlog.get_logger()

# seconds a client request waits for a converted segment before getting the original
CONVERT_WAIT = 6.0
# seconds ffmpeg may take to accept one segment; the segment's clients wait on it
FEED_WAIT = 3.0
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

    def set_converted(self, data: bytes) -> None:
        # converted is final once set, so the raw bytes are never served again
        self.converted = data
        released: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        released.set_result(b"")
        self.raw = released


@dataclass
class _Slot:
    entries: "OrderedDict[int, _Entry]" = field(default_factory=OrderedDict)
    # None marks a flush, taken in order after the segments queued before it
    queue: "asyncio.Queue[_Entry | None]" = field(default_factory=asyncio.Queue)
    worker: asyncio.Task[None] | None = None
    converter: AudioConverter | None = None
    run: int = 0
    # false until the next fetched segment decides whether its run converts
    decided: bool = False
    # entries with a client in get(); harvested even once evicted from the cache, and
    # their outputs kept until the last client leaves
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
        keep: int = 14,
    ) -> None:
        self._fetch = fetch
        self._ffmpeg = ffmpeg
        # run dir names repeat across restarts
        shutil.rmtree(workroot, ignore_errors=True)
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
        while len(state.entries) > self._keep:
            _, evicted = state.entries.popitem(last=False)
            self._discard(state, evicted)
        state.queue.put_nowait(entry)
        if state.worker is None or state.worker.done():
            state.worker = asyncio.create_task(self._work(slot, state))

    def flush(self, slot: int) -> None:
        """End the slot's converting run once its queued segments are in.

        ffmpeg holds a run's last 1-2 outputs until more input or EOF; the server
        calls this when it lists filler after them. The next segment starts a new run.
        """
        state = self._slots.get(slot)
        if state is None or state.worker is None or state.worker.done():
            return
        # queued even with no converter open: the worker can be fetching a run's
        # first segment, whose converter opens only after this call
        state.queue.put_nowait(None)

    async def _work(self, slot: int, state: _Slot) -> None:
        while True:
            # a worker with an open converter stays up so a later flush reaches it
            if state.queue.empty() and state.converter is None:
                return
            entry = await state.queue.get()
            if entry is None:
                if state.converter is not None:
                    self._retire(state, state.converter)
                    state.converter = None
                    state.decided = False
                continue
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
            # evicted while still queued: its converter was set only now
            if state.entries.get(entry.seg.seq) is not entry:
                self._discard(state, entry)

    @staticmethod
    def _discard(state: _Slot, entry: _Entry) -> None:
        """Delete an uncached entry's converted output once no client waits on it.

        A run can last hours; without this its outputs fill the workdir until harvest.
        """
        if entry.converter is None or entry in state.waiting:
            return
        assert entry.pts is not None
        entry.converter.discard(entry.pts)
        entry.converter = None

    async def _route(self, slot: int, state: _Slot, entry: _Entry, data: bytes) -> None:
        ts = entry.seg.map_gen is None
        # every TS run with audio goes through the encoder, so all tiles get the same
        # 48 kHz AAC whatever the channel sends; a run decides once, at its first segment
        if entry.seg.discontinuity or not state.decided:
            if state.converter is not None:
                self._retire(state, state.converter)
                state.converter = None
            state.run += 1
            state.decided = True
            rate = audio_sample_rate(data) if ts else None
            if self._ffmpeg and rate is not None:
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
            logger.warning(
                "relay conversion failed, serving the rest of the run at the source rate",
                slot=slot,
                seq=entry.seg.seq,
                error=repr(e),
            )
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
                    out = await conv.output_for(entry.pts, 0)
                    if out is not None:
                        entry.set_converted(out)
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
        if state is None or entry is None:
            return None
        # listed while still fetching too: the worker discards an evicted entry's
        # output after the fetch, which this client still waits to read
        state.waiting.append(entry)
        try:
            try:
                # shielded: one client disconnecting must not cancel the fetch for all
                data = await asyncio.shield(entry.raw)
            except Exception:
                return None
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CONVERT_WAIT
            # re-read entry each pass: a retiring run moves its outputs into entry.converted
            while entry.converted is None:
                conv = entry.converter
                if conv is None:
                    return data
                assert entry.pts is not None
                out = await conv.output_for(entry.pts, 0)
                if out is not None:
                    entry.set_converted(out)
                    break
                if loop.time() >= deadline:
                    logger.warning(
                        "relay conversion late, serving this segment at the source rate",
                        slot=slot,
                        seq=seq,
                    )
                    return data
                await asyncio.sleep(0.1)
            return entry.converted
        finally:
            state.waiting.remove(entry)
            if state.entries.get(seq) is not entry:
                self._discard(state, entry)

    async def aclose(self) -> None:
        workers = [s.worker for s in self._slots.values() if s.worker is not None]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for state in self._slots.values():
            # queued entries can already be out of the cache; their clients still wait
            pending = list(state.entries.values())
            while not state.queue.empty():
                if (queued := state.queue.get_nowait()) is not None:
                    pending.append(queued)
            for entry in pending:
                if not entry.raw.done():
                    entry.raw.set_exception(RuntimeError("relay segment proxy closed"))
            if state.converter is not None:
                await state.converter.close()
                state.converter = None
        await asyncio.gather(*self._retiring, return_exceptions=True)
        self._slots.clear()
