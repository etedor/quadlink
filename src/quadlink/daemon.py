"""Main daemon loop for stream monitoring and quad selection."""

import asyncio
import signal
from collections.abc import Callable

import structlog
from aiohttp import web

from quadlink.config.loader import ConfigLoader
from quadlink.health import HealthServer
from quadlink.quad import QuadBuilder
from quadlink.quadstream import QuadStreamClient
from quadlink.stream.processor import StreamProcessor
from quadlink.webui import WebUI

logger = structlog.get_logger()


class Daemon:
    """Main daemon for QuadLink stream curation.

    Orchestrates config loading, stream processing, quad building,
    and QuadStream API updates in a continuous loop.

    Attributes:
        interval: Seconds between quad update cycles.
        one_shot: If True, run once and exit instead of looping.
    """

    def __init__(
        self,
        interval: int = 30,
        one_shot: bool = False,
        enable_health_server: bool = False,
        config_path: str | None = None,
        enable_webui: bool = False,
        webui_host: str = "0.0.0.0",
        webui_port: int = 8081,
    ):
        """Initialize daemon.

        Args:
            interval: Seconds between quad updates.
            one_shot: If True, run once and exit.
            enable_health_server: If True, start health check server on port 8080.
            config_path: Optional explicit path to config file (default: auto-discover).
            enable_webui: If True, start web UI for config editing.
            webui_host: Host address for web UI server.
            webui_port: Port for web UI server.
        """
        self.interval = interval
        self.one_shot = one_shot
        self.enable_health_server = enable_health_server
        self.enable_webui = enable_webui
        self.webui_host = webui_host
        self.webui_port = webui_port
        self.config_loader = ConfigLoader(explicit_path=config_path)
        self.health_server = HealthServer() if enable_health_server else None
        self.webui: WebUI | None = None
        self.webui_runner: web.AppRunner | None = None
        self.running = False
        self.processor: StreamProcessor | None = None
        self.quad_builder: QuadBuilder | None = None
        self.quadstream_client: QuadStreamClient | None = None
        self._config_hash: int | None = None

    async def start(self) -> None:
        """Start the daemon and optional health/webui servers."""
        if self.health_server:
            self.health_server.start()
        if self.enable_webui:
            self.webui = WebUI(self.config_loader, host=self.webui_host, port=self.webui_port)
            self.webui_runner = await self.webui.start()
        self.running = True
        try:
            await self._main_loop()
        except asyncio.CancelledError:
            raise

    async def _main_loop(self) -> None:
        """Main daemon loop - processes streams and updates quad repeatedly."""
        while self.running:
            try:
                config = await self.config_loader.load_or_cache()

                if not config:
                    logger.error("failed to load config, retrying in 30s")
                    await asyncio.sleep(30)
                    continue

                if self.health_server:
                    self.health_server.mark_ready()

                # detect config changes and recreate components
                config_hash = hash(str(config.model_dump(exclude={"credentials"})))
                if self._config_hash != config_hash:
                    if self._config_hash is not None:
                        logger.info("config changed, reloading")
                    self._config_hash = config_hash
                    self.processor = StreamProcessor(config)
                    self.quad_builder = QuadBuilder(config)

                assert self.processor is not None
                assert self.quad_builder is not None

                if not self.quadstream_client:
                    assert config.credentials.username is not None
                    assert config.credentials.secret is not None
                    self.quadstream_client = QuadStreamClient(
                        username=config.credentials.username,
                        secret=config.credentials.secret,
                    )

                    login_success = await self.quadstream_client.login()
                    if not login_success:
                        logger.error("quadstream login failed, retrying in 30s")
                        self.quadstream_client = None
                        await asyncio.sleep(30)
                        continue

                candidates = await self.processor.process_stream_groups()

                if not candidates:
                    logger.info("no stream candidates available")
                    await asyncio.sleep(self.interval)
                    continue

                quad = self.quad_builder.build_quad(candidates)

                if quad.is_empty():
                    logger.info("quad is empty, skipping update")
                    await asyncio.sleep(self.interval)
                    continue

                # only send updates if quad changed
                if self.quad_builder.quad_changed:
                    update_success = await self.quadstream_client.update_quad(quad)

                    if not update_success:
                        logger.error("quadstream update failed")
                    elif config.webhook.enabled and config.webhook.url:
                        await self.quadstream_client.send_webhook(config.webhook.url, quad)

                if self.one_shot:
                    self.running = False
                    break

                logger.debug("sleeping", interval=self.interval)
                await asyncio.sleep(self.interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "unexpected error in main loop",
                    error=str(e),
                    exc_info=True,
                )
                await asyncio.sleep(30)


async def run_daemon(
    one_shot: bool = False,
    interval: int = 30,
    enable_health_server: bool = False,
    config_path: str | None = None,
    enable_webui: bool = False,
    webui_host: str = "0.0.0.0",
    webui_port: int = 8081,
) -> None:
    """Entry point for running the daemon.

    Sets up signal handlers for graceful shutdown and runs the main loop.

    Args:
        one_shot: If True, run once and exit.
        interval: Seconds between quad updates.
        enable_health_server: If True, start health check server on port 8080.
        config_path: Optional explicit path to config file (default: auto-discover).
        enable_webui: If True, start web UI for config editing.
        webui_host: Host address for web UI server.
        webui_port: Port for web UI server.
    """
    daemon = Daemon(
        interval=interval,
        one_shot=one_shot,
        enable_health_server=enable_health_server,
        config_path=config_path,
        enable_webui=enable_webui,
        webui_host=webui_host,
        webui_port=webui_port,
    )
    task = asyncio.create_task(daemon.start())

    def handle_shutdown(sig: signal.Signals) -> None:
        logger.info("shutting down", signal=sig.name)
        daemon.running = False
        if daemon.health_server:
            daemon.health_server.stop()
        task.cancel()

    def make_handler(sig: signal.Signals) -> Callable[[], None]:
        return lambda: handle_shutdown(sig)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, make_handler(sig))

    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if daemon.health_server:
            daemon.health_server.stop()
        if daemon.webui_runner:
            await daemon.webui_runner.cleanup()
