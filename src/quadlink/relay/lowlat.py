"""Low-latency HLS (LL-HLS) of the relay's slots at /streams/ll/{1-4}.

Twitch lists its next segments early (#EXT-X-TWITCH-PREFETCH) and streams their
bytes while it encodes them. Each slot reads those bytes as they arrive, pipes
them through one ffmpeg per run for 48 kHz audio, and cuts the output into parts
(parts.PartCutter) as soon as ffmpeg writes them. A player then holds back
PART-HOLD-BACK behind the newest part instead of three whole segments.

AVPlayer uses LL-HLS only over HTTP/2 with ECN and SACK; over HTTP/1.1 it plays
these playlists as ordinary HLS.

The relay holds a cushion: it releases parts on its own real-time schedule,
about CUSHION seconds behind the newest part it has cut. Each run (a new
channel, or video back after filler) starts that far back in the source, in
segments already complete, so their bytes arrive at once and fill the cushion
while the first part goes out at once. A source hiccup shorter than the cushion
then drains it instead of the player's buffer.

QuadStream rebuilds any player that stalls, so a slot never lets its parts run
dry: once the cushion is empty and the next part is FILL_AFTER seconds past
its release, filler parts go out.
"""

import asyncio
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiohttp
import structlog
from aiohttp import web

from quadlink.relay.convert import Remuxer, command
from quadlink.relay.parts import Part, PartCutter
from quadlink.relay.playlist import (
    Segment,
    SlotState,
    _parse_pdt,
    ad_segment_uris,
    ingest,
    parse_media_playlist,
)
from quadlink.relay.tsinfo import audio_sample_rate, before, first_video_pts

if TYPE_CHECKING:
    from quadlink.relay.server import SlotRelay

logger = structlog.get_logger()

# Apple rejects a playlist with a part over PART-TARGET, or one other than a
# segment's last under 85% of it. Parts of 0.5s are 14-16 frames at 30 fps and
# 29-31 at 60, which this admits
PART_TARGET = 0.54
PART_HOLD_BACK = 3 * PART_TARGET
# seconds between source playlist fetches; the relay caches them for 1s
POLL = 0.5
# seconds the released edge may stand still before filler goes out, inside
# PART-HOLD-BACK so the player never stalls
FILL_AFTER = 1.0
# segments a playlist lists, and how many of the newest list their parts
WINDOW = 10
PARTS_FOR = 4
# segments fetched ahead of the one being fed: opening a Twitch request takes ~0.3s
FETCH_AHEAD = 2
# a slot nobody has asked for in this long stops following its source
IDLE_STOP = 30.0
# seconds ffmpeg may take to accept a chunk, drain at a run's end, or remux fMP4
FEED_WAIT = 3.0
FINISH_WAIT = 2.0
REMUX_WAIT = 2.0
# bytes of a segment read to find its first video PTS and audio before giving up
PROBE_MAX = 1 << 20
# seconds of cut parts held back from release. A source hiccup shorter than this
# goes unseen; each second of it is a second of latency. Twitch segments are 2s,
# so a run starts one complete segment back from the one being encoded
CUSHION = 2.5
# a cushion over CUSHION + CUSHION_SLACK (after a burst, or a stall the source
# makes up) releases CATCH_UP times faster than real time until back at CUSHION,
# or each hiccup would add its length to the latency for good. Never slower than
# real time: that would drain the player's buffer until it stalls
CUSHION_SLACK = 0.5
CATCH_UP = 1.05
# seconds a run's released media may run ahead of the real time since its first
# part. QuadStream never skips ahead, so catching up moves delay from the relay
# into the player's buffer; this keeps that within one more PART-HOLD-BACK
MAX_LEAD = PART_HOLD_BACK
# seconds between two "nothing released" reports for one slot, and between two
# reports of skipped repeats
EMPTY_REPORT = 10.0
# source segments a slot remembers by URI, to skip one listed again (about 2 minutes)
TAKEN_MEMORY = 64


def next_release(now: float, prev: "_Part | None", rate: float = 1.0) -> float:
    """When a part cut at `now` goes out: the one before's release plus its duration.

    The schedule runs `rate` times real time, and never ahead of the cut. It counts
    at most PART_TARGET per part. A source timestamp jump, such as a PTS wrap on a
    day-long stream, gives one part a duration of hours, and scheduling by it would
    hold every later part back that long: the playlist would list nothing, and
    QuadStream rebuilds a player on each 503.
    """
    if prev is None:
        return now
    return max(now, prev.release + min(prev.duration, PART_TARGET) / rate)


def start_back(segs: list[Segment], prefetch: set[str]) -> int:
    """Index of the segment a run starts at: about CUSHION of media back from the newest.

    Segments before the first prefetch one are complete, so their bytes arrive at
    once and become the cushion. The first prefetch one is being encoded; on
    average half of it is out.
    """
    edge = next((i for i, s in enumerate(segs) if s.uri in prefetch), len(segs))
    have = segs[edge].duration / 2 if edge < len(segs) else 0.0
    i = edge
    while i > 0 and have < CUSHION:
        i -= 1
        have += segs[i].duration
    return min(i, len(segs) - 1)


def with_prefetch(text: str) -> tuple[str, set[str]]:
    """The source playlist with Twitch's prefetch segments listed as ordinary ones.

    Returns the playlist and the prefetch URIs. While the newest listed segment is
    an ad, prefetch is left out: it would be more of the ad, and Twitch tags it
    only once listed. A prefetch segment gets a PROGRAM-DATE-TIME a second past
    its expected one, so its later ordinary listing, with an earlier time, reads
    as already ingested and not as a restarted broadcast.
    """
    lines = text.splitlines()
    prefetch = [
        line.split(":", 1)[1] for line in lines if line.startswith("#EXT-X-TWITCH-PREFETCH:")
    ]
    _, _, segments = parse_media_playlist(text)
    if not prefetch or not segments or segments[-1][1] in ad_segment_uris(text):
        return text, set()
    duration, _, pdt, _, _ = segments[-1]
    last_time = _parse_pdt(pdt)
    out = [line for line in lines if not line.startswith("#EXT-X-TWITCH-PREFETCH:")]
    for n, uri in enumerate(prefetch, 1):
        if last_time is not None:
            stamp = last_time + timedelta(seconds=duration * n + 1)
            out.append(f"#EXT-X-PROGRAM-DATE-TIME:{stamp.isoformat(timespec='milliseconds')}")
        out.append(f"#EXTINF:{duration:.3f},live")
        out.append(uri)
    return "\n".join(out) + "\n", set(prefetch)


@dataclass(eq=False)
class _Part:
    data: bytes
    duration: float
    independent: bool
    release: float  # loop time it is listed from


@dataclass(eq=False)
class _Seg:
    seq: int
    disc: bool
    # discontinuities up to and including this segment
    dseq: int
    parts: list[_Part] = field(default_factory=list)
    filler: bool = False
    complete: bool = False
    # PROGRAM-DATE-TIME (epoch seconds): the release clock after a discontinuity,
    # else the previous segment's plus its duration
    wall: float = 0.0

    def released(self, now: float) -> int:
        return sum(1 for p in self.parts if p.release <= now)

    @property
    def duration(self) -> float:
        return sum(p.duration for p in self.parts)


class _Run:
    """One continuous stretch of a channel through one ffmpeg (or none, without audio)."""

    def __init__(
        self, ffmpeg: str | None, identity: str, on_parts: Callable[["_Run", list[Part]], None]
    ) -> None:
        self.ffmpeg = ffmpeg
        self.identity = identity
        self._on_parts = on_parts
        self.cutter = PartCutter()
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self.last_key: int | None = None
        # false once the run is replaced; its parts then go nowhere
        self.live = True
        # fMP4 only: remuxes this run's segments, all of one init
        self.remuxer: Remuxer | None = None

    async def start(self) -> None:
        if self.ffmpeg is None:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *command(self.ffmpeg),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read())

    def continues(self, key: int) -> bool:
        return self.last_key is None or before(self.last_key, key)

    def expect(self, key: int) -> None:
        self.last_key = key
        self.cutter.expect(key)

    async def write(self, chunk: bytes) -> None:
        if self._proc is None:
            self._on_parts(self, self.cutter.push(chunk))
            return
        assert self._proc.stdin is not None
        self._proc.stdin.write(chunk)
        await asyncio.wait_for(self._proc.stdin.drain(), FEED_WAIT)

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while chunk := await self._proc.stdout.read(1 << 16):
            self._on_parts(self, self.cutter.push(chunk))

    async def finish(self) -> None:
        """End the input and deliver the run's last part once ffmpeg has drained."""
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            proc.stdin.close()
            try:
                async with asyncio.timeout(FINISH_WAIT):
                    assert self._reader is not None
                    await self._reader
            except Exception as e:
                logger.debug("relay ll run drain failed", error=repr(e))
        self._on_parts(self, self.cutter.end())
        await self.close()

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        self._on_parts = _ignore


def _ignore(run: _Run, parts: list[Part]) -> None:
    pass


class _Fetch:
    """One source segment, streamed into a queue of chunks as it arrives."""

    def __init__(self, session: aiohttp.ClientSession, uri: str) -> None:
        self._chunks: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._run(session, uri))

    async def _run(self, session: aiohttp.ClientSession, uri: str) -> None:
        try:
            async with session.get(uri) as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_any():
                    self._chunks.put_nowait(chunk)
        except asyncio.CancelledError:
            # dropped work: the feeder sees the segment end and checks why
            self._chunks.put_nowait(None)
            raise
        except Exception as e:
            self._chunks.put_nowait(e)
            return
        self._chunks.put_nowait(None)

    async def chunks(self) -> AsyncIterator[bytes]:
        while (item := await self._chunks.get()) is not None:
            if isinstance(item, Exception):
                raise item
            yield item

    def cancel(self) -> None:
        self._task.cancel()


@dataclass
class _Queued:
    seg: Segment
    identity: str
    gen: int
    fetch: _Fetch | None = None
    # starts a new run: the segments before it were skipped
    fresh: bool = False


class _Slot:
    """One slot's LL-HLS: follows its source, converts, cuts and lists parts."""

    def __init__(self, ll: "LowLatency", slot: int, resume: tuple[int, int] | None = None) -> None:
        self.ll = ll
        self.slot = slot
        self.source = SlotState(window=40)
        self.segs: list[_Seg] = []
        # numbers start from the clock in segment steps, so they only rise across idle
        # stops and daemon restarts: a player resuming asks for numbers past its last
        clock = int(time.time() // 2)
        last_seq, last_dseq = resume or (0, 0)
        self.next_seq, self.base_dseq = max(clock, last_seq), max(clock, last_dseq)
        self.resumed = resume is not None
        self.target = 2
        self.queue: deque[_Queued] = deque()
        # the segment being fed
        self.current: _Queued | None = None
        self.wake = asyncio.Event()
        # bumps at every channel change; queued work of an older one is dropped
        self.gen = 0
        self.run: _Run | None = None
        # channel of the latest run
        self.played: str | None = None
        # the next segment opens with a discontinuity; set by a new run or filler
        self.pending_disc = False
        # parts are skipped until a segment's first one, after filler or a cut-off run
        self.need_independent = False
        self.seen = time.monotonic()
        self.tasks: list[asyncio.Task[None]] = []
        self._failing = False
        self._init: tuple[str, bytes] | None = None
        # loop time of the last "nothing released" report
        self._empty_logged = -math.inf
        # source URIs (without query) queued lately, newest last; see _take
        self._taken: deque[str] = deque(maxlen=TAKEN_MEMORY)
        self._repeats = 0
        self._repeats_logged = -math.inf
        # the release schedule runs at CATCH_UP; see _add
        self._catching = False
        # (release of the run's first part, media scheduled since): bounds the lead
        self._line = (0.0, 0.0)
        # a restart back in the source is under way; no other until a part is cut
        self._restarting = False

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self._follow()),
            asyncio.create_task(self._feed()),
            asyncio.create_task(self._guard()),
        ]

    @property
    def active(self) -> bool:
        return time.monotonic() - self.seen < IDLE_STOP

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for queued in self.queue:
            if queued.fetch is not None:
                queued.fetch.cancel()
        self.queue.clear()
        if self.run is not None:
            await self.run.close()
            self.run = None

    # source

    async def _follow(self) -> None:
        while self.active:
            try:
                got = await self.ll.relay.current_source(self.slot)
                if got is not None:
                    self._take(*got)
                if self._failing:
                    self._failing = False
                    logger.info("relay ll source recovered", slot=self.slot)
            except Exception as e:
                if not self._failing:
                    self._failing = True
                    logger.warning("relay ll source failed", slot=self.slot, error=repr(e))
            await asyncio.sleep(POLL)
        await self.ll.retire(self)

    def _take(self, identity: str, text: str) -> None:
        text, prefetch = with_prefetch(text)
        switched = identity != self.source.last_identity
        before_seq = self.source.next_seq
        ingest(self.source, text, identity)
        new = [s for s in self.source.segments if s.seq >= before_seq]
        if not new:
            return
        if switched:
            start = new[start_back(new, prefetch)]
            self._restarting = False
            self._taken.clear()
            # the older segments skipped here count as taken, or one listed again
            # would be fed behind the edge, back in time
            self._taken.extend(s.uri.split("?", 1)[0] for s in new if s.seq < start.seq)
            new = [s for s in new if s.seq >= start.seq]
            self.gen += 1
            for queued in [*self.queue, *([self.current] if self.current else [])]:
                if queued.fetch is not None:
                    queued.fetch.cancel()
            self.queue.clear()
        for seg in new:
            # ingest takes a segment again when it returns with a later time, its test
            # for a restarted broadcast; a playlist whose times run ahead of the
            # prefetch guess then feeds media twice, the pacing runs ahead of the
            # clock and the tile lists nothing. a restart brings new URIs
            path = seg.uri.split("?", 1)[0]
            if path in self._taken:
                self._repeats += 1
                continue
            self._taken.append(path)
            self.queue.append(_Queued(seg, identity, self.gen))
        if not switched and self.segs and self.segs[-1].filler and not self._restarting:
            self._restart_back(prefetch)
        self._report_repeats()
        self._fetch_ahead()
        self.wake.set()

    def _restart_back(self, prefetch: set[str]) -> None:
        """Filler plays and complete segments wait: start a new run back in them.

        The segment being fed has stalled, or the source came back with a backlog;
        feeding all of it would keep its delay for good, and its newest part alone
        would leave no cushion.
        """
        queued = [q for q in self.queue if q.gen == self.gen]
        if not queued or queued[0].seg.uri in prefetch:
            return
        start = start_back([q.seg for q in queued], prefetch)
        self.gen += 1
        for q in [*queued[:start], *([self.current] if self.current else [])]:
            if q.fetch is not None:
                q.fetch.cancel()
        kept = queued[start:]
        for q in kept:
            q.gen = self.gen
        kept[0].fresh = True
        self.queue = deque(kept)
        self._restarting = True
        logger.info("relay ll restart back", slot=self.slot, skipped=start, segments=len(kept))

    def _report_repeats(self) -> None:
        now = time.monotonic()
        if self._repeats and now - self._repeats_logged >= EMPTY_REPORT:
            logger.info("relay ll repeated segments skipped", slot=self.slot, count=self._repeats)
            self._repeats = 0
            self._repeats_logged = now

    def _fetch_ahead(self) -> None:
        for queued in list(self.queue)[:FETCH_AHEAD]:
            if queued.fetch is None:
                queued.fetch = _Fetch(self.ll.session(), queued.seg.uri)

    # conversion

    async def _feed(self) -> None:
        while True:
            if not self.queue:
                self.wake.clear()
                await self.wake.wait()
                continue
            queued = self.queue.popleft()
            self._fetch_ahead()
            if queued.gen != self.gen:
                if queued.fetch is not None:
                    queued.fetch.cancel()
                continue
            if queued.fetch is None:
                queued.fetch = _Fetch(self.ll.session(), queued.seg.uri)
            self.current = queued
            try:
                await self._feed_one(queued)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "relay ll segment failed", slot=self.slot, seq=queued.seg.seq, error=repr(e)
                )
                queued.fetch.cancel()
                # the run's input now has a hole; the next segment starts a new run
                await self._end_run(cut=False)
            finally:
                self.current = None

    async def _feed_one(self, queued: _Queued) -> None:
        assert queued.fetch is not None
        seg = queued.seg
        if seg.map_gen is not None:
            await self._feed_fmp4(queued)
            return
        prefix = bytearray()
        started = False
        async for chunk in queued.fetch.chunks():
            if queued.gen != self.gen:
                queued.fetch.cancel()
                return
            if started:
                assert self.run is not None
                await self.run.write(chunk)
                continue
            prefix += chunk
            key = first_video_pts(bytes(prefix))
            # the run's converter depends on the audio, so both must be known first
            known = key is not None and audio_sample_rate(bytes(prefix)) is not None
            if not known and len(prefix) < PROBE_MAX:
                continue
            await self._begin(queued, bytes(prefix), key)
            started = True
        if not started and prefix and queued.gen == self.gen:
            await self._begin(queued, bytes(prefix), first_video_pts(bytes(prefix)))

    async def _feed_fmp4(self, queued: _Queued) -> None:
        """fMP4 remuxes only whole: its parts come out in a burst and queue in the cushion."""
        assert queued.fetch is not None
        ffmpeg = self.ll.ffmpeg
        seg = queued.seg
        if ffmpeg is None or seg.map_uri is None:
            return
        data = b"".join([chunk async for chunk in queued.fetch.chunks()])
        if queued.gen != self.gen:
            return
        init = await self._init_bytes(seg.map_uri)
        run = self.run
        remuxer = run.remuxer if run is not None else None
        fresh = remuxer is None or remuxer.init != init or seg.discontinuity
        if fresh:
            remuxer = Remuxer(ffmpeg, init)
        assert remuxer is not None
        ts = await remuxer.remux(data, REMUX_WAIT)
        await self._begin(queued, ts, first_video_pts(ts), force_new=fresh or remuxer.broke_run)
        assert self.run is not None
        self.run.remuxer = remuxer

    async def _init_bytes(self, uri: str) -> bytes:
        path = uri.split("?", 1)[0]
        if self._init is not None and self._init[0] == path:
            return self._init[1]
        async with self.ll.session().get(uri) as resp:
            resp.raise_for_status()
            data = await resp.read()
        self._init = (path, data)
        return data

    async def _begin(
        self, queued: _Queued, prefix: bytes, key: int | None, force_new: bool = False
    ) -> None:
        """Start the segment on its run, opening a new run where it cannot continue one."""
        run = self.run
        # by channel, not by run: a run that failed leaves none, and that is no splice
        switch = queued.identity != self.played
        if (
            run is None
            or switch
            or force_new
            or queued.fresh
            or queued.seg.discontinuity
            or (key is not None and not run.continues(key))
        ):
            await self._new_run(queued.identity, audio_sample_rate(prefix) is not None, switch)
            self.played = queued.identity
        assert self.run is not None
        if key is not None:
            self.run.expect(key)
        await self.run.write(prefix)

    async def _new_run(self, identity: str, audio: bool, switch: bool) -> None:
        if switch and self.segs:
            # a channel change: parts no player has fetched yet are dropped, so the
            # new channel goes out now instead of after the old one's queue
            now = asyncio.get_running_loop().time()
            cushion = self.cushion(now)
            dropped = self._drop_unreleased(now)
            logger.info(
                "relay ll splice",
                slot=self.slot,
                channel=identity,
                dropped_parts=dropped,
                cushion=round(cushion, 2),
            )
        await self._end_run(cut=switch)
        run = _Run(self.ll.ffmpeg if audio else None, identity, self._on_parts)
        try:
            await run.start()
        except Exception as e:
            logger.warning("relay ll conversion failed to start", slot=self.slot, error=repr(e))
            run = _Run(None, identity, self._on_parts)
        self.run = run
        self.pending_disc = bool(self.segs)
        self.need_independent = True

    async def _end_run(self, cut: bool) -> None:
        """End the open run: drain it for its last part, or drop its rest when cut."""
        run, self.run = self.run, None
        if run is None:
            return
        if cut:
            run.live = False
            await run.close()
        else:
            await run.finish()
            run.live = False
        if self.segs and not self.segs[-1].complete:
            self.segs[-1].complete = True

    # parts

    def _on_parts(self, run: _Run, parts: list[Part]) -> None:
        if run.live:
            self._add(parts, asyncio.get_running_loop().time())

    def _add(self, parts: list[Part], now: float) -> None:
        """Queue parts cut at loop time `now` behind the cushion."""
        for part in parts:
            if self.need_independent and not part.independent:
                continue
            if part.independent or not self.segs or self.segs[-1].complete:
                if not part.independent:
                    continue
                self._open(now)
            seg = self.segs[-1]
            prev = seg.parts[-1] if seg.parts else self._last_part()
            fresh = not seg.parts and (seg.disc or len(self.segs) == 1)
            # measured before this part joins, at the low point of its sawtooth
            cushion = self.cushion(now)
            if fresh:
                self._catching = False
            elif cushion > CUSHION + CUSHION_SLACK:
                self._catching = True
            elif cushion <= CUSHION:
                self._catching = False
            release = next_release(now, prev, CATCH_UP if self._catching else 1.0)
            start, media = self._line
            if fresh:
                # a new run: its line starts at its first part
                start, media = release, 0.0
            release = max(release, start + media - MAX_LEAD)
            self._line = (start, media + min(part.duration, PART_TARGET))
            self._restarting = False
            if not seg.parts and not seg.wall:
                seg.wall = time.time() + (release - now)
            seg.parts.append(_Part(part.data, part.duration, part.independent, release))
            if part.last:
                seg.complete = True
                self.target = max(self.target, math.ceil(seg.duration - 0.001))

    def cushion(self, now: float) -> float:
        """Seconds until the released edge passes the newest cut part, on its schedule."""
        last = self._last_part()
        if last is None:
            return 0.0
        return max(0.0, last.release + min(last.duration, PART_TARGET) - now)

    def _last_part(self) -> _Part | None:
        for seg in reversed(self.segs):
            if seg.parts:
                return seg.parts[-1]
        return None

    def _open(self, now: float, filler: bool = False) -> _Seg:
        """Close the open segment and start the next one."""
        if self.segs:
            last = self.segs[-1]
            last.complete = True
            if last.filler and not filler:
                # video is back: filler no player has fetched yet goes
                self._drop_unreleased(now)
        disc = self.pending_disc or filler or bool(self.segs and self.segs[-1].filler)
        prev = self.segs[-1] if self.segs else None
        seq = prev.seq + 1 if prev is not None else self.next_seq
        if prev is not None:
            seg = _Seg(seq, disc, prev.dseq + disc, filler=filler)
        else:
            seg = _Seg(seq, self.resumed, self.base_dseq + self.resumed, filler=filler)
        if prev is not None and not seg.disc:
            # HLS times a segment as its predecessor's PROGRAM-DATE-TIME plus duration;
            # release times jitter with Twitch's delivery and would overlap
            seg.wall = prev.wall + prev.duration
        self.segs.append(seg)
        self.pending_disc = False
        self.need_independent = False
        del self.segs[: -WINDOW - 1]
        return seg

    def _drop_unreleased(self, now: float) -> int:
        """Drop parts not yet released; return how many."""
        dropped = 0
        kept: list[_Seg] = []
        for seg in self.segs:
            released = seg.released(now)
            dropped += len(seg.parts) - released
            if released == 0:
                continue
            if released < len(seg.parts):
                del seg.parts[released:]
                seg.complete = True
            kept.append(seg)
        self.segs = kept
        if kept:
            self.next_seq = kept[-1].seq + 1
        return dropped

    async def _guard(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(0.1)
            last = self._last_part()
            now = loop.time()
            # the cushion is empty and the next part is FILL_AFTER past its release
            due = last.release + min(last.duration, PART_TARGET) if last else now
            if last is None or now < due + FILL_AFTER:
                continue
            filler = self.ll.filler_parts()
            if not filler:
                continue
            if not self.segs[-1].filler:
                logger.info(
                    "relay ll filler",
                    slot=self.slot,
                    stalled=round(now - due, 2),
                )
            seg = self._open(now, filler=True)
            # paced like video, so returning video drops what is not out yet
            release = now
            seg.wall = time.time()
            for part in filler:
                seg.parts.append(_Part(part.data, part.duration, part.independent, release))
                release += part.duration
            seg.complete = True
            # each filler repeats the same timestamps (see playlist.add_filler)
            self.pending_disc = True
            # video joining after filler must start at a keyframe
            self.need_independent = True

    # serving

    def render(self, now: float) -> str:
        live = [s for s in self.segs if s.released(now) > 0]
        if not live:
            return ""
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:9",
            f"#EXT-X-TARGETDURATION:{self.target}",
            f"#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK={PART_HOLD_BACK:.2f}",
            f"#EXT-X-PART-INF:PART-TARGET={PART_TARGET}",
            f"#EXT-X-MEDIA-SEQUENCE:{live[0].seq}",
            # RFC 8216 6.2.2 counts a DISCONTINUITY before the first segment, so the first
            # segment carries its own number and no tag: a segment's number may never change
            # between reloads, or AVPlayer fails the stream (-12312) and stops
            f"#EXT-X-DISCONTINUITY-SEQUENCE:{live[0].dseq}",
        ]
        n = self.slot + 1
        with_parts = live[-PARTS_FOR:]
        for seg in live:
            if seg.disc and seg is not live[0]:
                lines.append("#EXT-X-DISCONTINUITY")
            stamp = datetime.fromtimestamp(seg.wall, UTC)
            lines.append(
                f"#EXT-X-PROGRAM-DATE-TIME:{stamp.isoformat(timespec='milliseconds')[:-6]}Z"
            )
            released = seg.released(now)
            if seg in with_parts:
                for k in range(released):
                    part = seg.parts[k]
                    flag = ",INDEPENDENT=YES" if part.independent else ""
                    # a frame gap can stretch a part; a part over PART-TARGET fails the playlist
                    duration = min(part.duration, PART_TARGET)
                    lines.append(
                        f"#EXT-X-PART:DURATION={duration:.5f},"
                        f'URI="/streams/ll/{n}/part/{seg.seq}.{k}.ts"{flag}'
                    )
            if seg.complete and released == len(seg.parts):
                lines.append(f"#EXTINF:{seg.duration:.5f},")
                lines.append(f"/streams/ll/{n}/seg/{seg.seq}.ts")
        tail = live[-1]
        done = tail.released(now)
        hint = (
            (tail.seq, done) if not tail.complete or done < len(tail.parts) else (tail.seq + 1, 0)
        )
        lines.append(
            f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="/streams/ll/{n}/part/{hint[0]}.{hint[1]}.ts"'
        )
        return "\n".join(lines) + "\n"

    def has(self, msn: int, part: int | None, now: float) -> bool:
        for seg in self.segs:
            if seg.seq < msn:
                continue
            if seg.seq > msn:
                return seg.released(now) > 0
            released = seg.released(now)
            if part is None:
                return seg.complete and released == len(seg.parts)
            if part < released:
                return True
            if seg.complete and part >= len(seg.parts):
                # cut short (a splice or a stall): the next segment answers
                continue
            return False
        return False

    def report_empty(self, now: float) -> None:
        """Log, at most every EMPTY_REPORT seconds, why a slot with segments lists none."""
        if not self.segs or now - self._empty_logged < EMPTY_REPORT:
            return
        self._empty_logged = now
        parts = [p for s in self.segs for p in s.parts]
        logger.warning(
            "relay ll nothing released",
            slot=self.slot,
            segs=len(self.segs),
            parts=[len(s.parts) for s in self.segs],
            fillers=sum(s.filler for s in self.segs),
            next_release=round(min((p.release for p in parts), default=now) - now, 2),
            longest=round(max((p.duration for p in parts), default=0.0), 2),
            need_independent=self.need_independent,
        )

    def last_seq(self) -> int | None:
        return self.segs[-1].seq if self.segs else None

    def find(self, seq: int) -> _Seg | None:
        return next((s for s in self.segs if s.seq == seq), None)


class LowLatency:
    """The LL-HLS slots and their routes."""

    def __init__(self, relay: "SlotRelay", ffmpeg: str | None, filler: bytes) -> None:
        self.relay = relay
        self.ffmpeg = ffmpeg
        self._filler = filler
        self._filler_parts: list[Part] | None = None
        self._slots: dict[int, _Slot] = {}
        self._resume: dict[int, tuple[int, int]] = {}
        self._session: aiohttp.ClientSession | None = None

    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(sock_connect=5, sock_read=10)
            )
        return self._session

    def filler_parts(self) -> list[Part]:
        if self._filler_parts is None:
            cutter = PartCutter()
            key = first_video_pts(self._filler)
            parts: list[Part] = []
            if key is not None:
                cutter.expect(key)
                parts = cutter.push(self._filler) + cutter.end()
            self._filler_parts = parts
        return self._filler_parts

    def slot(self, n: int) -> _Slot:
        state = self._slots.get(n - 1)
        if state is None:
            state = self._slots[n - 1] = _Slot(self, n - 1, self._resume.get(n - 1))
            state.start()
            logger.info("relay ll slot started", slot=n - 1)
        state.seen = time.monotonic()
        return state

    async def retire(self, state: _Slot) -> None:
        if state.segs:
            self._resume[state.slot] = (state.segs[-1].seq + 1, state.segs[-1].dseq)
        if self._slots.get(state.slot) is state:
            del self._slots[state.slot]
            logger.info("relay ll slot stopped", slot=state.slot)
        # stop() cancels this task too; finish from a fresh one
        asyncio.get_running_loop().create_task(state.stop())

    def register_routes(self, router: web.UrlDispatcher) -> None:
        router.add_get("/streams/ll/{n:[1-4]}", self.handle_playlist)
        router.add_get("/streams/ll/{n:[1-4]}/part/{seq:\\d+}.{k:\\d+}.ts", self.handle_part)
        router.add_get("/streams/ll/{n:[1-4]}/seg/{seq:\\d+}.ts", self.handle_segment)

    async def _wait(self, state: _Slot, msn: int, part: int | None) -> bool:
        """Whether the part (or whole segment) came out within 3 target durations."""
        loop = asyncio.get_running_loop()
        # the server-side bound of a blocking request (draft-pantos-hls-rfc8216bis 6.2.5.2)
        deadline = loop.time() + 3 * state.target
        while not state.has(msn, part, loop.time()):
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    def _miss(self, event: str, request: web.Request, state: _Slot, **fields: object) -> None:
        logger.warning(
            event,
            slot=state.slot,
            last=state.last_seq(),
            agent=request.headers.get("User-Agent", "")[:60],
            **fields,
        )

    async def handle_playlist(self, request: web.Request) -> web.Response:
        state = self.slot(int(request.match_info["n"]))
        loop = asyncio.get_running_loop()
        q = request.query
        if "_HLS_msn" in q:
            try:
                msn = int(q["_HLS_msn"])
                part = int(q["_HLS_part"]) if "_HLS_part" in q else None
            except ValueError:
                return web.Response(status=400, text="bad _HLS_msn or _HLS_part")
            last = state.last_seq()
            if last is not None and msn > last + 2:
                self._miss("relay ll request too far ahead", request, state, msn=msn, part=part)
                return web.Response(status=400, text="_HLS_msn too far ahead")
            if not await self._wait(state, msn, part):
                self._miss("relay ll block timeout", request, state, msn=msn, part=part)
                return web.Response(status=503, text="not yet available")
        else:
            # AVPlayer blocks on every reload after its first, so a plain request is a
            # player starting: QuadStream rebuilds one after any stall
            logger.info(
                "relay ll player start",
                slot=state.slot,
                last=state.last_seq(),
                agent=request.headers.get("User-Agent", "")[:60],
            )
            # a fresh slot needs its first parts; QuadStream retries a 503 itself
            deadline = loop.time() + 10
            while not state.render(loop.time()) and loop.time() < deadline:
                await asyncio.sleep(0.1)
        body = state.render(loop.time())
        if not body:
            state.report_empty(loop.time())
            return web.Response(status=503, text="no parts yet")
        return web.Response(
            text=body,
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    async def handle_part(self, request: web.Request) -> web.Response:
        state = self.slot(int(request.match_info["n"]))
        seq, k = int(request.match_info["seq"]), int(request.match_info["k"])
        await self._wait(state, seq, k)
        seg = state.find(seq)
        loop = asyncio.get_running_loop()
        if seg is None or k >= seg.released(loop.time()):
            self._miss("relay ll part missing", request, state, seq=seq, part=k)
            return web.Response(status=404)
        return web.Response(body=seg.parts[k].data, content_type="video/mp2t")

    async def handle_segment(self, request: web.Request) -> web.Response:
        state = self.slot(int(request.match_info["n"]))
        seg = state.find(int(request.match_info["seq"]))
        loop = asyncio.get_running_loop()
        if seg is None or not seg.complete or seg.released(loop.time()) < len(seg.parts):
            return web.Response(status=404)
        return web.Response(body=b"".join(p.data for p in seg.parts), content_type="video/mp2t")

    async def aclose(self) -> None:
        slots = list(self._slots.values())
        self._slots.clear()
        await asyncio.gather(*(s.stop() for s in slots), return_exceptions=True)
        if self._session is not None:
            await self._session.close()
            self._session = None
