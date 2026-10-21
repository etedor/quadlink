# Relay Segment Proxy + 48 kHz Audio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** duke's relay serves every video segment itself (fetched from Twitch once, served to every client on the LAN), and converts any non-48 kHz audio to 48 kHz on the way, so QuadStream on macOS never resamples and never drifts out of lip sync.

**Architecture:** the relay's playlist keeps its existing logic (splicing, filler, ad removal, latency budget) unchanged; `render` only rewrites segment URIs to `/streams/{slot}/seg/{seq}.{ts|m4s}`. A new `SegmentProxy` fetches each newly listed segment once, in order, per slot. For a continuous run of MPEG-TS segments whose audio is not 48 kHz, it pipes them into one long-running `ffmpeg` (video copied, audio resampled + re-encoded) whose HLS output lines up 1:1 with the input by first video PTS; the relay serves those converted bytes instead. A run ends at every discontinuity, which restarts ffmpeg.

**Tech Stack:** Python 3.12 asyncio + aiohttp (existing), httpx (existing), `ffmpeg` CLI (ffmpeg-headless from nixpkgs), pure-Python MPEG-TS header parsing.

## Global Constraints

- Before every commit: `pytest` all pass; `black src/ tests/`; `ruff check src/ tests/` no findings; `mypy src/` no errors (CLAUDE.md).
- Comments: lowercase, terse; acronyms uppercase; explain why, not what; no history/provenance in comments.
- Commit format from CLAUDE.md, ending with the Co-Authored-By / Claude-Session lines.
- Client facts that constrain the design (see memory `quadstream-client-behavior`): QuadStream plays every served second at real speed and never skips; a playlist that stops advancing makes it rebuild players; a segment request that takes longer than its buffer stalls the tile.
- A 48 kHz AAC frame (1024 samples) is exactly 1920 ticks of the 90 kHz MPEG-TS clock.
- Verified by spike (2026-10-20, 6 live 44.1 kHz Namthre segments): one continuous `ffmpeg -copyts -f mpegts -i pipe:0 ... -f hls -hls_time 0.1` yields output segments whose first video PTS equals each input segment's, with audio frames exactly 1920 ticks apart across every join. Converting segments independently is **rejected**: joins showed ~5x the codec error of the rest (independent AAC encoder states never agree at a boundary).
- Only MPEG-TS sources are converted. fMP4 (`map_gen is not None`) segments are proxied unchanged.
- Converted segments appear in the served playlist only once their output exists (ffmpeg closes segment N when segment N+1's first keyframe arrives), so converted slots run about one segment (2 s) later; unconverted slots are not held back.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/quadlink/relay/tsinfo.py` (create) | pure MPEG-TS parsing: video/audio PIDs, first video PTS, all audio PTS, AAC sample rate from the ADTS header |
| `src/quadlink/relay/convert.py` (create) | `AudioConverter`: one ffmpeg process for one continuous run; feed TS bytes, look up output by first video PTS |
| `src/quadlink/relay/segments.py` (create) | `SegmentProxy`: per-slot ordered fetch-once cache, run tracking, decides conversion per run |
| `src/quadlink/relay/playlist.py` (modify) | `render` takes optional `uri_for` and `ready` callbacks |
| `src/quadlink/relay/server.py` (modify) | registers new segments with the proxy, renders proxied URIs, serves `/streams/{slot}/seg/{seq}.{ext}` |
| `src/quadlink/config/models.py` (modify) | `relay_segments: bool = True` |
| `src/quadlink/daemon.py` (modify) | builds the proxy, applies `relay_segments` each cycle |
| `nix/package.nix`, `flake.nix` (modify) | ffmpeg-headless on the runtime PATH and in the dev shell |
| `tests/test_relay/conftest.py` (create) | ffmpeg-generated TS fixtures |
| `tests/test_relay/test_tsinfo.py`, `test_convert.py`, `test_segments.py` (create) | unit tests |
| `tests/test_relay/test_server.py` (modify) | proxy routing tests |

---

### Task 1: ffmpeg dependency + TS fixtures

**Files:**
- Modify: `nix/package.nix`
- Modify: `flake.nix` (devShell `buildInputs`)
- Create: `tests/test_relay/conftest.py`

**Interfaces:**
- Produces: pytest fixtures `ffmpeg` (path str, skips the test when absent) and `ts_run(tmp_path_factory) -> Callable[[int, int], list[bytes]]` returning `count` consecutive 2 s MPEG-TS segments with audio at `rate` Hz and continuous timestamps.

- [ ] **Step 1: add ffmpeg to the package's runtime PATH**

In `nix/package.nix`, add to the `buildPythonApplication` attribute set:

```nix
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [ pkgs.ffmpeg-headless ])
  ];
```

- [ ] **Step 2: add ffmpeg to the dev shell**

In `flake.nix`, in `devShells.default.buildInputs`, add `pkgs.ffmpeg-headless` after `pkgs.streamlink`.

- [ ] **Step 3: create the fixtures**

`tests/test_relay/conftest.py`:

```python
"""Shared relay fixtures: real MPEG-TS segments made with ffmpeg."""

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        pytest.skip("ffmpeg not on PATH")
    return path


@pytest.fixture(scope="session")
def ts_run(ffmpeg: str, tmp_path_factory: pytest.TempPathFactory) -> Callable[[int, int], list[bytes]]:
    """Consecutive 2s TS segments with continuous timestamps, like one Twitch broadcast."""

    def make(rate: int, count: int) -> list[bytes]:
        out = tmp_path_factory.mktemp(f"ts{rate}")
        subprocess.run(
            [ffmpeg, "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=s=320x180:r=30",
             "-f", "lavfi", "-i", f"sine=f=440:r={rate}",
             "-t", str(2 * count), "-map", "1:a", "-map", "0:v",
             "-c:v", "libx264", "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
             "-c:a", "aac", "-ar", str(rate), "-ac", "2",
             "-output_ts_offset", "10",
             "-f", "segment", "-segment_time", "2", "-segment_format", "mpegts",
             "-reset_timestamps", "0", str(out / "s%02d.ts")],
            check=True,
        )
        return [p.read_bytes() for p in sorted(out.glob("s*.ts"))][:count]

    return make
```

- [ ] **Step 4: confirm the shell has ffmpeg**

Run: `nix develop -c ffmpeg -version | head -1`
Expected: `ffmpeg version 8...`

- [ ] **Step 5: commit**

```bash
git add nix/package.nix flake.nix tests/test_relay/conftest.py
git commit -m "relay: ffmpeg on PATH, TS test fixtures"
```

---

### Task 2: MPEG-TS header parsing (`tsinfo.py`)

**Files:**
- Create: `src/quadlink/relay/tsinfo.py`
- Test: `tests/test_relay/test_tsinfo.py`

**Interfaces:**
- Produces:
  - `first_video_pts(data: bytes) -> int | None`
  - `audio_pts(data: bytes) -> list[int]`
  - `audio_sample_rate(data: bytes) -> int | None`

- [ ] **Step 1: write the failing tests**

`tests/test_relay/test_tsinfo.py`:

```python
"""Tests for MPEG-TS header parsing."""

from quadlink.relay.tsinfo import audio_pts, audio_sample_rate, first_video_pts


def test_sample_rate_from_adts(ts_run):
    assert audio_sample_rate(ts_run(44100, 1)[0]) == 44100
    assert audio_sample_rate(ts_run(48000, 1)[0]) == 48000


def test_first_video_pts_is_continuous_across_segments(ts_run):
    segs = ts_run(48000, 3)
    starts = [first_video_pts(s) for s in segs]
    assert all(isinstance(s, int) for s in starts)
    assert starts[1] - starts[0] == 180000  # 2 s at 90 kHz
    assert starts[2] - starts[1] == 180000


def test_audio_pts_are_sorted_ticks(ts_run):
    pts = audio_pts(ts_run(48000, 1)[0])
    assert pts == sorted(pts)
    assert len(pts) > 10


def test_garbage_has_no_answers():
    assert first_video_pts(b"\x00" * 1880) is None
    assert audio_sample_rate(b"") is None
    assert audio_pts(b"\x47" + b"\x00" * 187) == []
```

- [ ] **Step 2: run to verify it fails**

Run: `pytest tests/test_relay/test_tsinfo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'quadlink.relay.tsinfo'`

- [ ] **Step 3: implement**

`src/quadlink/relay/tsinfo.py`:

```python
"""Just enough MPEG-TS parsing to route and convert relay segments (ISO/IEC 13818-1)."""

from collections.abc import Iterator

PACKET = 188
_VIDEO_TYPES = {0x1B, 0x24}  # H.264, HEVC
_AAC_ADTS = 0x0F
# ISO/IEC 14496-3 sampling_frequency_index
_ADTS_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350]


def _packets(data: bytes) -> Iterator[tuple[int, bool, bytes]]:
    """(pid, payload_unit_start, payload) for each well-formed packet."""
    for i in range(0, len(data) - PACKET + 1, PACKET):
        pkt = data[i : i + PACKET]
        if pkt[0] != 0x47:
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        start = bool(pkt[1] & 0x40)
        control = (pkt[3] >> 4) & 0x3
        if control in (0, 2):
            continue
        offset = 4 + (1 + pkt[4] if control == 3 else 0)
        if offset < PACKET:
            yield pid, start, pkt[offset:]


def _section(payload: bytes) -> bytes:
    pointer = payload[0]
    return payload[1 + pointer :]


def _streams(data: bytes) -> dict[int, int]:
    """pid -> stream_type from the first PAT/PMT."""
    pmt_pid = None
    for pid, start, payload in _packets(data):
        if pid == 0 and start and pmt_pid is None:
            sec = _section(payload)
            if len(sec) >= 12:
                pmt_pid = ((sec[10] & 0x1F) << 8) | sec[11]
        elif pmt_pid is not None and pid == pmt_pid and start:
            sec = _section(payload)
            length = ((sec[1] & 0x0F) << 8) | sec[2]
            info = ((sec[10] & 0x0F) << 8) | sec[11]
            pos, end = 12 + info, 3 + length - 4
            streams: dict[int, int] = {}
            while pos + 5 <= end:
                stype = sec[pos]
                epid = ((sec[pos + 1] & 0x1F) << 8) | sec[pos + 2]
                streams[epid] = stype
                pos += 5 + (((sec[pos + 3] & 0x0F) << 8) | sec[pos + 4])
            return streams
    return {}


def _pes_starts(data: bytes, wanted: int) -> Iterator[tuple[int | None, bytes]]:
    """(PTS, first payload bytes) of each PES packet on one PID."""
    for pid, start, payload in _packets(data):
        if pid != wanted or not start or payload[:3] != b"\x00\x00\x01":
            continue
        pts = None
        if len(payload) >= 14 and payload[7] & 0x80:
            p = payload[9:14]
            pts = ((p[0] >> 1) & 0x07) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1
        yield pts, payload[9 + payload[8] :] if len(payload) > 8 else b""


def _pid(data: bytes, types: set[int]) -> int | None:
    return next((pid for pid, t in _streams(data).items() if t in types), None)


def first_video_pts(data: bytes) -> int | None:
    pid = _pid(data, _VIDEO_TYPES)
    if pid is None:
        return None
    values = [pts for pts, _ in _pes_starts(data, pid) if pts is not None]
    return min(values) if values else None


def audio_pts(data: bytes) -> list[int]:
    pid = _pid(data, {_AAC_ADTS})
    if pid is None:
        return []
    return sorted(pts for pts, _ in _pes_starts(data, pid) if pts is not None)


def audio_sample_rate(data: bytes) -> int | None:
    pid = _pid(data, {_AAC_ADTS})
    if pid is None:
        return None
    for _, body in _pes_starts(data, pid):
        if len(body) >= 3 and body[0] == 0xFF and body[1] & 0xF0 == 0xF0:
            index = (body[2] >> 2) & 0x0F
            return _ADTS_RATES[index] if index < len(_ADTS_RATES) else None
    return None
```

Note for the implementer: ffmpeg's mpegts muxer may pack several AAC frames per PES; `audio_pts` then returns one PTS per PES, not per frame. Task 3's continuity test accounts for this by checking PES spacing is a multiple of 1920.

- [ ] **Step 4: run to verify it passes**

Run: `pytest tests/test_relay/test_tsinfo.py -v`
Expected: 4 passed

- [ ] **Step 5: lint, type-check, commit**

```bash
black src/ tests/ && ruff check src/ tests/ && mypy src/
git add src/quadlink/relay/tsinfo.py tests/test_relay/test_tsinfo.py
git commit -m "relay: MPEG-TS header parsing for segment routing"
```

---

### Task 3: one ffmpeg per continuous run (`convert.py`)

**Files:**
- Create: `src/quadlink/relay/convert.py`
- Test: `tests/test_relay/test_convert.py`

**Interfaces:**
- Consumes: `first_video_pts`, `audio_pts`, `audio_sample_rate` (Task 2)
- Produces:
  - `TARGET_RATE = 48000`
  - `class AudioConverter(ffmpeg: str, workdir: pathlib.Path)` with
    `async start() -> None`, `async feed(data: bytes) -> None`,
    `async output_for(video_pts: int, timeout: float) -> bytes | None`, `async close() -> None`

- [ ] **Step 1: write the failing tests**

`tests/test_relay/test_convert.py`:

```python
"""Tests for the continuous audio converter."""

import pytest

from quadlink.relay.convert import AudioConverter
from quadlink.relay.tsinfo import audio_pts, audio_sample_rate, first_video_pts


@pytest.mark.asyncio
async def test_converts_a_run_gaplessly_one_output_per_input(ffmpeg, ts_run, tmp_path):
    segs = ts_run(44100, 4)
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    for seg in segs:
        await conv.feed(seg)
    outs = []
    for seg in segs[:-1]:  # the last closes only when ffmpeg sees the next keyframe or EOF
        out = await conv.output_for(first_video_pts(seg), timeout=10)
        assert out is not None
        outs.append(out)
    await conv.close()

    for seg, out in zip(segs, outs):
        assert first_video_pts(out) == first_video_pts(seg)
        assert audio_sample_rate(out) == 48000
    pts = [p for out in outs for p in audio_pts(out)]
    assert all((b - a) % 1920 == 0 and b > a for a, b in zip(pts, pts[1:]))


@pytest.mark.asyncio
async def test_audio_stays_aligned_with_video(ffmpeg, ts_run, tmp_path):
    seg = ts_run(44100, 3)
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    for s in seg:
        await conv.feed(s)
    out = await conv.output_for(first_video_pts(seg[1]), timeout=10)
    await conv.close()
    src_offset = audio_pts(seg[1])[0] - first_video_pts(seg[1])
    out_offset = audio_pts(out)[0] - first_video_pts(out)
    assert abs(out_offset - src_offset) <= 1920  # within one 48 kHz AAC frame


@pytest.mark.asyncio
async def test_unknown_pts_times_out(ffmpeg, ts_run, tmp_path):
    conv = AudioConverter(ffmpeg, tmp_path)
    await conv.start()
    await conv.feed(ts_run(44100, 2)[0])
    assert await conv.output_for(123, timeout=0.5) is None
    await conv.close()
```

- [ ] **Step 2: run to verify it fails**

Run: `pytest tests/test_relay/test_convert.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'quadlink.relay.convert'`

- [ ] **Step 3: implement**

`src/quadlink/relay/convert.py`:

```python
"""One ffmpeg process per continuous run of MPEG-TS segments, converting audio to 48 kHz.

macOS AVPlayer resamples non-48 kHz audio with a small rate error, so a 44.1 kHz
channel drifts out of lip sync on machina; delivering 48 kHz removes the resampling.
A single long-running encoder is required: encoding segments separately leaves
audible seams, because independent AAC encoder states never agree at a boundary.
"""

import asyncio
import shutil
from pathlib import Path

import structlog

from quadlink.relay.tsinfo import first_video_pts

logger = structlog.get_logger()

TARGET_RATE = 48000
# the native AAC encoder emits its first frame 1024 samples early; shifting input
# timestamps by the same amount keeps audio aligned with the copied video
_ENCODER_DELAY = 1024


class AudioConverter:
    """Pipes consecutive TS segments through ffmpeg; output segments match inputs 1:1."""

    def __init__(self, ffmpeg: str, workdir: Path) -> None:
        self.ffmpeg = ffmpeg
        self.workdir = workdir
        self._proc: asyncio.subprocess.Process | None = None
        self._by_pts: dict[int, Path] = {}
        self._listed = 0

    async def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._proc = await asyncio.create_subprocess_exec(
            self.ffmpeg, "-v", "error", "-copyts", "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v", "-map", "0:a", "-c:v", "copy",
            "-af", f"aresample={TARGET_RATE},asetpts=PTS+{_ENCODER_DELAY}",
            "-c:a", "aac", "-b:a", "160k",
            # a tiny hls_time makes ffmpeg cut at every keyframe, which Twitch places
            # at each segment start, so outputs line up with inputs
            "-f", "hls", "-hls_time", "0.1", "-hls_list_size", "0",
            "-hls_segment_filename", str(self.workdir / "o%d.ts"),
            "-muxdelay", "0", "-muxpreload", "0", str(self.workdir / "out.m3u8"),
            stdin=asyncio.subprocess.PIPE,
        )

    async def feed(self, data: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    def _scan(self) -> None:
        playlist = self.workdir / "out.m3u8"
        if not playlist.exists():
            return
        names = [ln for ln in playlist.read_text().splitlines() if ln.endswith(".ts")]
        for name in names[self._listed :]:
            path = self.workdir / name
            pts = first_video_pts(path.read_bytes())
            if pts is not None:
                self._by_pts[pts] = path
        self._listed = len(names)

    async def output_for(self, video_pts: int, timeout: float) -> bytes | None:
        """Converted bytes of the input segment starting at this video PTS, once written."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self._scan()
            path = self._by_pts.get(video_pts)
            if path is not None:
                return path.read_bytes()
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        shutil.rmtree(self.workdir, ignore_errors=True)
```

- [ ] **Step 4: run to verify it passes**

Run: `pytest tests/test_relay/test_convert.py -v`
Expected: 3 passed. If `test_audio_stays_aligned_with_video` fails by about 1920 ticks, the encoder-delay shift has the wrong sign: change `asetpts=PTS+{_ENCODER_DELAY}` to `asetpts=PTS-{_ENCODER_DELAY}`, rerun, and keep whichever passes.

- [ ] **Step 5: lint, type-check, commit**

```bash
black src/ tests/ && ruff check src/ tests/ && mypy src/
git add src/quadlink/relay/convert.py tests/test_relay/test_convert.py
git commit -m "relay: continuous per-run ffmpeg audio conversion to 48 kHz"
```

---

### Task 4: fetch-once segment proxy (`segments.py`)

**Files:**
- Create: `src/quadlink/relay/segments.py`
- Test: `tests/test_relay/test_segments.py`

**Interfaces:**
- Consumes: `Segment` from `quadlink.relay.playlist` (fields `seq`, `uri`, `discontinuity`, `map_gen`, `filler`); `AudioConverter`, `TARGET_RATE` (Task 3); `audio_sample_rate`, `first_video_pts` (Task 2)
- Produces:
  - `class SegmentProxy(fetch: Callable[[str], Awaitable[bytes]], ffmpeg: str | None, workroot: Path, keep: int = 30)` with
    `register(slot: int, seg: Segment) -> None`,
    `ready(slot: int, seq: int) -> bool`,
    `async get(slot: int, seq: int) -> bytes | None`,
    `async aclose() -> None`

- [ ] **Step 1: write the failing tests**

`tests/test_relay/test_segments.py`:

```python
"""Tests for the fetch-once segment proxy."""

import asyncio

import pytest

from quadlink.relay.playlist import Segment
from quadlink.relay.segments import SegmentProxy
from quadlink.relay.tsinfo import audio_sample_rate


def _seg(seq: int, uri: str, disc: bool = False) -> Segment:
    return Segment(seq=seq, uri=uri, duration=2.0, program_date_time=None,
                   discontinuity=disc, map_gen=None)


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
```

- [ ] **Step 2: run to verify it fails**

Run: `pytest tests/test_relay/test_segments.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'quadlink.relay.segments'`

- [ ] **Step 3: implement**

`src/quadlink/relay/segments.py`:

```python
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
    raw: asyncio.Future[bytes]
    converter: AudioConverter | None = None
    converted: bytes | None = None


@dataclass
class _Slot:
    entries: OrderedDict[int, _Entry] = field(default_factory=OrderedDict)
    queue: asyncio.Queue[_Entry] = field(default_factory=asyncio.Queue)
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
                logger.warning("relay segment fetch failed", slot=slot, seq=entry.seg.seq, error=str(e))
                if not entry.raw.done():
                    entry.raw.set_exception(e)
                continue
            entry.raw.set_result(data)
            await self._route(slot, state, entry, data)

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
                await conv.start()
                state.converter = conv
                logger.info("relay converting audio", slot=slot, rate=rate)
        if state.converter is not None and ts:
            await state.converter.feed(data)
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
        entry.converter._scan()
        pts = first_video_pts(entry.raw.result())
        return pts is not None and pts in entry.converter._by_pts

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
```

Implementer note: `_has_output` reaches into `AudioConverter._scan` and `_by_pts`. If review objects, promote them to a public `AudioConverter.has_output(video_pts: int) -> bool` in Task 3 and use that here; behavior is the same.

- [ ] **Step 4: run to verify it passes**

Run: `pytest tests/test_relay/test_segments.py -v`
Expected: 4 passed

- [ ] **Step 5: lint, type-check, commit**

```bash
black src/ tests/ && ruff check src/ tests/ && mypy src/
git add src/quadlink/relay/segments.py tests/test_relay/test_segments.py
git commit -m "relay: fetch-once segment proxy with per-run audio conversion"
```

---

### Task 5: relay serves proxied segments

**Files:**
- Modify: `src/quadlink/relay/playlist.py` (`render`)
- Modify: `src/quadlink/relay/server.py` (`SlotRelay.__init__`, `_fold`, `handle`, new `handle_segment`, `register_routes`, `aclose`)
- Test: `tests/test_relay/test_server.py`

**Interfaces:**
- Consumes: `SegmentProxy` (Task 4)
- Produces:
  - `render(state: SlotState, slot: int, uri_for: Callable[[Segment], str] | None = None, ready: Callable[[Segment], bool] | None = None) -> str`
  - `SlotRelay(store, *, fetch=None, cache_ttl=1.0, window=12, time_fn=time.monotonic, segments: SegmentProxy | None = None)`; attribute `proxy_segments: bool` (default True; only used when `segments` is set)
  - route `GET /streams/{slot}/seg/{name}` where `name` is `{seq}.ts` or `{seq}.m4s`

- [ ] **Step 1: write the failing tests**

Append to `tests/test_relay/test_server.py`:

```python
class FakeProxy:
    def __init__(self, unready: set[int] | None = None) -> None:
        self.registered: list[tuple[int, int]] = []
        self.unready = unready or set()

    def register(self, slot, seg):
        self.registered.append((slot, seg.seq))

    def ready(self, slot, seq):
        return seq not in self.unready

    async def get(self, slot, seq):
        return b"seg-%d" % seq

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_proxied_playlist_points_at_relay_segments():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return _src(100, ["https://cdn/a.ts", "https://cdn/b.ts"])

    proxy = FakeProxy()
    relay = SlotRelay(store, fetch=fetch, segments=proxy)
    resp = await _call(relay, "1")
    assert "/streams/1/seg/0.ts" in resp.text
    assert "/streams/1/seg/1.ts" in resp.text
    assert "https://cdn/" not in resp.text
    assert proxy.registered == [(1, 0), (1, 1)]


@pytest.mark.asyncio
async def test_unready_trailing_segment_is_held_back():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return _src(100, ["https://cdn/a.ts", "https://cdn/b.ts"])

    relay = SlotRelay(store, fetch=fetch, segments=FakeProxy(unready={1}))
    resp = await _call(relay, "1")
    assert "/streams/1/seg/0.ts" in resp.text
    assert "/streams/1/seg/1.ts" not in resp.text


@pytest.mark.asyncio
async def test_proxy_disabled_keeps_cdn_uris():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return _src(100, ["https://cdn/a.ts"])

    relay = SlotRelay(store, fetch=fetch, segments=FakeProxy())
    relay.proxy_segments = False
    resp = await _call(relay, "1")
    assert "https://cdn/a.ts" in resp.text


@pytest.mark.asyncio
async def test_segment_route_serves_proxy_bytes():
    relay = SlotRelay(SlotStore(), segments=FakeProxy())
    request = make_mocked_request(
        "GET", "/streams/2/seg/7.ts", match_info={"slot": "2", "name": "7.ts"}
    )
    resp = await relay.handle_segment(request)
    assert resp.status == 200
    assert resp.body == b"seg-7"
    assert resp.content_type == "video/mp2t"


@pytest.mark.asyncio
async def test_segment_route_rejects_bad_names():
    relay = SlotRelay(SlotStore(), segments=FakeProxy())
    request = make_mocked_request(
        "GET", "/streams/2/seg/x.ts", match_info={"slot": "2", "name": "x.ts"}
    )
    assert (await relay.handle_segment(request)).status == 404
```

- [ ] **Step 2: run to verify they fail**

Run: `pytest tests/test_relay/test_server.py -v -k "proxied or held_back or disabled or segment_route"`
Expected: FAIL with `TypeError: SlotRelay.__init__() got an unexpected keyword argument 'segments'`

- [ ] **Step 3: extend `render`**

In `src/quadlink/relay/playlist.py`, change the signature and the segment loop:

```python
def render(
    state: SlotState,
    slot: int,
    uri_for: Callable[[Segment], str] | None = None,
    ready: Callable[[Segment], bool] | None = None,
) -> str:
    """Render our current window as a live HLS media playlist.

    `ready` holds back trailing segments a client could not fetch yet (a converted
    segment exists only once ffmpeg has seen the next keyframe); earlier ones stay.
    """
    segments = list(state.segments)
    if ready is not None:
        while len(segments) > 1 and not segments[-1].filler and not ready(segments[-1]):
            segments.pop()
```

and replace `lines.append(seg.uri)` with:

```python
        lines.append(uri_for(seg) if uri_for is not None and not seg.filler else seg.uri)
```

Add `from collections.abc import Callable` to the imports.

- [ ] **Step 4: wire the proxy into `SlotRelay`**

In `src/quadlink/relay/server.py`:

1. Import: `from quadlink.relay.segments import SegmentProxy` and `Segment` from `quadlink.relay.playlist`.
2. `__init__`: add keyword `segments: SegmentProxy | None = None`; store `self._segments = segments` and `self.proxy_segments = True`.
3. In `_fold`, after the `ingest(...)` calls, register what was appended:

```python
        if self._proxying():
            assert self._segments is not None
            for seg in state.segments:
                if seg.seq >= before:
                    self._segments.register(slot, seg)
```

4. Add helpers and use them in `handle` (replace `render(state, slot)`):

```python
    def _proxying(self) -> bool:
        return self._segments is not None and self.proxy_segments

    def _render(self, state: SlotState, slot: int) -> str:
        if not self._proxying():
            return render(state, slot)
        assert self._segments is not None
        proxy = self._segments

        def uri_for(seg: Segment) -> str:
            ext = "m4s" if seg.map_gen is not None else "ts"
            return f"/streams/{slot}/seg/{seg.seq}.{ext}"

        return render(state, slot, uri_for, lambda seg: proxy.ready(slot, seg.seq))
```

5. Add the route handler:

```python
    async def handle_segment(self, request: web.Request) -> web.Response:
        """Route handler for GET /streams/{slot}/seg/{seq}.{ts|m4s}."""
        name = request.match_info.get("name", "")
        stem, _, ext = name.partition(".")
        try:
            slot, seq = int(request.match_info.get("slot", "")), int(stem)
        except ValueError:
            return web.Response(status=404, text="bad segment")
        types = {"ts": "video/mp2t", "m4s": "video/iso.segment"}
        if slot not in (1, 2, 3, 4) or ext not in types or self._segments is None:
            return web.Response(status=404, text="bad segment")
        body = await self._segments.get(slot, seq)
        if body is None:
            return web.Response(status=404, text="no such segment")
        return web.Response(body=body, content_type=types[ext], headers={"Cache-Control": "max-age=60"})
```

6. `register_routes`: add `router.add_get("/streams/{slot}/seg/{name}", self.handle_segment)` before the init route.
7. `aclose`: `if self._segments is not None: await self._segments.aclose()` before closing the httpx client.

- [ ] **Step 5: run all relay tests**

Run: `pytest tests/test_relay -v`
Expected: all pass, including the existing latency, gap and server tests (proxy defaults to None there).

- [ ] **Step 6: lint, type-check, commit**

```bash
black src/ tests/ && ruff check src/ tests/ && mypy src/
git add src/quadlink/relay/playlist.py src/quadlink/relay/server.py tests/test_relay/test_server.py
git commit -m "relay: serve segments from duke, held back until ready"
```

---

### Task 6: config switch + daemon wiring

**Files:**
- Modify: `src/quadlink/config/models.py` (`Config`)
- Modify: `src/quadlink/daemon.py` (`Daemon.__init__`, `_main_loop`)
- Test: `tests/test_daemon.py`, `tests/test_config/` (whichever holds `Config` defaults tests)

**Interfaces:**
- Consumes: `SegmentProxy` (Task 4), `SlotRelay(..., segments=...)` and `proxy_segments` (Task 5)
- Produces: `Config.relay_segments: bool = True`

- [ ] **Step 1: write the failing tests**

Add to `tests/test_daemon.py` inside `TestDaemonRelayWiring` (the class holding `test_init_creates_store_and_relay`):

```python
    def test_init_gives_relay_a_segment_proxy(self):
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()
        assert daemon.slot_relay._segments is not None
```

Add a config test to `tests/test_config/test_models.py` (import `Config` from `quadlink.config.models` if the file doesn't already):

```python
def test_relay_segments_defaults_on():
    config = Config(priorities={}, mode="local")
    assert config.relay_segments is True
```

- [ ] **Step 2: run to verify they fail**

Run: `pytest tests/test_daemon.py tests/test_config -v -k "segment"`
Expected: FAIL (`_segments is None`; `Config` has no `relay_segments`)

- [ ] **Step 3: implement**

`src/quadlink/config/models.py`, in `Config` after `low_latency`:

```python
    # serve video segments from duke (fetched once for all clients) and convert
    # non-48 kHz audio; false sends clients straight to Twitch's CDN
    relay_segments: bool = True
```

Also add `relay_segments: Serve segments from the relay and convert audio to 48 kHz.` to the class docstring's Attributes list.

`src/quadlink/daemon.py`:

```python
import shutil
import tempfile
from pathlib import Path

import httpx

from quadlink.relay.segments import SegmentProxy
```

In `__init__`, replace `self.slot_relay = SlotRelay(self.slot_store)` with:

```python
        self._segment_client = httpx.AsyncClient(timeout=10)
        self.slot_relay = SlotRelay(
            self.slot_store,
            segments=SegmentProxy(
                self._fetch_segment,
                shutil.which("ffmpeg"),
                Path(tempfile.gettempdir()) / "quadlink-relay",
            ),
        )
```

Add the method:

```python
    async def _fetch_segment(self, uri: str) -> bytes:
        resp = await self._segment_client.get(uri)
        resp.raise_for_status()
        return resp.content
```

In `_main_loop`, right after `serve_local = config.mode in ("both", "local")`:

```python
                self.slot_relay.proxy_segments = config.relay_segments
```

- [ ] **Step 4: run the full suite**

Run: `pytest -q`
Expected: all pass

- [ ] **Step 5: lint, type-check, commit**

```bash
black src/ tests/ && ruff check src/ tests/ && mypy src/
git add src/quadlink/config/models.py src/quadlink/daemon.py tests/
git commit -m "relay: relay_segments switch, daemon builds the segment proxy"
```

---

### Task 7: lab validation with real AVPlayer and QuadStream (machina)

No code. Proves the behavior that unit tests cannot: lip sync and real-client playback.

- [ ] **Step 1: AVPlayer drift check through the relay**

Serve the 44.1 kHz test VOD (`~/.claude/jobs/.../src/a44`, or regenerate with the Task 1 fixture command at 25 minutes) through a local `SlotRelay` with `SegmentProxy` (adapt `~/Library/Logs/quadstream-player/lab/relaytest.py`: source channel = the a44 segments, `segments=SegmentProxy(...)`). Play it with the `avsrc` probe (volume 0.01) for 20 minutes while capturing `enqueueing #` logs; run `srcdrift.py`.
Expected: converted 44.1 kHz audio drifts like the native 48 kHz file (about 0 ppm against the video clock), not -30 ppm.

- [ ] **Step 2: QuadStream end-to-end**

Run `~/Library/Logs/quadstream-player/apptest2.sh` (it tests the main checkout's code; check out this branch's `src` or point `SRC` at the worktree).
Expected: same pass criteria as before (no stalls or rebuilds through freeze, 404, ad hold, fMP4), and the capture shows relay-served segment URLs.

- [ ] **Step 3: live listening check**

After deploy, unmute a 44.1 kHz channel tile on machina for an hour.
Expected: lip sync holds; duke's journal shows `relay converting audio` for that slot.

---

### Task 8: deploy

- [ ] **Step 1:** merge `worktree-relay-48k-audio` into `main` (fast-forward), push with the gh credential helper.
- [ ] **Step 2:** ask the coordinator (user approval quoted) to bump the quadlink flake input on nix main and deploy duke.
- [ ] **Step 3:** verify on duke: `/streams/1-4` return 200; playlists list `/streams/N/seg/` URIs; a segment fetched through the relay plays; journal shows no tracebacks; `relay converting audio` appears for 44.1 kHz slots.
- [ ] **Rollback:** set `relay_segments: false` in duke's quadlink config (no redeploy needed); clients go straight to Twitch's CDN again.

---

## Self-review notes

- Spec coverage: fetch-once serve-many (Tasks 4–5), 48 kHz conversion (Tasks 3–4), relay features preserved (render-only change, Task 5), rollback switch (Task 6), deployment (Task 8), real-client validation (Task 7).
- Known limits, accepted: fMP4 channels with non-48 kHz audio are proxied, not converted; converted slots run one segment later; duke now carries every viewer's video traffic over the LAN (about 6 Mbps per tile per client).
