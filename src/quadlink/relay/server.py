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
