"""Tests for the LL-HLS slots: prefetch, progressive parts, cushion, filler and splices."""

import asyncio
import re
import time
import types
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime

import aiohttp
import pytest
import structlog.testing
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from quadlink.relay import lowlat
from quadlink.relay.lowlat import (
    CATCH_UP,
    CUSHION,
    CUSHION_MARGIN,
    CUSHION_MAX,
    CUSHION_MIN,
    CUSHION_SETTLE,
    CUSHION_SLACK,
    CUSHION_STEP,
    LATENCY_REPORT,
    LOW_FOR,
    PART_HOLD_BACK,
    PART_TARGET,
    PREFETCH_LEAD,
    STARTUP,
    STARTUP_HOLD,
    Lateness,
    LowLatency,
    _Part,
    _Seg,
    _source_time,
    next_release,
    preload_hints,
    start_back,
    with_prefetch,
    without_prefetch,
)
from quadlink.relay.parts import Part
from quadlink.relay.playlist import Segment
from quadlink.relay.tsinfo import audio_sample_rate

SOURCE = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-PROGRAM-DATE-TIME:2026-10-23T22:41:03.088Z
#EXTINF:2.000,live
https://cdn/a.ts
#EXT-X-PROGRAM-DATE-TIME:2026-10-23T22:41:05.088Z
#EXTINF:2.000,live
https://cdn/b.ts
#EXT-X-TWITCH-PREFETCH:https://cdn/c.ts
#EXT-X-TWITCH-PREFETCH:https://cdn/d.ts
"""


class TestWithPrefetch:
    def test_prefetch_becomes_ordinary_segments(self):
        text, prefetch = with_prefetch(SOURCE)
        assert prefetch == {"https://cdn/c.ts", "https://cdn/d.ts"}
        uris = [line for line in text.splitlines() if line.startswith("https://")]
        assert uris == [
            "https://cdn/a.ts",
            "https://cdn/b.ts",
            "https://cdn/c.ts",
            "https://cdn/d.ts",
        ]
        assert "TWITCH-PREFETCH" not in text

    def test_prefetch_times_run_ahead_of_their_later_listing(self):
        text, _ = with_prefetch(SOURCE)
        times = re.findall(r"PROGRAM-DATE-TIME:(\S+)", text)
        # c is due 22:41:07.088: listed a second later, its real listing reads as old
        assert times[2:] == ["2026-10-23T22:41:08.088+00:00", "2026-10-23T22:41:10.088+00:00"]

    def test_no_prefetch_during_an_ad(self):
        ad = SOURCE.replace(
            "#EXTINF:2.000,live\nhttps://cdn/b.ts", "#EXTINF:2.000,Amazon\nhttps://cdn/b.ts"
        )
        assert with_prefetch(ad) == (ad, set())

    def test_without_prefetch_drops_only_the_prefetch_lines(self):
        text = without_prefetch(SOURCE)
        assert "TWITCH-PREFETCH" not in text
        assert [line for line in text.splitlines() if not line.startswith("#")] == [
            "https://cdn/a.ts",
            "https://cdn/b.ts",
        ]
        assert "#EXT-X-MEDIA-SEQUENCE:100" in text

    def test_a_playlist_without_prefetch_is_unchanged(self):
        plain = "\n".join(x for x in SOURCE.splitlines() if "PREFETCH" not in x) + "\n"
        assert with_prefetch(plain) == (plain, set())


class TestNextRelease:
    def test_a_part_follows_the_one_before_by_its_duration(self):
        prev = _Part(b"", 0.5, False, release=100.0)
        assert next_release(99.0, prev) == pytest.approx(100.5)
        assert next_release(99.0, prev, CATCH_UP) == pytest.approx(100.0 + 0.5 / CATCH_UP)

    def test_a_late_part_goes_out_now(self):
        assert next_release(200.0, _Part(b"", 0.5, False, release=100.0)) == 200.0
        assert next_release(5.0, None) == 5.0

    def test_a_timestamp_jump_holds_the_next_part_back_one_part_at_most(self):
        # a PTS wrap reads as a part lasting about a day
        prev = _Part(b"", 95_000.0, False, release=100.0)
        assert next_release(100.0, prev) == pytest.approx(100.0 + PART_TARGET)


def _listing(*uris: str, duration: float = 2.0) -> list[Segment]:
    return [Segment(k, uri, duration, None, False, None) for k, uri in enumerate(uris)]


class TestStartBack:
    def test_a_run_starts_one_complete_segment_back_from_the_encoding_one(self):
        # 2s complete plus about 1s of the one being encoded
        segs = _listing("a", "b", "c", "d", "e")
        assert start_back(segs, {"d", "e"}) == 2

    def test_without_prefetch_it_starts_a_cushion_back_from_the_end(self):
        segs = _listing("a", "b", "c", "d")
        assert start_back(segs, set()) == 2
        assert start_back(_listing("a", "b", "c", duration=1.0), set()) == 0

    def test_only_prefetch_starts_at_the_first(self):
        assert start_back(_listing("d", "e"), {"d", "e"}) == 0


def _slot() -> lowlat._Slot:
    return lowlat._Slot(types.SimpleNamespace(), 0)


class Cuts:
    """Feeds a slot 0.5s parts in 2s segments, each cut at a given loop time."""

    def __init__(self, state: lowlat._Slot) -> None:
        self.state = state
        self.k = 0
        self.released: list[float] = []
        self.out: dict[int, _Part] = {}

    def cut(self, now: float, n: int = 1) -> None:
        for _ in range(n):
            self.state._add([Part(0, b"", 0.5, self.k % 4 == 0, self.k % 4 == 3)], now)
            self.k += 1
            last = self.state._last_part()
            assert last is not None
            self.released.append(last.release)

    def releases(self) -> list[float]:
        return self.released

    def watch(self, now: float) -> None:
        for seg in self.state.segs:
            for p in seg.parts:
                if p.release <= now:
                    self.out[id(p)] = p

    def released_media(self) -> float:
        """Media of every part that went out, as a player received it."""
        return sum(p.duration for p in self.out.values())

    def run(self, start: float, end: float, at: Callable[[float], int] = lambda t: 1) -> None:
        """One step every 0.5s from start to end; `at(t)` parts are cut at each."""
        t = start
        while t < end - 1e-9:
            if n := at(t):
                self.cut(t, n)
            self.watch(t)
            t += 0.5


class TestCushion:
    def test_a_run_starts_with_its_cushion_queued_and_its_first_part_out(self):
        state = _slot()
        cuts = Cuts(state)
        cuts.cut(10.0, 5)
        assert cuts.releases()[0] == 10.0
        assert state.cushion(10.0) == pytest.approx(2.5)
        # real time from there: one part every 0.5s
        assert cuts.releases() == pytest.approx([10.0 + 0.5 * k for k in range(5)])

    def test_a_gap_shorter_than_the_cushion_keeps_the_schedule(self):
        state = _slot()
        cuts = Cuts(state)
        cuts.cut(0.0, 5)
        # the source sends nothing for 2s, then what it held back
        cuts.run(0.5, 20.0, lambda t: 0 if 5.0 <= t < 7.0 else 5 if t == 7.0 else 1)
        releases = cuts.releases()
        steps = [b - a for a, b in zip(releases, releases[1:], strict=False)]
        assert steps == pytest.approx([0.5] * len(steps))

    def test_excess_cushion_sheds_a_whole_segment(self):
        state = _slot()
        cuts = Cuts(state)
        cuts.cut(0.0, 5)
        cuts.run(0.5, 10.0)
        # 3s too much at once: more than one whole segment over CUSHION
        cuts.cut(10.0, 7)
        assert state.cushion(10.0) <= CUSHION + 2.0
        seqs = [seg.seq for seg in state.segs]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        disc = [seg for seg in state.segs if seg.disc]
        assert len(disc) == 1 and disc[0].parts[0].independent
        assert disc[0].dseq == state.segs[0].dseq + 1
        # what is left goes on at real time, from where the dropped segment would have
        assert all(seg.dseq == disc[0].dseq for seg in state.segs if seg.seq > disc[0].seq)
        releases = [p.release for seg in state.segs for p in seg.parts]
        steps = [b - a for a, b in zip(releases, releases[1:], strict=False)]
        assert steps == pytest.approx([0.5] * len(steps))
        # PROGRAM-DATE-TIME runs on by duration
        for a, b in zip(state.segs, state.segs[1:], strict=False):
            assert b.wall == pytest.approx(a.wall + a.duration)

    def test_catch_up_only_refills_what_a_stall_took(self):
        state = _slot()
        state.player_start(0.0)
        cuts = Cuts(state)
        cuts.cut(0.0, 9)
        # the startup lead is out; then 4s without a part: the released edge
        # stands still about 1.5s, then a burst
        cuts.run(0.5, 120.0, lambda t: 0 if 10.0 <= t < 14.0 else 9 if t == 14.0 else 1)
        later = cuts.releases()[9:]
        steps = [b - a for a, b in zip(later, later[1:], strict=False)]
        assert min(steps) >= 0.5 / CATCH_UP - 1e-9
        # back on real time since the player started plus its lead (which counts
        # the first part), never past it
        assert cuts.released_media() == pytest.approx(120.0 + PART_HOLD_BACK - 0.5, abs=0.5)
        # measured as _add does, at the low point of its sawtooth
        assert state.cushion(119.99) <= CUSHION + CUSHION_SLACK

    def test_a_player_start_on_a_warm_slot_grants_nothing(self):
        state = _slot()
        cuts = Cuts(state)
        cuts.cut(0.0, 5)
        cuts.run(0.5, 10.0)
        releases = [p.release for seg in state.segs for p in seg.parts]
        cushion = state.cushion(10.0)
        # a rebuild, a retry, a probe or a second client: the slot lists plenty
        for t in (10.0, 10.0 + STARTUP + 1):
            state.player_blocked()
            state.player_start(t)
        assert [p.release for seg in state.segs for p in seg.parts] == releases
        assert state.cushion(10.0) == cushion

    def test_a_cold_slot_gives_the_lead_as_parts_are_cut(self):
        # the request comes before any part: a prefetch source has none cut yet
        state = _slot()
        state.player_start(0.0)
        cuts = Cuts(state)
        cuts.cut(0.6, 1)
        cuts.cut(1.0, 1)
        cuts.cut(1.1, 3)
        cuts.watch(1.1)
        # what is cut goes out as soon as cut, up to the hold-back
        assert cuts.released_media() >= PART_HOLD_BACK - 0.5
        cuts.run(1.5, 20.0)
        assert cuts.released_media() <= 20.0 + PART_HOLD_BACK

    def test_a_player_start_keeps_program_date_times_running_on(self):
        state = _slot()
        cuts = Cuts(state)
        # cut before the request: the next segment's first part moves earlier
        cuts.cut(0.0, 6)
        state.player_start(0.0)
        for a, b in zip(state.segs, state.segs[1:], strict=False):
            assert b.wall == pytest.approx(a.wall + a.duration)

    def test_plain_requests_of_one_player_open_get_the_lead_once(self):
        state = _slot()
        cuts = Cuts(state)
        state.player_start(0.0)
        cuts.cut(0.6, 1)
        state.player_start(0.7)
        cuts.cut(1.0, 8)
        state.player_start(1.1)
        cuts.watch(1.1)
        assert cuts.released_media() == pytest.approx(STARTUP_HOLD, abs=0.5)
        cuts.run(1.5, 30.0)
        assert cuts.released_media() <= 30.0 - 0.6 + STARTUP_HOLD + 0.5

    def test_player_latency_stays_bounded_across_swaps(self):
        state = _slot()
        state.player_start(0.0)
        cuts = Cuts(state)
        cuts.cut(0.1, 5)
        t = 0.5
        for _ in range(10):
            cuts.run(t, t + 40.0)
            t += 40.0
            state._drop_unreleased(t)
            state.pending_disc = True
            cuts.k = 0
            # each channel starts with more than its cushion in hand at once, which
            # catch-up alone would push into the player
            cuts.cut(t + 0.2, 7)
            cuts.watch(t + 0.2)
            t += 0.5
        cuts.run(t, t + 6.0)
        t += 6.0
        # the player receives no more media than real time since it started, plus
        # its startup lead: its buffer, and so its delay, does not grow with swaps
        assert cuts.released_media() <= t + PART_HOLD_BACK + 0.5

    def test_a_cushion_stays_while_the_source_keeps_time(self):
        state = _slot()
        cuts = Cuts(state)
        cuts.cut(0.0, 5)
        cuts.run(0.5, 60.0)
        assert state.cushion(59.75) == pytest.approx(CUSHION, abs=0.5)

    def test_a_splice_goes_out_without_waiting_for_a_cushion(self):
        state = _slot()
        cuts = Cuts(state)
        cuts.cut(0.0, 5)
        cuts.run(0.5, 10.0)
        state._drop_unreleased(10.0)
        state.pending_disc = True
        cuts.k = 0
        cuts.cut(10.1)
        first = state.segs[-1]
        assert first.disc and first.parts[0].release - 10.1 <= PART_TARGET


def _numbers(text: str) -> dict[int, int]:
    """Segment number -> discontinuity number, as RFC 8216 6.2.2 counts them."""
    msn = int(re.search(r"MEDIA-SEQUENCE:(\d+)", text).group(1))
    disc = int(re.search(r"DISCONTINUITY-SEQUENCE:(\d+)", text).group(1))
    found, seq = {}, msn
    for line in text.splitlines():
        if line == "#EXT-X-DISCONTINUITY":
            disc += 1
        elif line.startswith("/streams/") and "/seg/" in line:
            found[seq] = disc
            seq += 1
    return found


def test_a_segment_keeps_its_discontinuity_number_as_the_window_slides():
    # segment 11 follows a discontinuity; once 10 leaves the window it lists first
    segs = []
    for seq, disc, dseq in [(10, False, 5), (11, True, 6), (12, False, 6)]:
        seg = lowlat._Seg(seq, disc, dseq, complete=True)
        seg.parts.append(_Part(b"", 0.5, True, release=0.0))
        segs.append(seg)
    state = types.SimpleNamespace(segs=segs, slot=0, target=2)
    before = _numbers(lowlat._Slot.render(state, 1.0))
    state.segs = segs[1:]
    after = _numbers(lowlat._Slot.render(state, 1.0))
    assert before == {10: 5, 11: 6, 12: 6}
    assert after == {11: 6, 12: 6}


class Source:
    """A Twitch-like live channel: each 2s segment streams out while it 'encodes'.

    Segment i is listed once complete; the next two are prefetch. Bytes of the one
    being encoded go out in step with real time, as Twitch's do.
    """

    def __init__(self, segs: list[bytes], identity: str = "chan") -> None:
        self.segs = segs
        self.identity = identity
        self.start = time.monotonic()
        # while set, nothing past this time is listed or sent; cleared, the held
        # bytes go out at once
        self.stopped_at: float | None = None
        # the wall clock at start, for PROGRAM-DATE-TIME as Twitch stamps each segment
        self.wall_start = time.time()
        # "twitch", "hint" or "none": lets tests set a source that lists only complete
        # segments against ones that announce the segment still being encoded
        self.announce = "twitch"
        self.base = ""
        # seconds late the bytes of these segments come when asked for as prefetch,
        # as a broadcast with a slow low-latency edge sends them
        self.prefetch_lag: dict[int, float] = {}

    def encoding(self) -> int:
        """Index of the segment being encoded now."""
        now = self.stopped_at or time.monotonic()
        return min(int((now - self.start) / 2) + 1, len(self.segs))

    def playlist(self) -> str:
        live = self.encoding()
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", "#EXT-X-MEDIA-SEQUENCE:0"]
        for i in range(live):
            began = datetime.fromtimestamp(self.wall_start + 2 * (i - 1), UTC)
            lines += [
                f"#EXT-X-PROGRAM-DATE-TIME:{began.isoformat(timespec='milliseconds')}",
                "#EXTINF:2.000,live",
                f"{self.base}/{self.identity}/{i}.ts",
            ]
        if self.announce == "twitch":
            for i in range(live, min(live + 2, len(self.segs))):
                lines.append(f"#EXT-X-TWITCH-PREFETCH:{self.base}/{self.identity}/{i}.ts")
        elif self.announce == "hint" and live < len(self.segs):
            uri = f"{self.base}/{self.identity}/{live}.ts"
            lines.append(f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="{uri}"')
        return "\n".join(lines) + "\n"

    async def serve(self, request: web.Request) -> web.StreamResponse:
        i = int(request.match_info["i"])
        data = self.segs[i]
        lag = self.prefetch_lag.get(i, 0.0) if i >= self.encoding() else 0.0
        resp = web.StreamResponse()
        await resp.prepare(request)
        chunks = 20
        size = -(-len(data) // chunks)
        for k in range(chunks):
            # segment i encodes from 2(i-1)s to 2i s after start; i=0 is old
            due = self.start + 2 * (i - 1) + 2 * (k + 1) / chunks + lag
            while (self.stopped_at is not None and i >= self.encoding()) or time.monotonic() < due:
                await asyncio.sleep(0.02)
            await resp.write(data[k * size : (k + 1) * size])
        await resp.write_eof()
        return resp


class FakeRelay:
    """The slot relay as the LL engine sees it, with its playlist cache."""

    def __init__(self, cache_ttl: float = 1.0) -> None:
        self.source: Source | None = None
        self.arrivals: list[tuple[int, str]] = []
        self.cache_ttl = cache_ttl
        self._cached: tuple[float, Source, str] | None = None
        self.fresh_reads = 0

    def note_arrival(self, slot: int, identity: str) -> None:
        self.arrivals.append((slot, identity))

    async def current_source(self, slot: int, fresh: bool = False) -> tuple[str, str] | None:
        if self.source is None:
            return None
        now = time.monotonic()
        cached = self._cached
        if fresh:
            self.fresh_reads += 1
        elif cached is not None and cached[1] is self.source and now - cached[0] < self.cache_ttl:
            return self.source.identity, cached[2]
        text = self.source.playlist()
        self._cached = (now, self.source, text)
        return self.source.identity, text


@pytest.fixture
async def setup(ffmpeg: str, ts_run: Callable[..., list[bytes]]) -> AsyncIterator:
    relay = FakeRelay()
    sources: dict[str, Source] = {}

    async def segment(request: web.Request) -> web.StreamResponse:
        return await sources[request.match_info["chan"]].serve(request)

    origin = web.Application()
    origin.router.add_get("/{chan}/{i:\\d+}.ts", segment)
    origin_server = TestServer(origin)
    await origin_server.start_server()
    base = str(origin_server.make_url("")).rstrip("/")

    filler = ts_run(48000, 1)[0]
    ll = LowLatency(relay, ffmpeg, filler)  # type: ignore[arg-type]
    app = web.Application()
    ll.register_routes(app.router)
    client = TestClient(TestServer(app))
    await client.start_server()

    def add(name: str, rate: int = 44100, count: int = 6) -> Source:
        source = Source(ts_run(rate, count), name)
        source.base = base
        sources[name] = source
        return source

    yield relay, add, client, ll
    await ll.aclose()
    await client.close()
    await origin_server.close()


def _parts(text: str) -> list[tuple[float, str, bool]]:
    return [
        (float(d), uri, bool(ind))
        for d, uri, ind in re.findall(
            r'#EXT-X-PART:DURATION=([0-9.]+),URI="([^"]+)"(,INDEPENDENT=YES)?', text
        )
    ]


async def _playlist(client: TestClient) -> str:
    resp = await client.get("/streams/ll/1")
    assert resp.status == 200
    return await resp.text()


async def test_parts_come_out_half_a_second_apart_behind_the_cushion(setup):
    relay, add, client, _ = setup
    source = relay.source = add("chan")
    seen: dict[str, float] = {}
    while time.monotonic() - source.start < 5:
        resp = await client.get("/streams/ll/1")
        if resp.status == 200:
            text = await resp.text()
            for _, uri, _ in _parts(text):
                seen.setdefault(uri, time.monotonic() - source.start)
            for uri in re.findall(r"^/streams/ll/1/seg/\S+", text, re.M):
                seen.setdefault(uri, time.monotonic() - source.start)
        await asyncio.sleep(0.05)
    # the run starts one complete segment back, as its cushion, and the startup lead
    # spends into the first segment still encoding (0s to 2s); the one after is paced
    seq = min(int(re.search(r"/part/(\d+)\.", uri)[1]) for uri in seen if "/part/" in uri) + 2
    first = [seen[f"/streams/ll/1/part/{seq}.{k}.ts"] for k in range(4)]
    assert first[0] < 5.0
    # the source sends 0.1s chunks and this polls every 0.05s
    assert all(abs(b - a - 0.5) <= 0.15 for a, b in zip(first, first[1:], strict=False)), first
    assert seen[f"/streams/ll/1/seg/{seq}.ts"] >= first[3]


async def test_parts_are_converted_to_48k_and_segments_start_independent(setup):
    relay, add, client, _ = setup
    relay.source = add("chan")
    await _playlist(client)
    await asyncio.sleep(4.5)
    text = await _playlist(client)
    parts = _parts(text)
    firsts = [uri for _, uri, ind in parts if ind]
    assert firsts
    resp = await client.get(firsts[0])
    assert resp.status == 200
    data = await resp.read()
    # SDT may come first; PAT and PMT open the segment
    assert b"\x47\x40\x00" in data[: 3 * 188]
    assert audio_sample_rate(data) == 48000
    # whole segments are listed with their parts' total
    extinf = [float(x) for x in re.findall(r"#EXTINF:([0-9.]+),", text)]
    assert extinf and all(1.9 < d < 2.1 for d in extinf)


async def test_blocking_reload_waits_for_the_next_part(setup):
    relay, add, client, _ = setup
    relay.source = add("chan")
    text = await _playlist(client)
    hint = re.search(r'PRELOAD-HINT:TYPE=PART,URI="[^"]*/part/(\d+)\.(\d+)\.ts"', text)
    assert hint
    started = time.monotonic()
    resp = await client.get(f"/streams/ll/1?_HLS_msn={hint[1]}&_HLS_part={hint[2]}")
    assert resp.status == 200
    assert f"/part/{hint[1]}.{hint[2]}.ts" in (await resp.text()).split("PRELOAD-HINT")[0]
    assert time.monotonic() - started < 1.5


async def test_far_ahead_request_is_rejected(setup):
    relay, add, client, _ = setup
    relay.source = add("chan")
    text = await _playlist(client)
    msn = int(re.search(r"MEDIA-SEQUENCE:(\d+)", text)[1]) + 100
    resp = await client.get(f"/streams/ll/1?_HLS_msn={msn}&_HLS_part=0")
    assert resp.status == 400


async def test_a_cold_slot_lists_its_first_player_a_hold_back_at_once(setup):
    relay, add, client, ll = setup
    # a prefetch source: the first request comes before any part is cut
    relay.source = add("chan")
    loop = asyncio.get_running_loop()
    # the first playlist the player sees already holds its hold-back
    first = await _playlist(client)
    # (to within the part the lead ends inside)
    assert sum(d for d, _, _ in _parts(first)) >= PART_HOLD_BACK - PART_TARGET
    await asyncio.sleep(1.0)
    now = loop.time()
    parts = [p for s in ll.slot(1).segs for p in s.parts if p.release <= now]
    ahead = sum(p.duration for p in parts) - (now - parts[0].release)
    # the hold-back counts the first part, which is no lead
    assert ahead >= PART_HOLD_BACK - PART_TARGET - 0.2


async def test_the_first_playlist_waits_for_the_hold_back(setup):
    relay, add, client, _ = setup
    source = relay.source = add("chan")
    # nothing complete yet: the first segment is still encoding, its parts come
    # at real time
    source.start = time.monotonic() + 2
    first = await _playlist(client)
    assert sum(d for d, _, _ in _parts(first)) >= PART_HOLD_BACK - 0.01


async def test_a_plain_request_mid_play_grants_nothing(setup):
    relay, add, client, ll = setup
    relay.source = add("chan")
    await _playlist(client)
    await asyncio.sleep(4.0)
    state = ll.slot(1)
    before = [p.release for s in state.segs for p in s.parts]
    # after the startup window: a QuadStream rebuild, or a probe
    state._player_at -= STARTUP
    await _playlist(client)
    after = [p.release for s in state.segs for p in s.parts]
    assert after[: len(before)] == before


async def test_a_stall_shorter_than_the_cushion_sends_no_filler(setup):
    relay, add, client, ll = setup
    source = relay.source = add("chan", count=8)
    await _playlist(client)
    await asyncio.sleep(3.5)
    source.stopped_at = time.monotonic()
    await asyncio.sleep(1.0)
    source.stopped_at = None
    await asyncio.sleep(4.0)
    state = ll.slot(1)
    assert not any(seg.filler for seg in state.segs)
    releases = [p.release for s in state.segs for p in s.parts]
    # the startup lead took most of this source's short cushion into the player's
    # buffer; the edge stands still for less than that buffer
    assert max(b - a for a, b in zip(releases, releases[1:], strict=False)) < PART_HOLD_BACK


async def test_filler_goes_out_when_the_source_stalls(setup, monkeypatch):
    relay, add, client, ll = setup
    source = relay.source = add("chan")
    await _playlist(client)
    await asyncio.sleep(2.5)
    source.stopped_at = time.monotonic()
    await asyncio.sleep(CUSHION + lowlat.FILL_AFTER + 1.5)
    text = await _playlist(client)
    assert "#EXT-X-DISCONTINUITY" in text
    state = ll.slot(1)
    assert any(seg.filler for seg in state.segs)


async def test_a_channel_change_splices_at_once(setup):
    relay, add, client, ll = setup
    relay.source = add("one")
    await _playlist(client)
    await asyncio.sleep(3)
    loop = asyncio.get_running_loop()
    state = ll.slot(1)
    # the splice drops segments still in the cushion; their numbers are used again
    before = max(seg.seq for seg in state.segs if seg.released(loop.time()))
    relay.source = add("two")
    swapped = time.monotonic()
    while time.monotonic() - swapped < 3:
        new = [s for s in state.segs if s.seq > before and s.disc and s.released(loop.time())]
        if new:
            break
        await asyncio.sleep(0.05)
    # the new channel's first part goes out without waiting for its cushion
    assert new and time.monotonic() - swapped < 1.5
    await asyncio.sleep(1)
    text = await _playlist(client)
    assert new[0].parts[0].independent
    assert "#EXT-X-DISCONTINUITY" in text


async def test_an_ll_slot_reports_its_source_arrivals(setup):
    relay, add, client, _ = setup
    relay.source = add("chan")
    await _playlist(client)
    assert (0, "chan") in relay.arrivals


async def test_a_new_slot_counts_discontinuities_from_zero(setup):
    relay, add, client, _ = setup
    relay.source = add("chan")
    text = await _playlist(client)
    assert int(re.search(r"MEDIA-SEQUENCE:(\d+)", text)[1]) >= int(time.time() // 2) - 5
    assert int(re.search(r"DISCONTINUITY-SEQUENCE:(\d+)", text)[1]) == 0


async def test_a_restarted_slot_carries_on_its_numbering(setup):
    relay, add, client, ll = setup
    relay.source = add("chan")
    await _playlist(client)
    await asyncio.sleep(2.5)
    old = ll.slot(1)
    last, dseq = old.segs[-1].seq, old.segs[-1].dseq
    await ll.retire(old)
    await asyncio.sleep(0.2)
    text = await _playlist(client)
    first = int(re.search(r"MEDIA-SEQUENCE:(\d+)", text)[1])
    assert first > last
    assert int(re.search(r"DISCONTINUITY-SEQUENCE:(\d+)", text)[1]) >= dseq
    assert ll.slot(1).segs[0].disc


async def test_video_back_after_filler_starts_a_cushion_back_in_the_backlog(setup):
    _, _, _, ll = setup
    state = lowlat._Slot(ll, 0)

    def listing(complete: list[str], prefetch: list[str]) -> str:
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0"]
        for name in complete:
            lines += ["#EXTINF:2.000,live", f"http://127.0.0.1:9/{name}.ts"]
        lines += [f"#EXT-X-TWITCH-PREFETCH:http://127.0.0.1:9/{name}.ts" for name in prefetch]
        return "\n".join(lines) + "\n"

    def queued() -> list[str]:
        return [q.seg.uri.rsplit("/", 1)[1] for q in state.queue]

    state._take("chan", listing(["a"], ["b", "c"]))
    # one complete segment back from the one being encoded
    assert queued() == ["a.ts", "b.ts", "c.ts"]
    # all of it fed, then the source went quiet and filler went out
    state.queue.clear()
    filler = lowlat._Seg(1, True, 1, filler=True, complete=True)
    filler.parts.append(_Part(b"", 0.5, True, release=0.0))
    state.segs.append(filler)
    state._take("chan", listing(["a", "b", "c", "d", "e"], ["f", "g"]))
    assert queued() == ["e.ts", "f.ts", "g.ts"]
    assert state.queue[0].fresh and all(q.gen == state.gen for q in state.queue)
    await state.stop()


def _source_listing(complete: list[str], prefetch: list[str], ads: list[str] = ()) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", "#EXT-X-MEDIA-SEQUENCE:0"]
    for name in ads:
        lines += ["#EXTINF:2.000,Amazon", f"http://127.0.0.1:9/{name}.ts"]
    for name in complete:
        lines += ["#EXTINF:2.000,live", f"http://127.0.0.1:9/{name}.ts"]
    lines += [f"#EXT-X-TWITCH-PREFETCH:http://127.0.0.1:9/{name}.ts" for name in prefetch]
    return "\n".join(lines) + "\n"


def _queued(state: lowlat._Slot) -> list[str]:
    return [q.seg.uri.rsplit("/", 1)[1] for q in state.queue]


async def test_a_new_channel_listing_only_ads_still_switches(setup):
    _, _, _, ll = setup
    state = lowlat._Slot(ll, 0)
    state._take("one", _source_listing(["a"], ["b", "c"]))
    gen = state.gen
    state._take("two", _source_listing([], [], ads=["x", "y"]))
    assert state.gen > gen and not state.queue
    # the first listing with segments picks the start, a cushion back
    state._take("two", _source_listing(["p", "q"], ["r", "s"], ads=["x", "y"]))
    assert _queued(state) == ["q.ts", "r.ts", "s.ts"]
    await state.stop()


async def test_a_cushion_low_for_long_restarts_back_once(setup):
    _, _, _, ll = setup
    state = lowlat._Slot(ll, 0)
    now = asyncio.get_running_loop().time()
    state._take("chan", _source_listing(["a"], ["b", "c"]))
    # video out long ago, nothing since: the segment being fed has stalled
    seg = lowlat._Seg(1, False, 1, complete=True)
    seg.parts.append(_Part(b"", 0.5, True, release=now - 30))
    state.segs.append(seg)
    state.queue.popleft()
    listing = _source_listing(["a", "b", "c", "d"], ["e", "f"])
    state._take("chan", listing)
    assert not any(q.fresh for q in state.queue)
    state._low_since = now - LOW_FOR - 1
    state._take("chan", _source_listing(["a", "b", "c", "d", "e"], ["f", "g"]))
    assert _queued(state) == ["e.ts", "f.ts", "g.ts"] and state.queue[0].fresh
    # still low, but a restart just happened: no other
    gen = state.gen
    state._restarting = False
    state._low_since = now - LOW_FOR - 1
    state._take("chan", _source_listing(["a", "b", "c", "d", "e", "f"], ["g", "h"]))
    assert state.gen == gen
    await state.stop()


async def test_a_segment_listed_again_with_a_later_time_is_fed_once(setup):
    _, _, _, ll = setup
    state = ll.slot(1)

    def listing(media_seq: int, start: str, uris: list[str]) -> str:
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{media_seq}"]
        for k, uri in enumerate(uris):
            lines += [
                f"#EXT-X-PROGRAM-DATE-TIME:{start}:{10 + 2 * k:02d}.000Z",
                "#EXTINF:2.000,live",
                uri,
            ]
        return "\n".join(lines) + "\n"

    state._take("chan", listing(5, "2026-10-24T19:00", ["http://x/a.ts?t=1", "http://x/b.ts?t=1"]))
    queued = len(state.queue)
    # the same media again, its seq at the watermark but its time later: ingest's
    # test for a restarted broadcast passes, and only the URI shows it is a repeat
    state._take("chan", listing(5, "2026-10-24T19:05", ["http://x/a.ts?t=2", "http://x/b.ts?t=2"]))
    assert len(state.queue) == queued


async def test_program_date_times_follow_segment_durations(setup):
    relay, add, client, _ = setup
    relay.source = add("chan")
    await _playlist(client)
    await asyncio.sleep(6.5)
    text = await _playlist(client)
    stamps = [
        datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        for t in re.findall(r"PROGRAM-DATE-TIME:(\S+)", text)
    ]
    durations = [float(d) for d in re.findall(r"#EXTINF:([0-9.]+),", text)]
    assert len(durations) >= 2
    for a, b, d in zip(stamps, stamps[1:], durations, strict=False):
        assert b - a == pytest.approx(d, abs=0.002)


async def test_a_cold_first_playlist_closes_a_segment(setup):
    # AVPlayer fails (-12312) a stream whose first playlist lists only an open
    # segment's parts and whose next lists that segment complete
    relay, add, client, _ = setup
    relay.source = add("chan")
    text = await _playlist(client)
    assert re.search(r"^#EXTINF:", text, re.M), text
    assert sum(d for d, _, _ in _parts(text)) >= STARTUP_HOLD - PART_TARGET


def _stalled_slot(ll: LowLatency, channel: str = "chan") -> lowlat._Slot:
    """A slot playing channel from prefetch: a, then b and c being encoded."""
    state = lowlat._Slot(ll, 0)
    state._take(channel, _source_listing(["a"], ["b", "c"]))
    state.played = channel
    return state


class TestPrefetchBackOff:
    async def test_one_stall_keeps_prefetch(self, setup):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        state._note_stall(100.0)
        assert state.no_prefetch is None and not state._backing_off
        await state.stop()

    async def test_stalls_close_together_turn_prefetch_off(self, setup):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        with structlog.testing.capture_logs() as logs:
            state._note_stall(100.0)
            state._note_stall(100.0 + lowlat.BACKOFF_WINDOW - 1)
        assert state.no_prefetch == "chan" and state._backing_off
        assert [e for e in logs if e["event"] == "relay ll prefetch off"] == [
            {
                "event": "relay ll prefetch off",
                "slot": 0,
                "channel": "chan",
                "stalls": 2,
                "log_level": "info",
            }
        ]
        await state.stop()

    async def test_stalls_far_apart_keep_prefetch(self, setup):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        state._note_stall(100.0)
        state._note_stall(100.0 + lowlat.BACKOFF_WINDOW + 1)
        assert state.no_prefetch is None
        await state.stop()

    @pytest.mark.parametrize("cause", ["failing", "starting", "other channel playing"])
    async def test_stalls_not_of_prefetch_do_not_count(self, setup, cause):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        if cause == "failing":
            state._failing = True
        elif cause == "starting":
            state._start_pending = True
        else:
            state.played = "before"
        state._note_stall(100.0)
        state._note_stall(101.0)
        assert state.no_prefetch is None
        await state.stop()

    async def test_backing_off_restarts_a_cushion_back_in_finished_segments(self, setup):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        assert _queued(state) == ["a.ts", "b.ts", "c.ts"]
        state.queue.clear()
        gen = state.gen
        state._note_stall(100.0)
        state._note_stall(101.0)
        # b is finished now; c, d and e are prefetch, and c was taken as prefetch
        state._take("chan", _source_listing(["a", "b"], ["c", "d"]))
        # back from the newest finished one, never a prefetch one
        assert _queued(state) == ["a.ts", "b.ts"]
        assert state.gen > gen and state.queue[0].fresh
        assert all(q.gen == state.gen for q in state.queue)
        assert not state._backing_off and state._restarting
        await state.stop()

    async def test_a_backed_off_channel_takes_only_finished_segments(self, setup):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        state._note_stall(100.0)
        state._note_stall(101.0)
        state._take("chan", _source_listing(["a", "b"], ["c", "d"]))
        # c was taken as prefetch before the back-off: owed, it is fed once finished
        state._take("chan", _source_listing(["a", "b", "c"], ["d", "e"]))
        assert _queued(state) == ["a.ts", "b.ts", "c.ts"]
        assert not state.queue[-1].fresh
        state._take("chan", _source_listing(["a", "b", "c", "d"], ["e", "f"]))
        state._take("chan", _source_listing(["a", "b", "c", "d"], ["e", "f"]))
        assert _queued(state) == ["a.ts", "b.ts", "c.ts", "d.ts"]
        assert not state._owed
        await state.stop()

    async def test_a_backed_off_channel_stalls_without_counting(self, setup):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        state._note_stall(100.0)
        state._note_stall(101.0)
        state._take("chan", _source_listing(["a", "b"], ["c", "d"]))
        state._note_stall(102.0)
        state._note_stall(103.0)
        assert not state._backing_off and not state._stalls
        await state.stop()

    async def test_a_channel_change_turns_prefetch_back_on(self, setup):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        state._note_stall(100.0)
        state._note_stall(101.0)
        state._take("two", _source_listing(["p"], ["q", "r"]))
        assert state.no_prefetch is None and not state._backing_off
        assert _queued(state) == ["p.ts", "q.ts", "r.ts"]
        # the first channel back later starts with prefetch again
        state.played = "two"
        state._take("chan", _source_listing(["x"], ["y", "z"]))
        assert _queued(state) == ["x.ts", "y.ts", "z.ts"]
        await state.stop()

    async def test_a_stall_count_ends_with_its_channel(self, setup):
        _, _, _, ll = setup
        state = _stalled_slot(ll)
        state._note_stall(100.0)
        state._take("two", _source_listing(["p"], ["q", "r"]))
        state.played = "two"
        state._note_stall(101.0)
        assert state.no_prefetch is None
        await state.stop()


async def test_a_channel_with_a_late_prefetch_edge_backs_off_and_plays_on(setup):
    relay, add, client, ll = setup
    source = relay.source = add("chan", count=20)
    # every other segment comes 4s late while it is prefetch, past what the cushion
    # covers: the edge stalls again and again; finished, the same bytes come at once
    source.prefetch_lag = dict.fromkeys(range(1, 20, 2), 4.0)
    await _playlist(client)
    state = ll.slot(1)
    started = time.monotonic()
    while state.no_prefetch is None and time.monotonic() - started < 25:
        await _playlist(client)
        await asyncio.sleep(0.5)
    assert state.no_prefetch == "chan"
    # once settled behind the finished edge, no more filler
    await asyncio.sleep(4)
    before = sum(1 for seg in state.segs if seg.filler)
    newest = max(seg.seq for seg in state.segs)
    for _ in range(14):
        await _playlist(client)
        await asyncio.sleep(0.5)
    assert not [seg for seg in state.segs if seg.filler and seg.seq > newest], before
    assert state.no_prefetch == "chan"


async def test_a_channel_with_a_steady_prefetch_edge_keeps_prefetch(setup):
    relay, add, client, ll = setup
    relay.source = add("chan", count=12)
    await _playlist(client)
    state = ll.slot(1)
    for _ in range(24):
        await _playlist(client)
        await asyncio.sleep(0.5)
    assert state.no_prefetch is None
    assert not any(seg.filler for seg in state.segs)


def _segment(uri: str, pdt: str | None) -> Segment:
    return Segment(
        seq=1, uri=uri, duration=2.0, program_date_time=pdt, discontinuity=False, map_gen=None
    )


class TestLatency:
    @pytest.mark.parametrize(
        "pdt",
        ["2026-11-09T01:00:00.000Z", "2026-11-08T17:00:00.000-0800", "2026-11-09T01:00:00+00:00"],
    )
    def test_source_times_read_any_offset(self, pdt):
        expected = datetime(2026, 11, 9, 1, tzinfo=UTC).timestamp()
        assert _source_time(_segment("a.ts", pdt), set()) == expected

    def test_a_prefetch_segment_is_timed_without_its_lead(self):
        seg = _segment("b.ts", "2026-11-09T01:00:02.000Z")
        base = datetime(2026, 11, 9, 1, 0, 2, tzinfo=UTC).timestamp()
        assert _source_time(seg, {"b.ts"}) == base - PREFETCH_LEAD

    def test_no_time_without_a_program_date_time(self):
        assert _source_time(_segment("a.ts", None), set()) is None

    async def test_parts_take_their_segments_source_time(self, setup):
        _, _, _, ll = setup
        state = lowlat._Slot(ll, 0)
        state._source_times[900] = 1000.0
        state._add([Part(900, b"", 0.5, True, False), Part(900, b"", 0.5, False, False)], 0.0)
        assert state.segs[-1].source == 1000.0
        await state.stop()

    async def test_behind_measures_to_the_end_of_released_media(self, setup):
        _, _, _, ll = setup
        state = lowlat._Slot(ll, 0)
        seg = _Seg(1, False, 0, source=1000.0)
        seg.parts += [_Part(b"", 0.5, True, release=0.0), _Part(b"", 0.5, False, release=5.0)]
        state.segs.append(seg)
        # one part out: the edge is 0.5 s of media past the segment's start
        assert state.behind(1.0, wall=1003.0) == pytest.approx(2.5)
        assert state.behind(6.0, wall=1003.0) == pytest.approx(2.0)
        state.segs.append(_Seg(2, True, 1, filler=True, parts=[_Part(b"", 2.0, True, release=6.0)]))
        assert state.behind(7.0, wall=1004.0) is None
        await state.stop()

    async def test_a_report_each_minute_with_percentiles(self, setup):
        _, _, _, ll = setup
        state = lowlat._Slot(ll, 0)
        state.played = "chan"
        seg = _Seg(1, False, 0, source=1000.0)
        seg.parts.append(_Part(b"", 0.5, True, release=0.0))
        state.segs.append(seg)
        with structlog.testing.capture_logs() as logs:
            for k in range(10):
                state.sample_latency(1.0 + k, wall=1001.0 + k)
            state.sample_latency(1.0 + LATENCY_REPORT, wall=1001.0)
        reports = [e for e in logs if e["event"] == "relay ll latency"]
        # the first sample reports at once; the rest wait a full minute
        assert len(reports) == 2
        last = reports[-1]
        assert last["channel"] == "chan" and last["samples"] == 10
        assert last["edge_p50"] == pytest.approx(5.5) and last["edge_p95"] == pytest.approx(9.5)
        assert last["player_p50"] == pytest.approx(5.5 + PART_HOLD_BACK)
        await state.stop()


async def test_released_media_carries_its_source_time(setup):
    relay, add, client, ll = setup
    relay.source = add("chan")
    await _playlist(client)
    await asyncio.sleep(5)
    state = ll.slot(1)
    timed = [seg for seg in state.segs if seg.parts and not seg.filler]
    assert timed and all(seg.source is not None for seg in timed)
    lag = state.behind(asyncio.get_running_loop().time(), time.time())
    # the source stamps a segment as it begins encoding; the relay releases it a
    # cushion later, after the segment's own time to arrive
    assert lag is not None and 0 < lag < CUSHION + 3


HINTED = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-PROGRAM-DATE-TIME:2026-11-09T01:00:00.000Z
#EXTINF:2.000,
https://cam.lan/seg7.ts
#EXT-X-PRELOAD-HINT:TYPE=PART,URI="https://cam.lan/seg8.ts"
"""


class TestPreloadHint:
    def test_a_hint_names_the_segment_being_written(self):
        text, prefetch = with_prefetch(HINTED)
        assert prefetch == {"https://cam.lan/seg8.ts"}
        assert [line for line in text.splitlines() if not line.startswith("#")] == [
            "https://cam.lan/seg7.ts",
            "https://cam.lan/seg8.ts",
        ]
        assert "PRELOAD-HINT" not in text
        # timed one segment on, plus the prefetch lead, as Twitch's prefetch is
        assert "#EXT-X-PROGRAM-DATE-TIME:2026-11-09T01:00:03.000+00:00" in text

    def test_a_playlist_with_parts_hints_a_part_not_a_segment(self):
        parted = HINTED.replace(
            "#EXT-X-PRELOAD-HINT", '#EXT-X-PART:DURATION=0.5,URI="p.ts"\n#EXT-X-PRELOAD-HINT'
        )
        assert preload_hints(parted, set()) == []

    def test_a_hint_past_the_first_byte_is_left(self):
        ranged = HINTED.replace(
            'URI="https://cam.lan/seg8.ts"', 'URI="https://cam.lan/seg8.ts",BYTERANGE-START=4096'
        )
        assert preload_hints(ranged, set()) == []

    def test_a_hint_for_a_listed_segment_is_no_news(self):
        assert preload_hints(HINTED, {"https://cam.lan/seg8.ts"}) == []

    def test_other_hint_types_are_left(self):
        mapped = HINTED.replace("TYPE=PART", "TYPE=MAP")
        assert preload_hints(mapped, set()) == []


async def _oldest_edge(state: lowlat._Slot, seconds: float) -> float:
    """The most the newest cut media trailed the source over `seconds` of sampling."""
    loop = asyncio.get_running_loop()
    worst = 0.0
    end = loop.time() + seconds
    while loop.time() < end:
        seg = next((g for g in reversed(state.segs) if g.parts and not g.filler), None)
        if seg is not None and seg.source is not None:
            newest = seg.source + seg.duration
            worst = max(worst, time.time() - newest)
        await asyncio.sleep(0.05)
    return worst


async def test_a_hinted_segment_is_cut_as_it_is_written(setup):
    relay, add, client, ll = setup
    plain = relay.source = add("plain", count=16)
    plain.announce = "none"
    await _playlist(client)
    state = ll.slot(1)
    await asyncio.sleep(4)
    # listed only once complete, a segment arrives whole: the newest media ages a
    # full segment between arrivals
    finished_only = await _oldest_edge(state, 5)
    hinted_source = add("hinted", count=16)
    hinted_source.announce = "hint"
    relay.source = hinted_source
    await asyncio.sleep(4)
    # hinted, it streams in as encoded: the newest media is never more than a part old
    hinted = await _oldest_edge(state, 5)
    assert finished_only > 1.5, finished_only
    assert hinted < 1.0, hinted
    assert not any(seg.filler for seg in state.segs)


class TestLateness:
    def test_an_opening_burst_costs_nothing(self):
        late = Lateness()
        # 2.5 s of backlog cut at once, then parts at their own pace
        for k in range(5):
            late.add(0.0, "run", 0.0 - 0.5 * k)
        for k in range(10):
            late.add(0.5 * (k + 1), "run", -2.5)
        assert late.worst() == 0.0

    def test_finished_segments_arrive_a_segment_late(self):
        late = Lateness()
        media = 0.0
        for burst in range(5):
            for _ in range(4):
                late.add(2.0 * burst, "run", 2.0 * burst - media)
                media += 0.5
        # each burst's first part waited the 1.5 s the previous burst's last left
        assert late.worst() == pytest.approx(1.5)

    def test_a_streamed_source_shows_only_its_jitter(self):
        late = Lateness()
        for k, jitter in enumerate([0.0, 0.1, 0.05, 0.2, 0.0, 0.15]):
            late.add(0.5 * k + jitter, "run", jitter)
        assert late.worst() == pytest.approx(0.2)

    def test_runs_do_not_mix(self):
        late = Lateness()
        late.add(0.0, "old", 5.0)
        late.add(1.0, "new", 9.0)
        assert late.worst() == 0.0

    def test_old_lateness_leaves_the_window(self):
        late = Lateness(window=10.0)
        late.add(0.0, "run", 0.0)
        late.add(1.0, "run", 2.0)
        assert late.worst() == 2.0
        late.add(12.0, "run", 2.0)
        assert late.worst() == 0.0


class TestCushionTarget:
    def _slot(self, ll: LowLatency) -> lowlat._Slot:
        state = lowlat._Slot(ll, 0)
        state._take("chan", _source_listing(["a"], ["b", "c"]))
        return state

    async def test_late_arrivals_grow_it_at_once(self, setup):
        _, _, _, ll = setup
        state = self._slot(ll)
        state.lateness.add(0.0, "run", 0.0)
        state.lateness.add(1.0, "run", 2.8)
        with structlog.testing.capture_logs() as logs:
            state._adapt(1.0)
        assert state.cushion_target == CUSHION_MAX
        assert [e["event"] for e in logs] == ["relay ll cushion grown"]
        await state.stop()

    async def test_it_shrinks_a_step_per_settled_minute(self, setup):
        _, _, _, ll = setup
        state = self._slot(ll)
        state.lateness.add(0.0, "run", 0.0)
        state._adapt(0.0)
        assert state.cushion_target == pytest.approx(CUSHION - CUSHION_STEP)
        state._adapt(CUSHION_SETTLE / 2)
        assert state.cushion_target == pytest.approx(CUSHION - CUSHION_STEP)
        state._adapt(CUSHION_SETTLE)
        assert state.cushion_target == pytest.approx(CUSHION - 2 * CUSHION_STEP)
        await state.stop()

    async def test_it_never_shrinks_below_what_arrivals_need(self, setup):
        _, _, _, ll = setup
        state = self._slot(ll)
        state.cushion_target = 1.0
        state.lateness.add(0.0, "run", 0.0)
        state.lateness.add(1.0, "run", 0.65)
        state._adapt(1000.0)
        assert state.cushion_target == pytest.approx(0.65 + CUSHION_MARGIN)
        state.lateness.clear()
        for k in range(100):
            state._adapt(1000.0 + CUSHION_SETTLE * (k + 1))
        assert state.cushion_target == CUSHION_MIN
        await state.stop()

    async def test_a_new_channel_starts_at_the_default(self, setup):
        _, _, _, ll = setup
        state = self._slot(ll)
        state.cushion_target = 0.6
        state.lateness.add(0.0, "run", 0.0)
        state._take("two", _source_listing(["p"], ["q", "r"]))
        assert state.cushion_target == CUSHION and state.lateness.worst() == 0.0
        await state.stop()

    def test_a_run_starts_its_cushion_back(self):
        segs = [Segment(i, f"{i}.ts", 2.0, None, False, None) for i in range(6)]
        assert start_back(segs, set(), cushion=0.5) == 5
        assert start_back(segs, set(), cushion=3.0) == 4


async def test_a_streamed_source_settles_to_a_small_cushion(setup, monkeypatch):
    relay, add, client, ll = setup
    # minutes of settling, compressed
    monkeypatch.setattr(lowlat, "CUSHION_SETTLE", 0.2)
    monkeypatch.setattr(lowlat, "CUSHION_STEP", 0.5)
    source = relay.source = add("chan", count=20)
    source.announce = "hint"
    await _playlist(client)
    state = ll.slot(1)
    loop = asyncio.get_running_loop()
    await asyncio.sleep(4)
    before = state.behind(loop.time(), time.time())
    for _ in range(30):
        await _playlist(client)
        await asyncio.sleep(0.5)
    after = state.behind(loop.time(), time.time())
    # a source streamed as it is written arrives on time: the cushion falls to the floor
    assert state.cushion_target == CUSHION_MIN, state.cushion_target
    assert state.lateness.worst() + CUSHION_MARGIN <= CUSHION_MIN + 0.1
    assert before is not None and after is not None and after < before, (before, after)
    assert not any(seg.filler for seg in state.segs)


async def test_a_fetch_signals_its_end_only_when_complete(setup):
    _, add, _, ll = setup
    source = add("chan", count=4)
    ended: list[str] = []
    fetch = lowlat._Fetch(ll.session(), f"{source.base}/chan/0.ts", lambda: ended.append("end"))
    assert b"".join([c async for c in fetch.chunks()])
    await asyncio.sleep(0)
    assert ended == ["end"]
    failing = lowlat._Fetch(ll.session(), f"{source.base}/nope/0.ts", lambda: ended.append("bad"))
    # the origin has no such channel: an error status, never a complete segment
    with pytest.raises(aiohttp.ClientResponseError):
        _ = [c async for c in failing.chunks()]
    assert ended == ["end"]


async def test_a_hinted_source_is_read_again_as_each_segment_ends(setup):
    relay, add, client, ll = setup
    source = relay.source = add("chan", count=16)
    source.announce = "hint"
    await _playlist(client)
    state = ll.slot(1)
    await asyncio.sleep(9)
    # each hinted segment's end brought a fresh read past the 1 s cache, so the
    # next one started on time: lateness stays near the source's own jitter
    assert relay.fresh_reads >= 3
    assert state.lateness.worst() < 0.4, state.lateness.worst()
    assert not any(seg.filler for seg in state.segs)
