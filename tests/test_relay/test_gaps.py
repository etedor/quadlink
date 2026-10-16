"""Tests for ad removal, filler and outgoing-channel hold in the relay."""

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from quadlink.relay.playlist import (
    FILLER_FMP4,
    FILLER_FMP4_INIT,
    FILLER_TS,
    SlotState,
    ad_segment_uris,
    add_filler,
    has_live_segment,
    ingest,
    render,
)
from quadlink.relay.server import STALL_AFTER, SlotRelay
from quadlink.relay.store import SlotEntry, SlotStore

AD_SRC = """#EXTM3U
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:50
#EXT-X-DATERANGE:ID="stitched-ad-1-30",CLASS="twitch-stitched-ad",\
START-DATE="2026-10-13T03:43:10.000Z",DURATION=4.000
#EXT-X-PROGRAM-DATE-TIME:2026-10-13T03:43:10.000Z
#EXTINF:2.000,Amazon|123
ad1.ts
#EXT-X-PROGRAM-DATE-TIME:2026-10-13T03:43:12.000Z
#EXTINF:2.000,
ad2.ts
#EXT-X-PROGRAM-DATE-TIME:2026-10-13T03:43:14.000Z
#EXTINF:2.000,live
live1.ts
"""

ONLY_ADS = AD_SRC.replace("#EXTINF:2.000,live\nlive1.ts\n", "")


def _src(media_seq: int, uris: list[str]) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{media_seq}"]
    for uri in uris:
        lines += ["#EXTINF:2.000,live", uri]
    return "\n".join(lines) + "\n"


def _fmp4_src(media_seq: int, uris: list[str]) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-TARGETDURATION:2",
        f"#EXT-X-MEDIA-SEQUENCE:{media_seq}",
        '#EXT-X-MAP:URI="https://cdn.example/init.mp4"',
    ]
    for uri in uris:
        lines += ["#EXTINF:2.000,live", uri]
    return "\n".join(lines) + "\n"


def test_ad_segments_found_by_title_and_daterange():
    assert ad_segment_uris(AD_SRC) == {"ad1.ts", "ad2.ts"}


def test_no_ads_in_plain_playlist():
    assert ad_segment_uris(_src(1, ["a.ts"])) == set()


def test_has_live_segment():
    assert has_live_segment(AD_SRC)
    assert not has_live_segment(ONLY_ADS)


def test_ingest_skips_ads_and_splices_after_them():
    state = SlotState()
    ingest(state, _src(10, ["a.ts"]), "chan-A")
    ingest(state, AD_SRC, "chan-B")
    assert [s.uri for s in state.segments] == ["a.ts", "live1.ts"]
    assert state.segments[1].discontinuity
    # ads are below the watermark, so a refetch adds nothing
    ingest(state, AD_SRC, "chan-B")
    assert len(state.segments) == 2


def test_filler_follows_ts_window_with_discontinuity():
    state = SlotState()
    ingest(state, _src(10, ["a.ts"]), "chan-A")
    add_filler(state)
    add_filler(state)
    out = render(state, 1)
    assert out.count(FILLER_TS) == 2
    assert out.count("#EXT-X-DISCONTINUITY\n") == 2
    assert "#EXT-X-MAP" not in out
    ingest(state, _src(11, ["b.ts"]), "chan-A")
    assert state.segments[-1].discontinuity


def test_filler_follows_fmp4_window_with_its_own_map():
    state = SlotState()
    ingest(state, _fmp4_src(0, ["a.mp4"]), "chan-A")
    add_filler(state)
    ingest(state, _fmp4_src(1, ["b.mp4"]), "chan-A")
    lines = render(state, 2).splitlines()
    assert lines.index('#EXT-X-MAP:URI="/streams/2/init/1"') < lines.index("a.mp4")
    assert lines.index(f'#EXT-X-MAP:URI="{FILLER_FMP4_INIT}"') < lines.index(FILLER_FMP4)
    # the source's init is declared again after the filler
    assert lines.index('#EXT-X-MAP:URI="/streams/2/init/2"') < lines.index("b.mp4")


def test_filler_on_empty_window_is_ts():
    state = SlotState()
    add_filler(state)
    assert state.segments[0].uri == FILLER_TS


def test_store_remembers_outgoing_channel_until_released():
    store = SlotStore()
    store.update(["a1", "", "", ""], ["A", "", "", ""])
    assert store.outgoing(1) is None
    store.update(["b1", "", "", ""], ["B", "", "", ""])
    assert store.outgoing(1) == SlotEntry("A", "a1")
    store.release_outgoing(1)
    assert store.outgoing(1) is None


def test_store_swap_back_drops_outgoing():
    store = SlotStore()
    store.update(["a1", "", "", ""], ["A", "", "", ""])
    store.update(["b1", "", "", ""], ["B", "", "", ""])
    store.update(["a2", "", "", ""], ["A", "", "", ""])
    assert store.outgoing(1) == SlotEntry("B", "b1")
    store.update(["b2", "", "", ""], ["B", "", "", ""])
    store.update(["", "", "", ""], ["", "", "", ""])
    assert store.outgoing(1) is None


async def _call(relay: SlotRelay, slot: str) -> web.Response:
    request = make_mocked_request("GET", f"/streams/{slot}", match_info={"slot": slot})
    return await relay.handle(request)


class Source:
    """Fake Twitch: playlist text per URL, an exception to raise, or a 404."""

    def __init__(self, playlists: dict[str, str | Exception]) -> None:
        self.playlists = playlists

    async def __call__(self, url: str) -> str:
        value = self.playlists[url]
        if isinstance(value, Exception):
            raise value
        return value


def _relay(store: SlotStore, source: Source, clock: dict[str, float]) -> SlotRelay:
    return SlotRelay(store, fetch=source, cache_ttl=0.0, time_fn=lambda: clock["t"])


@pytest.mark.asyncio
async def test_stalled_slot_gets_filler_paced_after_threshold():
    store = SlotStore()
    store.update(["a", "", "", ""], ["A", "", "", ""])
    source = Source({"a": _src(10, ["a10.ts"])})
    clock = {"t": 0.0}
    relay = _relay(store, source, clock)

    await _call(relay, "1")
    clock["t"] = STALL_AFTER - 0.5
    assert FILLER_TS not in (await _call(relay, "1")).text

    clock["t"] = STALL_AFTER
    assert (await _call(relay, "1")).text.count(FILLER_TS) == 1
    clock["t"] = STALL_AFTER + 1.0
    assert (await _call(relay, "1")).text.count(FILLER_TS) == 1
    clock["t"] = STALL_AFTER + 2.0
    assert (await _call(relay, "1")).text.count(FILLER_TS) == 2


@pytest.mark.asyncio
async def test_failed_source_still_gets_filler():
    store = SlotStore()
    store.update(["a", "", "", ""], ["A", "", "", ""])
    source = Source({"a": _src(10, ["a10.ts"])})
    clock = {"t": 0.0}
    relay = _relay(store, source, clock)
    await _call(relay, "1")

    source.playlists["a"] = RuntimeError("boom")
    clock["t"] = STALL_AFTER
    resp = await _call(relay, "1")
    assert resp.status == 200
    assert FILLER_TS in resp.text


@pytest.mark.asyncio
async def test_new_channel_preroll_plays_outgoing_channel():
    store = SlotStore()
    store.update(["a", "", "", ""], ["A", "", "", ""])
    source = Source({"a": _src(10, ["a10.ts"]), "b": ONLY_ADS})
    clock = {"t": 0.0}
    relay = _relay(store, source, clock)
    await _call(relay, "1")

    store.update(["b", "", "", ""], ["B", "", "", ""])
    source.playlists["a"] = _src(11, ["a11.ts"])
    out = (await _call(relay, "1")).text
    assert "a11.ts" in out
    assert "ad1.ts" not in out

    # once the new channel is live, it replaces the outgoing one
    source.playlists["b"] = AD_SRC
    source.playlists["a"] = _src(12, ["a12.ts"])
    out = (await _call(relay, "1")).text
    assert "live1.ts" in out
    assert "a12.ts" not in out
    assert store.outgoing(1) is None


@pytest.mark.asyncio
async def test_init_follows_the_url_being_played():
    store = SlotStore()
    store.update(["a", "", "", ""], ["A", "", "", ""])
    source = Source({"a": _fmp4_src(10, ["a10.mp4"]), "b": ONLY_ADS})
    clock = {"t": 0.0}
    relay = _relay(store, source, clock)
    await _call(relay, "1")
    store.update(["b", "", "", ""], ["B", "", "", ""])
    await _call(relay, "1")

    request = make_mocked_request("GET", "/streams/1/init/1", match_info={"slot": "1", "gen": "1"})
    resp = await relay.handle_init(request)
    assert resp.status == 302
    assert resp.headers["Location"] == "https://cdn.example/init.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,ctype",
    [("2s.ts", "video/mp2t"), ("2s.m4s", "video/iso.segment"), ("init.mp4", "video/mp4")],
)
async def test_filler_files_are_served(name, ctype):
    relay = SlotRelay(SlotStore())
    request = make_mocked_request("GET", f"/streams/filler/{name}", match_info={"name": name})
    resp = await relay.handle_filler(request)
    assert resp.status == 200
    assert resp.content_type == ctype
    assert len(resp.body) > 1000


@pytest.mark.asyncio
async def test_unknown_filler_is_404():
    relay = SlotRelay(SlotStore())
    request = make_mocked_request("GET", "/streams/filler/x", match_info={"name": "x"})
    assert (await relay.handle_filler(request)).status == 404


def test_budget_keeps_the_newest_segments_and_splices_over_the_rest():
    state = SlotState()
    ingest(state, _src(10, ["a.ts"]), "chan-A")
    appended = ingest(state, _src(11, ["b.ts", "c.ts", "d.ts", "e.ts"]), "chan-A", budget=4.0)
    assert appended == 4.0
    assert [s.uri for s in state.segments] == ["a.ts", "d.ts", "e.ts"]
    assert [s.discontinuity for s in state.segments] == [False, True, False]
    # the skipped backlog is behind the watermark, so it never comes back
    ingest(state, _src(11, ["b.ts", "c.ts", "d.ts", "e.ts"]), "chan-A", budget=10.0)
    assert len(state.segments) == 3


def test_budget_within_reach_appends_without_a_splice():
    state = SlotState()
    ingest(state, _src(10, ["a.ts"]), "chan-A")
    ingest(state, _src(10, ["a.ts", "b.ts"]), "chan-A", budget=4.0)
    assert [s.discontinuity for s in state.segments] == [False, False]
