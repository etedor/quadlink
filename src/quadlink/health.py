"""Health check HTTP server for monitoring."""

import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any

import structlog

logger = structlog.get_logger()


class _HealthHTTPServer(HTTPServer):
    health: "HealthServer"


class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP request handler for health and readiness checks."""

    server: _HealthHTTPServer

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
        """Handle /ready endpoint - 200 if a daemon cycle completed recently, 503 otherwise."""
        if self.server.health.is_ready():
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

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, max_age: float = 90.0):
        """Initialize health server.

        Args:
            host: Host address to bind to.
            port: Port number to listen on.
            max_age: Seconds after the last completed cycle before /ready fails.
        """
        self.host = host
        self.port = port
        self.max_age = max_age
        # monotonic time of the last completed cycle; written by the event loop,
        # read by the server thread (a float assignment is atomic)
        self.last_cycle: float | None = None
        self.server: _HealthHTTPServer | None = None
        self.thread: Thread | None = None

    def start(self) -> None:
        """Start the health server in a background thread."""
        if self.server is not None:
            logger.warning("health server already running")
            return

        self.server = _HealthHTTPServer((self.host, self.port), HealthCheckHandler)
        self.server.health = self
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

    def mark_cycle(self) -> None:
        """Record that a daemon cycle completed now."""
        self.last_cycle = time.monotonic()

    def is_ready(self) -> bool:
        """Whether a daemon cycle completed within max_age seconds."""
        return self.last_cycle is not None and time.monotonic() - self.last_cycle <= self.max_age
