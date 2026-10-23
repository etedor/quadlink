"""Low-latency HLS trial: re-serve the relay's slots as LL-HLS at /streams/ll/{1-4}.

Throwaway. Polls the relay's own /streams/N over loopback, cuts each new
segment into parts of about half a second at video frame boundaries, and
releases those parts in real time with blocking playlist reload and preload
hints. Parts exist only once the relay lists the whole segment, so this
measures the player's hold-back, not a relay that emits parts as it converts.

AVPlayer uses LL-HLS only over HTTP/2 (with ECN and SACK); over HTTP/1.1 it
plays these playlists as ordinary HLS.
"""

import asyncio
import math
import re
import time
from dataclasses import dataclass, field

import aiohttp
import structlog
from aiohttp import web

from quadlink.relay.tsinfo import PACKET, pes_pts, program

logger = structlog.get_logger()

VIDEO_TYPES = {0x1B, 0x24}
# Apple rejects a playlist with a part under 85% of PART-TARGET or over it; at
# 30 fps, 0.54 admits parts of 14-16 frames
PART_SPLIT = 0.5
PART_TARGET = 0.54
POLL = 0.5
BLOCK_WAIT = 6.0
WINDOW = 12
# a slot nobody has asked for in this long stops polling the relay
IDLE_STOP = 30.0


@dataclass
class Seg:
    seq: int
    duration: float
    disc: bool
    dseq: int
    body: bytes
    cuts: list[tuple[int, float]]  # (byte offset, part duration)
    release: float = 0.0  # monotonic time its first part is released
    src: int = 0  # the relay's sequence number for this segment

    def parts_released(self, now: float) -> int:
        t = self.release
        n = 0
        for _, dur in self.cuts:
            if now < t:
                break
            n += 1
            t += dur
        return n

    def end(self) -> float:
        return self.release + self.duration


@dataclass
class Slot:
    segs: list[Seg] = field(default_factory=list)
    target: int = 2
    task: asyncio.Task[None] | None = None
    seen: float = 0.0


def cut(body: bytes, duration: float) -> list[tuple[int, float]]:
    """(byte offset, duration) of each part, cut at video PES starts."""
    _, streams = program(body)
    video = next((pid for pid, t in streams.items() if t in VIDEO_TYPES), None)
    frames: list[tuple[int, int]] = []
    for i in range(0, len(body) - PACKET + 1, PACKET):
        pkt = body[i : i + PACKET]
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid != video or not pkt[1] & 0x40:
            continue
        control = (pkt[3] >> 4) & 0x3
        offset = 4 + (1 + pkt[4] if control == 3 else 0)
        pts = pes_pts(pkt[offset:])
        if pts is not None:
            frames.append((i, pts))
    n = max(1, math.ceil(duration / PART_TARGET), round(duration / PART_SPLIT))
    if not frames or n == 1:
        return [(0, duration)]
    first = frames[0][1]
    offsets = [0]
    times = [0.0]
    for k in range(1, n):
        want = k * duration / n
        off, pts = min(frames, key=lambda f: abs((f[1] - first) / 90000 - want))
        t = (pts - first) / 90000
        if off > offsets[-1] and times[-1] < t < duration:
            offsets.append(off)
            times.append(t)
    times.append(duration)
    return [(off, times[i + 1] - times[i]) for i, off in enumerate(offsets)]


def parse(text: str) -> tuple[int, int, int, list[tuple[float, str, bool]]]:
    """(media sequence, discontinuity sequence, target, [(duration, URI, discontinuity)])."""
    seq_m = re.search(r"MEDIA-SEQUENCE:(\d+)", text)
    target_m = re.search(r"TARGETDURATION:(\d+)", text)
    if seq_m is None or target_m is None:
        raise ValueError("not a media playlist")
    dseq_m = re.search(r"DISCONTINUITY-SEQUENCE:(\d+)", text)
    out = []
    dur, disc = 0.0, False
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            dur = float(line[8:].split(",")[0])
        elif line == "#EXT-X-DISCONTINUITY":
            disc = True
        elif line and not line.startswith("#"):
            out.append((dur, line, disc))
            disc = False
    return int(seq_m.group(1)), int(dseq_m.group(1)) if dseq_m else 0, int(target_m.group(1)), out


def render(n: int, slot: Slot, now: float) -> str:
    live = [s for s in slot.segs if s.parts_released(now) > 0]
    if not live:
        return ""
    wall = time.time() - now
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:9",
        f"#EXT-X-TARGETDURATION:{slot.target}",
        f"#EXT-X-SERVER-CONTROL:CAN-BLOCK-RELOAD=YES,PART-HOLD-BACK={3 * PART_TARGET:.2f}",
        f"#EXT-X-PART-INF:PART-TARGET={PART_TARGET}",
        f"#EXT-X-MEDIA-SEQUENCE:{live[0].seq}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{live[0].dseq - live[0].disc}",
    ]
    with_parts = live[-4:]
    for s in live:
        if s.disc and s is not live[0]:
            lines.append("#EXT-X-DISCONTINUITY")
        stamp = s.release + wall
        lines.append(
            "#EXT-X-PROGRAM-DATE-TIME:"
            + time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(stamp))
            + f".{int(stamp % 1 * 1000):03d}Z"
        )
        released = s.parts_released(now)
        if s in with_parts:
            for k in range(released):
                dur = s.cuts[k][1]
                flag = ",INDEPENDENT=YES" if k == 0 else ""
                lines.append(
                    f'#EXT-X-PART:DURATION={dur:.5f},URI="/streams/ll/{n}/part/{s.seq}.{k}.ts"{flag}'
                )
        if released == len(s.cuts):
            lines.append(f"#EXTINF:{s.duration:.5f},")
            lines.append(f"/streams/ll/{n}/seg/{s.seq}.ts")
    tail = live[-1]
    done = tail.parts_released(now)
    nxt = (tail.seq, done) if done < len(tail.cuts) else (tail.seq + 1, 0)
    lines.append(f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="/streams/ll/{n}/part/{nxt[0]}.{nxt[1]}.ts"')
    return "\n".join(lines) + "\n"


def _drop_unreleased(segs: list[Seg], now: float) -> list[Seg]:
    """Segments cut back to the parts released by `now`; unreleased ones go."""
    kept = []
    for s in segs:
        released = s.parts_released(now)
        if released == 0:
            continue
        if released < len(s.cuts):
            end = s.cuts[released][0]
            s.cuts = s.cuts[:released]
            s.duration = sum(d for _, d in s.cuts)
            s.body = s.body[:end]
        kept.append(s)
    return kept


def has(slot: Slot, msn: int, part: int | None, now: float) -> bool:
    for s in slot.segs:
        if s.seq == msn:
            if part is not None and part >= len(s.cuts):
                # a splice dropped this part: answered once the next segment starts
                continue
            got = s.parts_released(now)
            return got == len(s.cuts) if part is None else got > part
        if s.seq > msn:
            return True
    return False


class LLTrial:
    """Serves /streams/ll/{1-4} from the relay at `upstream`."""

    def __init__(self, upstream: str) -> None:
        self.upstream = upstream.rstrip("/")
        self.slots: dict[int, Slot] = {}
        self._session: aiohttp.ClientSession | None = None

    def register_routes(self, router: web.UrlDispatcher) -> None:
        router.add_get("/streams/ll/{n:[1-4]}", self.playlist)
        router.add_get("/streams/ll/{n:[1-4]}/part/{seq:\\d+}.{k:\\d+}.ts", self.part)
        router.add_get("/streams/ll/{n:[1-4]}/seg/{seq:\\d+}.ts", self.segment)

    def _slot(self, request: web.Request) -> tuple[int, Slot]:
        n = int(request.match_info["n"])
        slot = self.slots.setdefault(n, Slot())
        slot.seen = time.monotonic()
        if slot.task is None or slot.task.done():
            slot.segs = []
            slot.task = asyncio.create_task(self._follow(n, slot))
        return n, slot

    async def _follow(self, n: int, slot: Slot) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        session = self._session
        while time.monotonic() - slot.seen < IDLE_STOP:
            started = time.monotonic()
            try:
                async with session.get(f"{self.upstream}/streams/{n}") as resp:
                    text = await resp.text()
                seq, dseq, target, listed = parse(text)
                slot.target = target
                initial = not slot.segs
                backlog: list[Seg] = []
                last = slot.segs[-1].src if slot.segs else seq - 1
                for i, (dur, uri, disc) in enumerate(listed):
                    dseq += disc
                    s = seq + i
                    if s <= last:
                        continue
                    url = uri if uri.startswith("http") else self.upstream + uri
                    async with session.get(url) as resp:
                        body = await resp.read()
                    now = time.monotonic()
                    seg = Seg(s, dur, disc, dseq, body, cut(body, dur), src=s)
                    if initial:
                        backlog.append(seg)
                        continue
                    if disc:
                        # a splice: parts no player has seen yet are dropped, so the
                        # new channel goes out now instead of after the queue
                        before = sum(len(x.cuts) for x in slot.segs)
                        slot.segs = _drop_unreleased(slot.segs, now)
                        logger.info(
                            "ll trial splice",
                            slot=n,
                            since_poll=round(now - started, 2),
                            dropped_parts=before - sum(len(x.cuts) for x in slot.segs),
                        )
                    prev = slot.segs[-1] if slot.segs else None
                    if prev is not None:
                        # own numbering: dropping segments must not leave gaps
                        seg.seq = prev.seq + 1
                        seg.dseq = prev.dseq + disc
                    seg.release = max(now, prev.end()) if prev else now
                    slot.segs = (slot.segs + [seg])[-WINDOW:]
                if backlog:
                    # the backlog is already past: fully released, ending now
                    t = time.monotonic()
                    for seg in reversed(backlog):
                        t -= seg.duration
                        seg.release = t
                    slot.segs = backlog[-WINDOW:]
            except Exception as e:
                logger.warning("ll trial poll failed", slot=n, error=repr(e))
            await asyncio.sleep(POLL)

    async def _wait_for(self, slot: Slot, msn: int, part: int | None) -> None:
        deadline = time.monotonic() + BLOCK_WAIT
        while not has(slot, msn, part, time.monotonic()) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def playlist(self, request: web.Request) -> web.Response:
        n, slot = self._slot(request)
        q = request.query
        if "_HLS_msn" in q:
            part = int(q["_HLS_part"]) if "_HLS_part" in q else None
            await self._wait_for(slot, int(q["_HLS_msn"]), part)
        else:
            for _ in range(100):
                if render(n, slot, time.monotonic()):
                    break
                await asyncio.sleep(0.1)
        body = render(n, slot, time.monotonic())
        if not body:
            return web.Response(status=503, text="no segments")
        return web.Response(
            text=body,
            content_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache"},
        )

    async def part(self, request: web.Request) -> web.Response:
        _, slot = self._slot(request)
        seq, k = int(request.match_info["seq"]), int(request.match_info["k"])
        await self._wait_for(slot, seq, k)
        seg = next((s for s in slot.segs if s.seq == seq), None)
        if seg is None or k >= len(seg.cuts) or seg.parts_released(time.monotonic()) <= k:
            return web.Response(status=404)
        start = seg.cuts[k][0]
        end = seg.cuts[k + 1][0] if k + 1 < len(seg.cuts) else len(seg.body)
        return web.Response(body=seg.body[start:end], content_type="video/mp2t")

    async def segment(self, request: web.Request) -> web.Response:
        _, slot = self._slot(request)
        seq = int(request.match_info["seq"])
        seg = next((s for s in slot.segs if s.seq == seq), None)
        if seg is None:
            return web.Response(status=404)
        return web.Response(body=seg.body, content_type="video/mp2t")

    async def aclose(self) -> None:
        for slot in self.slots.values():
            if slot.task is not None:
                slot.task.cancel()
        if self._session is not None:
            await self._session.close()
