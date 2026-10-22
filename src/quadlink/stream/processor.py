"""Stream processing with filtering and prioritization."""

import asyncio
import random

import structlog

from quadlink.config.models import Config
from quadlink.stream.fetcher import LookupFailed, StreamlinkFetcher
from quadlink.stream.filters import StreamFilter
from quadlink.types import (
    ChannelState,
    ChannelStatus,
    PrioritizedStream,
    Stream,
    channel_login,
)

logger = structlog.get_logger()


class StreamProcessor:
    """Processes stream groups with filtering, deduplication, and prioritization."""

    def __init__(
        self,
        config: Config,
        max_concurrent: int = 4,
        lookup_timeout: float | None = None,
    ):
        """
        Initialize stream processor.

        Args:
            config: Application configuration
            max_concurrent: Maximum concurrent stream fetches
            lookup_timeout: Seconds before one stream lookup is abandoned (None waits forever)
        """
        self.config = config
        self.fetcher = StreamlinkFetcher(
            proxy_playlist=config.proxy_playlist,
            low_latency=config.low_latency,
            max_workers=max_concurrent,  # match thread pool size to concurrency limit
            timeout=lookup_timeout,
        )
        self.filter = StreamFilter(config)
        self.semaphore = asyncio.Semaphore(max_concurrent)
        # channel key -> this run's one lookup, shared by every configured entry of
        # the channel (one channel may sit in several groups with different rulesets)
        self._lookups: dict[str, asyncio.Future[Stream | None]] = {}
        # channel keys accepted this run; entries are evaluated in priority order,
        # so the highest-priority entry that passes its filters wins
        self._seen: set[str] = set()
        # channel key -> rejection reason of its highest-priority entry
        self._rejections: dict[str, str] = {}
        # channels whose lookup returned nothing this run; filter rejections are
        # absent, since those channels are known live and deliberately excluded
        self._unresolved: set[str] = set()
        # channel key -> this run's resolved stream
        self._streams: dict[str, Stream] = {}
        # channel key -> why this run's lookup failed
        self._failures: dict[str, str] = {}
        # channel key -> priority of the entry that put it in the live pool
        self._accepted: dict[str, int] = {}

    def close(self) -> None:
        """Release the lookup threads; a lookup still running finishes on its own."""
        self.fetcher.executor.shutdown(wait=False, cancel_futures=True)

    def lookup_failed(self, url: str) -> bool:
        """Whether the last run's lookup for this channel returned no stream.

        Covers offline, timed-out and errored lookups alike. False for a channel
        that resolved but a filter rejected.
        """
        return channel_login(url) in self._unresolved

    def rejection(self, url: str) -> str | None:
        """Why the last run left out a live channel, from its highest-priority entry.

        None when the channel was accepted or its lookup failed.
        """
        channel = channel_login(url)
        if channel in self._accepted:
            return None
        return self._rejections.get(channel)

    def statuses(self) -> list[ChannelStatus]:
        """Every configured channel's outcome in the last run, one row per channel.

        Rows follow config order, highest priority first.
        """
        priorities: dict[str, list[int]] = {}
        for priority in sorted(self.config.priorities, reverse=True):
            for group in self.config.priorities[priority]:
                for url in group.urls:
                    priorities.setdefault(channel_login(url), []).append(priority)

        rows = []
        for channel, entries in priorities.items():
            stream = self._streams.get(channel)
            # accepted first: one entry may hit a filter error while another passes
            if stream is not None and channel in self._accepted:
                row = ChannelStatus(
                    channel,
                    ChannelState.LIVE,
                    entries,
                    stream=stream,
                    priority=self._accepted[channel],
                )
            elif channel in self._failures:
                row = ChannelStatus(
                    channel,
                    ChannelState.LOOKUP_FAILED,
                    entries,
                    reason=self._failures[channel],
                )
            elif stream is None:
                row = ChannelStatus(channel, ChannelState.OFFLINE, entries)
            elif not stream.has_video:
                row = ChannelStatus(channel, ChannelState.NO_VIDEO, entries, stream=stream)
            else:
                row = ChannelStatus(
                    channel,
                    ChannelState.REJECTED,
                    entries,
                    stream=stream,
                    reason=self._rejections.get(channel),
                )
            rows.append(row)
        return rows

    async def process_stream_groups(self) -> list[PrioritizedStream]:
        """Look up every configured channel and return the live candidates.

        Lookups for all priority levels run at once, bounded by the semaphore and
        started highest priority first. Entries are evaluated only after every
        lookup ends, in priority then config order, so which entry of a channel
        wins never depends on which lookup finished first.

        Returns:
            List of PrioritizedStream objects, sorted by priority (descending)
        """
        self._lookups.clear()
        self._seen.clear()
        self._rejections.clear()
        self._unresolved.clear()
        self._streams.clear()
        self._failures.clear()
        self._accepted.clear()

        entries = [
            (priority, url, group.rulesets)
            for priority in sorted(self.config.priorities, reverse=True)
            for group in self.config.priorities[priority]
            for url in group.urls
        ]
        for _, url, _ in entries:
            self._start_lookup(url)
        await asyncio.gather(*self._lookups.values(), return_exceptions=True)

        all_streams = []
        for priority, url, rulesets in entries:
            # the lookup is done, so this does not wait
            stream = await self._process_single_stream(url, priority, rulesets)
            if stream is not None:
                all_streams.append(stream)

        all_streams.sort(key=lambda s: (s.priority, s.tiebreaker), reverse=True)

        logger.debug(
            "stream processing complete",
            total_candidates=len(all_streams),
            priorities=len(self.config.priorities),
        )

        return all_streams

    async def _process_single_stream(
        self,
        url: str,
        priority: int,
        rulesets: list[str],
    ) -> PrioritizedStream | None:
        """
        Process a single stream URL.

        Args:
            url: Stream URL or username
            priority: Priority level
            rulesets: Rulesets to apply

        Returns:
            PrioritizedStream if successful and passes filters, None otherwise
        """
        channel = channel_login(url)
        try:
            stream = await self._lookup(url)
        except Exception as e:
            if not isinstance(e, LookupFailed):
                logger.error("error looking up stream", url=url, error=str(e))
            self._unresolved.add(channel)
            self._failures[channel] = str(e)
            return None

        try:
            if not stream:
                self._unresolved.add(channel)
                return None
            self._streams[channel] = stream

            # kept out of _unresolved: a failed lookup keeps a channel whose
            # segments still arrive, and audio-only segments do arrive
            if not stream.has_video:
                logger.debug("stream rejected", author=stream.metadata.author, reason="no video")
                self._rejections.setdefault(channel, "no video")
                return None

            passed, reason, ruleset = self.filter.apply_filters(stream, rulesets)
            if not passed:
                logger.debug(
                    "stream rejected",
                    author=stream.metadata.author,
                    reason=reason,
                    ruleset=ruleset,
                )
                self._rejections.setdefault(channel, reason)
                return None

            if stream.channel in self._seen:
                logger.debug("duplicate stream (already seen)", channel=stream.channel)
                self._rejections.setdefault(channel, "duplicate of another configured channel")
                return None

            self._seen.add(stream.channel)
            self._accepted[channel] = priority

            prioritized = PrioritizedStream(
                stream=stream,
                priority=priority,
                tiebreaker=random.random(),
            )

            logger.debug(
                "stream accepted",
                author=stream.metadata.author,
                category=stream.metadata.category,
                priority=priority,
            )

            return prioritized

        except Exception as e:
            logger.error(
                "error processing stream",
                url=url,
                error=str(e),
            )
            self._unresolved.add(channel)
            self._failures.setdefault(channel, str(e))
            return None

    def _start_lookup(self, url: str) -> asyncio.Future[Stream | None]:
        """Start this run's one lookup of the channel, unless an entry already did."""
        channel = channel_login(url)
        lookup = self._lookups.get(channel)
        if lookup is None:
            lookup = self._lookups[channel] = asyncio.ensure_future(self._fetch(url))
        return lookup

    async def _lookup(self, url: str) -> Stream | None:
        return await asyncio.shield(self._start_lookup(url))

    async def _fetch(self, url: str) -> Stream | None:
        async with self.semaphore:
            return await self.fetcher.fetch_stream_info(url)
