"""aiohttp server that republishes each slot as a live HLS media playlist.

Fetches the slot's current Twitch media playlist on demand (short cache),
folds it into a rolling window via the pure core in playlist.py, and serves
our own continuously-live playlist. Segments stay absolute Twitch CDN URLs.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

import httpx
import structlog
from aiohttp import web

from quadlink.relay.playlist import SlotState, first_map_uri, ingest, render
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
        # slot -> (identity, time) of the last fetch that added new segments.
        # set only while a client is polling the slot; an unwatched slot has none
        self._arrivals: dict[int, tuple[str, float]] = {}
        self._cache: dict[str, tuple[float, str]] = {}
        self._client: httpx.AsyncClient | None = None
        # set when Twitch 404s a slot's playlist: the broadcast ended, so the
        # daemon re-picks now instead of letting the served playlist stall
        self.source_gone = asyncio.Event()

    def clock(self) -> float:
        """Current time on the clock that video_arrived_since compares against."""
        return self._time()

    def video_arrived_since(self, slot: int, identity: str, since: float) -> bool:
        """Whether new segments from this channel reached the slot after `since`.

        New segments prove the channel is live right now, whatever a lookup said.
        """
        arrival = self._arrivals.get(slot)
        return arrival is not None and arrival[0] == identity and arrival[1] > since

    async def _default_fetch(self, url: str) -> str:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.text

    async def _cached_fetch(self, url: str) -> str:
        now = self._time()
        # drop expired entries so the cache stays bounded on a long-running daemon
        self._cache = {
            u: entry for u, entry in self._cache.items() if now - entry[0] < self.cache_ttl
        }
        hit = self._cache.get(url)
        if hit is not None:
            return hit[1]
        fetch = self._fetch or self._default_fetch
        text = await fetch(url)
        self._cache[url] = (now, text)
        return text

    async def _fetch_source(self, slot: int, url: str) -> str:
        """Fetch a slot's playlist, moving to its newest URL once the kept one fails."""
        try:
            return await self._cached_fetch(url)
        except httpx.HTTPStatusError as e:
            # 403: the kept URL's token expired; 404: its session ended
            if e.response.status_code not in (403, 404) or not self.store.refresh(slot):
                raise
            entry = self.store.get(slot)
            assert entry is not None
            logger.info("relay source refreshed", slot=slot, status=e.response.status_code)
            return await self._cached_fetch(entry.url)

    def _note_failure(self, error: Exception) -> None:
        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 404:
            self.source_gone.set()

    async def handle(self, request: web.Request) -> web.Response:
        """Route handler for GET /streams/{slot}."""
        raw = request.match_info.get("slot", "")
        try:
            slot = int(raw)
        except ValueError:
            return web.Response(status=404, text="bad slot")
        if slot not in (1, 2, 3, 4):
            return web.Response(status=404, text="bad slot")

        entry = self.store.get(slot)
        if entry is None:
            return web.Response(status=503, text="slot empty")

        state = self._states.setdefault(slot, SlotState(window=self.window))
        try:
            text = await self._fetch_source(slot, entry.url)
        except Exception as e:
            logger.warning("relay source fetch failed", slot=slot, url=entry.url, error=str(e))
            self._note_failure(e)
            if state.segments:
                return self._playlist_response(render(state, slot))
            return web.Response(status=503, text="source unavailable")

        before = state.next_seq
        ingest(state, text, entry.identity)
        if state.next_seq > before:
            self._arrivals[slot] = (entry.identity, self._time())
        if not state.segments:
            return web.Response(status=503, text="no segments")
        return self._playlist_response(render(state, slot))

    async def handle_init(self, request: web.Request) -> web.Response:
        """Route handler for GET /streams/{slot}/init/{gen}.

        The played fMP4 EXT-X-MAP points here (a stable per-run URL). We resolve
        the slot's current source and redirect to a fresh Twitch init segment,
        so the init token is never stale even though the URL never changes.
        """
        raw = request.match_info.get("slot", "")
        try:
            slot = int(raw)
        except ValueError:
            return web.Response(status=404, text="bad slot")
        if slot not in (1, 2, 3, 4):
            return web.Response(status=404, text="bad slot")

        entry = self.store.get(slot)
        if entry is None:
            return web.Response(status=503, text="slot empty")
        try:
            text = await self._fetch_source(slot, entry.url)
        except Exception as e:
            logger.warning("relay init fetch failed", slot=slot, url=entry.url, error=str(e))
            self._note_failure(e)
            return web.Response(status=503, text="source unavailable")

        uri = first_map_uri(text)
        if not uri:
            return web.Response(status=404, text="no init segment")
        return web.Response(status=302, headers={"Location": uri, **NO_CACHE_HEADERS})

    def _playlist_response(self, body: str) -> web.Response:
        return web.Response(
            text=body,
            content_type="application/vnd.apple.mpegurl",
            headers=NO_CACHE_HEADERS,
        )

    def register_routes(self, router: web.UrlDispatcher) -> None:
        """Register the relay routes on an existing aiohttp router."""
        router.add_get("/streams/{slot}", self.handle)
        router.add_get("/streams/{slot}/init/{gen}", self.handle_init)

    async def aclose(self) -> None:
        """Close the internal httpx client, if one was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
