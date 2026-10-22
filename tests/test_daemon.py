"""Tests for daemon module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from quadlink.config.loader import ConfigNotFoundError
from quadlink.daemon import Daemon, run_daemon
from quadlink.quad import QuadState
from quadlink.types import (
    ChannelState,
    ChannelStatus,
    Metadata,
    PrioritizedStream,
    Quad,
    Stream,
    channel_login,
)


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


def composed(urls: list[str], channels: list[str] | None = None) -> QuadState:
    """A composition holding urls, identified by channels (default: each URL's login)."""
    quad = Quad(urls)
    if channels is None:
        channels = [channel_login(u) if u else "" for u in quad.slots]
    return QuadState(quad=quad, channels=tuple(channels))


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
        assert daemon.quad_state == QuadState()
        assert daemon.quadstream_client is None
        assert daemon.enable_webui is False
        assert daemon.webui_host == "0.0.0.0"
        assert daemon.webui_port == 8081
        assert daemon.webui is None
        assert daemon.webui_runner is None

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
        config.mode = "both"
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
        daemon.one_shot = False

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
    async def test_main_loop_login_failure_is_not_a_completed_cycle(
        self, daemon_with_mocks, mock_config
    ):
        """A failed login should not count as a completed cycle for /ready."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        daemon.one_shot = False
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor"):
            with patch("quadlink.daemon.compose"):
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

                    mock_health.mark_cycle.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_loop_initializes_components(self, daemon_with_mocks, mock_config):
        """Should initialize the processor on first run."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        daemon.one_shot = False
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.compose") as MockCompose:
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

                    MockProcessor.assert_called_once_with(
                        mock_config, lookup_timeout=daemon.interval
                    )
                    # login failed, so nothing was composed
                    MockCompose.assert_not_called()

    @pytest.mark.asyncio
    async def test_main_loop_login_failure(self, daemon_with_mocks, mock_config):
        """Should retry on login failure."""
        daemon, mock_loader, mock_health = daemon_with_mocks
        daemon.running = True
        daemon.one_shot = False
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor"):
            with patch("quadlink.daemon.compose"):
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
        daemon.one_shot = False
        mock_loader.load_or_cache = AsyncMock(return_value=mock_config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.compose"):
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
            with patch("quadlink.daemon.compose") as MockCompose:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    MockCompose.return_value = composed(Quad().slots)

                    mock_client = AsyncMock()
                    mock_client.login = AsyncMock(return_value=True)
                    MockClient.return_value = mock_client

                    await daemon._main_loop()

                    MockCompose.assert_called_once()
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
            with patch("quadlink.daemon.compose") as MockCompose:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(["https://twitch.tv/streamer1"])
                    MockCompose.return_value = composed(quad.slots)

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
            with patch("quadlink.daemon.compose") as MockCompose:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(["https://twitch.tv/streamer1"])
                    MockCompose.return_value = composed(quad.slots)

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
        config.mode = "both"
        config.credentials.username = "user"
        config.credentials.secret = "secret"
        config.webhook.enabled = True
        config.webhook.url = "https://webhook.example.com"

        mock_loader.load_or_cache = AsyncMock(return_value=config)

        with patch("quadlink.daemon.StreamProcessor") as MockProcessor:
            with patch("quadlink.daemon.compose") as MockCompose:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(["https://twitch.tv/streamer1"])
                    MockCompose.return_value = composed(quad.slots)

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
            with patch("quadlink.daemon.compose") as MockCompose:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(["https://twitch.tv/streamer1"])
                    MockCompose.return_value = composed(quad.slots)

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
            with patch("quadlink.daemon.compose") as MockCompose:
                with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                    mock_processor = MagicMock()
                    mock_processor.process_stream_groups = AsyncMock(
                        return_value=[make_stream("streamer1")]
                    )
                    MockProcessor.return_value = mock_processor

                    quad = Quad(["https://twitch.tv/streamer1"])
                    MockCompose.return_value = composed(quad.slots)

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
        daemon.one_shot = False

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
            mock_daemon.webui_runner = None
            mock_daemon.slot_relay = AsyncMock()
            mock_daemon._segment_client = AsyncMock()
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
                    interval=60,
                    one_shot=True,
                    enable_health_server=False,
                    config_path=None,
                    enable_webui=False,
                    webui_host="0.0.0.0",
                    webui_port=8081,
                    webui_allowed_hosts=None,
                )

    @pytest.mark.asyncio
    async def test_run_daemon_stops_health_server_on_exit(self):
        """run_daemon should stop health server on exit."""
        with patch("quadlink.daemon.Daemon") as MockDaemon:
            mock_daemon = MagicMock()
            mock_health = MagicMock()
            mock_daemon.health_server = mock_health
            mock_daemon.webui_runner = None
            mock_daemon.slot_relay = AsyncMock()
            mock_daemon._segment_client = AsyncMock()
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
    async def test_run_daemon_closes_relay_and_segment_client_on_exit(self):
        """run_daemon should close the relay and its segment client on exit."""
        with patch("quadlink.daemon.Daemon") as MockDaemon:
            mock_daemon = MagicMock()
            mock_daemon.health_server = None
            mock_daemon.webui_runner = None
            mock_daemon.slot_relay = AsyncMock()
            mock_daemon._segment_client = AsyncMock()
            mock_daemon.running = True
            MockDaemon.return_value = mock_daemon

            with patch("asyncio.get_event_loop") as mock_get_loop:
                mock_loop = MagicMock()
                mock_get_loop.return_value = mock_loop

                async def mock_start():
                    raise asyncio.CancelledError()

                mock_daemon.start = mock_start

                await run_daemon()

                mock_daemon.slot_relay.aclose.assert_called_once()
                mock_daemon._segment_client.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_daemon_sets_up_signal_handlers(self):
        """run_daemon should set up SIGINT and SIGTERM handlers."""
        import signal

        with patch("quadlink.daemon.Daemon") as MockDaemon:
            mock_daemon = MagicMock()
            mock_daemon.health_server = MagicMock()
            mock_daemon.webui_runner = None
            mock_daemon.slot_relay = AsyncMock()
            mock_daemon._segment_client = AsyncMock()
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
            mock_daemon.webui_runner = None
            mock_daemon.slot_relay = AsyncMock()
            mock_daemon._segment_client = AsyncMock()
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


class TestDaemonRelayWiring:
    """Tests for slot store / relay integration."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock config."""
        config = MagicMock()
        config.mode = "both"
        config.credentials.username = "user"
        config.credentials.secret = "secret"
        config.webhook.enabled = False
        config.webhook.url = ""
        return config

    def test_init_creates_store_and_relay(self):
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()
        assert daemon.slot_store is not None
        assert daemon.slot_relay.store is daemon.slot_store

    def test_init_gives_relay_a_segment_proxy(self):
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()
        assert daemon.slot_relay._segments is not None

    @pytest.mark.asyncio
    async def test_fetch_segment_returns_the_body_and_raises_on_error(self):
        import httpx

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/gone.ts":
                return httpx.Response(404)
            return httpx.Response(200, content=b"x" * 100_000)

        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()
        await daemon._segment_client.aclose()
        daemon._segment_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await daemon._fetch_segment("http://cdn/a.ts") == b"x" * 100_000
            with pytest.raises(httpx.HTTPStatusError):
                await daemon._fetch_segment("http://cdn/gone.ts")
        finally:
            await daemon._segment_client.aclose()

    @pytest.mark.asyncio
    async def test_fetch_segment_bounds_a_trickling_body(self):
        import httpx

        async def trickle():
            while True:
                yield b"x"
                await asyncio.sleep(0.05)

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=trickle())

        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                daemon = Daemon()
        await daemon._segment_client.aclose()
        daemon._segment_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with patch("quadlink.daemon.SEGMENT_FETCH_WAIT", 0.3):
                with pytest.raises(TimeoutError):
                    await daemon._fetch_segment("http://cdn/slow.ts")
        finally:
            await daemon._segment_client.aclose()

    @pytest.mark.asyncio
    async def test_main_loop_writes_quad_to_slot_store(self, mock_config):
        from quadlink.types import Quad

        with patch("quadlink.daemon.ConfigLoader") as MockLoader:
            with patch("quadlink.daemon.HealthServer"):
                mock_loader = MagicMock()
                mock_loader.load_or_cache = AsyncMock(return_value=mock_config)
                MockLoader.return_value = mock_loader

                daemon = Daemon(one_shot=True)
                daemon.running = True

                quad = Quad(["u1", "u2", "u3", "u4"])

                with patch("quadlink.daemon.StreamProcessor") as MockProc:
                    with patch("quadlink.daemon.compose") as MockCompose:
                        with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                            proc = MagicMock()
                            proc.process_stream_groups = AsyncMock(return_value=[make_stream("c")])
                            MockProc.return_value = proc

                            MockCompose.return_value = composed(
                                quad.slots, ["c1", "c2", "c3", "c4"]
                            )

                            client = AsyncMock()
                            client.login = AsyncMock(return_value=True)
                            client.update_quad = AsyncMock(return_value=True)
                            MockClient.return_value = client

                            with patch("asyncio.sleep", new_callable=AsyncMock):
                                await daemon._main_loop()

        assert daemon.slot_store.get(0).url == "u1"
        assert daemon.slot_store.get(0).identity == "c1"
        assert daemon.slot_store.get(3).url == "u4"
        assert daemon.slot_store.get(3).identity == "c4"

    @pytest.mark.asyncio
    async def test_local_mode_feeds_store_and_skips_quadstream(self, mock_config):

        mock_config.mode = "local"

        with patch("quadlink.daemon.ConfigLoader") as MockLoader:
            with patch("quadlink.daemon.HealthServer"):
                mock_loader = MagicMock()
                mock_loader.load_or_cache = AsyncMock(return_value=mock_config)
                MockLoader.return_value = mock_loader

                daemon = Daemon(one_shot=True)
                daemon.running = True

                with patch("quadlink.daemon.StreamProcessor") as MockProc:
                    with patch("quadlink.daemon.compose") as MockCompose:
                        with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                            proc = MagicMock()
                            proc.process_stream_groups = AsyncMock(return_value=[make_stream("c")])
                            MockProc.return_value = proc

                            MockCompose.return_value = composed(
                                ["u1", "u2", "u3", "u4"], ["c1", "c2", "c3", "c4"]
                            )

                            with patch("asyncio.sleep", new_callable=AsyncMock):
                                await daemon._main_loop()

                            # local mode never touches quadstream
                            MockClient.assert_not_called()

        # but the relay store is still fed
        assert daemon.slot_store.get(0).url == "u1"
        assert daemon.quadstream_client is None

    @pytest.mark.asyncio
    async def test_remote_mode_pushes_quadstream_and_skips_store(self, mock_config):

        mock_config.mode = "remote"

        with patch("quadlink.daemon.ConfigLoader") as MockLoader:
            with patch("quadlink.daemon.HealthServer"):
                mock_loader = MagicMock()
                mock_loader.load_or_cache = AsyncMock(return_value=mock_config)
                MockLoader.return_value = mock_loader

                daemon = Daemon(one_shot=True)
                daemon.running = True

                with patch("quadlink.daemon.StreamProcessor") as MockProc:
                    with patch("quadlink.daemon.compose") as MockCompose:
                        with patch("quadlink.daemon.QuadStreamClient") as MockClient:
                            proc = MagicMock()
                            proc.process_stream_groups = AsyncMock(return_value=[make_stream("c")])
                            MockProc.return_value = proc

                            MockCompose.return_value = composed(
                                ["u1", "u2", "u3", "u4"], ["c1", "c2", "c3", "c4"]
                            )

                            client = AsyncMock()
                            client.login = AsyncMock(return_value=True)
                            client.update_quad = AsyncMock(return_value=True)
                            MockClient.return_value = client

                            with patch("asyncio.sleep", new_callable=AsyncMock):
                                await daemon._main_loop()

                            # remote mode pushes to quadstream
                            client.update_quad.assert_awaited_once()

        # but the relay store is not fed
        assert daemon.slot_store.get(0) is None


class TestDroppedFromPool:
    """Tests for the dropped-channel diagnostic."""

    def test_flags_missing_quad_channels(self):
        prev = {"wallah": 1, "shroud": 0, "grad": 2}
        candidates = [make_stream("shroud"), make_stream("grad")]
        assert Daemon._dropped_from_pool(prev, candidates) == ["wallah"]

    def test_empty_when_all_present_case_insensitive(self):
        prev = {"shroud": 0, "grad": 1}
        candidates = [make_stream("Shroud"), make_stream("grad")]
        assert Daemon._dropped_from_pool(prev, candidates) == []

    def test_empty_when_no_previous_quad(self):
        assert Daemon._dropped_from_pool({}, [make_stream("shroud")]) == []


class TestStillPlaying:
    """Tests for keeping an on-screen channel whose lookup failed but video arrives."""

    @pytest.fixture
    def daemon(self):
        with patch("quadlink.daemon.ConfigLoader"):
            with patch("quadlink.daemon.HealthServer"):
                d = Daemon()
        d.processor = MagicMock()
        d.processor.lookup_failed = MagicMock(return_value=True)
        d.quad_state = composed(["bob", "", "alice"])
        d._on_screen = {"alice": make_stream("alice"), "bob": make_stream("bob")}
        d._refreshed_at = 10.0
        d.slot_relay.video_arrived_since = MagicMock(return_value=True)
        return d

    def test_keeps_failed_lookup_with_video_arriving(self, daemon):
        kept = daemon._still_playing([make_stream("bob")])
        assert [k.stream.metadata.author for k in kept] == ["alice"]
        daemon.slot_relay.video_arrived_since.assert_called_once_with(2, "alice", 10.0)
        daemon.processor.lookup_failed.assert_called_once_with("alice")

    def test_pool_is_keyed_by_login_not_display_name(self, daemon):
        """A renamed display name does not drop the channel from the pool."""
        renamed = make_stream("bob")
        renamed.stream.metadata.author = "Bob The Streamer"
        assert Daemon._dropped_from_pool({"alice": 2, "bob": 0}, [renamed]) == ["alice"]

    def test_drops_channel_rejected_by_filter(self, daemon):
        daemon.processor.lookup_failed.return_value = False
        assert daemon._still_playing([make_stream("bob")]) == []

    def test_drops_channel_without_new_video(self, daemon):
        daemon.slot_relay.video_arrived_since.return_value = False
        assert daemon._still_playing([make_stream("bob")]) == []

    def test_nothing_kept_before_first_feed(self, daemon):
        daemon._refreshed_at = None
        assert daemon._still_playing([make_stream("bob")]) == []


class TestDaemonCycleFixes:
    """One-shot exits, heartbeat, credential reload and missing-config waits."""

    @pytest.fixture
    def config(self):
        config = MagicMock()
        config.mode = "both"
        config.credentials.username = "user"
        config.credentials.secret = "secret"
        config.webhook.enabled = False
        config.model_dump.return_value = {"same": True}
        return config

    @pytest.fixture
    def daemon(self, config):
        with patch("quadlink.daemon.ConfigLoader") as MockLoader:
            with patch("quadlink.daemon.HealthServer") as MockHealth:
                MockLoader.return_value.load_or_cache = AsyncMock(return_value=config)
                MockHealth.return_value = MagicMock()
                daemon = Daemon(one_shot=True, enable_health_server=True)
        daemon.running = True
        return daemon

    @staticmethod
    def client(login: bool = True) -> AsyncMock:
        client = AsyncMock()
        client.login = AsyncMock(return_value=login)
        client.update_quad = AsyncMock(return_value=True)
        return client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["no_candidates", "empty_quad", "login_failure"])
    async def test_one_shot_exits_without_sleeping(self, daemon, outcome):
        processor = MagicMock()
        processor.process_stream_groups = AsyncMock(
            return_value=[] if outcome == "no_candidates" else [make_stream("a")]
        )
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=processor),
            patch("quadlink.daemon.compose", return_value=QuadState()),
            patch(
                "quadlink.daemon.QuadStreamClient",
                return_value=self.client(login=outcome != "login_failure"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock) as sleep,
        ):
            await asyncio.wait_for(daemon._main_loop(), 1)
        assert daemon.running is False
        sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_completed_cycle_marks_heartbeat(self, daemon):
        processor = MagicMock()
        processor.process_stream_groups = AsyncMock(return_value=[])
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=processor),
            patch("quadlink.daemon.compose"),
            patch("quadlink.daemon.QuadStreamClient", return_value=self.client()),
        ):
            await daemon._main_loop()
        daemon.health_server.mark_cycle.assert_called_once()

    def test_heartbeat_window_is_three_intervals(self):
        with patch("quadlink.daemon.ConfigLoader"), patch("quadlink.daemon.HealthServer") as MH:
            Daemon(interval=20, enable_health_server=True)
        MH.assert_called_once_with(max_age=60)

    @pytest.mark.asyncio
    async def test_credential_change_logs_in_again(self, daemon, config):
        processor = MagicMock()
        processor.process_stream_groups = AsyncMock(return_value=[make_stream("a")])
        first, second = self.client(), self.client()
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=processor) as MP,
            patch("quadlink.daemon.compose", return_value=composed(["https://twitch.tv/a"])),
            patch("quadlink.daemon.QuadStreamClient", side_effect=[first, second]) as MC,
        ):
            await daemon._cycle()
            await daemon._cycle()
            assert MC.call_count == 1
            config.credentials.secret = "rotated"
            await daemon._cycle()
        assert MC.call_count == 2
        assert MC.call_args.kwargs == {"username": "user", "secret": "rotated"}
        assert daemon.quadstream_client is second
        # the new account gets the quad although it did not change
        second.update_quad.assert_called_once()
        # credentials are not part of the config hash: no pipeline rebuild
        MP.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_closes_old_processor(self, daemon, config):
        old, new = MagicMock(), MagicMock()
        for p in (old, new):
            p.process_stream_groups = AsyncMock(return_value=[])
        config.mode = "local"
        with (
            patch("quadlink.daemon.StreamProcessor", side_effect=[old, new]),
            patch("quadlink.daemon.compose"),
        ):
            await daemon._cycle()
            config.model_dump.return_value = {"same": False}
            await daemon._cycle()
        old.close.assert_called_once()
        new.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_config_waits_an_interval(self, daemon):
        daemon.one_shot = False
        daemon.config_loader.load_or_cache = AsyncMock(side_effect=ConfigNotFoundError("none"))
        assert await daemon._cycle() == daemon.interval

    @pytest.mark.asyncio
    async def test_missing_config_fails_one_shot(self, daemon):
        daemon.config_loader.load_or_cache = AsyncMock(side_effect=ConfigNotFoundError("none"))
        with pytest.raises(ConfigNotFoundError):
            await daemon._main_loop()

    @pytest.mark.asyncio
    async def test_unexpected_error_fails_one_shot(self, daemon):
        daemon.config_loader.load_or_cache = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await daemon._main_loop()

    @pytest.mark.asyncio
    async def test_webui_starts_without_a_config_file(self):
        with (
            patch("quadlink.daemon.ConfigLoader") as MockLoader,
            patch("quadlink.daemon.WebUI") as MockWebUI,
        ):
            MockLoader.return_value.load_or_cache = AsyncMock(
                side_effect=ConfigNotFoundError("none")
            )
            MockWebUI.return_value.start = AsyncMock()
            daemon = Daemon(enable_webui=True)
            daemon._main_loop = AsyncMock()
            await daemon.start()
        MockWebUI.return_value.start.assert_awaited_once()
        assert MockWebUI.call_args.kwargs["slot_relay"] is daemon.slot_relay
        daemon._main_loop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dropped_channels_log_at_info(self, daemon, config):
        config.mode = "local"
        processor = MagicMock()
        processor.process_stream_groups = AsyncMock(return_value=[make_stream("b")])
        daemon.quad_state = composed(["https://twitch.tv/a"])
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=processor),
            patch("quadlink.daemon.compose", return_value=composed(["https://twitch.tv/b"])),
            patch("quadlink.daemon.logger") as log,
        ):
            await daemon._cycle()
        log.info.assert_any_call(
            "quad channels dropped from live pool", dropped=["a"], candidates=1
        )
        log.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_push_is_retried_next_cycle(self, daemon):
        """A push QuadStream did not accept is sent again although the lineup is unchanged."""
        processor = MagicMock()
        processor.process_stream_groups = AsyncMock(return_value=[make_stream("a")])
        client = self.client()
        client.update_quad = AsyncMock(side_effect=[False, True])
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=processor),
            patch("quadlink.daemon.compose", return_value=composed(["https://twitch.tv/a"])),
            patch("quadlink.daemon.QuadStreamClient", return_value=client),
        ):
            await daemon._cycle()
            await daemon._cycle()
            await daemon._cycle()
        # failed, retried and accepted, then nothing new to send
        assert client.update_quad.await_count == 2


@pytest.mark.asyncio
async def test_config_reload_keeps_slots():
    """The same 4 channels keep their slots across a config change."""
    from quadlink.config.models import Config

    before = Config(mode="local", rulesets=[], priorities={})
    after = Config(mode="local", rulesets=[], priorities={}, diversity_bonus=10)
    channels = ["a", "b", "c", "d"]
    processor = MagicMock()
    processor.lookup_failed = MagicMock(return_value=False)
    processor.process_stream_groups = AsyncMock(
        side_effect=[
            [make_stream(c) for c in channels],
            [make_stream(c) for c in reversed(channels)],
        ]
    )
    with patch("quadlink.daemon.ConfigLoader") as MockLoader:
        MockLoader.return_value.load_or_cache = AsyncMock(side_effect=[before, after])
        daemon = Daemon()
    with patch("quadlink.daemon.StreamProcessor", return_value=processor) as MP:
        await daemon._cycle()
        await daemon._cycle()
    assert MP.call_count == 2
    assert daemon.quad_state.channels == ("a", "b", "c", "d")
    assert [daemon.slot_store.get(i).identity for i in range(4)] == channels
    await daemon.slot_relay.aclose()
    await daemon._segment_client.aclose()


@pytest.mark.asyncio
async def test_cycle_keeps_the_latest_channel_statuses():
    """Each refresh replaces the status list and stamps its time."""
    from quadlink.config.models import Config

    config = Config(mode="local", rulesets=[], priorities={})
    first = [ChannelStatus("a", ChannelState.OFFLINE, [100])]
    second = [ChannelStatus("a", ChannelState.LOOKUP_FAILED, [100], reason="timed out")]
    processor = MagicMock()
    processor.lookup_failed = MagicMock(return_value=False)
    processor.process_stream_groups = AsyncMock(return_value=[])
    processor.statuses = MagicMock(side_effect=[first, second])
    with patch("quadlink.daemon.ConfigLoader") as MockLoader:
        MockLoader.return_value.load_or_cache = AsyncMock(return_value=config)
        daemon = Daemon()
    assert daemon.channel_statuses == []
    assert daemon.statuses_at is None
    with (
        patch("quadlink.daemon.StreamProcessor", return_value=processor),
        patch("quadlink.daemon.time.time", side_effect=[1000.0, 1030.0]),
    ):
        await daemon._cycle()
        assert daemon.channel_statuses is first
        assert daemon.statuses_at == 1000.0
        await daemon._cycle()
    assert daemon.channel_statuses is second
    assert daemon.statuses_at == 1030.0
    await daemon.slot_relay.aclose()
    await daemon._segment_client.aclose()


class TestRefreshRecompose:
    """The refresh loop looks channels up; the recompose loop composes and pushes."""

    @staticmethod
    def config(mode: str = "local", **kwargs):
        from quadlink.config.models import Config, Credentials

        credentials = Credentials(username="user", secret="secret") if mode != "local" else None
        return Config(mode=mode, credentials=credentials, rulesets=[], priorities={}, **kwargs)

    @staticmethod
    def processor(channels=("a", "b", "c", "d")) -> MagicMock:
        processor = MagicMock()
        processor.lookup_failed = MagicMock(return_value=False)
        processor.statuses = MagicMock(return_value=[])
        processor.process_stream_groups = AsyncMock(
            side_effect=lambda: [make_stream(c) for c in channels]
        )
        return processor

    @pytest.fixture
    async def make_daemon(self):
        made = []

        def make(config, one_shot: bool = False) -> Daemon:
            with patch("quadlink.daemon.ConfigLoader") as MockLoader:
                MockLoader.return_value.load_or_cache = AsyncMock(return_value=config)
                daemon = Daemon(one_shot=one_shot)
            daemon.running = True
            made.append(daemon)
            return daemon

        yield make
        for daemon in made:
            await daemon.slot_relay.aclose()
            await daemon._segment_client.aclose()

    @staticmethod
    def count_recomposes(daemon: Daemon) -> dict:
        """Wrap daemon._recompose to count calls and the most running at once."""
        counts = {"calls": 0, "running": 0, "peak": 0}
        original = daemon._recompose

        async def counted() -> None:
            counts["calls"] += 1
            counts["running"] += 1
            counts["peak"] = max(counts["peak"], counts["running"])
            try:
                await original()
            finally:
                counts["running"] -= 1

        daemon._recompose = counted
        return counts

    @staticmethod
    async def settle() -> None:
        for _ in range(10):
            await asyncio.sleep(0)

    @staticmethod
    def recompose_loops() -> list[asyncio.Task]:
        return [
            t
            for t in asyncio.all_tasks()
            if t.get_coro().__qualname__ == "Daemon._recompose_loop" and not t.done()
        ]

    @pytest.mark.asyncio
    async def test_refresh_triggers_exactly_one_recompose(self, make_daemon):
        daemon = make_daemon(self.config())
        counts = self.count_recomposes(daemon)
        with patch("quadlink.daemon.StreamProcessor", return_value=self.processor()):
            loop = asyncio.create_task(daemon._recompose_loop())
            await daemon._refresh()
            await self.settle()
            loop.cancel()
        assert counts["calls"] == 1
        assert daemon.quad_state.channels == ("a", "b", "c", "d")
        assert daemon.slot_store.get(0).identity == "a"

    @pytest.mark.asyncio
    async def test_request_recompose_composes_without_a_lookup(self, make_daemon):
        from quadlink.quad import compose

        daemon = make_daemon(self.config())
        processor = self.processor()
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=processor),
            patch("quadlink.daemon.compose", side_effect=compose) as composer,
        ):
            loop = asyncio.create_task(daemon._recompose_loop())
            await daemon._refresh()
            await self.settle()
            daemon.request_recompose()
            await self.settle()
            loop.cancel()
        assert composer.call_count == 2
        assert processor.process_stream_groups.await_count == 1
        # the cached pool fed the second composition
        assert composer.call_args.args[0] is daemon._pool
        assert daemon.quad_state.channels == ("a", "b", "c", "d")

    @pytest.mark.asyncio
    async def test_recompose_never_runs_concurrently_with_itself(self, make_daemon):
        daemon = make_daemon(self.config("remote"))
        counts = self.count_recomposes(daemon)
        gate = asyncio.Event()
        client = AsyncMock()
        client.login = AsyncMock(return_value=True)

        async def slow_push(quad):
            await gate.wait()
            return True

        client.update_quad = AsyncMock(side_effect=slow_push)
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=self.processor()),
            patch("quadlink.daemon.QuadStreamClient", return_value=client),
        ):
            loop = asyncio.create_task(daemon._recompose_loop())
            await daemon._refresh()
            await self.settle()
            assert counts["running"] == 1
            # a refresh and two web actions while the push hangs
            await daemon._refresh()
            daemon.request_recompose()
            daemon.request_recompose()
            await self.settle()
            gate.set()
            await self.settle()
            loop.cancel()
        assert counts["peak"] == 1
        # the requests made during the push are served by one more recompose
        assert counts["calls"] == 2
        # which found the lineup already accepted
        assert client.update_quad.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_push_is_retried_after_the_next_refresh(self, make_daemon):
        daemon = make_daemon(self.config("remote"))
        client = AsyncMock()
        client.login = AsyncMock(return_value=True)
        client.update_quad = AsyncMock(side_effect=[False, True])
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=self.processor()),
            patch("quadlink.daemon.QuadStreamClient", return_value=client),
        ):
            loop = asyncio.create_task(daemon._recompose_loop())
            for _ in range(3):
                await daemon._refresh()
                await self.settle()
            loop.cancel()
        # failed, sent again after the next refresh and accepted, then nothing new
        assert client.update_quad.await_count == 2
        assert daemon._pushed == ("a", "b", "c", "d")

    @pytest.mark.asyncio
    async def test_one_shot_refreshes_and_recomposes_once(self, make_daemon):
        daemon = make_daemon(self.config(), one_shot=True)
        processor = self.processor()
        counts = self.count_recomposes(daemon)
        with patch("quadlink.daemon.StreamProcessor", return_value=processor):
            await asyncio.wait_for(daemon._main_loop(), 1)
        assert daemon.running is False
        assert processor.process_stream_groups.await_count == 1
        assert counts["calls"] == 1
        assert daemon.slot_store.get(3).identity == "d"
        assert self.recompose_loops() == []

    @pytest.mark.asyncio
    async def test_config_change_during_a_refresh_applies_on_the_next(self, make_daemon):
        before = self.config()
        after = self.config(diversity_bonus=10)
        daemon = make_daemon(before)
        gate = asyncio.Event()
        first = self.processor()
        lookup = first.process_stream_groups.side_effect

        async def slow_lookup():
            await gate.wait()
            return lookup()

        first.process_stream_groups = AsyncMock(side_effect=slow_lookup)
        second = self.processor(channels=("d", "c", "b", "a"))
        with patch("quadlink.daemon.StreamProcessor", side_effect=[first, second]) as MP:
            refresh = asyncio.create_task(daemon._cycle())
            await self.settle()
            daemon.config_loader.load_or_cache = AsyncMock(return_value=after)
            gate.set()
            await refresh
            # the refresh in progress finished with the config it started with
            assert daemon._config is before
            assert MP.call_count == 1
            await daemon._cycle()
        assert daemon._config is after
        assert MP.call_count == 2
        first.close.assert_called_once()
        # the reload kept every channel in its slot
        assert daemon.quad_state.channels == ("a", "b", "c", "d")

    @pytest.mark.asyncio
    async def test_cancel_stops_both_loops(self, make_daemon):
        daemon = make_daemon(self.config())
        daemon.interval = 0.01
        with patch("quadlink.daemon.StreamProcessor", return_value=self.processor()):
            task = asyncio.create_task(daemon._main_loop())
            while daemon.slot_store.get(0) is None:
                await asyncio.sleep(0.005)
            assert len(self.recompose_loops()) == 1
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert task.done()
        assert self.recompose_loops() == []

    @pytest.mark.asyncio
    async def test_stopping_ends_both_loops(self, make_daemon):
        daemon = make_daemon(self.config())
        daemon.interval = 0.01
        with patch("quadlink.daemon.StreamProcessor", return_value=self.processor()):
            task = asyncio.create_task(daemon._main_loop())
            while daemon.slot_store.get(0) is None:
                await asyncio.sleep(0.005)
            daemon.running = False
            await asyncio.wait_for(task, 1)
        assert self.recompose_loops() == []

    @pytest.mark.asyncio
    async def test_recompose_error_keeps_the_loop_running(self, make_daemon):
        daemon = make_daemon(self.config())
        with (
            patch("quadlink.daemon.StreamProcessor", return_value=self.processor()),
            patch("quadlink.daemon.compose", side_effect=[RuntimeError("boom"), QuadState()]),
        ):
            loop = asyncio.create_task(daemon._recompose_loop())
            await daemon._refresh()
            await self.settle()
            daemon.request_recompose()
            await self.settle()
            assert not loop.done()
            loop.cancel()
        assert daemon.quad_state == QuadState()
