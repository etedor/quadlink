"""Tests for health check server."""

import socket
import urllib.error
import urllib.request
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from quadlink.health import HealthCheckHandler, HealthServer


def get_free_port() -> int:
    """Get a free port for testing."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class TestHealthCheckHandler:
    """Tests for HealthCheckHandler."""

    @pytest.fixture(autouse=True)
    def reset_config_loaded(self):
        """Reset config_loaded between tests."""
        HealthCheckHandler.config_loaded = False
        yield
        HealthCheckHandler.config_loaded = False

    def test_log_message_suppressed(self):
        """log_message should not produce output."""
        # create a mock handler without a real socket
        handler = MagicMock(spec=HealthCheckHandler)
        # call the actual log_message method
        HealthCheckHandler.log_message(handler, "%s", "test message")
        # no exception means it worked (it's a no-op)

    def test_do_get_health(self):
        """GET /health should call _health_check."""
        handler = MagicMock(spec=HealthCheckHandler)
        handler.path = "/health"
        handler.wfile = BytesIO()

        HealthCheckHandler.do_GET(handler)

        handler._health_check.assert_called_once()

    def test_do_get_ready(self):
        """GET /ready should call _readiness_check."""
        handler = MagicMock(spec=HealthCheckHandler)
        handler.path = "/ready"
        handler.wfile = BytesIO()

        HealthCheckHandler.do_GET(handler)

        handler._readiness_check.assert_called_once()

    def test_do_get_unknown_path(self):
        """GET unknown path should return 404."""
        handler = MagicMock(spec=HealthCheckHandler)
        handler.path = "/unknown"

        HealthCheckHandler.do_GET(handler)

        handler.send_response.assert_called_once_with(404)
        handler.end_headers.assert_called_once()

    def test_health_check_returns_ok(self):
        """_health_check should return 200 OK."""
        handler = MagicMock(spec=HealthCheckHandler)
        handler.wfile = BytesIO()

        HealthCheckHandler._health_check(handler)

        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_called_once_with("Content-Type", "text/plain")
        handler.end_headers.assert_called_once()
        assert handler.wfile.getvalue() == b"OK"

    def test_readiness_check_ready(self):
        """_readiness_check should return 200 READY when config loaded."""
        HealthCheckHandler.config_loaded = True
        handler = MagicMock(spec=HealthCheckHandler)
        handler.wfile = BytesIO()

        HealthCheckHandler._readiness_check(handler)

        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_called_once_with("Content-Type", "text/plain")
        handler.end_headers.assert_called_once()
        assert handler.wfile.getvalue() == b"READY"

    def test_readiness_check_not_ready(self):
        """_readiness_check should return 503 NOT READY when config not loaded."""
        HealthCheckHandler.config_loaded = False
        handler = MagicMock(spec=HealthCheckHandler)
        handler.wfile = BytesIO()

        HealthCheckHandler._readiness_check(handler)

        handler.send_response.assert_called_once_with(503)
        handler.send_header.assert_called_once_with("Content-Type", "text/plain")
        handler.end_headers.assert_called_once()
        assert handler.wfile.getvalue() == b"NOT READY"


class TestHealthServerInit:
    """Tests for HealthServer initialization."""

    def test_default_initialization(self):
        """Should initialize with default host and port."""
        server = HealthServer()

        assert server.host == "0.0.0.0"
        assert server.port == 8080
        assert server.server is None
        assert server.thread is None

    def test_custom_host_port(self):
        """Should accept custom host and port."""
        server = HealthServer(host="127.0.0.1", port=9090)

        assert server.host == "127.0.0.1"
        assert server.port == 9090


class TestHealthServerStartStop:
    """Tests for HealthServer start/stop."""

    @pytest.fixture(autouse=True)
    def reset_config_loaded(self):
        """Reset config_loaded between tests."""
        HealthCheckHandler.config_loaded = False
        yield
        HealthCheckHandler.config_loaded = False

    def test_start_creates_server(self):
        """start() should create and start server thread."""
        port = get_free_port()
        server = HealthServer(host="127.0.0.1", port=port)

        try:
            server.start()

            assert server.server is not None
            assert server.thread is not None
            assert server.thread.is_alive()
        finally:
            server.stop()

    def test_start_already_running(self):
        """start() when already running should log warning."""
        port = get_free_port()
        server = HealthServer(host="127.0.0.1", port=port)

        try:
            server.start()

            with patch("quadlink.health.logger") as mock_logger:
                server.start()  # try to start again
                mock_logger.warning.assert_called_once_with("health server already running")

        finally:
            server.stop()

    def test_stop_when_not_running(self):
        """stop() when not running should do nothing."""
        server = HealthServer()
        # should not raise
        server.stop()
        assert server.server is None

    def test_stop_shuts_down_server(self):
        """stop() should shut down server and thread."""
        port = get_free_port()
        server = HealthServer(host="127.0.0.1", port=port)

        server.start()
        assert server.thread.is_alive()

        server.stop()

        assert server.server is None
        assert server.thread is None


class TestHealthServerReadiness:
    """Tests for HealthServer readiness methods."""

    @pytest.fixture(autouse=True)
    def reset_config_loaded(self):
        """Reset config_loaded between tests."""
        HealthCheckHandler.config_loaded = False
        yield
        HealthCheckHandler.config_loaded = False

    def test_mark_ready(self):
        """mark_ready() should set config_loaded to True."""
        server = HealthServer()

        assert HealthCheckHandler.config_loaded is False
        server.mark_ready()
        assert HealthCheckHandler.config_loaded is True

    def test_mark_not_ready(self):
        """mark_not_ready() should set config_loaded to False."""
        HealthCheckHandler.config_loaded = True
        server = HealthServer()

        server.mark_not_ready()
        assert HealthCheckHandler.config_loaded is False


class TestHealthServerIntegration:
    """Integration tests with actual HTTP requests."""

    @pytest.fixture(autouse=True)
    def reset_config_loaded(self):
        """Reset config_loaded between tests."""
        HealthCheckHandler.config_loaded = False
        yield
        HealthCheckHandler.config_loaded = False

    @pytest.fixture
    def running_server(self):
        """Create a running health server."""
        port = get_free_port()
        server = HealthServer(host="127.0.0.1", port=port)
        server.start()
        yield server, port
        server.stop()

    def test_health_endpoint(self, running_server):
        """GET /health should return 200 OK."""
        server, port = running_server

        response = urllib.request.urlopen(f"http://127.0.0.1:{port}/health")

        assert response.status == 200
        assert response.read() == b"OK"

    def test_ready_endpoint_not_ready(self, running_server):
        """GET /ready should return 503 when not ready."""
        server, port = running_server

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ready")

        assert exc_info.value.code == 503

    def test_ready_endpoint_ready(self, running_server):
        """GET /ready should return 200 when ready."""
        server, port = running_server
        server.mark_ready()

        response = urllib.request.urlopen(f"http://127.0.0.1:{port}/ready")

        assert response.status == 200
        assert response.read() == b"READY"

    def test_unknown_endpoint(self, running_server):
        """GET unknown path should return 404."""
        server, port = running_server

        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/unknown")

        assert exc_info.value.code == 404

    def test_ready_toggle(self, running_server):
        """Should be able to toggle ready state."""
        server, port = running_server

        # initially not ready
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ready")
        assert exc_info.value.code == 503

        # mark ready
        server.mark_ready()
        response = urllib.request.urlopen(f"http://127.0.0.1:{port}/ready")
        assert response.status == 200

        # mark not ready again
        server.mark_not_ready()
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ready")
        assert exc_info.value.code == 503
