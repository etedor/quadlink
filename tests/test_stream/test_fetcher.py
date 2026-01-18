"""Tests for streamlink fetcher."""

from unittest.mock import MagicMock, patch

import pytest
from streamlink.exceptions import NoPluginError, PluginError, StreamlinkError

from quadlink.stream.fetcher import StreamlinkFetcher


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
        """Should return None when 'best' quality not available."""
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
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_no_plugin_error(self):
        """Should return None on NoPluginError."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = NoPluginError()

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://invalid.url/test")

        assert result is None

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
        """Should return None on other PluginError."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = PluginError("Some other error")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_streamlink_error(self):
        """Should return None on StreamlinkError."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = StreamlinkError("Connection failed")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_unexpected_exception(self):
        """Should return None on unexpected exception."""
        fetcher = StreamlinkFetcher()

        mock_session = MagicMock()
        mock_session.resolve_url.side_effect = RuntimeError("Unexpected!")

        with patch.object(fetcher, "_get_session", return_value=mock_session):
            result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

    def test_no_metadata_returns_none(self):
        """Should return None when metadata extraction fails."""
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
                result = fetcher._fetch_stream_info_sync("https://twitch.tv/test")

        assert result is None

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
