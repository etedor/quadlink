"""aiohttp server that republishes each slot as a live HLS media playlist.

Fetches the slot's current Twitch media playlist on demand (short cache),
folds it into a rolling window via the pure core in playlist.py, and serves
our own continuously-live playlist. Segments stay absolute Twitch CDN URLs.
"""

import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import structlog
from aiohttp import web

from quadlink.relay.playlist import (
    FILLER_DURATION,
    SlotState,
    add_filler,
    first_map_uri,
    has_live_segment,
    ingest,
    render,
)
from quadlink.relay.store import SlotEntry, SlotStore

logger = structlog.get_logger()

NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

FetchFn = Callable[[str], Awaitable[str]]

# seconds without new video before filler starts. A client plays about 3
# segments (6s) behind the live edge, so filler must land before that buffer
# runs dry: on a stall QuadStream rebuilds the player, which replays the stale
# window every 6s for as long as the playlist stays unchanged
STALL_AFTER = 3.0

_FILLER_DIR = Path(__file__).parent / "filler"
_FILLER_TYPES = {"2s.ts": "video/mp2t", "2s.m4s": "video/iso.segment", "init.mp4": "video/mp4"}


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
        # slot -> URL last folded into its window, which the fMP4 init must match
        self._source_urls: dict[int, str] = {}
        self._filler = {name: (_FILLER_DIR / name).read_bytes() for name in _FILLER_TYPES}
        self._client: httpx.AsyncClient | None = None

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
            if not state.segments:
                return web.Response(status=503, text="source unavailable")
        else:
            await self._ingest_slot(slot, state, entry, text)

        self._fill_gap(state)
        if not state.segments:
            return web.Response(status=503, text="no segments")
        return self._playlist_response(render(state, slot))

    async def _ingest_slot(self, slot: int, state: SlotState, entry: SlotEntry, text: str) -> None:
        """Fold the slot's channel into its window, or the outgoing one during a preroll."""
        if has_live_segment(text):
            self.store.release_outgoing(slot)
        else:
            outgoing = self.store.outgoing(slot)
            if outgoing is not None:
                try:
                    out_text = await self._cached_fetch(outgoing.url)
                except Exception as e:
                    logger.debug("relay outgoing fetch failed", slot=slot, error=str(e))
                else:
                    self._fold(slot, state, outgoing, out_text)
                    return
        self._fold(slot, state, entry, text)

    def _fold(self, slot: int, state: SlotState, entry: SlotEntry, text: str) -> None:
        before = state.next_seq
        ingest(state, text, entry.identity)
        self._source_urls[slot] = entry.url
        if state.next_seq > before:
            now = self._time()
            self._arrivals[slot] = (entry.identity, now)
            state.served_until = now

    def _fill_gap(self, state: SlotState) -> None:
        """Add one filler segment once a slot's video has stalled.

        Called on each client poll, so filler follows at most one segment per
        poll, paced to FILLER_DURATION after the first.
        """
        if not state.segments:
            return
        now = self._time()
        if state.served_until is None:
            state.served_until = now
            return
        if now - state.served_until < STALL_AFTER:
            return
        add_filler(state)
        # after a long unpolled spell, pace from now instead of bursting filler
        state.served_until = max(
            state.served_until + FILLER_DURATION, now - STALL_AFTER + FILLER_DURATION
        )

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
        url = self._source_urls.get(slot, entry.url)
        try:
            if url == entry.url:
                text = await self._fetch_source(slot, url)
            else:
                text = await self._cached_fetch(url)
        except Exception as e:
            logger.warning("relay init fetch failed", slot=slot, url=url, error=str(e))
            return web.Response(status=503, text="source unavailable")

        uri = first_map_uri(text)
        if not uri:
            return web.Response(status=404, text="no init segment")
        return web.Response(status=302, headers={"Location": uri, **NO_CACHE_HEADERS})

    async def handle_filler(self, request: web.Request) -> web.Response:
        """Route handler for GET /streams/filler/{name}: black, silent 2s clips."""
        name = request.match_info.get("name", "")
        body = self._filler.get(name)
        if body is None:
            return web.Response(status=404, text="no such filler")
        return web.Response(
            body=body,
            content_type=_FILLER_TYPES[name],
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def _playlist_response(self, body: str) -> web.Response:
        return web.Response(
            text=body,
            content_type="application/vnd.apple.mpegurl",
            headers=NO_CACHE_HEADERS,
        )

    def register_routes(self, router: web.UrlDispatcher) -> None:
        """Register the relay routes on an existing aiohttp router."""
        router.add_get("/streams/{slot}", self.handle)
        router.add_get("/streams/filler/{name}", self.handle_filler)
        router.add_get("/streams/{slot}/init/{gen}", self.handle_init)

    async def aclose(self) -> None:
        """Close the internal httpx client, if one was created."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
