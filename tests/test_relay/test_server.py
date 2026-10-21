"""Tests for the SlotRelay aiohttp server."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from quadlink.relay.segments import SegmentProxy
from quadlink.relay.server import FRESH_WAIT, SlotRelay
from quadlink.relay.store import SlotStore
from quadlink.webui import WebUI


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
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

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
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])
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
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])
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
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        raise RuntimeError("boom")

    relay = SlotRelay(store, fetch=fetch)
    resp = await _call(relay, "1")
    assert resp.status == 503


@pytest.mark.asyncio
async def test_cache_evicts_expired_entries():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return _src(100, ["a.ts"])

    clock = {"t": 0.0}
    relay = SlotRelay(store, fetch=fetch, cache_ttl=1.0, time_fn=lambda: clock["t"])
    await _call(relay, "1")
    assert "url-A" in relay._cache

    store.update(["url-B", "", "", ""], ["chan-B", "", "", ""])
    clock["t"] = 2.0
    await _call(relay, "1")
    assert "url-A" not in relay._cache


@pytest.mark.asyncio
async def test_aclose_without_client_is_noop():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    assert relay._client is None
    await relay.aclose()
    assert relay._client is None


@pytest.mark.asyncio
async def test_aclose_closes_created_client():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    mock_client = AsyncMock()
    relay._client = mock_client
    await relay.aclose()
    mock_client.aclose.assert_awaited_once()
    assert relay._client is None


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


def test_webui_registers_relay_routes():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    webui = WebUI(MagicMock(), slot_relay=relay)
    paths = {r.resource.canonical for r in webui.app.router.routes()}
    assert "/streams/{slot}" in paths


def test_webui_without_relay_has_no_stream_routes():
    webui = WebUI(MagicMock())
    paths = {r.resource.canonical for r in webui.app.router.routes()}
    assert "/streams/{slot}" not in paths


async def _call_init(relay: SlotRelay, slot: str) -> web.Response:
    request = make_mocked_request(
        "GET", f"/streams/{slot}/init/1", match_info={"slot": slot, "gen": "1"}
    )
    return await relay.handle_init(request)


@pytest.mark.asyncio
async def test_init_redirects_to_current_fresh_twitch_init():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
            '#EXT-X-MAP:URI="https://cdn.example/init.mp4?dna=FRESH"\n'
            "#EXTINF:2.000,live\nhttps://cdn.example/a.mp4?dna=aaa\n"
        )

    relay = SlotRelay(store, fetch=fetch)
    resp = await _call_init(relay, "1")
    assert resp.status == 302
    assert resp.headers["Location"] == "https://cdn.example/init.mp4?dna=FRESH"


@pytest.mark.asyncio
async def test_init_404_for_ts_slot_without_map():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return _src(0, ["a.ts", "b.ts"])  # legacy TS: no EXT-X-MAP

    relay = SlotRelay(store, fetch=fetch)
    resp = await _call_init(relay, "1")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_init_503_for_empty_slot():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    resp = await _call_init(relay, "1")
    assert resp.status == 503


def test_register_routes_adds_init_path():
    relay = SlotRelay(SlotStore(), fetch=AsyncNever())
    app = web.Application()
    relay.register_routes(app.router)
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/streams/{slot}/init/{gen}" in paths


@pytest.mark.asyncio
async def test_video_arrived_since_tracks_new_segments_per_channel():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])
    now = [100.0]
    source = [_src(100, ["a.ts", "b.ts"])]

    async def fetch(url):
        return source[0]

    relay = SlotRelay(store, fetch=fetch, cache_ttl=0, time_fn=lambda: now[0])
    assert not relay.video_arrived_since(1, "chan-A", 0.0)  # nothing fetched yet

    await _call(relay, "1")
    assert relay.video_arrived_since(1, "chan-A", 99.0)
    assert not relay.video_arrived_since(1, "chan-A", 100.0)  # strictly after `since`
    assert not relay.video_arrived_since(1, "chan-B", 99.0)  # other channel's video

    # the same segments again add nothing, so no fresh arrival is recorded
    now[0] = 130.0
    await _call(relay, "1")
    assert not relay.video_arrived_since(1, "chan-A", 120.0)

    source[0] = _src(101, ["b.ts", "c.ts"])
    await _call(relay, "1")
    assert relay.video_arrived_since(1, "chan-A", 120.0)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://twitch.example/playlist.m3u8")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404])
async def test_dead_kept_url_moves_to_newest_url(status):
    store = SlotStore()
    store.update(["url-old", "", "", ""], ["chan-A", "", "", ""])
    store.update(["url-new", "", "", ""], ["chan-A", "", "", ""])
    fetched = []

    async def fetch(url):
        fetched.append(url)
        if url == "url-old":
            raise _http_error(status)
        return _src(100, ["a.ts"])

    relay = SlotRelay(store, fetch=fetch)
    resp = await _call(relay, "1")
    assert resp.status == 200
    assert fetched == ["url-old", "url-new"]
    assert store.get(1).url == "url-new"


@pytest.mark.asyncio
async def test_dead_newest_url_without_window_returns_503():
    store = SlotStore()
    store.update(["url-old", "", "", ""], ["chan-A", "", "", ""])
    store.update(["url-new", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        raise _http_error(404)

    relay = SlotRelay(store, fetch=fetch)
    resp = await _call(relay, "1")
    assert resp.status == 503


class FakeProxy:
    def __init__(self, unready: set[int] | None = None) -> None:
        self.registered: list[tuple[int, int]] = []
        self.unready = unready or set()
        self.waits: list[tuple[int, int, float]] = []
        self.wait_gate: asyncio.Event | None = None
        self.missing = False
        self.error: Exception | None = None

    def register(self, slot, seg):
        self.registered.append((slot, seg.seq))

    def ready(self, slot, seq):
        return seq not in self.unready

    async def wait_ready(self, slot, seq, timeout):
        self.waits.append((slot, seq, timeout))
        if self.wait_gate is not None:
            await self.wait_gate.wait()
        return self.ready(slot, seq)

    async def get(self, slot, seq):
        if self.error is not None:
            raise self.error
        return None if self.missing else b"seg-%d" % seq

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
async def test_proxy_enabled_mid_stream_keeps_cdn_uri_for_segments_already_in_window():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])
    now = [0.0]
    calls: list[str] = []

    async def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            return _src(100, ["https://cdn/a.ts"])
        return _src(100, ["https://cdn/a.ts", "https://cdn/b.ts"])

    proxy = FakeProxy()
    relay = SlotRelay(store, fetch=fetch, cache_ttl=0, time_fn=lambda: now[0], segments=proxy)
    relay.proxy_segments = False
    resp = await _call(relay, "1")
    assert "https://cdn/a.ts" in resp.text
    assert proxy.registered == []

    relay.proxy_segments = True
    now[0] += 1
    resp = await _call(relay, "1")
    # already in the window before proxying turned on: never registered, keeps its CDN URI
    assert "https://cdn/a.ts" in resp.text
    # registered once proxying began: served through the relay
    assert "/streams/1/seg/1.ts" in resp.text
    assert proxy.registered == [(1, 1)]


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


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [48000, 44100])
async def test_fresh_window_lists_up_to_two_before_newest(rate, ffmpeg, ts_run, tmp_path):
    segs = ts_run(rate, 6)

    async def fetch_segment(uri):
        return segs[int(uri)]

    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return _src(100, [str(i) for i in range(6)])

    relay = SlotRelay(store, fetch=fetch, segments=SegmentProxy(fetch_segment, ffmpeg, tmp_path))
    try:
        resp = await _call(relay, "1")
        for seq in range(4):
            assert f"/streams/1/seg/{seq}.ts" in resp.text
    finally:
        await relay.aclose()


@pytest.mark.asyncio
async def test_steady_state_polls_do_not_wait():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])
    now = [0.0]
    uris = ["a.ts", "b.ts"]

    async def fetch(url):
        return _src(100, uris)

    proxy = FakeProxy()
    relay = SlotRelay(store, fetch=fetch, cache_ttl=0, time_fn=lambda: now[0], segments=proxy)
    await _call(relay, "1")
    assert proxy.waits == [(1, 0, FRESH_WAIT)]
    for _ in range(3):
        now[0] += 2
        uris.append(f"{len(uris)}.ts")
        await _call(relay, "1")
    assert len(proxy.waits) == 1


@pytest.mark.asyncio
async def test_concurrent_fresh_polls_share_one_wait():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return _src(100, ["a.ts", "b.ts", "c.ts"])

    proxy = FakeProxy()
    proxy.wait_gate = asyncio.Event()
    relay = SlotRelay(store, fetch=fetch, segments=proxy)
    calls = [asyncio.create_task(_call(relay, "1")) for _ in range(3)]
    await asyncio.sleep(0.01)
    assert not any(c.done() for c in calls)
    proxy.wait_gate.set()
    resps = await asyncio.gather(*calls)
    assert all(r.status == 200 for r in resps)
    assert proxy.waits == [(1, 0, FRESH_WAIT)]


def _seg_request(name: str) -> web.Request:
    return make_mocked_request(
        "GET", f"/streams/2/seg/{name}", match_info={"slot": "2", "name": name}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["+7.ts", " 7.ts", "7_0.ts", "-1.ts", "7.mp4"])
async def test_segment_route_rejects_loose_seq(name):
    relay = SlotRelay(SlotStore(), segments=FakeProxy())
    assert (await relay.handle_segment(_seg_request(name))).status == 404


@pytest.mark.asyncio
async def test_segment_route_is_not_cached():
    relay = SlotRelay(SlotStore(), segments=FakeProxy())
    resp = await relay.handle_segment(_seg_request("7.ts"))
    assert resp.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_segment_route_502_when_get_raises():
    proxy = FakeProxy()
    proxy.error = RuntimeError("boom")
    relay = SlotRelay(SlotStore(), segments=proxy)
    assert (await relay.handle_segment(_seg_request("7.ts"))).status == 502


@pytest.mark.asyncio
async def test_segment_route_404_when_segment_unknown():
    proxy = FakeProxy()
    proxy.missing = True
    relay = SlotRelay(SlotStore(), segments=proxy)
    assert (await relay.handle_segment(_seg_request("7.ts"))).status == 404


@pytest.mark.asyncio
async def test_fmp4_segments_use_m4s_uri_and_type():
    store = SlotStore()
    store.update(["url-A", "", "", ""], ["chan-A", "", "", ""])

    async def fetch(url):
        return (
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
            '#EXT-X-MAP:URI="https://cdn.example/init.mp4"\n'
            "#EXTINF:2.000,live\nhttps://cdn.example/a.mp4\n"
        )

    relay = SlotRelay(store, fetch=fetch, segments=FakeProxy())
    resp = await _call(relay, "1")
    assert "/streams/1/seg/0.m4s" in resp.text
    seg = await relay.handle_segment(
        make_mocked_request(
            "GET", "/streams/1/seg/0.m4s", match_info={"slot": "1", "name": "0.m4s"}
        )
    )
    assert seg.status == 200
    assert seg.content_type == "video/iso.segment"
