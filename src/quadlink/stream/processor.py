"""Stream processing with filtering and prioritization."""

import asyncio
import random

import structlog

from quadlink.config.models import Config, StreamGroup
from quadlink.stream.fetcher import StreamlinkFetcher, channel_url
from quadlink.stream.filters import StreamFilter
from quadlink.types import PrioritizedStream

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
        self._seen_urls: set[str] = set()  # deduplicate by normalized URL
        # channels whose lookup returned nothing this run; filter rejections are
        # absent, since those channels are known live and deliberately excluded
        self._unresolved: set[str] = set()

    def close(self) -> None:
        """Release the lookup threads; a lookup still running finishes on its own."""
        self.fetcher.executor.shutdown(wait=False, cancel_futures=True)

    def lookup_failed(self, url: str) -> bool:
        """Whether the last run's lookup for this channel returned no stream.

        Covers offline, timed-out and errored lookups alike. False for a channel
        that resolved but a filter rejected.
        """
        return self._normalize_url(channel_url(url)) in self._unresolved

    async def process_stream_groups(self) -> list[PrioritizedStream]:
        """
        Process all stream groups from config, sorted by priority.

        Returns:
            List of PrioritizedStream objects, sorted by priority (descending)
        """
        all_streams = []
        self._seen_urls.clear()
        self._unresolved.clear()

        sorted_priorities = sorted(self.config.priorities.keys(), reverse=True)

        for priority in sorted_priorities:
            stream_groups = self.config.priorities[priority]

            logger.debug(
                "processing priority level",
                priority=priority,
                groups=len(stream_groups),
            )

            streams = await self._process_priority_level(priority, stream_groups)
            all_streams.extend(streams)

        all_streams.sort(key=lambda s: (s.priority, s.tiebreaker), reverse=True)

        logger.debug(
            "stream processing complete",
            total_candidates=len(all_streams),
            priorities=len(sorted_priorities),
        )

        return all_streams

    async def _process_priority_level(
        self, priority: int, stream_groups: list[StreamGroup]
    ) -> list[PrioritizedStream]:
        """
        Process all stream groups at a given priority level.

        Args:
            priority: Priority level
            stream_groups: List of StreamGroup objects

        Returns:
            List of PrioritizedStream objects from this priority level
        """
        tasks = []

        for group in stream_groups:
            for url in group.urls:
                task = self._process_single_stream(url, priority, group.rulesets)
                tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)
        streams = [r for r in results if isinstance(r, PrioritizedStream)]

        logger.debug(
            "priority level complete",
            priority=priority,
            successful=len(streams),
            total=len(tasks),
        )

        return streams

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
        async with self.semaphore:
            try:
                stream = await self.fetcher.fetch_stream_info(url)

                if not stream:
                    self._unresolved.add(self._normalize_url(channel_url(url)))
                    return None

                # kept out of _unresolved: a failed lookup keeps a channel whose
                # segments still arrive, and audio-only segments do arrive
                if not stream.has_video:
                    logger.debug(
                        "stream rejected", author=stream.metadata.author, reason="no video"
                    )
                    return None

                passed, reason, ruleset = self.filter.apply_filters(stream, rulesets)
                if not passed:
                    logger.debug(
                        "stream rejected",
                        author=stream.metadata.author,
                        reason=reason,
                        ruleset=ruleset,
                    )
                    return None

                normalized_url = self._normalize_url(stream.url)
                if normalized_url in self._seen_urls:
                    logger.debug(
                        "duplicate stream (already seen)",
                        author=stream.metadata.author,
                        url=normalized_url,
                    )
                    return None

                self._seen_urls.add(normalized_url)

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
                self._unresolved.add(self._normalize_url(channel_url(url)))
                return None

    def _normalize_url(self, url: str) -> str:
        """
        Normalize URL for deduplication.

        Extracts host + path, removes trailing slashes.

        Args:
            url: URL to normalize

        Returns:
            Normalized URL
        """
        return url.lower().rstrip("/")
