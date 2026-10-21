"""Relay memory and CPU benchmark: 4 slots through SlotRelay, SegmentProxy and ffmpeg.

Run from the repo root, inside `nix develop`:

    python bench/relay_memory.py gen                  # build media in bench/data (once)
    python bench/relay_memory.py run --duration 600   # accelerated, 0.25 s per 2 s segment
    python bench/relay_memory.py run --realtime --duration 600
    python bench/relay_memory.py run --tracemalloc --profile --duration 600
    python bench/relay_memory.py micro                # per-call costs: tsinfo, _scan, fetch

`run` uses three processes so the measured one holds only the relay:

- cdn: serves the generated segments over HTTP. It shifts PTS/DTS/PCR by one loop
  length per pass, so a channel plays as one continuous broadcast of any length.
- relay (this process): aiohttp TCPSite + SlotRelay + SegmentProxy + an httpx segment
  client, wired as in the daemon. Source playlists come from an in-process model of
  Twitch: a 15-segment window, one new 2 s segment per tick, a channel switch per slot
  every --switch-every seconds and a delivery stall every --stall-every seconds, so
  runs retire and harvest and filler appears.
- clients: per slot, --clients players that poll the playlist once per tick, start 3
  segments behind the newest and fetch every newly listed segment once, in order.

The relay clock runs at 2/tick times real time; the proxy's own waits (CONVERT_WAIT,
FEED_WAIT, STEP_WAIT, polling) stay in real seconds. Accelerated runs therefore poll
less per segment and start extra runs (a poll held by a 1 s wait looks like a 10 s idle
gap to the relay clock). Use --realtime for per-segment CPU and run-start behaviour.

Variants: --fetch stream reads segments without storing them on the httpx Response;
--kf-slots 3 gives slot 3 an extra keyframe 1.2 s into each segment; --keep sets the
cache size; --no-convert passes audio through; --src DIR imports quadlink from another
source tree (e.g. one unpacked with `git archive <commit> src`).

Samples go to stdout and bench/data/out/<label>-samples.jsonl; the end of the run
prints large bytes objects grouped by referrer. Process memory comes from /proc on
Linux (RssAnon) and from ps plus phys_footprint on macOS. macOS numbers do not
transfer: RSS keeps freed malloc pages and the compressor shrinks the footprint, and
glibc heap retention exists only on Linux. For production-like numbers, run in a
Linux container with python 3.13, ffmpeg, aiohttp, httpx and structlog, with the repo
mounted and --workroot on a container-local path:

    docker run --rm -v "$PWD":/w -w /w <image> \\
        python bench/relay_memory.py run --realtime --duration 720 --workroot /tmp/qr
"""

import argparse
import asyncio
import cProfile
import gc
import io
import json
import os
import pstats
import random
import re
import resource
import shutil
import subprocess
import sys
import time
import tracemalloc
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = DATA / "out"
# --src picks the source tree before quadlink is imported, so one bench compares commits
SRC = sys.argv[sys.argv.index("--src") + 1] if "--src" in sys.argv else str(ROOT.parent / "src")
sys.path.insert(0, SRC)

import httpx  # noqa: E402
import structlog  # noqa: E402
from aiohttp import ClientSession, web  # noqa: E402

from quadlink.relay import convert, segments  # noqa: E402
from quadlink.relay.server import SlotRelay  # noqa: E402
from quadlink.relay.store import SlotStore  # noqa: E402
from quadlink.relay.tsinfo import (  # noqa: E402
    _VIDEO_TYPES,
    PACKET,
    _packets,
    _pes_starts,
    _pid,
    audio_sample_rate,
    first_video_pts,
)

RATES = (44100, 48000)
SEG = 2.0
WINDOW = 15
PTS_WRAP = 1 << 33
MIB = 1024 * 1024
# the nix dev shell's ps lacks cputime
PS = "/bin/ps" if Path("/bin/ps").exists() else "ps"


# media generation


def media_dir(name: str) -> Path:
    return DATA / "media" / name


def media_names() -> list[str]:
    return [f"{rate}{kf}" for rate in RATES for kf in ("", "kf")]


def gen(args: argparse.Namespace) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg not on PATH"
    for name in media_names():
        rate, kf = int(name[:5]), name.endswith("kf")
        out = media_dir(name)
        if out.exists() and len(list(out.glob("s*.ts"))) >= args.count and not args.force:
            print(f"{out}: present")
            continue
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        t0 = time.monotonic()
        # CBR with filler NALs gives Twitch-sized segments from cheap synthetic video;
        # keyframes only at 2 s boundaries, as Twitch requires of broadcasters
        x264 = "bframes=2:nal-hrd=cbr:keyint=120:min-keyint=120:scenecut=0"
        br = args.video_kbps
        # the kf set adds an IDR 1.2 s into every segment, as some Twitch channels have
        extra = ["-force_key_frames", "expr:eq(mod(n,120),0)+eq(mod(n,120),72)"] if kf else []
        extra += ["-forced-idr", "1"] if kf else []
        subprocess.run(
            [
                ffmpeg, "-v", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=s={args.size}:r=60",
                "-f", "lavfi", "-i", f"sine=f=440:r={rate}",
                "-f", "lavfi", "-i", f"anoisesrc=r={rate}:a=0.05",
                "-filter_complex", "[1:a][2:a]amix=inputs=2[a]",
                "-t", str(SEG * args.count),
                "-map", "0:v", "-map", "[a]",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-x264-params", x264, *extra,
                "-b:v", f"{br}k", "-minrate", f"{br}k", "-maxrate", f"{br}k",
                "-bufsize", f"{br}k",
                "-c:a", "aac", "-b:a", "160k", "-ar", str(rate), "-ac", "2",
                "-output_ts_offset", "10",
                "-f", "segment", "-segment_time", "2", "-segment_format", "mpegts",
                "-reset_timestamps", "0",
                str(out / "s%04d.ts"),
            ],
            check=True,
        )  # fmt: skip
        files = sorted(out.glob("s*.ts"))
        sizes = [p.stat().st_size for p in files]
        print(
            f"{out}: {len(files)} segments, {sum(sizes) / len(sizes) / MIB:.2f} MiB avg, "
            f"{min(sizes) / MIB:.2f}-{max(sizes) / MIB:.2f} MiB, {time.monotonic() - t0:.0f}s"
        )


def load_media(name: str) -> list[bytes]:
    files = sorted(media_dir(name).glob("s*.ts"))
    if not files:
        sys.exit(f"no media in {media_dir(name)}: run `python bench/relay_memory.py gen`")
    return [p.read_bytes() for p in files]


# cdn role


def _ts_patches(data: bytes) -> list[tuple[int, str, int]]:
    """(offset, kind, value) of every PCR base and PES PTS/DTS in a segment."""
    found = []
    for i in range(0, len(data) - PACKET + 1, PACKET):
        if data[i] != 0x47:
            continue
        control = (data[i + 3] >> 4) & 3
        payload = i + 4
        if control in (2, 3):
            alen = data[i + 4]
            if alen and data[i + 5] & 0x10:
                b = data[i + 6 : i + 12]
                base = b[0] << 25 | b[1] << 17 | b[2] << 9 | b[3] << 1 | b[4] >> 7
                found.append((i + 6, "pcr", base))
            payload = i + 5 + alen
        if not data[i + 1] & 0x40 or control == 2 or data[payload : payload + 3] != b"\0\0\1":
            continue
        flags = data[payload + 7] >> 6
        for pos, present in ((payload + 9, flags & 2), (payload + 14, flags == 3)):
            if present:
                p = data[pos : pos + 5]
                v = ((p[0] >> 1) & 7) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1
                found.append((pos, "ts", v))
    return found


def _shifted(data: bytes, patches: list[tuple[int, str, int]], offset: int) -> bytes:
    if not offset:
        return data
    buf = bytearray(data)
    for pos, kind, value in patches:
        v = (value + offset) % PTS_WRAP
        if kind == "pcr":
            buf[pos : pos + 4] = (v >> 1).to_bytes(4, "big")
            buf[pos + 4] = (v & 1) << 7 | (buf[pos + 4] & 0x7F)
        else:
            prefix = buf[pos] & 0xF0
            buf[pos] = prefix | ((v >> 30) & 7) << 1 | 1
            buf[pos + 1] = (v >> 22) & 0xFF
            buf[pos + 2] = ((v >> 15) & 0x7F) << 1 | 1
            buf[pos + 3] = (v >> 7) & 0xFF
            buf[pos + 4] = (v & 0x7F) << 1 | 1
    return bytes(buf)


async def cdn(args: argparse.Namespace) -> None:
    media = {name: load_media(name) for name in media_names()}
    patches = {name: [_ts_patches(d) for d in segs] for name, segs in media.items()}

    async def handle(request: web.Request) -> web.Response:
        name, index = request.match_info["name"], int(request.match_info["index"])
        segs = media[name]
        loop, i = divmod(index, len(segs))
        offset = loop * len(segs) * int(SEG * 90000)
        body = _shifted(segs[i], patches[name][i], offset)
        return web.Response(body=body, content_type="video/mp2t")

    app = web.Application()
    app.router.add_get("/{name}/{index}.ts", handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    print(port, flush=True)
    # parent closes stdin to stop us
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)
    await runner.cleanup()


# clients role


async def clients(args: argparse.Namespace) -> None:
    base = f"http://127.0.0.1:{args.port}"
    stats: dict[str, Any] = {
        "segments": 0,
        "filler": 0,
        "bytes": 0,
        "errors": {},
        "unconverted_44100": 0,
        "short_video": 0,
        "latency": [],
        "stale_polls": 0,
        "playlist_latency": [],
    }
    deadline = time.monotonic() + args.duration

    async def player(session: ClientSession, slot: int) -> None:
        await asyncio.sleep(random.uniform(0, args.tick))
        last: int | None = None
        changed_at = time.monotonic()
        last_text = ""
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            async with session.get(f"{base}/streams/{slot}") as resp:
                text = await resp.text()
                ok = resp.status == 200
            stats["playlist_latency"].append(time.monotonic() - t0)
            if not ok:
                stats["errors"][f"playlist {resp.status}"] = (
                    stats["errors"].get(f"playlist {resp.status}", 0) + 1
                )
            else:
                if text != last_text:
                    last_text, changed_at = text, time.monotonic()
                elif time.monotonic() - changed_at > 1.5 * args.tick:
                    # AVPlayer fails a playlist unchanged for 1.5 x target duration
                    stats["stale_polls"] += 1
                m = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
                first = int(m.group(1)) if m else 0
                uris = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
                listed = [(first + n, uri) for n, uri in enumerate(uris)]
                if last is None and listed:
                    last = listed[max(0, len(listed) - 4)][0]
                for seq, uri in listed:
                    if last is not None and seq <= last:
                        continue
                    last = seq
                    t1 = time.monotonic()
                    async with session.get(base + uri) as seg:
                        body = await seg.read()
                        status = seg.status
                    if status != 200:
                        key = f"segment {status}"
                        stats["errors"][key] = stats["errors"].get(key, 0) + 1
                        continue
                    stats["latency"].append(time.monotonic() - t1)
                    stats["bytes"] += len(body)
                    if "filler" in uri:
                        stats["filler"] += 1
                        continue
                    stats["segments"] += 1
                    if audio_sample_rate(body) == 44100:
                        stats["unconverted_44100"] += 1
                    # every source segment holds 2 s = 120 frames of 60 fps video
                    if video_frames(body) < 110:
                        stats["short_video"] += 1
            await asyncio.sleep(max(0.0, args.tick - (time.monotonic() - t0)))

    async with ClientSession() as session:
        tasks = [
            player(session, slot) for slot in range(1, args.slots + 1) for _ in range(args.clients)
        ]
        await asyncio.gather(*tasks)
    for key in ("latency", "playlist_latency"):
        values = sorted(stats[key]) or [0.0]
        stats[key] = {
            "p50": values[len(values) // 2],
            "p95": values[int(len(values) * 0.95)],
            "max": values[-1],
        }
    print(json.dumps(stats), flush=True)


# process accounting


def proc_stats(pid: int) -> dict[str, float] | None:
    """rss and anon in MiB, cpu in seconds; anon equals rss where the OS has no split."""
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        try:
            fields = dict(ln.split(":", 1) for ln in status.read_text().splitlines() if ":" in ln)
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            return None
        tick = os.sysconf("SC_CLK_TCK")

        def kib(name: str) -> float:
            return int(fields.get(name, "0 kB").split()[0]) / 1024

        return {
            "rss": kib("VmRSS"),
            "anon": kib("RssAnon"),
            "cpu": (int(stat[11]) + int(stat[12])) / tick,
        }
    out = subprocess.run(
        [PS, "-o", "rss=,cputime=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.split()
    if len(out) < 2:
        return None
    parts = [float(x) for x in out[1].replace("-", ":").split(":")]
    cpu = 0.0
    for part in parts:
        cpu = cpu * 60 + part
    rss = int(out[0]) / 1024
    return {"rss": rss, "anon": _footprint(pid) or rss, "cpu": cpu}


def _footprint(pid: int) -> float | None:
    """macOS phys_footprint in MiB: dirty private memory, without MADV_FREE pages.

    macOS RSS keeps freed-but-reusable malloc pages until memory pressure, so it
    overstates live memory; the footprint is the closest match to Linux RssAnon.
    """
    import ctypes

    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
    except OSError:
        return None
    # struct rusage_info_v0: uuid[16] then u64 fields; ri_phys_footprint is the 8th
    buf = (ctypes.c_uint64 * 12)()
    if lib.proc_pid_rusage(pid, 0, ctypes.byref(buf)) != 0:
        return None
    return buf[9] / MIB


def ffmpeg_pids() -> list[int]:
    out = subprocess.run(
        [PS, "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True
    ).stdout
    me = os.getpid()
    pids = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and int(parts[1]) == me and "ffmpeg" in parts[2]:
            pids.append(int(parts[0]))
    return pids


def video_frames(data: bytes) -> int:
    pid = _pid(data, _VIDEO_TYPES)
    return 0 if pid is None else sum(1 for _ in _pes_starts(data, pid))


def disk_usage(root: Path) -> tuple[float, int, dict[str, int]]:
    """Total MiB and file count under root, and file count per slot."""
    total, count = 0, 0
    per_slot: dict[str, int] = {}
    for dirpath, _, names in os.walk(root):
        slot = Path(dirpath).name.split("-")[0]
        for name in names:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
            count += 1
            per_slot[slot] = per_slot.get(slot, 0) + 1
    return total / MIB, count, dict(sorted(per_slot.items()))


def _outputs(conv: Any) -> dict[int, list[Path]]:
    """Input video PTS -> output pieces; --src can load either converter layout."""
    if hasattr(conv, "_by_pts"):
        return {pts: [path] for pts, path in conv._by_pts.items()}
    return {pts: list(rng.pieces) for pts, rng in conv._ranges.items()}


def _dropped(conv: Any) -> int:
    if hasattr(conv, "_dropped"):
        return len(conv._dropped)
    return sum(1 for rng in conv._ranges.values() if rng.dropped)


def orphans() -> tuple[int, float]:
    """Scanned outputs that no live entry maps to, so discard() never deletes them.

    An output whose first video PTS matches no entry (ffmpeg cut a segment at a
    keyframe inside it) stays on disk and in _by_pts until the run's close().
    """
    live = [o for o in gc.get_objects() if isinstance(o, segments._Entry)]
    n, size = 0, 0
    for conv in list(CONVERTERS):
        wanted = {e.pts for e in live if e.converter is conv}
        reachable = {p for pts, paths in _outputs(conv).items() if pts in wanted for p in paths}
        for path in conv.workdir.glob("o*.ts"):
            if int(path.name[1:-3]) < conv._next and path not in reachable:
                try:
                    size += path.stat().st_size
                except OSError:
                    continue
                n += 1
    return n, size / MIB


CONVERTERS: "weakref.WeakSet[convert.AudioConverter]" = weakref.WeakSet()


def _track_converters() -> None:
    init = convert.AudioConverter.__init__

    def tracked(self: convert.AudioConverter, *a: Any, **kw: Any) -> None:
        init(self, *a, **kw)
        CONVERTERS.add(self)

    convert.AudioConverter.__init__ = tracked  # type: ignore[method-assign]


def proxy_stats(proxy: segments.SegmentProxy, relay: SlotRelay) -> dict[str, Any]:
    s: dict[str, Any] = dict.fromkeys(
        ["entries", "waiting", "queued", "raw_mib", "raw_n", "conv_mib", "conv_n"], 0
    )
    for state in proxy._slots.values():
        s["entries"] += len(state.entries)
        s["waiting"] += len(state.waiting)
        s["queued"] += state.queue.qsize()
    # every live entry, cached or not, so held bytes outside the cache show too
    live = [o for o in gc.get_objects() if isinstance(o, segments._Entry)]
    s["entry_objs"] = len(live)
    for e in live:
        if e.raw.done() and not e.raw.cancelled() and e.raw.exception() is None:
            n = len(e.raw.result())
            if n:
                s["raw_mib"] += n / MIB
                s["raw_n"] += 1
        if e.converted is not None:
            s["conv_mib"] += len(e.converted) / MIB
            s["conv_n"] += 1
    convs = list(CONVERTERS)
    s["converters"] = len(convs)
    s["running"] = sum(1 for c in convs if c._proc is not None)
    s["by_pts"] = sum(len(_outputs(c)) for c in convs)
    s["dropped"] = sum(_dropped(c) for c in convs)
    s["retiring"] = len(proxy._retiring)
    s["tasks"] = len(asyncio.all_tasks())
    s["relay_cache"] = len(relay._cache)
    return s


def _holder(o: Any) -> str:
    if asyncio.iscoroutine(o) and hasattr(o, "cr_frame"):
        frame = o.cr_frame
        where = f":{frame.f_lineno}" if frame is not None else ""
        return f"coroutine {o.__qualname__}{where}"
    if isinstance(o, dict):
        return "dict{" + ",".join(sorted(str(k) for k in list(o)[:6])) + "}"
    name = f"{type(o).__module__}.{type(o).__qualname__}"
    if isinstance(o, segments._Entry):
        return name
    # a holder that is not an entry is often garbage in a cycle; its referrers show which
    up = sorted({_up(r) for r in gc.get_referrers(o)} - {"list", "frame"})
    return f"{name}<-[{','.join(up)}]"


def _up(o: Any) -> str:
    if asyncio.iscoroutine(o) and hasattr(o, "cr_frame"):
        frame = o.cr_frame
        return f"coroutine {o.__qualname__}:{frame.f_lineno if frame else ''}"
    if isinstance(o, dict):
        return "dict{" + ",".join(sorted(str(k) for k in list(o)[:4])) + "}"
    return type(o).__qualname__


def census(min_size: int = 256 * 1024) -> list[tuple[str, int, float]]:
    """(holders, count, MiB) of large bytes objects, grouped by what references them.

    bytes are not GC-tracked, so they are found through their GC-tracked referrers;
    memory referenced only from C (socket and pipe buffers) does not show here.
    """
    found: dict[int, tuple[int, set[str]]] = {}
    for o in gc.get_objects():
        for ref in gc.get_referents(o):
            if type(ref) is bytes and len(ref) >= min_size:
                found.setdefault(id(ref), (len(ref), set()))[1].add(_holder(o))
    groups: dict[str, list[float]] = {}
    for size, names in found.values():
        g = groups.setdefault(" + ".join(sorted(names)), [0, 0.0])
        g[0] += 1
        g[1] += size / MIB
    return sorted(((k, int(v[0]), v[1]) for k, v in groups.items()), key=lambda x: -x[2])


class LogCounter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def __call__(self, _logger: Any, level: str, event: dict[str, Any]) -> Any:
        key = f"{level}: {event.get('event')}"
        self.counts[key] = self.counts.get(key, 0) + 1
        raise structlog.DropEvent


# relay role


@dataclass
class Channel:
    identity: str
    rate: int
    start: float
    media: str
    frozen: int | None = None

    def newest(self, now: float) -> int:
        if self.frozen is not None:
            return self.frozen
        return int((now - self.start) // SEG)


async def run(args: argparse.Namespace) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tick = SEG if args.realtime else args.tick
    speed = SEG / tick
    label = args.label or f"{'rt' if args.realtime else 'x' + format(speed, 'g')}-keep{args.keep}"
    log = LogCounter()
    structlog.configure(processors=[log])
    _track_converters()
    if args.tracemalloc:
        tracemalloc.start(args.frames)

    me = sys.executable, str(Path(__file__).resolve())
    cdn_proc = await asyncio.create_subprocess_exec(
        *me, "cdn", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
    )
    assert cdn_proc.stdout is not None and cdn_proc.stdin is not None
    cdn_port = int(await cdn_proc.stdout.readline())

    t0 = time.monotonic()

    def clock() -> float:
        return 1000.0 + (time.monotonic() - t0) * speed

    channels: dict[int, Channel] = {}
    generation = dict.fromkeys(range(1, args.slots + 1), 0)
    store = SlotStore()

    def publish() -> None:
        ids = [channels[s].identity if s in channels else "" for s in range(1, 5)]
        urls = [f"src://{s}/{generation[s]}" if s in channels else "" for s in range(1, 5)]
        store.update(urls, ids)

    def new_channel(slot: int, rate: int) -> None:
        generation[slot] += 1
        media = f"{rate}kf" if slot in args.kf_slots else str(rate)
        # a live channel joins with a full window already published
        channels[slot] = Channel(
            f"chan{slot}-{generation[slot]}", rate, clock() - WINDOW * SEG, media
        )

    async def playlist(url: str) -> str:
        slot = int(url.split("/")[2])
        ch = channels[slot]
        newest = ch.newest(clock())
        first = max(0, newest - WINDOW + 1)
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for i in range(first, newest + 1):
            lines += [f"#EXTINF:{SEG:.3f},live", f"http://127.0.0.1:{cdn_port}/{ch.media}/{i}.ts"]
        return "\n".join(lines) + "\n"

    fetched = {"n": 0, "bytes": 0}
    seg_client = httpx.AsyncClient(timeout=5)

    async def fetch_segment(uri: str) -> bytes:
        if args.fetch == "stream":
            # the body is never stored on the Response, which httpx keeps in a reference
            # cycle (Response <-> BoundAsyncStream) that only the cyclic GC frees
            async with seg_client.stream("GET", uri) as resp:
                resp.raise_for_status()
                data = b"".join([chunk async for chunk in resp.aiter_bytes()])
        else:
            # the fetch Daemon._fetch_segment does in production
            resp = await seg_client.get(uri)
            resp.raise_for_status()
            data = resp.content
        fetched["n"] += 1
        fetched["bytes"] += len(data)
        return data

    workroot = Path(args.workroot or DATA / "work") / label
    ffmpeg = None if args.no_convert else shutil.which("ffmpeg")
    proxy = segments.SegmentProxy(fetch_segment, ffmpeg, workroot, keep=args.keep)
    relay = SlotRelay(store, fetch=playlist, time_fn=clock, segments=proxy)
    app = web.Application()
    relay.register_routes(app.router)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    for slot in range(1, args.slots + 1):
        new_channel(slot, RATES[slot % 2])
    publish()

    async def director() -> None:
        # per-slot offsets keep switches and stalls from landing on every slot at once
        nxt_switch = {s: clock() + args.switch_every * s / 4 for s in channels}
        nxt_stall = {s: clock() + args.stall_every * (s + 0.5) / 4 for s in channels}
        while True:
            now = clock()
            for s, ch in channels.items():
                if args.switch_every and now >= nxt_switch[s]:
                    new_channel(s, RATES[(RATES.index(ch.rate) + 1) % 2])
                    publish()
                    nxt_switch[s] += args.switch_every
                if args.stall_every and now >= nxt_stall[s] and ch.frozen is None:
                    ch.frozen = ch.newest(now)
                    nxt_stall[s] += args.stall_every
                if ch.frozen is not None and now >= nxt_stall[s] - args.stall_every + 8:
                    ch.frozen = None
            await asyncio.sleep(tick / 4)

    director_task = asyncio.create_task(director())
    client_proc = await asyncio.create_subprocess_exec(
        *me, "clients", "--port", str(port), "--slots", str(args.slots),
        "--clients", str(args.clients), "--tick", str(tick), "--duration", str(args.duration),
        stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip

    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    samples: list[dict[str, Any]] = []
    early: tracemalloc.Snapshot | None = None
    ff_cpu: dict[int, float] = {}
    prev_ff_cpu = 0.0
    prev_py_cpu = 0.0
    prev_t = 0.0
    sample_file = (OUT / f"{label}-samples.jsonl").open("w")
    header = (
        f"{'t':>5} {'segs':>5} {'pyRSS':>6} {'anon':>6} {'trace':>6} {'conv':>6} {'raw':>6} "
        f"{'ent':>4} {'obj':>4} {'wait':>4} {'bypts':>5} {'drop':>4} {'cvs':>3} {'ret':>3} "
        f"{'ffN':>3} {'ffRSS':>6} {'ff%':>5} {'py%':>5} {'disk':>6} {'files':>5} {'orph':>4} "
        "files/slot"
    )
    print(f"relay :{port} cdn :{cdn_port} speed x{speed:g} label {label}\n{header}", flush=True)
    end = t0 + args.duration
    while time.monotonic() < end:
        await asyncio.sleep(min(args.sample, max(0.0, end - time.monotonic())))
        t = time.monotonic() - t0
        if args.tracemalloc and early is None and t >= args.duration * 0.25:
            early = tracemalloc.take_snapshot()
        me_stats = proc_stats(os.getpid()) or {"rss": 0, "anon": 0}
        ru = resource.getrusage(resource.RUSAGE_SELF)
        py_cpu = ru.ru_utime + ru.ru_stime
        ffs = [(pid, proc_stats(pid)) for pid in ffmpeg_pids()]
        for pid, st in ffs:
            if st:
                ff_cpu[pid] = st["cpu"]
        ff_total = sum(ff_cpu.values())
        disk, files, files_by_slot = disk_usage(workroot)
        orphan_n, orphan_mib = orphans()
        sample = {
            "t": round(t, 1),
            "segments_per_slot": fetched["n"] / args.slots,
            "py_rss": me_stats["rss"],
            "py_anon": me_stats["anon"],
            "traced": tracemalloc.get_traced_memory()[0] / MIB if args.tracemalloc else None,
            "py_cpu_s": py_cpu,
            "py_cpu_pct": 100 * (py_cpu - prev_py_cpu) / max(1e-9, t - prev_t),
            "ff_n": len(ffs),
            "ff_rss": sum(st["rss"] for _, st in ffs if st),
            "ff_anon": sum(st["anon"] for _, st in ffs if st),
            "ff_rss_each": [round(st["rss"], 1) for _, st in ffs if st],
            "ff_cpu_s": ff_total,
            "ff_cpu_pct": 100 * (ff_total - prev_ff_cpu) / max(1e-9, t - prev_t),
            "disk_mib": disk,
            "gc_collections": [g["collections"] for g in gc.get_stats()],
            "files": files,
            "files_by_slot": files_by_slot,
            "orphans": orphan_n,
            "orphan_mib": orphan_mib,
            **proxy_stats(proxy, relay),
        }
        prev_py_cpu, prev_ff_cpu, prev_t = py_cpu, ff_total, t
        samples.append(sample)
        sample_file.write(json.dumps(sample) + "\n")
        sample_file.flush()
        s = sample
        print(
            f"{s['t']:5.0f} {s['segments_per_slot']:5.0f} {s['py_rss']:6.1f} {s['py_anon']:6.1f} "
            f"{(s['traced'] or 0):6.1f} {s['conv_mib']:6.1f} {s['raw_mib']:6.1f} "
            f"{s['entries']:4d} {s['entry_objs']:4d} {s['waiting']:4d} {s['by_pts']:5d} "
            f"{s['dropped']:4d} {s['converters']:3d} {s['retiring']:3d} {s['ff_n']:3d} "
            f"{s['ff_rss']:6.1f} {s['ff_cpu_pct']:5.1f} {s['py_cpu_pct']:5.1f} "
            f"{s['disk_mib']:6.1f} {s['files']:5d} {s['orphans']:4d} "
            + " ".join(f"{k}:{v}" for k, v in s["files_by_slot"].items()),
            flush=True,
        )
    if profiler:
        profiler.disable()
    late = tracemalloc.take_snapshot() if args.tracemalloc else None
    held = census()
    assert client_proc.stdout is not None
    client_stats = json.loads((await client_proc.stdout.read()).decode().strip() or "{}")
    await client_proc.wait()

    director_task.cancel()
    await relay.aclose()
    await seg_client.aclose()
    await runner.cleanup()
    cdn_proc.stdin.close()
    await cdn_proc.wait()
    sample_file.close()

    report(args, label, samples, fetched, client_stats, log.counts, early, late, profiler, held)


def slope(samples: list[dict[str, Any]], key: str) -> float:
    """Least-squares MiB per minute over the second half of the run."""
    pts = [(s["t"], s[key]) for s in samples[len(samples) // 2 :]]
    if len(pts) < 2:
        return 0.0
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    den = sum((x - mx) ** 2 for x, _ in pts) or 1.0
    return 60 * sum((x - mx) * (y - my) for x, y in pts) / den


def report(
    args: argparse.Namespace,
    label: str,
    samples: list[dict[str, Any]],
    fetched: dict[str, int],
    client_stats: dict[str, Any],
    logs: dict[str, int],
    early: tracemalloc.Snapshot | None,
    late: tracemalloc.Snapshot | None,
    profiler: cProfile.Profile | None,
    held: list[tuple[str, int, float]],
) -> None:
    last = samples[-1]
    segs = fetched["n"]
    print(f"\n== {label}: {segs} segments fetched ({segs / args.slots:.0f}/slot)")
    print(f"avg source segment {fetched['bytes'] / max(1, segs) / MIB:.2f} MiB")
    keys = (
        "py_rss",
        "py_anon",
        "conv_mib",
        "raw_mib",
        "ff_rss",
        "ff_anon",
        "disk_mib",
        "orphan_mib",
        "by_pts",
    )
    for key in keys:
        peak = max(s[key] for s in samples)
        print(
            f"{key:10} last {last[key]:8.1f} peak {peak:8.1f} slope {slope(samples, key):+.2f}/min"
        )
    print(
        f"python CPU {last['py_cpu_s']:.1f}s = {1000 * last['py_cpu_s'] / max(1, segs):.1f} ms/seg"
    )
    print(
        f"ffmpeg CPU {last['ff_cpu_s']:.1f}s = {1000 * last['ff_cpu_s'] / max(1, segs):.1f} ms/seg"
    )
    print("clients:", json.dumps(client_stats))
    print("logs:", json.dumps(logs))
    print("\nlarge bytes (>= 256 KiB) at the end, by referrer:")
    for names, count, mib in held:
        print(f"  {mib:8.2f} MiB {count:4d}  {names}")
    filters = [
        tracemalloc.Filter(False, tracemalloc.__file__),
        tracemalloc.Filter(False, "<frozen importlib._bootstrap*>"),
    ]
    if late is not None:
        late = late.filter_traces(filters)
        print("\ntracemalloc top (late, by line):")
        for stat in late.statistics("lineno")[:20]:
            print(f"  {stat.size / MIB:8.2f} MiB {stat.count:7d}  {stat.traceback}")
        print("\ntracemalloc top (late, by traceback, 3 largest):")
        for stat in late.statistics("traceback")[:3]:
            print(f"  {stat.size / MIB:8.2f} MiB {stat.count} blocks")
            for line in stat.traceback.format()[-2 * args.frames :]:
                print("    " + line)
    if early is not None and late is not None:
        print("\ntracemalloc diff (late - early, by line):")
        for stat in late.compare_to(early.filter_traces(filters), "lineno")[:20]:
            print(f"  {stat.size_diff / MIB:+8.2f} MiB {stat.count_diff:+7d}  {stat.traceback}")
    if profiler is not None:
        path = OUT / f"{label}.prof"
        profiler.dump_stats(path)
        buf = io.StringIO()
        st = pstats.Stats(profiler, stream=buf)
        st.sort_stats("tottime").print_stats(25)
        print("\ncProfile by tottime (" + str(path) + "):")
        print("\n".join(buf.getvalue().splitlines()[4:40]))
        print("\nper fetched segment (cumulative):")
        for (file, _line, name), (_cc, nc, _tt, ct, _callers) in sorted(
            st.stats.items(),  # type: ignore[attr-defined]
            key=lambda kv: -kv[1][3],
        ):
            if ("tsinfo" in file or "relay/" in file) and ct / max(1, segs) > 0.0001:
                print(
                    f"  {Path(file).name}:{name:22} {nc / max(1, segs):7.1f} calls "
                    f"{1000 * ct / max(1, segs):7.2f} ms"
                )


# micro role


async def micro(args: argparse.Namespace) -> None:
    import timeit

    media = {rate: load_media(str(rate)) for rate in RATES}
    data = media[44100][1]
    print(f"segment {len(data) / MIB:.2f} MiB, {len(data) // PACKET} packets")

    def per_call(fn: Any, n: int = 20) -> float:
        return 1000 * min(timeit.repeat(fn, number=n, repeat=3)) / n

    print(
        f"_packets full iteration      {per_call(lambda: sum(1 for _ in _packets(data))):7.2f} ms"
    )
    print(f"first_video_pts              {per_call(lambda: first_video_pts(data)):7.2f} ms")
    print(f"audio_sample_rate            {per_call(lambda: audio_sample_rate(data)):7.2f} ms")
    head = 64 * 1024
    print(f"first_video_pts(first 64KiB) {per_call(lambda: first_video_pts(data[:head])):7.3f} ms")
    agree = sum(
        first_video_pts(d) == first_video_pts(d[:head]) for segs in media.values() for d in segs
    )
    total = sum(len(s) for s in media.values())
    print(f"  first 64KiB gives the same PTS for {agree}/{total} segments")

    # converter scan: an out.m3u8 plus outputs the size of real ones
    work = DATA / "work" / "micro"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    conv = convert.AudioConverter("ffmpeg", work)
    names = []
    for i, d in enumerate(media[48000][:14]):
        (work / f"o{i}.ts").write_bytes(d)
        names.append(f"#EXTINF:2.000000,\no{i}.ts")
    (work / "out.m3u8").write_text("#EXTM3U\n" + "\n".join(names) + "\n")
    t = time.perf_counter()
    conv._scan()
    print(f"_scan, 14 new outputs        {1000 * (time.perf_counter() - t) / 14:7.2f} ms/output")
    print(f"_scan, nothing new           {per_call(conv._scan, 200):7.3f} ms")
    path = work / "o3.ts"
    print(f"Path.read_bytes 2 MiB        {per_call(path.read_bytes, 50):7.3f} ms")
    shutil.rmtree(work)

    # fetch peak memory: httpx .content vs a streamed bytearray
    async def serve(_r: web.Request) -> web.Response:
        return web.Response(body=data)

    app = web.Application()
    app.router.add_get("/s", serve)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/s"  # type: ignore
    async with httpx.AsyncClient() as client:
        await client.get(url)

        async def content() -> bytes:
            r = await client.get(url)
            return r.content

        async def streamed() -> bytes:
            async with client.stream("GET", url) as r:
                buf = bytearray()
                async for chunk in r.aiter_raw():
                    buf += chunk
                return bytes(buf)

        for name, fn in (("httpx resp.content", content), ("httpx stream->bytes", streamed)):
            tracemalloc.start()
            tracemalloc.reset_peak()
            body = await fn()
            cur, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            t = time.perf_counter()
            for _ in range(20):
                await fn()
            ms = 50 * (time.perf_counter() - t)
            print(
                f"{name:22} peak {peak / MIB:5.2f} MiB for a {len(body) / MIB:.2f} MiB body, "
                f"held {cur / MIB:5.2f} MiB, {ms:.1f} ms"
            )
            del body
    await runner.cleanup()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen", help="generate media")
    g.add_argument("--count", type=int, default=120, help="segments per rate (loop length)")
    g.add_argument("--size", default="1920x1080")
    g.add_argument("--video-kbps", type=int, default=6500)
    g.add_argument("--force", action="store_true")
    r = sub.add_parser("run", help="run the benchmark")
    r.add_argument("--duration", type=float, default=300, help="real seconds")
    r.add_argument("--tick", type=float, default=0.25, help="real seconds per 2 s segment")
    r.add_argument("--realtime", action="store_true", help="tick = 2 s")
    r.add_argument("--slots", type=int, default=4)
    r.add_argument("--clients", type=int, default=1, help="players per slot")
    r.add_argument("--keep", type=int, default=14)
    r.add_argument("--switch-every", type=float, default=300, help="stream seconds; 0 = never")
    r.add_argument("--stall-every", type=float, default=420, help="stream seconds; 0 = never")
    r.add_argument("--sample", type=float, default=5, help="real seconds between samples")
    r.add_argument("--tracemalloc", action="store_true")
    r.add_argument("--frames", type=int, default=1, help="tracemalloc traceback depth")
    r.add_argument("--profile", action="store_true", help="cProfile the relay process")
    r.add_argument("--no-convert", action="store_true", help="pass-through (no ffmpeg)")
    r.add_argument("--label")
    r.add_argument(
        "--kf-slots",
        type=lambda v: {int(x) for x in v.split(",") if x},
        default=set(),
        help="slots whose channels carry an extra keyframe mid-segment, e.g. 3",
    )
    r.add_argument(
        "--fetch",
        choices=["content", "stream"],
        default="content",
        help="segment fetch: resp.content as the daemon does, or a streamed read",
    )
    r.add_argument("--workroot", help="converter work dir root (default bench/data/work)")
    r.add_argument("--src", help="source tree to import quadlink from (default ./src)")
    sub.add_parser("cdn")
    c = sub.add_parser("clients")
    c.add_argument("--port", type=int, required=True)
    c.add_argument("--slots", type=int, default=4)
    c.add_argument("--clients", type=int, default=1)
    c.add_argument("--tick", type=float, default=2.0)
    c.add_argument("--duration", type=float, default=300)
    sub.add_parser("micro", help="per-call costs")
    args = p.parse_args()
    if args.cmd == "gen":
        gen(args)
    else:
        asyncio.run({"run": run, "cdn": cdn, "clients": clients, "micro": micro}[args.cmd](args))


if __name__ == "__main__":
    main()
