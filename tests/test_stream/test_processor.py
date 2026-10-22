"""Tests for stream processor."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog

from quadlink.config.models import Config, Credentials, Filters, Ruleset, StreamGroup
from quadlink.stream.fetcher import LookupFailed
from quadlink.stream.processor import StreamProcessor
from quadlink.types import ChannelState, Metadata, PrioritizedStream, Stream, channel_login


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
) -> Config:
    """Helper to create a config."""
    return Config(
        credentials=Credentials(username="test", secret="test"),
        priorities=priorities or {},
        rulesets=rulesets or [],
    )


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
    async def test_display_name_differing_from_login_is_not_dropped(self, mock_fetcher):
        """A stream whose display name differs from its login keeps its full priority."""
        stream = Stream(
            url="https://twitch.tv/hanryang1125",
            metadata=Metadata(author="풍월량", category="Games", title="Test"),
        )
        mock_fetcher.fetch_stream_info.return_value = stream
        config = make_config()
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        result = await processor._process_single_stream("hanryang1125", 100, [])

        assert result is not None
        assert result.priority == 100
        assert result.stream.metadata.author == "풍월량"

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
    async def test_lookup_failed_records_unresolved_channel(self, mock_fetcher):
        """A lookup that returns nothing is recorded, keyed like Stream.url."""
        mock_fetcher.fetch_stream_info.return_value = None
        processor = StreamProcessor(make_config())
        processor.fetcher = mock_fetcher

        await processor._process_single_stream("Streamer", 100, [])

        assert processor.lookup_failed("https://twitch.tv/streamer")
        assert processor.lookup_failed("streamer")
        assert not processor.lookup_failed("https://twitch.tv/other")

    @pytest.mark.asyncio
    async def test_lookup_failed_false_for_filter_rejection(self, mock_fetcher):
        """A channel that resolved but a filter rejected is not a failed lookup."""
        mock_fetcher.fetch_stream_info.return_value = make_stream("Streamer", "Minecraft")
        config = make_config(
            rulesets=[Ruleset(name="global", filters=Filters(block_categories=["^Minecraft$"]))]
        )
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        await processor._process_single_stream("streamer", 100, ["global"])

        assert not processor.lookup_failed("https://twitch.tv/streamer")

    @pytest.mark.asyncio
    async def test_audio_only_stream_is_rejected_not_failed(self, mock_fetcher):
        """An audio-only channel is dropped without counting as a failed lookup."""
        stream = make_stream("Streamer", "Just Chatting")
        stream.has_video = False
        mock_fetcher.fetch_stream_info.return_value = stream
        processor = StreamProcessor(make_config())
        processor.fetcher = mock_fetcher

        result = await processor._process_single_stream("streamer", 100, [])

        assert result is None
        assert not processor.lookup_failed("https://twitch.tv/streamer")

    @pytest.mark.asyncio
    async def test_lookup_failed_on_fetch_error(self, mock_fetcher):
        """An exception during lookup counts as a failed lookup."""
        mock_fetcher.fetch_stream_info.side_effect = Exception("Network error")
        processor = StreamProcessor(make_config())
        processor.fetcher = mock_fetcher

        await processor._process_single_stream("streamer", 100, [])

        assert processor.lookup_failed("https://twitch.tv/streamer")

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
        groups = [
            StreamGroup(urls=["streamer1", "streamer2", "streamer3"], rulesets=[]),
        ]

        processor = StreamProcessor(make_config(priorities={100: groups}))
        processor.fetcher = mock_fetcher

        results = await processor.process_stream_groups()

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
        groups = [
            StreamGroup(urls=["streamer1"], rulesets=[]),
            StreamGroup(urls=["streamer2"], rulesets=[]),
        ]

        processor = StreamProcessor(make_config(priorities={100: groups}))
        processor.fetcher = mock_fetcher

        results = await processor.process_stream_groups()
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

    @pytest.mark.asyncio
    async def test_every_spelling_of_a_channel_is_looked_up_once(self, mock_fetcher):
        """Scheme, www. and case differences name one channel; the best priority wins."""
        mock_fetcher.fetch_stream_info.return_value = make_stream("Streamer", "Games")
        config = make_config(
            priorities={
                999: [StreamGroup(urls=["https://www.twitch.tv/Streamer"], rulesets=[])],
                500: [StreamGroup(urls=["http://twitch.tv/streamer/", "STREAMER"], rulesets=[])],
                100: [StreamGroup(urls=["twitch.tv/streamer"], rulesets=[])],
            }
        )
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        results = await processor.process_stream_groups()

        assert mock_fetcher.fetch_stream_info.await_count == 1
        mock_fetcher.fetch_stream_info.assert_awaited_with("https://www.twitch.tv/Streamer")
        assert [r.priority for r in results] == [999]
        assert results[0].stream.channel == "streamer"

    @staticmethod
    def two_entry_config() -> Config:
        """Foo at 100 only while playing X (group A), at 50 otherwise (group B)."""
        return make_config(
            priorities={
                100: [StreamGroup(urls=["https://twitch.tv/Foo"], rulesets=["only-x"])],
                50: [StreamGroup(urls=["foo"], rulesets=[])],
            },
            rulesets=[Ruleset(name="only-x", filters=Filters(allow_categories=["^X$"]))],
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("category", "priority"), [("Y", 50), ("X", 100)])
    async def test_channel_takes_its_best_passing_entry(self, mock_fetcher, category, priority):
        """Each entry applies its own group's rules; the best one that passes wins."""
        mock_fetcher.fetch_stream_info.return_value = make_stream("Foo", category)
        processor = StreamProcessor(self.two_entry_config())
        processor.fetcher = mock_fetcher

        results = await processor.process_stream_groups()

        assert [(r.stream.channel, r.priority) for r in results] == [("foo", priority)]
        # one lookup per cycle, though the entries spell the channel differently
        assert mock_fetcher.fetch_stream_info.await_count == 1
        assert processor.statuses()[0].state is ChannelState.LIVE

    @pytest.mark.asyncio
    async def test_channel_is_looked_up_once_per_cycle(self, mock_fetcher):
        """Every cycle looks the channel up again, once."""
        mock_fetcher.fetch_stream_info.return_value = make_stream("Foo", "Y")
        processor = StreamProcessor(self.two_entry_config())
        processor.fetcher = mock_fetcher

        await processor.process_stream_groups()
        await processor.process_stream_groups()

        assert mock_fetcher.fetch_stream_info.await_count == 2

    @pytest.mark.asyncio
    async def test_rejected_channel_records_its_best_entry_reason(self, mock_fetcher):
        """When no entry passes, the highest-priority entry's reason is kept."""
        mock_fetcher.fetch_stream_info.return_value = make_stream("Foo", "Y")
        config = make_config(
            priorities={
                100: [StreamGroup(urls=["https://twitch.tv/Foo"], rulesets=["only-x"])],
                50: [StreamGroup(urls=["foo"], rulesets=["no-y"])],
            },
            rulesets=[
                Ruleset(name="only-x", filters=Filters(allow_categories=["^X$"])),
                Ruleset(name="no-y", filters=Filters(block_categories=["^Y$"])),
            ],
        )
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        assert await processor.process_stream_groups() == []

        (row,) = processor.statuses()
        assert row.state is ChannelState.REJECTED
        assert row.reason == processor.filter.apply_filters(make_stream("Foo", "Y"), ["only-x"])[1]
        assert not processor.lookup_failed("foo")

    @pytest.mark.asyncio
    async def test_lookup_failed_matches_any_spelling(self, mock_fetcher):
        """A failed lookup is found by any spelling of the channel."""
        mock_fetcher.fetch_stream_info.return_value = None
        processor = StreamProcessor(make_config())
        processor.fetcher = mock_fetcher

        await processor._process_single_stream("https://www.twitch.tv/Streamer", 100, [])

        assert processor.lookup_failed("http://twitch.tv/streamer/")
        assert processor.lookup_failed("STREAMER")


def test_close_shuts_down_the_lookup_executor():
    config = Config(credentials=Credentials(username="u", secret="s"), rulesets=[], priorities={})
    processor = StreamProcessor(config)
    processor.close()
    with pytest.raises(RuntimeError):
        processor.fetcher.executor.submit(lambda: None)


class TestStatuses:
    """Tests for the per-channel status report."""

    @pytest.fixture
    def mock_fetcher(self):
        with patch("quadlink.stream.processor.StreamlinkFetcher") as MockFetcher:
            mock_instance = MagicMock()
            mock_instance.fetch_stream_info = AsyncMock()
            MockFetcher.return_value = mock_instance
            yield mock_instance

    @staticmethod
    def config() -> Config:
        return make_config(
            priorities={
                100: [StreamGroup(urls=["https://twitch.tv/Foo", "bar"], rulesets=["only-x"])],
                50: [StreamGroup(urls=["foo", "baz", "qux", "quux"], rulesets=["no-y"])],
            },
            rulesets=[
                Ruleset(name="only-x", filters=Filters(allow_categories=["^X$"])),
                Ruleset(name="no-y", filters=Filters(block_categories=["^Y$"])),
            ],
        )

    async def run(self, mock_fetcher, outcomes: dict) -> StreamProcessor:
        async def fetch(url):
            outcome = outcomes.get(channel_login(url))
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        mock_fetcher.fetch_stream_info.side_effect = fetch
        processor = StreamProcessor(self.config())
        processor.fetcher = mock_fetcher
        await processor.process_stream_groups()
        return processor

    @pytest.mark.asyncio
    async def test_every_status_kind(self, mock_fetcher):
        """Each configured channel gets one row, in config order."""
        silent = make_stream("Qux", "X")
        silent.has_video = False
        processor = await self.run(
            mock_fetcher,
            {
                "foo": make_stream("Foo", "X"),
                "bar": make_stream("Bar", "Y"),
                "baz": None,
                "qux": silent,
                "quux": LookupFailed("timed out after 30s"),
            },
        )

        rows = {r.channel: r for r in processor.statuses()}

        assert list(rows) == ["foo", "bar", "baz", "qux", "quux"]
        assert rows["foo"].state is ChannelState.LIVE
        assert rows["bar"].state is ChannelState.REJECTED
        assert rows["bar"].reason
        assert rows["baz"].state is ChannelState.OFFLINE
        assert rows["baz"].stream is None
        assert rows["qux"].state is ChannelState.NO_VIDEO
        assert rows["qux"].stream is silent
        assert rows["quux"].state is ChannelState.LOOKUP_FAILED
        assert rows["quux"].reason == "timed out after 30s"
        assert processor.lookup_failed("quux")

    @pytest.mark.asyncio
    async def test_live_row_carries_stream_and_thumbnail(self, mock_fetcher):
        stream = Stream(
            url="https://twitch.tv/foo",
            metadata=Metadata(author="Foo", category="X", title="hello"),
            master_url="https://example.com/foo.m3u8",
        )
        processor = await self.run(mock_fetcher, {"foo": stream})

        (row,) = [r for r in processor.statuses() if r.state is ChannelState.LIVE]

        assert row.stream is stream
        assert row.stream.metadata.title == "hello"
        assert row.stream.master_url == "https://example.com/foo.m3u8"
        assert row.thumbnail_url == (
            "https://static-cdn.jtvnw.net/previews-ttv/live_user_foo-320x180.jpg"
        )

    @pytest.mark.parametrize(("category", "priority"), [("X", 100), ("Z", 50)])
    @pytest.mark.asyncio
    async def test_multi_entry_channel_is_one_row_with_its_winning_entry(
        self, mock_fetcher, category, priority
    ):
        processor = await self.run(mock_fetcher, {"foo": make_stream("Foo", category)})

        rows = [r for r in processor.statuses() if r.channel == "foo"]

        assert len(rows) == 1
        assert rows[0].state is ChannelState.LIVE
        assert rows[0].priority == priority
        assert rows[0].priorities == [100, 50]

    @pytest.mark.asyncio
    async def test_rejection_reason_is_the_highest_priority_entrys(self, mock_fetcher):
        processor = await self.run(mock_fetcher, {"foo": make_stream("Foo", "Y")})

        (row,) = [r for r in processor.statuses() if r.channel == "foo"]

        assert row.state is ChannelState.REJECTED
        assert row.reason == processor.filter.apply_filters(make_stream("Foo", "Y"), ["only-x"])[1]
        assert row.reason != processor.filter.apply_filters(make_stream("Foo", "Y"), ["no-y"])[1]
        assert row.thumbnail_url is None

    @pytest.mark.asyncio
    async def test_filter_error_on_one_entry_keeps_the_channel_live(self, mock_fetcher):
        """A channel another entry accepted is live, whatever its other entries hit."""
        processor = StreamProcessor(self.config())
        apply = processor.filter.apply_filters

        def flaky(stream, rulesets):
            if rulesets == ["only-x"]:
                raise ValueError("bad pattern")
            return apply(stream, rulesets)

        processor.filter.apply_filters = flaky
        mock_fetcher.fetch_stream_info.side_effect = lambda url: make_stream("Foo", "Z")
        processor.fetcher = mock_fetcher
        await processor.process_stream_groups()

        (row,) = [r for r in processor.statuses() if r.channel == "foo"]

        assert row.state is ChannelState.LIVE
        assert row.priority == 50

    @pytest.mark.asyncio
    async def test_filter_error_leaves_the_channel_out_with_the_error(self, mock_fetcher):
        """A filter that raises on every entry of a channel keeps it out of the pool."""
        processor = StreamProcessor(self.config())

        def broken(stream, rulesets):
            raise ValueError("bad pattern")

        processor.filter.apply_filters = broken
        mock_fetcher.fetch_stream_info.side_effect = lambda url: make_stream("Foo", "X")
        processor.fetcher = mock_fetcher
        with structlog.testing.capture_logs() as logs:
            results = await processor.process_stream_groups()

        (row,) = [r for r in processor.statuses() if r.channel == "foo"]

        assert results == []
        assert row.state is ChannelState.LOOKUP_FAILED
        assert row.reason == "bad pattern"
        assert processor.lookup_failed("foo")
        assert any(e["event"] == "error processing stream" for e in logs)

    @pytest.mark.asyncio
    async def test_second_channel_resolving_to_a_live_one_is_a_duplicate(self, mock_fetcher):
        """Two configured channels resolving to one stream: the second is rejected."""

        async def fetch(url):
            return Stream(
                url=url,
                metadata=Metadata(author="Same", category="X", title="t"),
                channel="same",
            )

        mock_fetcher.fetch_stream_info.side_effect = fetch
        processor = StreamProcessor(
            make_config(
                priorities={100: [StreamGroup(urls=["alias"])], 50: [StreamGroup(urls=["same"])]}
            )
        )
        processor.fetcher = mock_fetcher
        results = await processor.process_stream_groups()

        rows = {r.channel: r for r in processor.statuses()}

        assert [r.priority for r in results] == [100]
        assert rows["alias"].state is ChannelState.LIVE
        assert rows["same"].state is ChannelState.REJECTED
        assert rows["same"].reason == "duplicate of another configured channel"


class TestConcurrentLevels:
    """Lookups for every priority level run at once; results keep priority order."""

    @pytest.fixture
    def mock_fetcher(self):
        with patch("quadlink.stream.processor.StreamlinkFetcher") as MockFetcher:
            mock_instance = MagicMock()
            MockFetcher.return_value = mock_instance
            yield mock_instance

    @pytest.mark.asyncio
    async def test_levels_overlap_within_the_concurrency_limit(self, mock_fetcher):
        in_flight = peak = 0

        async def fetch(url):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return None

        mock_fetcher.fetch_stream_info = fetch
        config = make_config(
            priorities={p: [StreamGroup(urls=[f"ch{p}"])] for p in range(100, 40, -10)}
        )
        processor = StreamProcessor(config, max_concurrent=4)
        processor.fetcher = mock_fetcher

        await processor.process_stream_groups()

        # six levels of one channel each: one level at a time would peak at 1
        assert peak == 4

    @pytest.mark.asyncio
    async def test_higher_priority_wins_although_its_lookup_ends_last(self, mock_fetcher):
        """Two entries resolving to one channel: the higher-priority one wins."""

        async def fetch(url):
            login = channel_login(url)
            # the high-priority lookup is the slow one
            await asyncio.sleep(0.05 if login == "alias" else 0)
            return Stream(
                url=url,
                metadata=Metadata(author=login, category="Games", title="t"),
                channel="same",
            )

        mock_fetcher.fetch_stream_info = fetch
        config = make_config(
            priorities={
                100: [StreamGroup(urls=["alias"])],
                50: [StreamGroup(urls=["same"])],
            }
        )
        processor = StreamProcessor(config)
        processor.fetcher = mock_fetcher

        results = await processor.process_stream_groups()

        assert [(r.stream.metadata.author, r.priority) for r in results] == [("alias", 100)]
        rows = {r.channel: r for r in processor.statuses()}
        assert rows["same"].state is ChannelState.REJECTED
        assert rows["same"].reason == "duplicate of another configured channel"

    @pytest.mark.asyncio
    async def test_lookups_start_highest_priority_first(self, mock_fetcher):
        started = []

        async def fetch(url):
            started.append(channel_login(url))
            return None

        mock_fetcher.fetch_stream_info = fetch
        config = make_config(
            priorities={
                10: [StreamGroup(urls=["low"])],
                90: [StreamGroup(urls=["high1", "high2"])],
                50: [StreamGroup(urls=["mid"])],
            }
        )
        processor = StreamProcessor(config, max_concurrent=1)
        processor.fetcher = mock_fetcher

        await processor.process_stream_groups()

        assert started == ["high1", "high2", "mid", "low"]


async def test_hung_lookup_threads_leave_room_for_the_next_lookups():
    """A timed-out lookup keeps its thread; the lookups after it still get one."""
    release = threading.Event()

    def lookup(url):
        if channel_login(url).startswith("hung"):
            release.wait(5)
        return None

    config = make_config(
        priorities={100: [StreamGroup(urls=["hung1", "hung2", "a", "b", "c", "d"])]}
    )
    processor = StreamProcessor(config, max_concurrent=2, lookup_timeout=0.2)
    try:
        with patch.object(processor.fetcher, "_fetch_stream_info_sync", side_effect=lookup):
            await processor.process_stream_groups()
        states = {s.channel: s.state for s in processor.statuses()}
    finally:
        release.set()
        processor.close()
    assert states == {
        "hung1": ChannelState.LOOKUP_FAILED,
        "hung2": ChannelState.LOOKUP_FAILED,
        **dict.fromkeys("abcd", ChannelState.OFFLINE),
    }
