"""Health check HTTP server for monitoring."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import structlog

logger = structlog.get_logger()


class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health and readiness checks.

    Attributes:
        config_loaded: Class variable tracking if config has been loaded.
    """

    config_loaded = False

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default HTTP request logging (too noisy for health checks)."""
        pass

    def do_GET(self) -> None:
        """Route GET requests to health or readiness endpoints."""
        if self.path == "/health":
            self._health_check()
        elif self.path == "/ready":
            self._readiness_check()
        else:
            self.send_response(404)
            self.end_headers()

    def _health_check(self) -> None:
        """Handle /health endpoint - always returns 200 OK.

        Indicates the process is running regardless of readiness state.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def _readiness_check(self) -> None:
        """Handle /ready endpoint - returns 200 if config loaded, 503 otherwise.

        Indicates whether the daemon is ready to process streams.
        """
        if HealthCheckHandler.config_loaded:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"READY")
        else:
            self.send_response(503)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"NOT READY")


class HealthServer:
    """HTTP server for health and readiness checks.

    Runs in a background thread to avoid blocking the main async loop.

    Attributes:
        host: Host address to bind to.
        port: Port number to listen on.
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        """Initialize health server.

        Args:
            host: Host address to bind to.
            port: Port number to listen on.
        """
        self.host = host
        self.port = port
        self.server: HTTPServer | None = None
        self.thread: Thread | None = None

    def start(self) -> None:
        """Start the health server in a background thread."""
        if self.server is not None:
            logger.warning("health server already running")
            return

        self.server = HTTPServer((self.host, self.port), HealthCheckHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        logger.debug("health server started", host=self.host, port=self.port)

    def stop(self) -> None:
        """Stop the health server and wait for thread to finish."""
        if self.server is None:
            return

        self.server.shutdown()
        self.server.server_close()

        if self.thread:
            self.thread.join(timeout=5)

        self.server = None
        self.thread = None

    def mark_ready(self) -> None:
        """Mark the daemon as ready (config loaded successfully)."""
        HealthCheckHandler.config_loaded = True

    def mark_not_ready(self) -> None:
        """Mark the daemon as not ready (config not loaded)."""
        HealthCheckHandler.config_loaded = False
        logger.debug("health server marked not ready")
