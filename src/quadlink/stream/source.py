"""Live checks of configured non-Twitch HLS sources."""

import httpx
import structlog

from quadlink.config.models import Source
from quadlink.relay.playlist import parse_media_playlist
from quadlink.stream.fetcher import LookupFailed
from quadlink.types import Metadata, Stream

logger = structlog.get_logger()

# seconds a playlist fetch may take before the source counts as offline
TIMEOUT = 5.0


class SourceChecker:
    """Whether each source is live: its playlist answers, lists segments and moves on.

    A playlist that stops changing between checks is a stalled encoder, not a live
    stream, so the second identical listing in a row counts as offline.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        # source name -> the newest segment URI its last check saw
        self._last: dict[str, str] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
        return self._client

    async def check(self, source: Source) -> Stream | None:
        """The source as a stream while live, None while offline.

        Raises:
            LookupFailed: The URL answers with a playlist this check cannot use.
        """
        try:
            resp = await self._http().get(source.url)
        except httpx.HTTPError as e:
            logger.debug("source unavailable", source=source.name, error=repr(e))
            self._last.pop(source.name, None)
            return None
        if resp.status_code != 200:
            self._last.pop(source.name, None)
            return None
        text = resp.text
        if "#EXT-X-STREAM-INF" in text:
            raise LookupFailed("a master playlist: give the media playlist's URL")
        if "#EXT-X-ENDLIST" in text:
            self._last.pop(source.name, None)
            return None
        try:
            _, _, segments = parse_media_playlist(text)
        except ValueError as e:
            raise LookupFailed(f"not a media playlist: {e}") from None
        if not segments:
            return None
        newest = segments[-1][1]
        if self._last.get(source.name) == newest:
            logger.info("source stalled", source=source.name)
            return None
        self._last[source.name] = newest
        return Stream(
            url=source.url,
            metadata=Metadata(author=source.name, category=source.category, title=source.title),
            master_url=source.url,
            has_video=True,
            channel=source.name,
            source=True,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
