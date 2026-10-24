"""Tests for the LL-HLS slots: prefetch, progressive parts, filler and splices."""

import asyncio
import re
import time
import types
from collections.abc import AsyncIterator, Callable
from datetime import datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from quadlink.relay import lowlat
from quadlink.relay.lowlat import PACE, PART_TARGET, LowLatency, _Part, next_release, with_prefetch
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

    def test_a_playlist_without_prefetch_is_unchanged(self):
        plain = "\n".join(x for x in SOURCE.splitlines() if "PREFETCH" not in x) + "\n"
        assert with_prefetch(plain) == (plain, set())


class TestNextRelease:
    def test_a_part_follows_the_one_before_at_the_pace(self):
        prev = _Part(b"", 0.5, False, release=100.0)
        assert next_release(99.0, prev) == pytest.approx(100.0 + 0.5 * PACE)

    def test_a_late_part_goes_out_now(self):
        assert next_release(200.0, _Part(b"", 0.5, False, release=100.0)) == 200.0
        assert next_release(5.0, None) == 5.0

    def test_a_timestamp_jump_holds_the_next_part_back_one_part_at_most(self):
        # a PTS wrap reads as a part lasting about a day
        prev = _Part(b"", 95_000.0, False, release=100.0)
        assert next_release(100.0, prev) == pytest.approx(100.0 + PART_TARGET * PACE)


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
        self.stopped_at: float | None = None
        self.base = ""

    def encoding(self) -> int:
        """Index of the segment being encoded now."""
        now = self.stopped_at or time.monotonic()
        return min(int((now - self.start) / 2) + 1, len(self.segs))

    def playlist(self) -> str:
        live = self.encoding()
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", "#EXT-X-MEDIA-SEQUENCE:0"]
        for i in range(live):
            lines += ["#EXTINF:2.000,live", f"{self.base}/{self.identity}/{i}.ts"]
        for i in range(live, min(live + 2, len(self.segs))):
            lines.append(f"#EXT-X-TWITCH-PREFETCH:{self.base}/{self.identity}/{i}.ts")
        return "\n".join(lines) + "\n"

    async def serve(self, request: web.Request) -> web.StreamResponse:
        i = int(request.match_info["i"])
        data = self.segs[i]
        resp = web.StreamResponse()
        await resp.prepare(request)
        chunks = 20
        size = -(-len(data) // chunks)
        for k in range(chunks):
            # segment i encodes from 2(i-1)s to 2i s after start; i=0 is old
            due = self.start + 2 * (i - 1) + 2 * (k + 1) / chunks
            while (self.stopped_at is not None and i >= self.encoding()) or time.monotonic() < due:
                await asyncio.sleep(0.02)
            await resp.write(data[k * size : (k + 1) * size])
        await resp.write_eof()
        return resp


class FakeRelay:
    def __init__(self) -> None:
        self.source: Source | None = None

    async def current_source(self, slot: int) -> tuple[str, str] | None:
        if self.source is None:
            return None
        return self.source.identity, self.source.playlist()


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


async def test_parts_come_out_while_the_segment_still_streams(setup):
    relay, add, client, _ = setup
    source = relay.source = add("chan")
    seen: dict[str, float] = {}
    while time.monotonic() - source.start < 4:
        resp = await client.get("/streams/ll/1")
        if resp.status == 200:
            text = await resp.text()
            for _, uri, _ in _parts(text):
                seen.setdefault(uri, time.monotonic() - source.start)
            if "#EXTINF" in text:
                seen.setdefault("segment", time.monotonic() - source.start)
        await asyncio.sleep(0.05)
    seq = min(int(re.search(r"/part/(\d+)\.", uri)[1]) for uri in seen if "/part/" in uri)
    first = [seen[f"/streams/ll/1/part/{seq}.{k}.ts"] for k in range(4)]
    # the live segment streams over 2s: its parts follow it, half a second apart
    assert first[0] < 1.0
    # the source sends 0.1s chunks and this polls every 0.05s
    assert all(abs(b - a - 0.5) <= 0.15 for a, b in zip(first, first[1:], strict=False)), first
    assert seen["segment"] > first[2]


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


async def test_filler_goes_out_when_the_source_stalls(setup, monkeypatch):
    relay, add, client, ll = setup
    source = relay.source = add("chan")
    await _playlist(client)
    await asyncio.sleep(2.5)
    source.stopped_at = time.monotonic()
    await asyncio.sleep(lowlat.FILL_AFTER + 2.5)
    text = await _playlist(client)
    assert "#EXT-X-DISCONTINUITY" in text
    state = ll.slot(1)
    assert any(seg.filler for seg in state.segs)


async def test_a_channel_change_splices_at_once(setup):
    relay, add, client, ll = setup
    relay.source = add("one")
    await _playlist(client)
    await asyncio.sleep(3)
    before = ll.slot(1).segs[-1].seq
    relay.source = add("two")
    await asyncio.sleep(2)
    text = await _playlist(client)
    state = ll.slot(1)
    new = [s for s in state.segs if s.seq > before and s.disc]
    assert new, text
    assert new[0].parts[0].independent
    assert "#EXT-X-DISCONTINUITY" in text


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
