"""Fetch each relay segment from Twitch once and serve it to every client on the LAN.

Segments are processed per slot in playlist order, because a converting run must
feed ffmpeg its input in sequence. A run is a stretch without a discontinuity
whose video PTS keep rising.

With ffmpeg, fMP4 segments are served as MPEG-TS too: the server lists them as .ts
with no EXT-X-MAP, so a client must never get fMP4 bytes for them.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from quadlink.relay.convert import AudioConverter, Remuxer
from quadlink.relay.playlist import Segment
from quadlink.relay.tsinfo import audio_sample_rate

logger = structlog.get_logger()

# seconds a client request waits for a converted segment before getting the original;
# for fMP4 this includes the remux
CONVERT_WAIT = 6.0
# seconds ffmpeg may take to accept one segment; the segment's clients wait on it
FEED_WAIT = 3.0
# seconds the MPEG-TS remux of one fMP4 segment may take; its clients wait on it
REMUX_WAIT = 2.0
# seconds all attempts at one init segment may take; its segment's clients wait on it
INIT_WAIT = 4.0
# seconds under which a failed init fetch is tried again within INIT_WAIT
INIT_RETRY_UNDER = 1.0
# seconds the segments of a generation whose init failed skip fetching it
INIT_BACKOFF = 10.0


# identity equality: entries go in sets and are compared by object
@dataclass(eq=False)
class _Entry:
    seg: Segment
    raw: "asyncio.Future[bytes]"
    # set while the segment's converted output is still to come
    converter: AudioConverter | None = None
    pts: int | None = None
    converted: bytes | None = None
    # fMP4 only: the init segment its media segment needs
    init: bytes | None = None

    def set_converted(self, data: bytes) -> None:
        # converted is final once set: every client of a segment gets the same bytes,
        # and the raw bytes are never served again
        if self.converted is not None:
            return
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
    # entries with a client in get(); they still take their output once evicted
    waiting: "list[_Entry]" = field(default_factory=list)
    # set and replaced whenever anything ready() reads changes
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    # (map generation, URI without query, bytes) of the newest fMP4 init segment
    init: tuple[int, str, bytes] | None = None
    # (map generation, URI without query, loop time) of the last init that failed
    init_failed: tuple[int, str, float] | None = None
    # remuxes the fMP4 segments of the newest init that had any
    remuxer: Remuxer | None = None
    # init of the open run's segments; None for TS
    run_init: bytes | None = None

    def notify(self) -> None:
        event, self.changed = self.changed, asyncio.Event()
        event.set()

    async def wait_change(self, deadline: float) -> bool:
        """Wait for the next notify() until loop time `deadline`; False on timeout."""
        try:
            async with asyncio.timeout_at(deadline):
                await self.changed.wait()
        except TimeoutError:
            return False
        return True


def _mark_retrieved(fut: "asyncio.Future[bytes]") -> None:
    # a failed fetch nobody requested must not log "exception was never retrieved"
    if not fut.cancelled():
        fut.exception()


class SegmentProxy:
    def __init__(
        self,
        fetch: Callable[[str], Awaitable[bytes]],
        ffmpeg: str | None,
        keep: int = 14,
    ) -> None:
        self._fetch = fetch
        self._ffmpeg = ffmpeg
        self._keep = keep
        self._slots: dict[int, _Slot] = {}
        self._retiring: set[asyncio.Task[None]] = set()

    @property
    def converts_fmp4(self) -> bool:
        """Whether fMP4 segments are served as MPEG-TS."""
        return self._ffmpeg is not None

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

        ffmpeg holds a run's last output until more input or EOF; the server calls
        this when it lists filler after it. The next segment starts a new run.
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
            try:
                await self._process(slot, state, entry)
            finally:
                state.notify()

    async def _process(self, slot: int, state: _Slot, entry: _Entry | None) -> None:
        if entry is None:
            if state.converter is not None:
                self._retire(state, state.converter)
                state.converter = None
                state.decided = False
            return
        try:
            data = await self._fetch(entry.seg.uri)
        except Exception as e:
            logger.warning("relay segment fetch failed", slot=slot, seq=entry.seg.seq, error=str(e))
            if not entry.raw.done():
                entry.raw.set_exception(e)
            return
        if entry.seg.map_gen is not None and self._ffmpeg is not None:
            entry.init = await self._init(slot, state, entry.seg)
            # the TS stands in for the fetched bytes: clients get it unconverted too
            data = await self._as_ts(slot, state, entry, data)
        # route decides entry.converter before raw is visible to get(), so a
        # waiting client never observes an entry mid-decision as unconverted
        try:
            await self._route(slot, state, entry, data)
        except Exception as e:
            logger.warning("relay conversion failed", slot=slot, seq=entry.seg.seq, error=str(e))
        if not entry.raw.done():
            entry.raw.set_result(data)
        # evicted while still queued: its converter was set only now
        if state.entries.get(entry.seg.seq) is not entry:
            self._discard(state, entry)

    async def _init(self, slot: int, state: _Slot, seg: Segment) -> bytes | None:
        """The init segment of seg's map generation and URI, fetched once for both."""
        assert seg.map_gen is not None
        if seg.map_uri is None:
            return None
        # the query holds the CDN token, which changes for the same init
        path = seg.map_uri.split("?", 1)[0]
        if state.init is not None and state.init[:2] == (seg.map_gen, path):
            return state.init[2]
        loop = asyncio.get_running_loop()
        failed = state.init_failed
        if failed is not None and failed[:2] == (seg.map_gen, path):
            if loop.time() - failed[2] < INIT_BACKOFF:
                return self._init_fallback(state, seg.map_gen, path)
        data: bytes | None = None
        deadline = loop.time() + INIT_WAIT
        for attempt in range(2):
            started = loop.time()
            try:
                async with asyncio.timeout_at(deadline):
                    data = await self._fetch(seg.map_uri)
                break
            except Exception as e:
                logger.warning(
                    "relay init fetch failed",
                    slot=slot,
                    seq=seg.seq,
                    attempt=attempt,
                    error=repr(e),
                )
                # a slow failure would leave the retry too little of INIT_WAIT
                now = loop.time()
                if now - started >= INIT_RETRY_UNDER or now >= deadline:
                    break
        if data is None:
            state.init_failed = (seg.map_gen, path, loop.time())
            return self._init_fallback(state, seg.map_gen, path)
        state.init_failed = None
        state.init = (seg.map_gen, path, data)
        return data

    @staticmethod
    def _init_fallback(state: _Slot, map_gen: int, path: str) -> bytes | None:
        """The cached init to use when a generation's own init could not be fetched."""
        if state.init is None or state.init[1] != path:
            # not cached: a later segment tries again once INIT_BACKOFF has passed
            return None
        # filler under a listed segment URI has no DISCONTINUITY, so an init at
        # the same path is the better guess
        state.init = (map_gen, path, state.init[2])
        return state.init[2]

    async def _as_ts(self, slot: int, state: _Slot, entry: _Entry, data: bytes) -> bytes:
        """An fMP4 segment as MPEG-TS; b"" when none could be made, served as filler."""
        assert self._ffmpeg is not None
        out: bytes | None = None
        if entry.init is not None:
            if state.remuxer is None or state.remuxer.init != entry.init:
                state.remuxer = Remuxer(self._ffmpeg, entry.init)
            try:
                out = await state.remuxer.remux(data, REMUX_WAIT)
            except Exception as e:
                logger.warning("relay remux failed", slot=slot, seq=entry.seg.seq, error=repr(e))
        if out is None:
            logger.warning(
                "relay serving filler in place of an fMP4 segment", slot=slot, seq=entry.seg.seq
            )
            return b""
        return out

    @staticmethod
    def _discard(state: _Slot, entry: _Entry) -> None:
        """Stop an uncached entry from taking its output once no client waits on it.

        The output is then dropped on arrival instead of held until the entry is freed.
        """
        if entry.converter is not None and entry not in state.waiting:
            entry.converter = None

    async def _route(self, slot: int, state: _Slot, entry: _Entry, data: bytes) -> None:
        """Feed a TS segment, or an fMP4 one remuxed to TS, to its run's converter."""
        if entry.seg.map_gen is not None:
            if self._ffmpeg is None:
                return
            if not data:
                # without TS this segment gets filler and no converter can take it;
                # the next segment starts a new run
                if state.converter is not None:
                    self._retire(state, state.converter)
                    state.converter = None
                state.decided = False
                return
        # a PTS step without a discontinuity tag, a new init segment, or a failed
        # ffmpeg also ends the run: the old converter could never complete this
        # segment's output
        conv = state.converter
        # init bytes compare by value: a refetch under a new URI can return the same init
        broken = conv is not None and (state.run_init != entry.init or not conv.continues(data))
        # every TS run with audio goes through the encoder, so all tiles get the same
        # 48 kHz AAC whatever the channel sends; a run decides once, at its first segment
        if entry.seg.discontinuity or not state.decided or broken:
            if state.converter is not None:
                self._retire(state, state.converter)
                state.converter = None
            state.run += 1
            state.decided = True
            state.run_init = entry.init
            rate = audio_sample_rate(data)
            if self._ffmpeg and rate is not None:
                new = AudioConverter(
                    self._ffmpeg, lambda key, out: self._deliver(state, new, key, out)
                )
                # held before start so aclose() reaches it if cancelled mid-start
                state.converter = new
                try:
                    await new.start()
                except Exception as e:
                    logger.warning("relay conversion failed", slot=slot, error=str(e))
                    state.converter = None
                    await new.close()
                else:
                    logger.info(
                        "relay converting audio", slot=slot, rate=rate, fmp4=entry.init is not None
                    )
        conv = state.converter
        if conv is None:
            return
        # ffmpeg still gets every segment, so a segment without its own output only
        # skips conversion itself, keeping the encoder continuous
        pts = conv.register(data)
        if pts is not None:
            # set before feeding: the previous segment's output can complete mid-feed
            entry.pts = pts
            entry.converter = conv
        try:
            # a hung ffmpeg must not hold this segment's clients past their buffer
            await asyncio.wait_for(conv.feed(data), FEED_WAIT)
        except Exception as e:
            logger.warning(
                "relay conversion failed, serving this segment at the source rate",
                slot=slot,
                seq=entry.seg.seq,
                error=repr(e),
            )
            entry.converter = None
            state.converter = None
            # retry conversion on the next segment; a failure can be transient
            state.decided = False
            self._retire(state, conv)

    @staticmethod
    def _deliver(state: _Slot, conv: AudioConverter, key: int, out: bytes | None) -> None:
        """Hand a finished output to the entry still taking it; None means serve raw."""
        for entry in {*state.entries.values(), *state.waiting}:
            if entry.converter is conv and entry.pts == key:
                # empty: ffmpeg's output skipped this segment, so serve it raw
                if out:
                    entry.set_converted(out)
                entry.converter = None
                state.notify()

    def _retire(self, state: _Slot, conv: AudioConverter) -> None:
        """Finish a run's converter in the background, delivering its last outputs."""
        task = asyncio.create_task(self._harvest(state, conv))
        self._retiring.add(task)
        task.add_done_callback(self._harvested)

    def _harvested(self, task: asyncio.Task[None]) -> None:
        self._retiring.discard(task)
        if not task.cancelled() and (e := task.exception()) is not None:
            logger.warning("relay conversion failed", error=repr(e))

    async def _harvest(self, state: _Slot, conv: AudioConverter) -> None:
        try:
            # EOF makes ffmpeg write the run's last output, which no next keyframe closes
            await conv.finish()
        finally:
            for entry in {*state.entries.values(), *state.waiting}:
                if entry.converter is conv:
                    entry.converter = None
            await conv.close()
            state.notify()

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
            if self._ffmpeg is None:
                return True
            if not state.decided or state.converter is not None:
                return False
            # a pending discontinuity at or before this segment starts a run not yet decided
            return not any(
                e.seg.discontinuity and not e.raw.done() and e.seg.seq <= seq
                for e in state.entries.values()
            )
        return entry.converter is None

    async def wait_ready(self, slot: int, seq: int, timeout: float) -> bool:
        """Wait until ready(slot, seq) holds or `timeout` seconds pass; return ready()."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.ready(slot, seq):
            if not await self._slots[slot].wait_change(deadline):
                return self.ready(slot, seq)
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
            wait = CONVERT_WAIT
            if entry.seg.map_gen is not None and self._ffmpeg is not None:
                # the remux took up to REMUX_WAIT before raw was set, and the whole
                # request must fit the client's buffer
                wait -= REMUX_WAIT
            deadline = asyncio.get_running_loop().time() + wait
            while entry.converted is None:
                if entry.converter is None:
                    return data
                if not await state.wait_change(deadline):
                    logger.warning(
                        "relay conversion late, serving this segment at the source rate",
                        slot=slot,
                        seq=seq,
                    )
                    return data
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
