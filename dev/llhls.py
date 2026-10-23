"""Low-latency HLS trial: re-serve a running relay's slots as LL-HLS.

usage: PYTHONPATH=src python dev/llhls.py [--upstream https://quadlink.et42.net] [--port 8091]

Polls the upstream's /streams/N, cuts each new segment into parts of about
half a second at video frame boundaries, and releases those parts in real time
as LL-HLS (EXT-X-PART, blocking playlist reload, preload hints). /compare plays
/streams/1 and /ll/1 side by side and shows how far each sits behind its edge.

Parts exist only once the relay lists the whole segment, so this measures the
player's hold-back, not a relay that emits parts as it converts.
"""

import argparse
import asyncio
import math
import re
import time
from dataclasses import dataclass, field

import aiohttp
from aiohttp import web

from quadlink.relay.tsinfo import PACKET, pes_pts, program

VIDEO_TYPES = {0x1B, 0x24}
# Apple rejects a playlist whose parts fall under 85% of PART-TARGET
PART_SPLIT = 0.5
PART_TARGET = 0.55
POLL = 0.5
BLOCK_WAIT = 6.0
WINDOW = 12


@dataclass
class Seg:
    seq: int
    duration: float
    disc: bool
    dseq: int
    body: bytes
    cuts: list[tuple[int, float]]  # (byte offset, part duration)
    release: float = 0.0  # monotonic time its first part is released

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
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


def cut(body: bytes, duration: float) -> list[tuple[int, float]]:
    """Byte offsets of parts at video PES starts, about PART_SPLIT seconds apart."""
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
        if off > offsets[-1] and t > times[-1]:
            offsets.append(off)
            times.append(t)
    times.append(duration)
    return [(off, times[i + 1] - times[i]) for i, off in enumerate(offsets)]


def parse(text: str) -> tuple[int, int, int, list[tuple[float, str, bool]]]:
    seq = int(re.search(r"MEDIA-SEQUENCE:(\d+)", text).group(1))
    dseq_m = re.search(r"DISCONTINUITY-SEQUENCE:(\d+)", text)
    dseq = int(dseq_m.group(1)) if dseq_m else 0
    target = int(re.search(r"TARGETDURATION:(\d+)", text).group(1))
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
    return seq, dseq, target, out


async def follow(app: web.Application, n: int, slot: Slot) -> None:
    session: aiohttp.ClientSession = app["session"]
    base = app["upstream"]
    while True:
        try:
            async with session.get(f"{base}/streams/{n}") as resp:
                text = await resp.text()
            seq, dseq, target, listed = parse(text)
            slot.target = target
            initial = not slot.segs
            last = slot.segs[-1].seq if slot.segs else seq - 1
            for i, (dur, uri, disc) in enumerate(listed):
                dseq += disc
                s = seq + i
                if s <= last:
                    continue
                url = uri if uri.startswith("http") else base + uri
                async with session.get(url) as resp:
                    body = await resp.read()
                now = time.monotonic()
                seg = Seg(s, dur, disc, dseq, body, cut(body, dur))
                prev = slot.segs[-1] if slot.segs else None
                seg.release = max(now, prev.end()) if prev else now - dur
                slot.segs = (slot.segs + [seg])[-WINDOW:]
                print(f"slot {n} seg {s} {dur:.2f}s parts {len(seg.cuts)} "
                      f"lag {seg.release - now:+.2f}s")
            if initial and slot.segs:
                # the backlog is already past: fully released, ending now
                t = time.monotonic()
                for seg in reversed(slot.segs):
                    t -= seg.duration
                    seg.release = t
        except Exception as e:
            print(f"slot {n} poll failed: {e!r}")
        slot.changed.set()
        slot.changed = asyncio.Event()
        await asyncio.sleep(POLL)


def get_slot(request: web.Request) -> tuple[int, Slot]:
    n = int(request.match_info["n"])
    slots: dict[int, Slot] = request.app["slots"]
    slot = slots.setdefault(n, Slot())
    if slot.task is None:
        slot.task = asyncio.create_task(follow(request.app, n, slot))
    return n, slot


def render(n: int, slot: Slot, now: float) -> str:
    live = [s for s in slot.segs if s.parts_released(now) > 0]
    if not live:
        return ""
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
    wall = time.time() - now
    for s in live:
        if s.disc and s is not live[0]:
            lines.append("#EXT-X-DISCONTINUITY")
        released = s.parts_released(now)
        stamp = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(s.release + wall))
        millis = int((s.release + wall) % 1 * 1000)
        lines.append(f'#EXT-X-PROGRAM-DATE-TIME:{stamp}.{millis:03d}Z')
        if s in with_parts:
            for k in range(released):
                off, dur = s.cuts[k]
                flag = ",INDEPENDENT=YES" if k == 0 else ""
                lines.append(f'#EXT-X-PART:DURATION={dur:.5f},URI="/ll/{n}/part/{s.seq}.{k}.ts"{flag}')
        if released == len(s.cuts):
            lines.append(f"#EXTINF:{s.duration:.5f},")
            lines.append(f"/ll/{n}/seg/{s.seq}.ts")
    tail = live[-1]
    done = tail.parts_released(now)
    nxt = (tail.seq, done) if done < len(tail.cuts) else (tail.seq + 1, 0)
    lines.append(f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="/ll/{n}/part/{nxt[0]}.{nxt[1]}.ts"')
    return "\n".join(lines) + "\n"


def has(slot: Slot, msn: int, part: int | None, now: float) -> bool:
    for s in slot.segs:
        if s.seq == msn:
            got = s.parts_released(now)
            return got == len(s.cuts) if part is None else got > part
        if s.seq > msn:
            return True
    return False


async def wait_for(slot: Slot, msn: int, part: int | None) -> None:
    deadline = time.monotonic() + BLOCK_WAIT
    while not has(slot, msn, part, time.monotonic()) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


async def playlist(request: web.Request) -> web.Response:
    n, slot = get_slot(request)
    q = request.query
    if "_HLS_msn" in q:
        part = int(q["_HLS_part"]) if "_HLS_part" in q else None
        await wait_for(slot, int(q["_HLS_msn"]), part)
    else:
        for _ in range(100):
            if render(n, slot, time.monotonic()):
                break
            await asyncio.sleep(0.1)
    now = time.monotonic()
    tail = slot.segs[-1] if slot.segs else None
    print(f"GET {request.path_qs} -> tail {tail.seq if tail else '-'}"
          f".{tail.parts_released(now) if tail else '-'}")
    return web.Response(
        text=render(n, slot, now),
        content_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache"},
    )


async def part(request: web.Request) -> web.Response:
    _, slot = get_slot(request)
    seq, k = map(int, request.match_info["id"].split("."))
    await wait_for(slot, seq, k)
    seg = next((s for s in slot.segs if s.seq == seq), None)
    if seg is None or k >= len(seg.cuts):
        return web.Response(status=404)
    start = seg.cuts[k][0]
    end = seg.cuts[k + 1][0] if k + 1 < len(seg.cuts) else len(seg.body)
    return web.Response(body=seg.body[start:end], content_type="video/mp2t")


async def segment(request: web.Request) -> web.Response:
    _, slot = get_slot(request)
    seq = int(request.match_info["seq"])
    seg = next((s for s in slot.segs if s.seq == seq), None)
    if seg is None:
        return web.Response(status=404)
    return web.Response(body=seg.body, content_type="video/mp2t")


async def proxy(request: web.Request) -> web.StreamResponse:
    session: aiohttp.ClientSession = request.app["session"]
    async with session.get(request.app["upstream"] + request.path_qs) as up:
        body = await up.read()
        return web.Response(
            body=body,
            status=up.status,
            content_type=up.content_type,
            headers={"Cache-Control": "no-cache"},
        )


COMPARE = """<!doctype html><meta charset="utf-8"><title>LL-HLS trial</title>
<style>body{background:#111;color:#eee;font:14px system-ui;margin:16px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}video{width:100%;background:#000}
b{font-variant-numeric:tabular-nums}</style>
<div class="row">
<div><div>normal <code>/streams/N</code>: <b id="a"></b></div><video id="va" muted autoplay playsinline></video></div>
<div><div>LL-HLS <code>/ll/N</code>: <b id="b"></b></div><video id="vb" muted autoplay playsinline></video></div>
</div>
<p>Behind = seekable end minus playhead. Native player only (Safari).</p>
<script>
const n = new URLSearchParams(location.search).get('n') || '1';
va.src = '/streams/' + n; vb.src = '/ll/' + n;
setInterval(() => {
  for (const [v, out] of [[va, a], [vb, b]]) {
    const s = v.seekable;
    if (v.error) { out.textContent = 'error ' + v.error.code + ' ' + (v.error.message || ''); continue; }
    out.textContent = s.length ? (s.end(s.length - 1) - v.currentTime).toFixed(2) + ' s behind' : '-';
  }
}, 250);
</script>
"""


async def compare(request: web.Request) -> web.Response:
    return web.Response(text=COMPARE, content_type="text/html")


async def _session(app: web.Application):  # type: ignore[no-untyped-def]
    app["session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(sock_read=30))
    yield
    for slot in app["slots"].values():
        if slot.task:
            slot.task.cancel()
    await app["session"].close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", default="https://quadlink.et42.net")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    app = web.Application()
    app["upstream"] = args.upstream.rstrip("/")
    app["slots"] = {}
    app.cleanup_ctx.append(_session)
    app.router.add_get("/compare", compare)
    app.router.add_get("/ll/{n:[1-4]}", playlist)
    app.router.add_get("/ll/{n:[1-4]}/part/{id}.ts", part)
    app.router.add_get("/ll/{n:[1-4]}/seg/{seq:\\d+}.ts", segment)
    app.router.add_get("/streams/{path:.+}", proxy)
    print(f"http://{args.host}:{args.port}/compare")
    import logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    web.run_app(app, host=args.host, port=args.port, print=None,
                access_log_format='%r %s %b %{User-Agent}i')


if __name__ == "__main__":
    main()
