"""Tests for streamlink fetcher."""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
from streamlink.exceptions import NoPluginError, PluginError, StreamlinkError

from quadlink.stream.fetcher import (
    SLOW_TIMEOUT_FACTOR,
    LookupFailed,
    StreamlinkFetcher,
    _release_plugin_cache,
)
from quadlink.types import channel_login, channel_url


class TestStreamlinkFetcherInit:
    """Tests for fetcher initialization."""

    def test_default_initialization(self):
        """Should initialize with default values."""
        fetcher = StreamlinkFetcher()

        assert fetcher.proxy_playlist == "https://eu.luminous.dev"
        assert fetcher.low_latency is True
        assert fetcher.executor is not None

    def test_custom_initialization(self):
        """Should accept custom values."""
        fetcher = StreamlinkFetcher(
            proxy_playlist="https://custom.proxy",
            low_latency=False,
            max_workers=5,
        )

        assert fetcher.proxy_playlist == "https://custom.proxy"
        assert fetcher.low_latency is False


class TestFetchStreamInfo:
    """Tests for fetch_stream_info async method."""

    @pytest.mark.asyncio
    async def test_url_normalization_username(self):
        """Username should be converted to full URL."""
        fetcher = StreamlinkFetcher()

        with patch.object(fetcher, "_fetch_stream_info_sync") as mock_sync:
            mock_sync.return_value = None
            await fetcher.fetch_stream_info("streamer")
            mock_sync.assert_called_once_with("https://twitch.tv/streamer")

    @pytest.mark.asyncio
    async def test_url_normalization_full_url(self):
        """Full URL should be passed as-is."""
        fetcher = StreamlinkFetcher()

        with patch.object(fetcher, "_fetch_stream_info_sync") as mock_sync:
            mock_sync.return_value = None
            await fetcher.fetch_stream_info("https://twitch.tv/streamer")
            mock_sync.assert_called_once_with("https://twitch.tv/streamer")


class TestGetSession:
    """Tests for thread-local session management."""

    def test_creates_session_on_first_call(self):
        """Should create new session on first call."""
        fetcher = StreamlinkFetcher()

        with patch("quadlink.stream.fetcher.Streamlink") as MockStreamlink:
            mock_session = MagicMock()
            mock_plugins = MagicMock()
            mock_plugins.__contains__ = MagicMock(return_value=False)
            mock_session.plugins = mock_plugins
            MockStreamlink.return_value = mock_session

            session = fetcher._get_session()

            MockStreamlink.assert_called_once()
            assert session is mock_session

    def test_reuses_session_on_subsequent_calls(self):
        """Should reuse session on subsequent calls."""
        fetcher = StreamlinkFetcher()

        with patch("quadlink.stream.fetcher.Streamlink") as MockStreamlink:
            mock_session = MagicMock()
            mock_plugins = MagicMock()
            mock_plugins.__contains__ = MagicMock(return_value=False)
            mock_session.plugins = mock_plugins
            MockStreamlink.return_value = mock_session

            session1 = fetcher._get_session()
            session2 = fetcher._get_session()

            # should only create once
            MockStreamlink.assert_called_once()
            assert session1 is session2

    def test_loads_custom_plugins(self):
        """Should load custom plugins from PLUGINS_DIR."""
        fetcher = StreamlinkFetcher()

        with patch("quadlink.stream.fetcher.Streamlink") as MockStreamlink:
            mock_session = MagicMock()
            mock_plugins = MagicMock()
            mock_session.plugins = mock_plugins
            MockStreamlink.return_value = mock_session

            mock_path = MagicMock()
            mock_path.is_dir.return_value = True

            with patch("quadlink.stream.fetcher.PLUGINS_DIR", mock_path):
                fetcher._get_session()

            mock_plugins.load_path.assert_called_once_with(mock_path)


class TestFetchStreamInfoSync:
    """Tests for synchronous fetch logic."""

    def test_successful_fetch(self):
        """Should return Stream on successful fetch."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_plugin_class = MagicMock()
        mock_plugin = MagicMock()
        mock_plugin.get_author.return_value = "TestAuthor"
        mock_plugin.get_category.return_value = "TestCategory"
        mock_plugin.get_title.return_value = "TestTitle"
        mock_plugin_class.return_value = mock_plugin

        mock_stream = MagicMock()
        mock_stream.url = "https://playlist.url/master.m3u8"
        mock_plugin.streams.return_value = {"best": mock_stream, "720p": mock_stream}

        mock_session.resolve_url.return_value = (
            "twitch",
            mock_plugin_class,
            "https://twitch.tv/test",
        )

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is not None
        assert result.url == "https://twitch.tv/test"
        assert result.metadata.author == "TestAuthor"
        assert result.metadata.category == "TestCategory"
        assert result.metadata.title == "TestTitle"
        assert result.master_url == "https://playlist.url/master.m3u8"
        assert result.channel == "test"

    def test_channel_comes_from_the_plugin_login(self):
        """The plugin's resolved login, lowercased, is the stream's channel key."""
        fetcher = StreamlinkFetcher()
        mock_session = MagicMock()
        mock_plugin = MagicMock()
        mock_plugin.channel = "SomeLogin"
        mock_plugin.get_author.return_value = "Some Login"
        mock_plugin.streams.return_value = {"best": MagicMock()}
        mock_session.resolve_url.return_value = (
            "twitch",
            MagicMock(return_value=mock_plugin),
            "https://www.twitch.tv/SomeLogin",
        )

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://www.twitch.tv/SomeLogin")

        assert result is not None
        assert result.channel == "somelogin"
        assert result.metadata.author == "Some Login"

    def test_audio_only_stream_is_marked_without_video(self):
        """Should flag a stream whose only rendition is audio."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_plugin_class = MagicMock()
        mock_plugin = MagicMock()
        audio = MagicMock()
        mock_plugin.streams.return_value = {"audio_only": audio, "best": audio, "worst": audio}
        mock_plugin_class.return_value = mock_plugin

        mock_session.resolve_url.return_value = (
            "twitch",
            mock_plugin_class,
            "https://twitch.tv/test",
        )

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is not None
        assert result.has_video is False

    def test_offline_stream_no_streams(self):
        """Should return None when no streams available."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_plugin_class = MagicMock()
        mock_plugin = MagicMock()
        mock_plugin.streams.return_value = {}
        mock_plugin_class.return_value = mock_plugin

        mock_session.resolve_url.return_value = (
            "twitch",
            mock_plugin_class,
            "https://twitch.tv/test",
        )

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_no_best_stream(self):
        """Should fail the lookup when 'best' quality not available."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_plugin_class = MagicMock()
        mock_plugin = MagicMock()
        mock_plugin.streams.return_value = {"720p": MagicMock(), "480p": MagicMock()}
        mock_plugin_class.return_value = mock_plugin

        mock_session.resolve_url.return_value = (
            "twitch",
            mock_plugin_class,
            "https://twitch.tv/test",
        )

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            with pytest.raises(LookupFailed):
                fetcher._fetch_stream_info_sync("https://twitch.tv/test")

    def test_no_plugin_error(self):
        """Should fail the lookup on NoPluginError."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = NoPluginError()

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            with pytest.raises(LookupFailed):
                fetcher._fetch_stream_info_sync("https://invalid.url/test")

    def test_plugin_error_offline(self):
        """Should return None on PluginError with offline message."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = PluginError("Channel is offline")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_plugin_error_not_streaming(self):
        """Should return None on PluginError with not streaming message."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = PluginError("User is not streaming")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_plugin_error_channel_not_found(self):
        """Should return None on PluginError with channel not found."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = PluginError("Channel not found")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_plugin_error_user_not_found(self):
        """Should return None on PluginError with user not found."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = PluginError("User not found")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_plugin_error_no_playable_streams(self):
        """Should return None on PluginError with no playable streams."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = PluginError("No playable streams found")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_plugin_error_other(self):
        """Should fail the lookup on other PluginError."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = PluginError("Some other error")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            with pytest.raises(LookupFailed):
                fetcher._fetch_stream_info_sync("https://twitch.tv/test")

    def test_streamlink_error(self):
        """Should fail the lookup on StreamlinkError."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = StreamlinkError("Connection failed")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            with pytest.raises(LookupFailed):
                fetcher._fetch_stream_info_sync("https://twitch.tv/test")

    def test_unexpected_exception(self):
        """Should fail the lookup on unexpected exception."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = RuntimeError("Unexpected!")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            with pytest.raises(LookupFailed):
                fetcher._fetch_stream_info_sync("https://twitch.tv/test")

    def test_no_metadata_fails_the_lookup(self):
        """Should fail the lookup when metadata extraction fails."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_plugin_class = MagicMock()
        mock_plugin = MagicMock()
        mock_plugin_class.return_value = mock_plugin

        mock_stream = MagicMock()
        mock_plugin.streams.return_value = {"best": mock_stream}

        mock_session.resolve_url.return_value = ("twitch", mock_plugin_class, "")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            with patch.object(fetcher, "_extract_metadata", return_value=None):
                with pytest.raises(LookupFailed):
                    fetcher._fetch_stream_info_sync("https://twitch.tv/test")

    def test_stream_without_url_attribute(self):
        """Should handle stream without url attribute."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_plugin_class = MagicMock()
        mock_plugin = MagicMock()
        mock_plugin.get_author.return_value = "Author"
        mock_plugin.get_category.return_value = "Category"
        mock_plugin.get_title.return_value = "Title"
        mock_plugin_class.return_value = mock_plugin

        # stream without url attribute
        mock_stream = MagicMock(spec=[])
        mock_plugin.streams.return_value = {"best": mock_stream}

        mock_session.resolve_url.return_value = (
            "twitch",
            mock_plugin_class,
            "https://twitch.tv/test",
        )

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is not None
        assert result.master_url is None

    def test_proxy_playlist_options(self):
        """Should set proxy playlist options when configured."""
        fetcher = StreamlinkFetcher(proxy_playlist="https://proxy.example.com")

        mock_session = MagicMock()
        mock_plugin_class = MagicMock()
        mock_plugin = MagicMock()
        mock_plugin.get_author.return_value = "Author"
        mock_plugin.get_category.return_value = "Category"
        mock_plugin.get_title.return_value = "Title"
        mock_plugin.streams.return_value = {"best": MagicMock()}
        mock_plugin_class.return_value = mock_plugin

        mock_session.resolve_url.return_value = (
            "twitch",
            mock_plugin_class,
            "https://twitch.tv/test",
        )

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            with patch("quadlink.stream.fetcher.Options") as MockOptions:
                mock_opts = MagicMock()
                MockOptions.return_value = mock_opts
                fetcher._fetch_stream_info_sync("https://twitch.tv/test")

                # verify proxy options were set
                calls = [str(c) for c in mock_opts.set.call_args_list]
                assert any("proxy-playlist" in str(c) for c in calls)

    def test_no_proxy_playlist_options(self):
        """Should not set proxy options when proxy_playlist is empty."""
        fetcher = StreamlinkFetcher(proxy_playlist="")

        mock_session = MagicMock()
        mock_plugin_class = MagicMock()
        mock_plugin = MagicMock()
        mock_plugin.get_author.return_value = "Author"
        mock_plugin.get_category.return_value = "Category"
        mock_plugin.get_title.return_value = "Title"
        mock_plugin.streams.return_value = {"best": MagicMock()}
        mock_plugin_class.return_value = mock_plugin

        mock_session.resolve_url.return_value = (
            "twitch",
            mock_plugin_class,
            "https://twitch.tv/test",
        )

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            with patch("quadlink.stream.fetcher.Options") as MockOptions:
                mock_opts = MagicMock()
                MockOptions.return_value = mock_opts
                fetcher._fetch_stream_info_sync("https://twitch.tv/test")

                # verify proxy options were NOT set
                calls = [str(c) for c in mock_opts.set.call_args_list]
                assert not any("proxy-playlist" in str(c) for c in calls)


class TestExtractMetadata:
    """Tests for metadata extraction."""

    def test_extract_with_get_methods(self):
        """Should use get_* methods when available."""
        fetcher = StreamlinkFetcher()

        mock_plugin = MagicMock()
        mock_plugin.get_author.return_value = "GetAuthor"
        mock_plugin.get_category.return_value = "GetCategory"
        mock_plugin.get_title.return_value = "GetTitle"

        result = fetcher._extract_metadata(mock_plugin, "https://twitch.tv/test")

        assert result is not None
        assert result.author == "GetAuthor"
        assert result.category == "GetCategory"
        assert result.title == "GetTitle"

    def test_extract_with_attributes(self):
        """Should fall back to attributes when get_* methods return None."""
        fetcher = StreamlinkFetcher()

        mock_plugin = MagicMock()
        mock_plugin.get_author.return_value = None
        mock_plugin.get_category.return_value = None
        mock_plugin.get_title.return_value = None
        mock_plugin.author = "AttrAuthor"
        mock_plugin.category = "AttrCategory"
        mock_plugin.title = "AttrTitle"

        result = fetcher._extract_metadata(mock_plugin, "https://twitch.tv/test")

        assert result is not None
        assert result.author == "AttrAuthor"
        assert result.category == "AttrCategory"
        assert result.title == "AttrTitle"

    def test_extract_with_alternate_attributes(self):
        """Should try alternate attribute names."""
        fetcher = StreamlinkFetcher()

        mock_plugin = MagicMock()
        mock_plugin.get_author.return_value = None
        mock_plugin.get_category.return_value = None
        mock_plugin.get_title.return_value = None
        mock_plugin.author = None
        mock_plugin.category = None
        mock_plugin.title = None
        mock_plugin.channel = "ChannelName"
        mock_plugin.game = "GameName"

        result = fetcher._extract_metadata(mock_plugin, "https://twitch.tv/test")

        assert result is not None
        assert result.author == "ChannelName"
        assert result.category == "GameName"

    def test_extract_author_from_url(self):
        """Should extract author from URL as last resort."""
        fetcher = StreamlinkFetcher()

        mock_plugin = MagicMock(spec=[])  # no methods or attributes

        result = fetcher._extract_metadata(mock_plugin, "https://twitch.tv/urlauthor")

        assert result is not None
        assert result.author == "urlauthor"

    def test_extract_no_author_returns_none(self):
        """Should return None when no author can be extracted."""
        fetcher = StreamlinkFetcher()

        mock_plugin = MagicMock(spec=[])  # no methods or attributes

        result = fetcher._extract_metadata(mock_plugin, "")

        assert result is None

    def test_extract_empty_values_become_empty_strings(self):
        """Empty values should become empty strings."""
        fetcher = StreamlinkFetcher()

        mock_plugin = MagicMock()
        mock_plugin.get_author.return_value = "Author"
        mock_plugin.get_category.return_value = None
        mock_plugin.get_title.return_value = None
        mock_plugin.category = None
        mock_plugin.game = None
        mock_plugin.title = None

        result = fetcher._extract_metadata(mock_plugin, "https://twitch.tv/test")

        assert result is not None
        assert result.category == ""
        assert result.title == ""

    def test_extract_exception_returns_none(self):
        """Should return None on exception."""
        fetcher = StreamlinkFetcher()

        mock_plugin = MagicMock()
        mock_plugin.get_author.side_effect = RuntimeError("Failed")

        result = fetcher._extract_metadata(mock_plugin, "https://twitch.tv/test")

        assert result is None


def test_release_plugin_cache_unregisters_save_hook():
    class _Cache:
        def _save(self):
            pass

    class _Plugin:
        def __init__(self):
            self.cache = _Cache()

    plugin = _Plugin()
    with patch("quadlink.stream.fetcher.atexit.unregister") as unregister:
        _release_plugin_cache(plugin)
    unregister.assert_called_once_with(plugin.cache._save)


def test_release_plugin_cache_handles_missing_cache():
    class _Plugin:
        pass

    with patch("quadlink.stream.fetcher.atexit.unregister") as unregister:
        _release_plugin_cache(_Plugin())  # no cache attribute -> no error
    unregister.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_stream_info_gives_up_after_timeout():
    fetcher = StreamlinkFetcher(timeout=0.05)
    release = threading.Event()

    def hung_lookup(url):
        release.wait(5)
        return None

    with patch.object(fetcher, "_fetch_stream_info_sync", side_effect=hung_lookup):
        with pytest.raises(LookupFailed, match="timed out"):
            await asyncio.wait_for(fetcher.fetch_stream_info("streamer"), 2)
    release.set()
    fetcher.executor.shutdown(wait=True)


class _ManualClock:
    """A `loop.time()` stand-in the test advances by hand.

    `fetch_stream_info` reads the deadline off `loop.time()`, and so does
    asyncio's own timer machinery, so patching the loop's clock with this
    makes a lookup's result race its deadline on our schedule instead of the
    scheduler's: real executor-thread delay (e.g. CPU contention) never flips
    a test's outcome, since the clock only moves when we move it.
    """

    def __init__(self, start: float) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, by: float) -> None:
        self._now += by


async def _pump() -> None:
    """Give the loop a few ready-queue turns to react to a clock advance."""
    for _ in range(3):
        await asyncio.sleep(0)


def _blocks_on(event: threading.Event):
    """A lookup that returns None once `event` is set (or after 5s, as a safety net)."""

    def lookup(url):
        event.wait(5)
        return None

    return lookup


class TestSlowChannels:
    """A channel whose lookup outlives the timeout gets a longer one next time."""

    @staticmethod
    def _install_clock(monkeypatch, loop) -> _ManualClock:
        clock = _ManualClock(loop.time())
        monkeypatch.setattr(loop, "time", clock)
        return clock

    async def test_slow_channel_resolves_on_the_next_lookup(self, monkeypatch):
        fetcher = StreamlinkFetcher(timeout=1.0)
        clock = self._install_clock(monkeypatch, asyncio.get_running_loop())

        hang = threading.Event()
        with patch.object(fetcher, "_fetch_stream_info_sync", side_effect=_blocks_on(hang)):
            task = asyncio.create_task(fetcher.fetch_stream_info("slow"))
            await _pump()
            clock.advance(fetcher.timeout + 0.01)
            with pytest.raises(LookupFailed, match="timed out after 1s"):
                await task
        hang.set()

        # held past the base timeout (it would already be unresolved without
        # the 3x extension) but released before 3x it, so it resolves
        for _ in range(2):
            release = threading.Event()
            with patch.object(fetcher, "_fetch_stream_info_sync", side_effect=_blocks_on(release)):
                task = asyncio.create_task(fetcher.fetch_stream_info("slow"))
                await _pump()
                clock.advance(fetcher.timeout + 0.01)
                await _pump()
                assert not task.done()
                release.set()
                assert await task is None

        # other channels keep the normal timeout
        hang_other = threading.Event()
        with patch.object(fetcher, "_fetch_stream_info_sync", side_effect=_blocks_on(hang_other)):
            task = asyncio.create_task(fetcher.fetch_stream_info("other"))
            await _pump()
            clock.advance(fetcher.timeout + 0.01)
            with pytest.raises(LookupFailed, match="timed out after 1s"):
                await task
        hang_other.set()
        fetcher.executor.shutdown(wait=True)

    async def test_fast_lookup_restores_the_normal_timeout(self, monkeypatch):
        fetcher = StreamlinkFetcher(timeout=1.0)
        clock = self._install_clock(monkeypatch, asyncio.get_running_loop())

        hang = threading.Event()
        with patch.object(fetcher, "_fetch_stream_info_sync", side_effect=_blocks_on(hang)):
            task = asyncio.create_task(fetcher.fetch_stream_info("ch"))
            await _pump()
            clock.advance(fetcher.timeout + 0.01)
            with pytest.raises(LookupFailed):
                await task
        hang.set()

        # the clock never advances here, so this is within the base timeout
        # no matter how long the executor thread actually takes to run
        with patch.object(fetcher, "_fetch_stream_info_sync", return_value=None):
            assert await fetcher.fetch_stream_info("ch") is None

        # the mark is cleared: back to the plain base timeout, not 3x it
        hang2 = threading.Event()
        with patch.object(fetcher, "_fetch_stream_info_sync", side_effect=_blocks_on(hang2)):
            task = asyncio.create_task(fetcher.fetch_stream_info("ch"))
            await _pump()
            clock.advance(fetcher.timeout + 0.01)
            with pytest.raises(LookupFailed, match="timed out after 1s"):
                await task
        hang2.set()
        fetcher.executor.shutdown(wait=True)

    async def test_hung_channel_times_out_at_the_longer_timeout(self, monkeypatch):
        fetcher = StreamlinkFetcher(timeout=1.0)
        clock = self._install_clock(monkeypatch, asyncio.get_running_loop())
        release = threading.Event()

        with patch.object(fetcher, "_fetch_stream_info_sync", side_effect=_blocks_on(release)):
            task = asyncio.create_task(fetcher.fetch_stream_info("ch"))
            await _pump()
            clock.advance(fetcher.timeout + 0.01)
            with pytest.raises(LookupFailed, match="timed out after 1s"):
                await task

            for _ in range(2):
                task = asyncio.create_task(fetcher.fetch_stream_info("ch"))
                await _pump()
                clock.advance(fetcher.timeout * SLOW_TIMEOUT_FACTOR + 0.01)
                with pytest.raises(LookupFailed, match="timed out after 3s"):
                    await task
        release.set()
        fetcher.executor.shutdown(wait=True)

    async def test_cancelled_lookup_keeps_the_longer_timeout(self, monkeypatch):
        fetcher = StreamlinkFetcher(timeout=1.0)
        clock = self._install_clock(monkeypatch, asyncio.get_running_loop())
        release = threading.Event()

        with patch.object(fetcher, "_fetch_stream_info_sync", side_effect=_blocks_on(release)):
            task = asyncio.create_task(fetcher.fetch_stream_info("ch"))
            await _pump()
            clock.advance(fetcher.timeout + 0.01)
            with pytest.raises(LookupFailed):
                await task

            task = asyncio.create_task(fetcher.fetch_stream_info("ch"))
            await _pump()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # a cancelled lookup never proves itself fast, so the mark stays
            task = asyncio.create_task(fetcher.fetch_stream_info("ch"))
            await _pump()
            clock.advance(fetcher.timeout * SLOW_TIMEOUT_FACTOR + 0.01)
            with pytest.raises(LookupFailed, match="timed out after 3s"):
                await task
        release.set()
        fetcher.executor.shutdown(wait=True)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("foo", "https://twitch.tv/foo"),
        ("twitch.tv/foo", "https://twitch.tv/foo"),
        ("www.twitch.tv/foo", "https://www.twitch.tv/foo"),
        ("https://twitch.tv/foo", "https://twitch.tv/foo"),
        ("http://twitch.tv/foo", "http://twitch.tv/foo"),
        ("httpfoo", "https://twitch.tv/httpfoo"),
    ],
)
def test_channel_url(given, expected):
    assert channel_url(given) == expected


@pytest.mark.parametrize(
    "given",
    [
        "foo",
        "Foo",
        "twitch.tv/foo",
        "www.twitch.tv/FOO",
        "https://twitch.tv/foo",
        "http://twitch.tv/foo/",
        "https://www.twitch.tv/Foo",
        "HTTPS://WWW.TWITCH.TV/FOO/",
        "https://player.twitch.tv/?channel=Foo",
        " foo ",
    ],
)
def test_channel_login_maps_every_spelling_to_one_key(given):
    assert channel_login(given) == "foo"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://www.twitch.tv/videos/123", "twitch.tv/videos/123"),
        ("https://twitch.tv/videos/456/", "twitch.tv/videos/456"),
        ("twitch.tv/foo/clip/Slug", "twitch.tv/foo/clip/slug"),
        ("https://clips.twitch.tv/Slug", "clips.twitch.tv/slug"),
        ("https://www.twitch.tv/directory", "twitch.tv/directory"),
    ],
)
def test_channel_login_keys_other_pages_by_url(given, expected):
    """Pages that are not a live channel keep distinct keys."""
    assert channel_login(given) == expected
