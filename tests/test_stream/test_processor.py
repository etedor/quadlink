"""Tests for stream processor."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from quadlink.config.models import Config, Credentials, Filters, Ruleset, StreamGroup
from quadlink.stream.processor import StreamProcessor
from quadlink.types import Metadata, PrioritizedStream, Stream


def make_stream(author: str, category: str, url: str = None) -> Stream:
    """Helper to create a stream."""
    if url is None:
        url = f"https://twitch.tv/{author.lower()}"
    return Stream(
        url=url,
        metadata=Metadata(author=author, category=category, title="Test Stream"),
    )


def make_config(
    priorities: dict = None,
    rulesets: list = None,
    skip_hosted: bool = True,
    hosted_offset: int = 50,
) -> Config:
    """Helper to create a config."""
    return Config(
        credentials=Credentials(username="test", secret="test"),
        priorities=priorities or {},
        rulesets=rulesets or [],
        skip_hosted=skip_hosted,
        hosted_offset=hosted_offset,
    )


class TestNormalizeUrl:
    """Tests for URL normalization."""

    def test_normalize_lowercase(self):
        """URL should be lowercased."""
        config = make_config()
        processor = StreamProcessor(config)

        result = processor._normalize_url("https://Twitch.TV/Streamer")
        assert result == "https://twitch.tv/streamer"

    def test_normalize_removes_trailing_slash(self):
        """Trailing slash should be removed."""
        config = make_config()
        processor = StreamProcessor(config)

        result = processor._normalize_url("https://twitch.tv/streamer/")
        assert result == "https://twitch.tv/streamer"

    def test_normalize_combined(self):
        """Both transformations should apply."""
        config = make_config()
        processor = StreamProcessor(config)

        result = processor._normalize_url("HTTPS://TWITCH.TV/STREAMER/")
        assert result == "https://twitch.tv/streamer"


class TestIsHosted:
    """Tests for hosted stream detection."""

    def test_not_hosted_matching_author(self):
        """Stream is not hosted when URL author matches metadata author."""
        config = make_config()
        processor = StreamProcessor(config)
        stream = make_stream("Streamer", "Games", "https://twitch.tv/streamer")

        assert processor._is_hosted(stream) is False

    def test_hosted_different_author(self):
        """Stream is hosted when URL author differs from metadata author."""
        config = make_config()
        processor = StreamProcessor(config)
        # URL says "hostchannel" but metadata says "actualstreamer"
        stream = Stream(
            url="https://twitch.tv/hostchannel",
            metadata=Metadata(author="actualstreamer", category="Games", title="Test"),
        )

        assert processor._is_hosted(stream) is True

    def test_hosted_case_insensitive(self):
        """Hosted check should be case insensitive."""
        config = make_config()
        processor = StreamProcessor(config)
        stream = make_stream("STREAMER", "Games", "https://twitch.tv/streamer")

        assert processor._is_hosted(stream) is False


class TestProcessSingleStream:
    """Tests for single stream processing."""

    @pytest.fixture
    def mock_fetcher(self):
        """Create a mock fetcher."""
        with patch("quadlink.stream.processor.StreamlinkFetcher") as MockFetcher:
            mock_instance = MagicMock()
            mock_instance.fetch_stream_info = AsyncMock()
            MockFetcher.return_value = mock_instance
            yield mock_instance

    @pytest.mark.asyncio
    async def test_offline_stream_returns_none(self, mock_fetcher):
        """Offline stream should return None."""
        mock_fetcher.fetch_stream_info.return_value = None
        config = make_config()
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        result = await processor._process_single_stream("streamer", 100, [])
        assert result is None

    @pytest.mark.asyncio
    async def test_stream_passes_filters(self, mock_fetcher):
        """Stream passing filters should return PrioritizedStream."""
        stream = make_stream("Streamer", "Destiny 2")
        mock_fetcher.fetch_stream_info.return_value = stream
        config = make_config()
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        result = await processor._process_single_stream("streamer", 100, [])

        assert result is not None
        assert isinstance(result, PrioritizedStream)
        assert result.priority == 100
        assert result.stream.metadata.author == "Streamer"

    @pytest.mark.asyncio
    async def test_stream_fails_filters(self, mock_fetcher):
        """Stream failing filters should return None."""
        stream = make_stream("Streamer", "Minecraft")
        mock_fetcher.fetch_stream_info.return_value = stream
        config = make_config(
            rulesets=[
                Ruleset(
                    name="global",
                    filters=Filters(block_categories=["^Minecraft$"]),
                )
            ]
        )
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        result = await processor._process_single_stream("streamer", 100, ["global"])
        assert result is None

    @pytest.mark.asyncio
    async def test_hosted_stream_skipped(self, mock_fetcher):
        """Hosted stream should be skipped when skip_hosted=True."""
        # URL is "hostchannel" but actual author is "realstreamer"
        stream = Stream(
            url="https://twitch.tv/hostchannel",
            metadata=Metadata(author="realstreamer", category="Games", title="Test"),
        )
        mock_fetcher.fetch_stream_info.return_value = stream
        config = make_config(skip_hosted=True)
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        result = await processor._process_single_stream("hostchannel", 100, [])
        assert result is None

    @pytest.mark.asyncio
    async def test_hosted_stream_priority_reduced(self, mock_fetcher):
        """Hosted stream should have reduced priority when skip_hosted=False."""
        stream = Stream(
            url="https://twitch.tv/hostchannel",
            metadata=Metadata(author="realstreamer", category="Games", title="Test"),
        )
        mock_fetcher.fetch_stream_info.return_value = stream
        config = make_config(skip_hosted=False, hosted_offset=50)
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        result = await processor._process_single_stream("hostchannel", 100, [])

        assert result is not None
        assert result.priority == 50  # 100 - 50 offset

    @pytest.mark.asyncio
    async def test_duplicate_stream_rejected(self, mock_fetcher):
        """Duplicate streams should be rejected."""
        stream = make_stream("Streamer", "Games")
        mock_fetcher.fetch_stream_info.return_value = stream
        config = make_config()
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        # first call succeeds
        result1 = await processor._process_single_stream("streamer", 100, [])
        assert result1 is not None

        # second call with same URL is duplicate
        result2 = await processor._process_single_stream("streamer", 100, [])
        assert result2 is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, mock_fetcher):
        """Exception during processing should return None."""
        mock_fetcher.fetch_stream_info.side_effect = Exception("Network error")
        config = make_config()
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        result = await processor._process_single_stream("streamer", 100, [])
        assert result is None


class TestProcessPriorityLevel:
    """Tests for priority level processing."""

    @pytest.fixture
    def mock_fetcher(self):
        """Create a mock fetcher."""
        with patch("quadlink.stream.processor.StreamlinkFetcher") as MockFetcher:
            mock_instance = MagicMock()
            mock_instance.fetch_stream_info = AsyncMock()
            MockFetcher.return_value = mock_instance
            yield mock_instance

    @pytest.mark.asyncio
    async def test_process_multiple_streams(self, mock_fetcher):
        """Should process multiple streams from groups."""
        streams = {
            "streamer1": make_stream("Streamer1", "Games"),
            "streamer2": make_stream("Streamer2", "Games"),
            "streamer3": None,  # offline
        }

        async def mock_fetch(url):
            username = url.replace("https://twitch.tv/", "").lower()
            return streams.get(username)

        mock_fetcher.fetch_stream_info.side_effect = mock_fetch
        config = make_config()
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        groups = [
            StreamGroup(urls=["streamer1", "streamer2", "streamer3"], rulesets=[]),
        ]

        results = await processor._process_priority_level(100, groups)

        assert len(results) == 2  # streamer3 is offline
        authors = {r.stream.metadata.author for r in results}
        assert "Streamer1" in authors
        assert "Streamer2" in authors

    @pytest.mark.asyncio
    async def test_process_multiple_groups(self, mock_fetcher):
        """Should process multiple groups at same priority."""
        streams = {
            "streamer1": make_stream("Streamer1", "Games"),
            "streamer2": make_stream("Streamer2", "Music"),
        }

        async def mock_fetch(url):
            username = url.replace("https://twitch.tv/", "").lower()
            return streams.get(username)

        mock_fetcher.fetch_stream_info.side_effect = mock_fetch
        config = make_config()
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        groups = [
            StreamGroup(urls=["streamer1"], rulesets=[]),
            StreamGroup(urls=["streamer2"], rulesets=[]),
        ]

        results = await processor._process_priority_level(100, groups)
        assert len(results) == 2


class TestProcessStreamGroups:
    """Tests for full stream group processing."""

    @pytest.fixture
    def mock_fetcher(self):
        """Create a mock fetcher."""
        with patch("quadlink.stream.processor.StreamlinkFetcher") as MockFetcher:
            mock_instance = MagicMock()
            mock_instance.fetch_stream_info = AsyncMock()
            MockFetcher.return_value = mock_instance
            yield mock_instance

    @pytest.mark.asyncio
    async def test_process_empty_priorities(self, mock_fetcher):
        """Empty priorities should return empty list."""
        config = make_config(priorities={})
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        results = await processor.process_stream_groups()
        assert results == []

    @pytest.mark.asyncio
    async def test_process_multiple_priorities(self, mock_fetcher):
        """Should process streams from multiple priority levels."""
        streams = {
            "high": make_stream("High", "Games"),
            "low": make_stream("Low", "Games"),
        }

        async def mock_fetch(url):
            username = url.replace("https://twitch.tv/", "").lower()
            return streams.get(username)

        mock_fetcher.fetch_stream_info.side_effect = mock_fetch

        config = make_config(
            priorities={
                999: [StreamGroup(urls=["high"], rulesets=[])],
                100: [StreamGroup(urls=["low"], rulesets=[])],
            }
        )
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        results = await processor.process_stream_groups()

        assert len(results) == 2
        # should be sorted by priority (descending)
        assert results[0].stream.metadata.author == "High"
        assert results[1].stream.metadata.author == "Low"

    @pytest.mark.asyncio
    async def test_seen_urls_cleared_between_cycles(self, mock_fetcher):
        """Seen URLs should be cleared for each processing cycle."""
        stream = make_stream("Streamer", "Games")
        mock_fetcher.fetch_stream_info.return_value = stream

        config = make_config(priorities={100: [StreamGroup(urls=["streamer"], rulesets=[])]})
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        # first cycle
        results1 = await processor.process_stream_groups()
        assert len(results1) == 1

        # second cycle - should work again because seen_urls cleared
        results2 = await processor.process_stream_groups()
        assert len(results2) == 1

    @pytest.mark.asyncio
    async def test_deduplication_within_cycle(self, mock_fetcher):
        """Same stream at different priorities should be deduplicated."""
        stream = make_stream("Streamer", "Games")
        mock_fetcher.fetch_stream_info.return_value = stream

        config = make_config(
            priorities={
                999: [StreamGroup(urls=["streamer"], rulesets=[])],
                100: [StreamGroup(urls=["streamer"], rulesets=[])],  # duplicate
            }
        )
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        results = await processor.process_stream_groups()

        # should only have one result (first one at priority 999)
        assert len(results) == 1
        assert results[0].priority == 999
