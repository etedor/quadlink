"""Tests for daemon module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from quadlink.daemon import Daemon, run_daemon
from quadlink.types import Metadata, PrioritizedStream, Quad, Stream


def make_stream(author: str) -> PrioritizedStream:
    """Create a prioritized stream."""
    return PrioritizedStream(
        stream=Stream(
            url=f"https://twitch.tv/{author}",
            metadata=Metadata(author=author, category="Games", title="Test"),
        ),
        priority=100,
        tiebreaker=0.5,
    )


class TestDaemonInit:
    """Tests for Daemon initialization."""

    def test_default_initialization(self):
        """Should initialize with default values."""
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()

        assert daemon.interval == 30
        assert daemon.one_shot is False
        assert daemon.running is False
        assert daemon.processor is None
        assert daemon.quad_builder is None
        assert daemon.quadstream_client is None

    def test_custom_initialization(self):
        """Should accept custom interval and one_shot."""
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon(interval=60, one_shot=True)

        assert daemon.interval == 60
        assert daemon.one_shot is True


class TestDaemonStart:
    """Tests for daemon start method."""

    @pytest.mark.asyncio
    async def test_start_starts_health_server(self):
        """start() should start the health server when enabled."""
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer") as MockHealth:
                mock_health = MagicMock()
                MockHealth.return_value = mock_health

                daemon = Daemon(enable_health_server=True)
                daemon._main_loop = AsyncMock()

                await daemon.start()

                mock_health.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_sets_running_true(self):
        """start() should set running to True."""
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()
                daemon._main_loop = AsyncMock()

                await daemon.start()

                assert daemon.running is True

    @pytest.mark.asyncio
    async def test_start_handles_cancelled_error(self):
        """start() should re-raise CancelledError."""
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()
                daemon._main_loop = AsyncMock(side_effect=asyncio.CancelledError())

                with pytest.raises(asyncio.CancelledError):
                    await daemon.start()


class TestDaemonMainLoop:
    """Tests for daemon main loop."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config."""
        config = MagicMock()
        config.credentials.username = "user"
        config.credentials.secret = "secret"
        config.webhook.enabled = False
        config.webhook.url = ""
        return config

    @pytest.fixture
    def daemon_with_mocks(self):
        """Create a daemon with mocked dependencies."""
        with patch("quadlink.daemon.ConfigLoader") as MockLoader:
            with patch("quadlink.daemon.HealthServer") as MockHealth:
                mock_loader = MagicMock()
                mock_loader.load_or_cache = AsyncMock()
                MockLoader.return_value = mock_loader

                mock_health = MagicMock()
                MockHealth.return_value = mock_health

                daemon = Daemon(one_shot=True, enable_health_server=True)
                yield daemon, mock_loader, mock_health

    @pytest.mark.asyncio
    async def test_main_loop_config_load_failure(self, daemon_with_mocks):
        """Should sleep and retry on config load failure."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True

        # first call returns None, second call we stop
        call_count = 0

        async def mock_load():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                daemon.running = False
            return None

        mock_loader.load_or_cache = mock_load

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await daemon._main_loop()
            mock_sleep.assert_called_with(30)

    @pytest.mark.asyncio
    async def test_main_loop_marks_ready_on_config_load(self, daemon_with_mocks, mock_config):
        """Should mark health server ready after config load."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor"):
            with patch("quadlink.daemon.QuadBuilder"):
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=False)
                    MockClient.return_value = mock_client

                    # login fails, so we stop after first iteration
                    call_count = 0

                    async def mock_load():
                        nonlocal call_count
                        call_count += 1
                        if call_count >= 2:
                            daemon.running = False
                        return mock_config

                    mock_loader.load_or_cache = mock_load

                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        await daemon._main_loop()

                    mock_health.mark_ready.assert_called()

    @pytest.mark.asyncio
    async def test_main_loop_initializes_components(self, daemon_with_mocks, mock_config):
        """Should initialize processor and builder on first run."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.QuadBuilder") as MockBuilder:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=False)
                    MockClient.return_value = mock_client

                    call_count = 0

                    async def mock_load():
                        nonlocal call_count
                        call_count += 1
                        if call_count >= 2:
                            daemon.running = False
                        return mock_config

                    mock_loader.load_or_cache = mock_load

                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        await daemon._main_loop()

                    MockProcessor.assert_called_once_with(mock_config)
                    MockBuilder.assert_called_once_with(mock_config)

    @pytest.mark.asyncio
    async def test_main_loop_login_failure(self, daemon_with_mocks, mock_config):
        """Should retry on login failure."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor"):
            with patch("quadlink.daemon.QuadBuilder"):
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=False)
                    MockClient.return_value = mock_client

                    call_count = 0

                    async def mock_load():
                        nonlocal call_count
                        call_count += 1
                        if call_count >= 2:
                            daemon.running = False
                        return mock_config

                    mock_loader.load_or_cache = mock_load

                    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                        await daemon._main_loop()
                        # should sleep on login failure
                        mock_sleep.assert_called_with(30)

    @pytest.mark.asyncio
    async def test_main_loop_no_candidates(self, daemon_with_mocks, mock_config):
        """Should sleep when no candidates available."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.QuadBuilder"):
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(return_value=[])
                    MockProcessor.return_value = mock_processor

                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=True)
                    MockClient.return_value = mock_client

                    call_count = 0
                    original_interval = daemon.interval

                    async def mock_load():
                        nonlocal call_count
                        call_count += 1
                        if call_count >= 2:
                            daemon.running = False
                        return mock_config

                    mock_loader.load_or_cache = mock_load

                    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                        await daemon._main_loop()
                        mock_sleep.assert_called_with(original_interval)

    @pytest.mark.asyncio
    async def test_main_loop_empty_quad(self, daemon_with_mocks, mock_config):
        """Should skip update when quad is empty."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.QuadBuilder") as MockBuilder:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    empty_quad = Quad()
                    mock_builder = MagicMock()
                    mock_builder.build_quad.return_value = empty_quad
                    MockBuilder.return_value = mock_builder

                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=True)
                    MockClient.return_value = mock_client

                    call_count = 0

                    async def mock_load():
                        nonlocal call_count
                        call_count += 1
                        if call_count >= 2:
                            daemon.running = False
                        return mock_config

                    mock_loader.load_or_cache = mock_load

                    with patch("asyncio.sleep", new_callable=AsyncMock):
                        await daemon._main_loop()
                        # update_quad should not be called for empty quad
                        mock_client.update_quad.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_loop_successful_update(self, daemon_with_mocks, mock_config):
        """Should update quad on success."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        daemon.one_shot = True
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.QuadBuilder") as MockBuilder:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(stream1="https://twitch.tv/streamer1")
                    mock_builder = MagicMock()
                    mock_builder.build_quad.return_value = quad
                    MockBuilder.return_value = mock_builder

                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=True)
                    mock_client.update_quad = AsyncMock(return_value=True)
                    MockClient.return_value = mock_client

                    await daemon._main_loop()

                    mock_client.update_quad.assert_called_once_with(quad)

    @pytest.mark.asyncio
    async def test_main_loop_update_failure(self, daemon_with_mocks, mock_config):
        """Should log error on update failure."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        daemon.one_shot = True
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.QuadBuilder") as MockBuilder:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(stream1="https://twitch.tv/streamer1")
                    mock_builder = MagicMock()
                    mock_builder.build_quad.return_value = quad
                    MockBuilder.return_value = mock_builder

                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=True)
                    mock_client.update_quad = AsyncMock(return_value=False)
                    MockClient.return_value = mock_client

                    # should not raise, just log error
                    await daemon._main_loop()

    @pytest.mark.asyncio
    async def test_main_loop_webhook_enabled(self, daemon_with_mocks):
        """Should send webhook when enabled."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        daemon.one_shot = True

        config = MagicMock()
        config.credentials.username = "user"
        config.credentials.secret = "secret"
        config.webhook.enabled = True
        config.webhook.url = "https://webhook.example.com"

        mock_loader.load_or_cache = AsyncMock(return_value=config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.QuadBuilder") as MockBuilder:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(stream1="https://twitch.tv/streamer1")
                    mock_builder = MagicMock()
                    mock_builder.build_quad.return_value = quad
                    MockBuilder.return_value = mock_builder

                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=True)
                    mock_client.update_quad = AsyncMock(return_value=True)
                    mock_client.send_webhook = AsyncMock(return_value=True)
                    MockClient.return_value = mock_client

                    await daemon._main_loop()

                    mock_client.send_webhook.assert_called_once_with(
                        "https://webhook.example.com", quad
                    )

    @pytest.mark.asyncio
    async def test_main_loop_one_shot_exits(self, daemon_with_mocks, mock_config):
        """Should exit after one iteration in one-shot mode."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        daemon.one_shot = True
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.QuadBuilder") as MockBuilder:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(stream1="https://twitch.tv/streamer1")
                    mock_builder = MagicMock()
                    mock_builder.build_quad.return_value = quad
                    MockBuilder.return_value = mock_builder

                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=True)
                    mock_client.update_quad = AsyncMock(return_value=True)
                    MockClient.return_value = mock_client

                    await daemon._main_loop()

                    assert daemon.running is False

    @pytest.mark.asyncio
    async def test_main_loop_continuous_mode_sleeps(self, daemon_with_mocks, mock_config):
        """Should sleep between iterations in continuous mode."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        daemon.one_shot = False
        daemon.interval = 45

        call_count = 0

        async def mock_load():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                daemon.running = False
            return mock_config

        mock_loader.load_or_cache = mock_load

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.QuadBuilder") as MockBuilder:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(stream1="https://twitch.tv/streamer1")
                    mock_builder = MagicMock()
                    mock_builder.build_quad.return_value = quad
                    MockBuilder.return_value = mock_builder

                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=True)
                    mock_client.update_quad = AsyncMock(return_value=True)
                    MockClient.return_value = mock_client

                    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                        await daemon._main_loop()
                        # should sleep for interval after successful update
                        mock_sleep.assert_called_with(45)

    @pytest.mark.asyncio
    async def test_main_loop_cancelled_error(self, daemon_with_mocks, mock_config):
        """Should exit cleanly on CancelledError."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True

        mock_loader.load_or_cache = AsyncMock(side_effect=asyncio.CancelledError())

        # should not raise
        await daemon._main_loop()

    @pytest.mark.asyncio
    async def test_main_loop_unexpected_error(self, daemon_with_mocks, mock_config):
        """Should sleep and retry on unexpected error."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True

        call_count = 0

        async def mock_load():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                daemon.running = False
                return None
            raise RuntimeError("unexpected")

        mock_loader.load_or_cache = mock_load

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await daemon._main_loop()
            mock_sleep.assert_called_with(30)


class TestRunDaemon:
    """Tests for run_daemon function."""

    @pytest.mark.asyncio
    async def test_run_daemon_creates_daemon(self):
        """run_daemon should create and run daemon."""
        with patch("quadlink.daemon.Daemon") as MockDaemon:
            mock_daemon = MagicMock()
            mock_daemon.start = AsyncMock()
            mock_daemon.running = True
            mock_daemon.health_server = MagicMock()
            MockDaemon.return_value = mock_daemon

            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_get_loop.return_value = mock_loop

                # cancel task immediately
                async def mock_start():
                    mock_daemon.running = False
                    raise asyncio.CancelledError()

                mock_daemon.start = mock_start

                await run_daemon(
                    one_shot=True, interval=60, enable_health_server=False, config_path=None
                )

                MockDaemon.assert_called_once_with(
                    interval=60, one_shot=True, enable_health_server=False, config_path=None
                )

    @pytest.mark.asyncio
    async def test_run_daemon_stops_health_server_on_exit(self):
        """run_daemon should stop health server on exit."""
        with patch("quadlink.daemon.Daemon") as MockDaemon:
            mock_daemon = MagicMock()
            mock_health = MagicMock()
            mock_daemon.health_server = mock_health
            mock_daemon.running = True
            MockDaemon.return_value = mock_daemon

            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_get_loop.return_value = mock_loop

                async def mock_start():
                    raise asyncio.CancelledError()

                mock_daemon.start = mock_start

                await run_daemon()

                mock_health.stop.assert_called()

    @pytest.mark.asyncio
    async def test_run_daemon_sets_up_signal_handlers(self):
        """run_daemon should set up SIGINT and SIGTERM handlers."""
        import signal

        with patch("quadlink.daemon.Daemon") as MockDaemon:
            mock_daemon = MagicMock()
            mock_daemon.health_server = MagicMock()
            mock_daemon.running = True
            MockDaemon.return_value = mock_daemon

            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_get_loop.return_value = mock_loop

                async def mock_start():
                    raise asyncio.CancelledError()

                mock_daemon.start = mock_start

                await run_daemon()

                # verify signal handlers were added
                assert mock_loop.add_signal_handler.call_count == 2
                calls = mock_loop.add_signal_handler.call_args_list
                signals_registered = {call[0][0] for call in calls}
                assert signal.SIGINT in signals_registered
                assert signal.SIGTERM in signals_registered

    @pytest.mark.asyncio
    async def test_run_daemon_signal_handler_callback(self):
        """Signal handler should stop daemon and cancel task."""
        import signal

        with patch("quadlink.daemon.Daemon") as MockDaemon:
            mock_daemon = MagicMock()
            mock_health = MagicMock()
            mock_daemon.health_server = mock_health
            mock_daemon.running = True
            MockDaemon.return_value = mock_daemon

            captured_handler = None

            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()

                def capture_handler(sig, handler):
                    nonlocal captured_handler
                    if sig == signal.SIGINT:
                        captured_handler = handler

                mock_loop.add_signal_handler.side_effect = capture_handler
                mock_get_loop.return_value = mock_loop

                async def mock_start():
                    # wait a bit to allow signal handler to be set up
                    await asyncio.sleep(0.01)
                    raise asyncio.CancelledError()

                mock_daemon.start = mock_start

                await run_daemon()

                # call the captured signal handler
                assert captured_handler is not None
                captured_handler()

                # verify handler effects
                assert mock_daemon.running is False
                mock_health.stop.assert_called()
