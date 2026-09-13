# HLS Slot Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Self-host QuadStream's per-slot read side — 4 endpoints on duke that serve a continuously-live HLS media playlist per slot, splicing new Twitch sources with `EXT-X-DISCONTINUITY` so the Apple TV switches reliably when QuadLink swaps a slot.

**Architecture:** A `SlotStore` (written by the daemon each cycle with the quad's 4 media-playlist URLs) feeds a `SlotRelay` that rides the existing aiohttp web UI app. On each `GET /streams/{n}`, the relay fetches the slot's current Twitch media playlist (on-demand, ~1s cache), republishes it as its *own* live media playlist with a monotonic `EXT-X-MEDIA-SEQUENCE`, and inserts `EXT-X-DISCONTINUITY` when the source URL changes. Segments keep absolute Twitch CDN URLs, so duke relays only playlist text.

**Tech Stack:** Python 3.13, aiohttp (existing web UI server), httpx (existing dep), pytest + pytest-asyncio (`asyncio_mode = "auto"`).

## Global Constraints

- Python 3.13; `black src/ tests/` clean; `mypy src/` clean (3 pre-existing streamlink errors expected).
- Comments: lowercase, terse. Acronyms uppercase in comments (URL, HLS, M3U8, CDN).
- Emitted playlist MUST use `#EXT-X-VERSION:6` (required by `#EXT-X-DISCONTINUITY-SEQUENCE`) and MUST NEVER emit `#EXT-X-ENDLIST` (stays live).
- Response Content-Type MUST be `application/vnd.apple.mpegurl`.
- No-cache headers on every playlist response, exact: `Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, `Expires: 0`.
- Routes live on the existing web UI aiohttp app (port 8081, Docker-exposed + nginx-fronted); path `/streams/{1..4}`; public (the web UI has no auth middleware — correct, the Apple TV can't authenticate).
- Dual-home: do NOT modify `QuadStreamClient.update_quad` or the daemon's existing update/webhook path. The relay is purely additive.
- Segments stay absolute Twitch CDN URLs; never proxy video through duke.
- Commit trailer per project CLAUDE.md:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)

  Co-Authored-By: Claude <claude-opus-4-8[1m]> <noreply@anthropic.com>
  ```

## File Structure

- Create `src/quadlink/relay/__init__.py` — package marker.
- Create `src/quadlink/relay/store.py` — `SlotStore` (current 4 URLs; written by daemon, read by relay).
- Create `src/quadlink/relay/playlist.py` — pure HLS media-playlist parse + republish core (`Segment`, `SlotState`, `parse_media_playlist`, `ingest`, `render`). No I/O, no aiohttp.
- Create `src/quadlink/relay/server.py` — `SlotRelay` (on-demand fetch + ~1s cache, aiohttp handler, route registration, httpx lifecycle).
- Modify `src/quadlink/webui.py` — `WebUI.__init__` accepts optional `slot_relay`; `_setup_routes` registers its routes.
- Modify `src/quadlink/daemon.py` — construct `SlotStore` + `SlotRelay`, pass relay to `WebUI`, write `quad.to_list()` to the store each cycle, close the relay client on shutdown.
- Create `tests/test_relay/__init__.py`, `tests/test_relay/test_store.py`, `tests/test_relay/test_playlist.py`, `tests/test_relay/test_server.py`.
- Modify `tests/test_daemon.py` — relay wiring + store-write-per-cycle.

---

### Task 1: SlotStore

**Files:**
- Create: `src/quadlink/relay/__init__.py`
- Create: `src/quadlink/relay/store.py`
- Create: `tests/test_relay/__init__.py`
- Test: `tests/test_relay/test_store.py`

**Interfaces:**
- Produces: `SlotStore()` with `update(urls: list[str]) -> None` (maps `urls[0..3]` to slots 1..4; empty string → `None`) and `get(slot: int) -> str | None`.

- [ ] **Step 1: Create package markers**

Create `src/quadlink/relay/__init__.py` (empty) and `tests/test_relay/__init__.py` (empty).

- [ ] **Step 2: Write the failing test**

`tests/test_relay/test_store.py`:

```python
"""Tests for the slot URL store."""

from quadlink.relay.store import SlotStore


def test_empty_store_returns_none():
    store = SlotStore()
    assert store.get(1) is None
    assert store.get(4) is None


def test_update_maps_urls_to_slots():
    store = SlotStore()
    store.update(["a", "b", "c", "d"])
    assert store.get(1) == "a"
    assert store.get(2) == "b"
    assert store.get(3) == "c"
    assert store.get(4) == "d"


def test_empty_string_becomes_none():
    store = SlotStore()
    store.update(["a", "", "c", ""])
    assert store.get(1) == "a"
    assert store.get(2) is None
    assert store.get(4) is None


def test_out_of_range_slot_returns_none():
    store = SlotStore()
    store.update(["a", "b", "c", "d"])
    assert store.get(0) is None
    assert store.get(5) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_relay/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quadlink.relay.store'`

- [ ] **Step 4: Write minimal implementation**

`src/quadlink/relay/store.py`:

```python
"""Holds the current per-slot Twitch media-playlist URLs.

Written by the daemon each cycle, read by the slot relay. In-process only —
daemon loop and aiohttp app share one event loop, so no locking is needed.
"""


class SlotStore:
    """Current media-playlist URL for each of the 4 quad slots."""

    def __init__(self) -> None:
        self._urls: dict[int, str | None] = {1: None, 2: None, 3: None, 4: None}

    def update(self, urls: list[str]) -> None:
        """Replace all 4 slot URLs from a quad's ordered URL list.

        Empty strings map to None (an empty slot).
        """
        for i in range(4):
            value = urls[i] if i < len(urls) else ""
            self._urls[i + 1] = value or None

    def get(self, slot: int) -> str | None:
        """Return the current URL for a slot, or None if empty/unknown."""
        return self._urls.get(slot)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_relay/test_store.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add src/quadlink/relay/__init__.py src/quadlink/relay/store.py tests/test_relay/__init__.py tests/test_relay/test_store.py
git commit -m "add SlotStore for per-slot URL state"
```

---

### Task 2: Media-playlist parser

**Files:**
- Create: `src/quadlink/relay/playlist.py`
- Test: `tests/test_relay/test_playlist.py`

**Interfaces:**
- Produces: `parse_media_playlist(text: str) -> tuple[int, int, list[tuple[float, str, str | None, bool]]]` returning `(target_duration, media_sequence, segments)`, where each segment is `(duration, uri, program_date_time, source_discontinuity)`.

- [ ] **Step 1: Write the failing test**

`tests/test_relay/test_playlist.py`:

```python
"""Tests for the HLS media-playlist parse + republish core."""

from quadlink.relay.playlist import parse_media_playlist

SAMPLE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DATERANGE:ID="x",CLASS="timestamp",START-DATE="2026-08-14T15:02:00.893Z"
#EXT-X-PROGRAM-DATE-TIME:2026-08-14T15:14:32.243Z
#EXTINF:2.000,live
https://cdn.example/seg100.ts?dna=aaa
#EXT-X-PROGRAM-DATE-TIME:2026-08-14T15:14:34.243Z
#EXTINF:2.000,live
https://cdn.example/seg101.ts?dna=bbb
"""


def test_parse_target_duration_and_media_sequence():
    td, seq, segs = parse_media_playlist(SAMPLE)
    assert td == 6
    assert seq == 100
    assert len(segs) == 2


def test_parse_segment_fields():
    _, _, segs = parse_media_playlist(SAMPLE)
    duration, uri, pdt, disc = segs[0]
    assert duration == 2.0
    assert uri == "https://cdn.example/seg100.ts?dna=aaa"
    assert pdt == "2026-08-14T15:14:32.243Z"
    assert disc is False


def test_parse_ignores_unknown_tags():
    # the EXT-X-DATERANGE line must not become a segment
    _, _, segs = parse_media_playlist(SAMPLE)
    assert all(uri.startswith("https://") for _, uri, _, _ in segs)


def test_parse_source_discontinuity_flag():
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:5\n"
        "#EXTINF:2.000,live\nhttps://cdn.example/a.ts\n"
        "#EXT-X-DISCONTINUITY\n#EXTINF:2.000,live\nhttps://cdn.example/b.ts\n"
    )
    _, _, segs = parse_media_playlist(text)
    assert segs[0][3] is False
    assert segs[1][3] is True


def test_parse_discontinuity_sequence_tag_not_treated_as_discontinuity():
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:5\n"
        "#EXT-X-DISCONTINUITY-SEQUENCE:2\n"
        "#EXTINF:2.000,live\nhttps://cdn.example/a.ts\n"
    )
    _, _, segs = parse_media_playlist(text)
    assert segs[0][3] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relay/test_playlist.py -v`
Expected: FAIL — `ImportError: cannot import name 'parse_media_playlist'`

- [ ] **Step 3: Write minimal implementation**

Create `src/quadlink/relay/playlist.py` with the parser (later tasks add to this file):

```python
"""Pure HLS media-playlist parse + republish core.

No I/O and no aiohttp here — this is the testable heart of the relay. It turns
a Twitch source media playlist into our own continuously-live media playlist
with a monotonic MEDIA-SEQUENCE and DISCONTINUITY splices on source change.
"""

# a parsed source segment: (duration, uri, program_date_time, source_discontinuity)
ParsedSegment = tuple[float, str, "str | None", bool]


def parse_media_playlist(text: str) -> tuple[int, int, list[ParsedSegment]]:
    """Parse a Twitch media playlist.

    Returns (target_duration, media_sequence, segments). Unknown tags are
    ignored; EXTINF/PROGRAM-DATE-TIME/DISCONTINUITY attach to the next segment.
    """
    target_duration = 6
    media_sequence = 0
    segments: list[ParsedSegment] = []

    duration = 0.0
    pdt: str | None = None
    disc = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])
        elif line == "#EXT-X-DISCONTINUITY":
            disc = True
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            # value is an ISO timestamp that itself contains colons
            pdt = line.split(":", 1)[1]
        elif line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:") :].split(",", 1)[0])
        elif line.startswith("#"):
            continue  # ignore all other tags (DATERANGE, TWITCH-*, etc.)
        else:
            segments.append((duration, line, pdt, disc))
            duration, pdt, disc = 0.0, None, False

    return target_duration, media_sequence, segments
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_relay/test_playlist.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/quadlink/relay/playlist.py tests/test_relay/test_playlist.py
git commit -m "add Twitch media-playlist parser"
```

---

### Task 3: Republish state machine (ingest + render)

**Files:**
- Modify: `src/quadlink/relay/playlist.py`
- Test: `tests/test_relay/test_playlist.py`

**Interfaces:**
- Consumes: `parse_media_playlist` (Task 2).
- Produces:
  - `Segment` dataclass: `seq: int, uri: str, duration: float, program_date_time: str | None, discontinuity: bool`.
  - `SlotState` dataclass: `window: int = 12`, plus internal fields `segments: deque[Segment]`, `next_seq: int`, `discontinuity_seq: int`, `last_source_url: str | None`, `last_source_seq: int | None`, `pending_discontinuity: bool`.
  - `ingest(state: SlotState, source_text: str, source_url: str) -> None` — appends new source segments (dedup by source sequence), marks a discontinuity when `source_url` changes, trims to `window`.
  - `render(state: SlotState) -> str` — our live media playlist (version 6, monotonic MEDIA-SEQUENCE, DISCONTINUITY-SEQUENCE, never ENDLIST).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relay/test_playlist.py`:

```python
import math

from quadlink.relay.playlist import Segment, SlotState, ingest, render


def _src(media_seq: int, uris: list[str]) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{media_seq}"]
    for uri in uris:
        lines.append("#EXTINF:2.000,live")
        lines.append(uri)
    return "\n".join(lines) + "\n"


def test_ingest_first_fetch_assigns_monotonic_seq():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "url-A")
    assert [s.seq for s in state.segments] == [0, 1]
    assert all(s.discontinuity is False for s in state.segments)


def test_ingest_dedupes_on_refetch():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "url-A")
    ingest(state, _src(100, ["a.ts", "b.ts"]), "url-A")
    assert [s.uri for s in state.segments] == ["a.ts", "b.ts"]
    assert [s.seq for s in state.segments] == [0, 1]


def test_ingest_appends_new_segments():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "url-A")
    ingest(state, _src(101, ["b.ts", "c.ts"]), "url-A")
    assert [s.uri for s in state.segments] == ["a.ts", "b.ts", "c.ts"]
    assert [s.seq for s in state.segments] == [0, 1, 2]


def test_ingest_marks_discontinuity_on_source_change():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "url-A")
    ingest(state, _src(500, ["x.ts", "y.ts"]), "url-B")
    uris = [s.uri for s in state.segments]
    assert "x.ts" in uris
    x = next(s for s in state.segments if s.uri == "x.ts")
    y = next(s for s in state.segments if s.uri == "y.ts")
    assert x.discontinuity is True
    assert y.discontinuity is False


def test_ingest_trims_to_window_and_counts_discontinuity_sequence():
    state = SlotState(window=3)
    ingest(state, _src(0, ["a.ts", "b.ts"]), "url-A")
    # switch source -> c.ts carries a discontinuity, then push past the window
    ingest(state, _src(0, ["c.ts", "d.ts", "e.ts"]), "url-B")
    assert len(state.segments) == 3
    # a.ts and b.ts evicted; the discontinuity on c.ts is still in-window
    assert [s.uri for s in state.segments] == ["c.ts", "d.ts", "e.ts"]
    assert state.discontinuity_seq == 0
    # push more so c.ts (the discontinuity segment) rolls out
    ingest(state, _src(3, ["f.ts", "g.ts", "h.ts"]), "url-B")
    assert state.discontinuity_seq == 1


def test_render_has_required_headers_and_no_endlist():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "url-A")
    out = render(state)
    assert out.startswith("#EXTM3U")
    assert "#EXT-X-VERSION:6" in out
    assert "#EXT-X-MEDIA-SEQUENCE:0" in out
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in out
    assert "#EXT-X-TARGETDURATION:2" in out
    assert "#EXTINF:2.000," in out
    assert "a.ts" in out and "b.ts" in out
    assert "#EXT-X-ENDLIST" not in out


def test_render_emits_discontinuity_tag_before_segment():
    state = SlotState()
    ingest(state, _src(100, ["a.ts"]), "url-A")
    ingest(state, _src(0, ["x.ts"]), "url-B")
    out = render(state)
    lines = out.splitlines()
    x_index = lines.index("x.ts")
    # the line two before the URI (EXTINF is directly before) should include the tag
    assert "#EXT-X-DISCONTINUITY" in lines[x_index - 2 : x_index]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relay/test_playlist.py -v`
Expected: FAIL — `ImportError: cannot import name 'Segment'`

- [ ] **Step 3: Write minimal implementation**

Add to the top imports and the bottom of `src/quadlink/relay/playlist.py`:

```python
import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class Segment:
    """One republished segment in our own playlist."""

    seq: int
    uri: str
    duration: float
    program_date_time: str | None
    discontinuity: bool


@dataclass
class SlotState:
    """Rolling republish state for one slot."""

    window: int = 12
    segments: deque[Segment] = field(default_factory=deque)
    next_seq: int = 0
    discontinuity_seq: int = 0
    last_source_url: str | None = None
    last_source_seq: int | None = None
    pending_discontinuity: bool = False


def ingest(state: SlotState, source_text: str, source_url: str) -> None:
    """Fold a freshly-fetched source playlist into our rolling window."""
    _, media_sequence, segments = parse_media_playlist(source_text)

    # source change -> reset source-sequence tracking, splice a discontinuity
    if source_url != state.last_source_url:
        state.last_source_url = source_url
        state.last_source_seq = None
        if state.segments:  # only splice if we've already served something
            state.pending_discontinuity = True

    for index, (duration, uri, pdt, src_disc) in enumerate(segments):
        source_seq = media_sequence + index
        if state.last_source_seq is not None and source_seq <= state.last_source_seq:
            continue  # already ingested this segment
        disc = state.pending_discontinuity or src_disc
        state.pending_discontinuity = False
        state.segments.append(
            Segment(
                seq=state.next_seq,
                uri=uri,
                duration=duration,
                program_date_time=pdt,
                discontinuity=disc,
            )
        )
        state.next_seq += 1
        state.last_source_seq = source_seq

    # trim to window; count out any discontinuity segments that roll off
    while len(state.segments) > state.window:
        removed = state.segments.popleft()
        if removed.discontinuity:
            state.discontinuity_seq += 1


def render(state: SlotState) -> str:
    """Render our current window as a live HLS media playlist."""
    segments = list(state.segments)
    target = max(1, max((math.ceil(s.duration) for s in segments), default=1))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target}",
        f"#EXT-X-MEDIA-SEQUENCE:{segments[0].seq}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{state.discontinuity_seq}",
    ]
    for seg in segments:
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if seg.program_date_time:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{seg.program_date_time}")
        lines.append(f"#EXTINF:{seg.duration:.3f},")
        lines.append(seg.uri)
    return "\n".join(lines) + "\n"
```

Note: `math` is imported both at the module top (add it there) and used in `render`. Ensure a single `import math` at the top of the file alongside the Task 2 code.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_relay/test_playlist.py -v`
Expected: PASS (all parse + ingest + render tests)

- [ ] **Step 5: Format, type-check, commit**

```bash
black src/quadlink/relay/playlist.py tests/test_relay/test_playlist.py
mypy src/quadlink/relay/playlist.py
git add src/quadlink/relay/playlist.py tests/test_relay/test_playlist.py
git commit -m "add relay republish state machine (ingest + render)"
```

---

### Task 4: SlotRelay HTTP server

**Files:**
- Create: `src/quadlink/relay/server.py`
- Test: `tests/test_relay/test_server.py`

**Interfaces:**
- Consumes: `SlotStore.get` (Task 1); `SlotState`, `ingest`, `render` (Task 3).
- Produces: `SlotRelay(store, *, fetch=None, cache_ttl=1.0, window=12, time_fn=time.monotonic)` with:
  - `async handle(request) -> web.Response` — the route handler.
  - `register_routes(router) -> None` — adds `GET /streams/{slot}`.
  - `async aclose() -> None` — closes the internal httpx client.
  - `fetch` (optional) is an async callable `(url: str) -> str`; when omitted the relay uses an internal `httpx.AsyncClient`.

- [ ] **Step 1: Write the failing test**

`tests/test_relay/test_server.py`:

```python
"""Tests for the SlotRelay aiohttp server."""

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from quadlink.relay.server import SlotRelay
from quadlink.relay.store import SlotStore


def _src(media_seq: int, uris: list[str]) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{media_seq}"]
    for uri in uris:
        lines.append("#EXTINF:2.000,live")
        lines.append(uri)
    return "\n".join(lines) + "\n"


async def _call(relay: SlotRelay, slot: str) -> web.Response:
    request = make_mocked_request("GET", f"/streams/{slot}", match_info={"slot": slot})
    return await relay.handle(request)


@pytest.mark.asyncio
async def test_empty_slot_returns_503():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    resp = await _call(relay, "1")
    assert resp.status == 503


@pytest.mark.asyncio
async def test_bad_slot_returns_404():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    resp = await _call(relay, "9")
    assert resp.status == 404
    resp = await _call(relay, "x")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_serves_playlist_with_headers():
    store = SlotStore()
    store.update(["url-A", "", "", ""])

    async def fetch(url):
        return _src(100, ["a.ts", "b.ts"])

    relay = SlotRelay(store, fetch=fetch)
    resp = await _call(relay, "1")
    assert resp.status == 200
    assert resp.content_type == "application/vnd.apple.mpegurl"
    assert resp.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
    assert resp.headers["Pragma"] == "no-cache"
    assert resp.headers["Expires"] == "0"
    assert "#EXT-X-MEDIA-SEQUENCE:0" in resp.text
    assert "#EXT-X-ENDLIST" not in resp.text


@pytest.mark.asyncio
async def test_cache_coalesces_fetches():
    store = SlotStore()
    store.update(["url-A", "", "", ""])
    calls = 0

    async def fetch(url):
        nonlocal calls
        calls += 1
        return _src(100, ["a.ts"])

    clock = {"t": 0.0}
    relay = SlotRelay(store, fetch=fetch, cache_ttl=1.0, time_fn=lambda: clock["t"])
    await _call(relay, "1")
    await _call(relay, "1")  # within TTL -> no second fetch
    assert calls == 1
    clock["t"] = 2.0
    await _call(relay, "1")  # past TTL -> refetch
    assert calls == 2


@pytest.mark.asyncio
async def test_fetch_failure_serves_last_window_then_503():
    store = SlotStore()
    store.update(["url-A", "", "", ""])
    state = {"fail": False}

    async def fetch(url):
        if state["fail"]:
            raise RuntimeError("boom")
        return _src(100, ["a.ts", "b.ts"])

    # first call ok (populates window), advance clock so cache misses, then fail
    clock = {"t": 0.0}
    relay = SlotRelay(store, fetch=fetch, cache_ttl=1.0, time_fn=lambda: clock["t"])
    ok = await _call(relay, "1")
    assert ok.status == 200
    clock["t"] = 5.0
    state["fail"] = True
    stale = await _call(relay, "1")
    assert stale.status == 200  # served last good window
    assert "a.ts" in stale.text


@pytest.mark.asyncio
async def test_fetch_failure_with_no_window_returns_503():
    store = SlotStore()
    store.update(["url-A", "", "", ""])

    async def fetch(url):
        raise RuntimeError("boom")

    relay = SlotRelay(store, fetch=fetch)
    resp = await _call(relay, "1")
    assert resp.status == 503


def test_register_routes_adds_streams_path():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    app = web.Application()
    relay.register_routes(app.router)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/streams/{slot}" in paths


class AsyncNever:
    """A fetch callable that must never be invoked in a given test."""

    async def __call__(self, url):  # pragma: no cover - guard
        raise AssertionError("fetch should not be called")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relay/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quadlink.relay.server'`

- [ ] **Step 3: Write minimal implementation**

`src/quadlink/relay/server.py`:

```python
"""aiohttp server that republishes each slot as a live HLS media playlist.

Fetches the slot's current Twitch media playlist on demand (short cache),
folds it into a rolling window via the pure core in playlist.py, and serves
our own continuously-live playlist. Segments stay absolute Twitch CDN URLs.
"""

import time
from collections.abc import Awaitable, Callable

import httpx
import structlog
from aiohttp import web

from quadlink.relay.playlist import SlotState, ingest, render
from quadlink.relay.store import SlotStore

logger = structlog.get_logger()

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

FetchFn = Callable[[str], Awaitable[str]]


class SlotRelay:
    """Serves /streams/{slot} as a live, source-swappable HLS media playlist."""

    def __init__(
        self,
        store: SlotStore,
        *,
        fetch: FetchFn | None = None,
        cache_ttl: float = 1.0,
        window: int = 12,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._fetch = fetch
        self.cache_ttl = cache_ttl
        self.window = window
        self._time = time_fn
        self._states: dict[int, SlotState] = {}
        self._cache: dict[str, tuple[float, str]] = {}
        self._client: httpx.AsyncClient | None = None

    async def _default_fetch(self, url: str) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.text

    async def _cached_fetch(self, url: str) -> str:
        now = self._time()
        hit = self._cache.get(url)
        if hit is not None and now - hit[0] < self.cache_ttl:
            return hit[1]
        fetch = self._fetch or self._default_fetch
        text = await fetch(url)
        self._cache[url] = (now, text)
        return text

    async def handle(self, request: web.Request) -> web.Response:
        """Route handler for GET /streams/{slot}."""
        raw = request.match_info.get("slot", "")
        try:
            slot = int(raw)
        except ValueError:
            return web.Response(status=404, text="bad slot")
        if slot not in (1, 2, 3, 4):
            return web.Response(status=404, text="bad slot")

        url = self.store.get(slot)
        if not url:
            return web.Response(status=503, text="slot empty")

        state = self._states.setdefault(slot, SlotState(window=self.window))
        try:
            text = await self._cached_fetch(url)
        except Exception as e:
            logger.warning("relay source fetch failed", slot=slot, url=url, error=str(e))
            if state.segments:
                return self._playlist_response(render(state))
            return web.Response(status=503, text="source unavailable")

        ingest(state, text, url)
        if not state.segments:
            return web.Response(status=503, text="no segments")
        return self._playlist_response(render(state))

    def _playlist_response(self, body: str) -> web.Response:
        return web.Response(
            text=body,
            content_type="application/vnd.apple.mpegurl",
            headers=NO_CACHE_HEADERS,
        )

    def register_routes(self, router: web.UrlDispatcher) -> None:
        """Register GET /streams/{slot} on an existing aiohttp router."""
        router.add_get("/streams/{slot}", self.handle)

    async def aclose(self) -> None:
        """Close the internal httpx client, if one was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_relay/test_server.py -v`
Expected: PASS (all server tests)

- [ ] **Step 5: Format, type-check, commit**

```bash
black src/quadlink/relay/server.py tests/test_relay/test_server.py
mypy src/quadlink/relay/server.py
git add src/quadlink/relay/server.py tests/test_relay/test_server.py
git commit -m "add SlotRelay aiohttp server"
```

---

### Task 5: Wire relay routes into the web UI

**Files:**
- Modify: `src/quadlink/webui.py` (`WebUI.__init__` ~lines 57-75; `_setup_routes` ~lines 83-88)
- Test: `tests/test_relay/test_server.py`

**Interfaces:**
- Consumes: `SlotRelay.register_routes` (Task 4).
- Produces: `WebUI(config_loader, host=..., port=..., slot_relay=None)` — when `slot_relay` is given, its routes are registered on the app.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_relay/test_server.py`:

```python
from unittest.mock import MagicMock

from quadlink.webui import WebUI


def test_webui_registers_relay_routes():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    webui = WebUI(MagicMock(), slot_relay=relay)
    paths = {r.resource.canonical for r in webui.app.router.routes()}
    assert "/streams/{slot}" in paths


def test_webui_without_relay_has_no_stream_routes():
    webui = WebUI(MagicMock())
    paths = {r.resource.canonical for r in webui.app.router.routes()}
    assert "/streams/{slot}" not in paths
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_relay/test_server.py::test_webui_registers_relay_routes -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'slot_relay'`

- [ ] **Step 3: Write minimal implementation**

In `src/quadlink/webui.py`, add the import near the top (after existing imports):

```python
from quadlink.relay.server import SlotRelay
```

Change `WebUI.__init__` signature and body to accept and store the relay:

```python
    def __init__(
        self,
        config_loader: ConfigLoader,
        host: str = "0.0.0.0",
        port: int = 8081,
        slot_relay: SlotRelay | None = None,
    ):
        """Initialize web UI server.

        Args:
            config_loader: ConfigLoader instance for reading config.
            host: Host address to bind to.
            port: Port number to listen on.
            slot_relay: Optional slot relay whose /streams routes are registered.
        """
        self.host = host
        self.port = port
        self.config_loader = config_loader
        self.slot_relay = slot_relay
        self.config_path = self._find_config_path()
        self.app = web.Application()
        self._setup_routes()
```

Update `_setup_routes` to register relay routes when present:

```python
    def _setup_routes(self) -> None:
        """Set up HTTP routes."""
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/api/config", self._handle_get_config)
        self.app.router.add_post("/api/config", self._handle_post_config)
        self.app.router.add_post("/api/validate", self._handle_validate)
        if self.slot_relay is not None:
            self.slot_relay.register_routes(self.app.router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_relay/test_server.py -v`
Expected: PASS (including the two new webui tests)

- [ ] **Step 5: Format, type-check, commit**

```bash
black src/quadlink/webui.py tests/test_relay/test_server.py
mypy src/quadlink/webui.py
git add src/quadlink/webui.py tests/test_relay/test_server.py
git commit -m "register relay routes on the web UI app"
```

---

### Task 6: Wire relay into the daemon

**Files:**
- Modify: `src/quadlink/daemon.py` (imports ~lines 10-15; `Daemon.__init__` ~lines 52-66; `start` ~lines 72-74; `_main_loop` after `build_quad` ~line 129; `run_daemon` finally ~lines 214-218)
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: `SlotStore` (Task 1), `SlotRelay` (Task 4), `WebUI(..., slot_relay=...)` (Task 5), `Quad.to_list()` (existing).
- Produces: `Daemon` with `self.slot_store: SlotStore` and `self.slot_relay: SlotRelay` created in `__init__`; the main loop calls `self.slot_store.update(quad.to_list())` each cycle; `run_daemon` closes the relay client on shutdown.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon.py`:

```python
class TestDaemonRelayWiring:
    """Tests for slot store / relay integration."""

    def test_init_creates_store_and_relay(self):
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()
        assert daemon.slot_store is not None
        assert daemon.slot_relay.store is daemon.slot_store

    @pytest.mark.asyncio
    async def test_main_loop_writes_quad_to_slot_store(self, mock_config):
        from quadlink.types import Quad

        with patch("quadlink.daemon.ConfigLoader") as MockLoader:
            with patch("quadlink.daemon.HealthServer"):
                mock_loader = MagicMock()
                mock_loader.load_or_cache = AsyncMock(return_value=mock_config)
                MockLoader.return_value = mock_loader

                daemon = Daemon(one_shot=True)
                daemon.running = True

                quad = Quad("u1", "u2", "u3", "u4")

                with patch("quadlink.daemon.StreamProcessor") as MockProc:
                    with patch("quadlink.daemon.QuadBuilder") as MockBuilder:
                        with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                            proc = MagicMock()
                            proc.process_stream_groups = AsyncMock(return_value=["c"])
                            MockProc.return_value = proc

                            builder = MagicMock()
                            builder.build_quad = MagicMock(return_value=quad)
                            builder.quad_changed = True
                            MockBuilder.return_value = builder

                            client = AsyncMock()
                            client.login = AsyncMock(return_value=True)
                            client.update_quad = AsyncMock(return_value=True)
                            MockClient.return_value = client

                            with patch("asyncio.sleep", new_callable=AsyncMock):
                                await daemon._main_loop()

        assert daemon.slot_store.get(1) == "u1"
        assert daemon.slot_store.get(4) == "u4"
```

Note: `mock_config` is the existing fixture in `TestDaemonMainLoop`. If it is not module-scoped, copy its definition into `TestDaemonRelayWiring` or promote it to a module-level fixture. It returns a `MagicMock` config whose `credentials.username`/`credentials.secret` are truthy strings and `webhook.enabled` is `False`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_daemon.py::TestDaemonRelayWiring -v`
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute 'slot_store'`

- [ ] **Step 3: Write minimal implementation**

In `src/quadlink/daemon.py`, add imports near the other `quadlink` imports:

```python
from quadlink.relay.server import SlotRelay
from quadlink.relay.store import SlotStore
```

In `Daemon.__init__`, after `self.health_server = ...` and before `self.webui`, create the store and relay:

```python
        self.slot_store = SlotStore()
        self.slot_relay = SlotRelay(self.slot_store)
```

In `start`, pass the relay when constructing `WebUI`:

```python
        if self.enable_webui:
            self.webui = WebUI(
                self.config_loader,
                host=self.webui_host,
                port=self.webui_port,
                slot_relay=self.slot_relay,
            )
            self.webui_runner = await self.webui.start()
```

In `_main_loop`, immediately after `quad = self.quad_builder.build_quad(candidates)` (line ~129), record the URLs for the relay — before the `is_empty` check, and independent of the quadstream update:

```python
                quad = self.quad_builder.build_quad(candidates)

                # feed the local relay every cycle, regardless of quadstream
                self.slot_store.update(quad.to_list())
```

In `run_daemon`, close the relay client in the `finally` block alongside the existing cleanup:

```python
    finally:
        if daemon.health_server:
            daemon.health_server.stop()
        if daemon.webui_runner:
            await daemon.webui_runner.cleanup()
        await daemon.slot_relay.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_daemon.py::TestDaemonRelayWiring -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Full suite, format, type-check**

```bash
pytest
black src/ tests/
mypy src/
```
Expected: all tests pass; mypy reports only the 3 pre-existing streamlink errors.

- [ ] **Step 6: Commit**

```bash
git add src/quadlink/daemon.py tests/test_daemon.py
git commit -m "wire slot relay into the daemon"
```

---

## Manual validation (after implementation)

Not automated — do these once the tasks are complete and before pointing the Apple TV at duke:

1. Run `python -m quadlink --webui --webui-host 127.0.0.1 --webui-port 8081` locally with real credentials.
2. `curl -s http://127.0.0.1:8081/streams/1 | head -20` — confirm a media playlist with `#EXT-X-VERSION:6`, a `#EXT-X-MEDIA-SEQUENCE`, `#EXTINF` + absolute `*.ttvnw.net` segment URLs, and no `#EXT-X-ENDLIST`.
3. Point Safari (same AVFoundation engine as tvOS) at `http://127.0.0.1:8081/streams/1` — confirm playback.
4. Watch a slot swap (or force one) and confirm `#EXT-X-DISCONTINUITY` appears and playback continues after a brief rebuffer.
5. Run the emitted playlist through Apple's `mediastreamvalidator` for conformance.
6. Deploy to duke; add an nginx `location /streams/` block proxying to the container if nginx isn't already a catch-all; point one Apple TV tile at the duke URL; watch switching + the aiohttp access log.

## Self-Review notes

- **Spec coverage:** SlotStore (§Component 1) → Task 1; parser + republisher core (§Component 2) → Tasks 2-3; HTTP routes + headers + on-demand fetch/cache (§Component 3, §harvest decision) → Task 4; web UI hosting (§Config) → Task 5; daemon write-hook + dual-home + shutdown (§Daemon integration) → Task 6. Failure table → Task 4 tests (503 empty, stale-window on fetch failure). Testing/validation (§Testing) → per-task unit tests + Manual validation section.
- **Version floor:** `#EXT-X-VERSION:6` enforced in `render` (Task 3) and asserted in tests.
- **Dual-home:** no task touches `update_quad`; `SlotStore.update` is additive in `_main_loop` (Task 6).
