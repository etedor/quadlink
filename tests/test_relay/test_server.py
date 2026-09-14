"""Tests for the SlotRelay aiohttp server."""

from unittest.mock import AsyncMock

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


@pytest.mark.asyncio
async def test_cache_evicts_expired_entries():
    store = SlotStore()
    store.update(["url-A", "", "", ""])

    async def fetch(url):
        return _src(100, ["a.ts"])

    clock = {"t": 0.0}
    relay = SlotRelay(store, fetch=fetch, cache_ttl=1.0, time_fn=lambda: clock["t"])
    await _call(relay, "1")
    assert "url-A" in relay._cache

    store.update(["url-B", "", "", ""])
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
