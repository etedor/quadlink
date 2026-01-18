"""Streamlink-based stream fetcher using Python library."""

import asyncio
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


class StreamlinkFetcher:
    """Fetches Twitch stream metadata using Streamlink."""

    def __init__(
        self,
        proxy_playlist: str = "https://eu.luminous.dev",
        low_latency: bool = True,
        max_workers: int = 3,
    ):
        """
        Initialize Streamlink fetcher.

        Args:
            proxy_playlist: Twitch playlist proxy URL
            low_latency: Enable low-latency mode
            max_workers: Maximum thread pool workers for blocking Streamlink calls
        """
        self.proxy_playlist = proxy_playlist
        self.low_latency = low_latency
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._thread_local = threading.local()

    async def fetch_stream_info(self, url: str) -> Stream | None:
        """
        Fetch stream metadata from Twitch URL.

        Args:
            url: Twitch stream URL (e.g., https://twitch.tv/streamer or just 'streamer')

        Returns:
            Stream object with metadata, or None if stream is offline/unavailable
        """
        if not url.startswith("http"):
            url = f"https://twitch.tv/{url}"

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self.executor, self._fetch_stream_info_sync, url)

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
            streams = plugin.streams()

            if not streams:
                logger.debug("stream unavailable", url=url)
                return None

            if "best" not in streams:
                logger.debug("no 'best' stream available", url=url, available=list(streams.keys()))
                return None

            stream = streams["best"]
            metadata = self._extract_metadata(plugin, url)

            if not metadata:
                logger.warning("could not extract metadata", url=url)
                return None

            master_url = stream.url if hasattr(stream, "url") else None

            return Stream(url=url, metadata=metadata, master_url=master_url)

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
