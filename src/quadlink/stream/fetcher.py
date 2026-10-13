"""Streamlink-based stream fetcher using Python library."""

import asyncio
import atexit
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import structlog
from streamlink import Streamlink
from streamlink.exceptions import NoPluginError, PluginError, StreamlinkError
from streamlink.options import Options

from quadlink.types import Metadata, Stream

# plugin directory (ttvlol twitch.py fetched by nix)
PLUGINS_DIR = Path(__file__).parent.parent / "plugins"

logger = structlog.get_logger()

# suppress Streamlink logging, we handle errors ourselves
logging.getLogger("streamlink").setLevel(logging.CRITICAL)


def channel_url(url: str) -> str:
    """Expand a bare channel name to the URL a fetched Stream carries as its url.

    Callers that track channels a lookup missed key them by this, so the keys
    match Stream.url for the same channel.
    """
    return url if url.startswith("http") else f"https://twitch.tv/{url}"


_VIDEO_CODECS = ("avc1", "avc3", "hvc1", "hev1", "av01", "vp09")


def _has_video(stream: Any) -> bool:
    """Whether the multivariant playlist lists this rendition with video.

    Rendition names do not tell: Twitch calls an audio rendition "audio_only",
    but through a playlist proxy the same rendition is named by bitrate
    ("160k"). A rendition missing from the multivariant counts as video.
    """
    multivariant = getattr(stream, "multivariant", None)
    url = getattr(stream, "url", None)
    for playlist in getattr(multivariant, "playlists", None) or []:
        if playlist.uri != url:
            continue
        info = playlist.stream_info
        codecs = info.codecs or []
        if info.resolution is not None or not codecs:
            return True
        return any(codec.startswith(_VIDEO_CODECS) for codec in codecs)
    return True


def _release_plugin_cache(plugin: Any) -> None:
    """Drop the atexit save hook streamlink adds for a plugin's Cache.

    streamlink's Cache.__init__ calls atexit.register(cache._save), and a new
    plugin (hence a new Cache) is built on every resolve. Each hook pins its
    Cache for the life of the process, so one leaks per resolve without this.
    """
    cache = getattr(plugin, "cache", None)
    save = getattr(cache, "_save", None)
    if save is not None:
        atexit.unregister(save)


class StreamlinkFetcher:
    """Fetches Twitch stream metadata using Streamlink."""

    def __init__(
        self,
        proxy_playlist: str = "https://eu.luminous.dev",
        low_latency: bool = True,
        max_workers: int = 3,
        timeout: float | None = None,
    ):
        """
        Initialize Streamlink fetcher.

        Args:
            proxy_playlist: Twitch playlist proxy URL
            low_latency: Enable low-latency mode
            max_workers: Maximum thread pool workers for blocking Streamlink calls
            timeout: Seconds before a lookup is abandoned as unresolved (None waits forever)
        """
        self.proxy_playlist = proxy_playlist
        self.low_latency = low_latency
        self.timeout = timeout
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._thread_local = threading.local()

    async def fetch_stream_info(self, url: str) -> Stream | None:
        """
        Fetch stream metadata from Twitch URL.

        Args:
            url: Twitch stream URL (e.g., https://twitch.tv/streamer or just 'streamer')

        Returns:
            Stream object with metadata, or None if stream is offline/unavailable
            or the lookup outlived the timeout
        """
        url = channel_url(url)

        loop = asyncio.get_running_loop()
        lookup = loop.run_in_executor(self.executor, self._fetch_stream_info_sync, url)
        if self.timeout is None:
            return await lookup
        try:
            return await asyncio.wait_for(lookup, self.timeout)
        except TimeoutError:
            # the worker thread can't be cancelled; it runs to completion and its
            # result is discarded. the caller stops waiting, so one hung lookup
            # can't stall the whole cycle.
            logger.warning("stream lookup timed out", url=url, timeout=self.timeout)
            return None

    def _get_session(self) -> Streamlink:
        """
        Get or create a thread-local Streamlink session.

        Reuses sessions within each worker thread to avoid file descriptor exhaustion.
        """
        if not hasattr(self._thread_local, "session"):
            session = Streamlink()

            # load ttvlol plugin (overrides built-in Twitch plugin)
            if PLUGINS_DIR.is_dir():
                session.plugins.load_path(PLUGINS_DIR)

            self._thread_local.session = session

        return self._thread_local.session

    def _fetch_stream_info_sync(self, url: str) -> Stream | None:
        """
        Fetch stream metadata synchronously.

        Args:
            url: Normalized Twitch stream URL

        Returns:
            Stream object with metadata, or None if stream is offline/unavailable
        """
        try:
            session = self._get_session()
            plugin_name, plugin_class, resolved_url = session.resolve_url(url)

            # pass options to constructor for ttvlol __init__
            opts = Options()
            opts.set("disable-ads", True)
            opts.set("low-latency", self.low_latency)
            opts.set("supported-codecs", ["h264"])
            if self.proxy_playlist:
                opts.set("proxy-playlist", [self.proxy_playlist])
                opts.set("proxy-playlist-fallback", False)

            plugin = plugin_class(session, resolved_url, options=opts)
            try:
                streams = plugin.streams()

                if not streams:
                    logger.debug("stream unavailable", url=url)
                    return None

                if "best" not in streams:
                    logger.debug(
                        "no 'best' stream available", url=url, available=list(streams.keys())
                    )
                    return None

                stream = streams["best"]
                # Twitch lists only an audio rendition while a broadcast starts or
                # restarts, or for one that sends no video; a tile would play it as
                # a frozen or black picture
                has_video = stream is not streams.get("audio_only") and _has_video(stream)
                metadata = self._extract_metadata(plugin, url)

                if not metadata:
                    logger.warning("could not extract metadata", url=url)
                    return None

                master_url = stream.url if hasattr(stream, "url") else None

                return Stream(
                    url=url, metadata=metadata, master_url=master_url, has_video=has_video
                )
            finally:
                _release_plugin_cache(plugin)

        except NoPluginError:
            logger.debug("no plugin for url", url=url)
            return None
        except PluginError as e:
            error_msg = str(e).lower()
            if any(
                msg in error_msg
                for msg in [
                    "offline",
                    "not streaming",
                    "channel not found",
                    "user not found",
                    "no playable streams found",
                ]
            ):
                logger.debug("stream unavailable", url=url, reason=str(e))
                return None
            logger.warning("streamlink plugin error", url=url, error=str(e))
            return None
        except StreamlinkError as e:
            logger.warning("streamlink error", url=url, error=str(e))
            return None
        except Exception as e:
            logger.error("unexpected error fetching stream", url=url, error=str(e))
            return None

    def _extract_metadata(self, plugin: Any, url: str) -> Metadata | None:
        """
        Extract metadata from Streamlink plugin.

        Args:
            plugin: Streamlink plugin instance
            url: Stream URL (for fallback author extraction)

        Returns:
            Metadata object or None if extraction fails
        """
        try:
            author = None
            category = None
            title = None

            if hasattr(plugin, "get_author"):
                author = plugin.get_author()
            if hasattr(plugin, "get_category"):
                category = plugin.get_category()
            if hasattr(plugin, "get_title"):
                title = plugin.get_title()

            if not author:
                author = getattr(plugin, "author", None) or getattr(plugin, "channel", None)
            if not category:
                category = getattr(plugin, "category", None) or getattr(plugin, "game", None)
            if not title:
                title = getattr(plugin, "title", None)

            # extract author from URL as last resort
            if not author:
                parts = url.rstrip("/").split("/")
                if len(parts) > 0:
                    author = parts[-1]

            if not author:
                return None

            return Metadata(
                author=author or "",
                category=category or "",
                title=title or "",
            )

        except Exception as e:
            logger.warning("error extracting metadata", url=url, error=str(e))
            return None
