"""Main daemon loop for stream monitoring and quad selection."""

import asyncio
import shutil
import signal
import time
from collections.abc import Callable

import httpx
import structlog
from aiohttp import web

from quadlink.config.loader import ConfigLoader, ConfigNotFoundError
from quadlink.health import HealthServer
from quadlink.quad import Overrides, QuadState, compose
from quadlink.quadstream import QuadStreamClient
from quadlink.relay.segments import SegmentProxy
from quadlink.relay.server import SlotRelay
from quadlink.relay.store import SlotStore
from quadlink.stream.processor import StreamProcessor
from quadlink.types import ChannelStatus, PrioritizedStream
from quadlink.webui import WebUI

logger = structlog.get_logger()

# seconds one segment fetch may take in all; httpx's timeout bounds each read only,
# so a trickling origin could hold the slot's worker, and every tile behind it, for longer
SEGMENT_FETCH_WAIT = 8.0


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
        webui_allowed_hosts: list[str] | None = None,
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
            webui_allowed_hosts: Host names beyond loopback the web UI answers to.
        """
        self.interval = interval
        self.one_shot = one_shot
        self.enable_health_server = enable_health_server
        self.enable_webui = enable_webui
        self.webui_host = webui_host
        self.webui_port = webui_port
        self.webui_allowed_hosts = webui_allowed_hosts or []
        self.config_loader = ConfigLoader(explicit_path=config_path)
        self.health_server = HealthServer(max_age=3 * interval) if enable_health_server else None
        self.slot_store = SlotStore()
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            logger.warning("relay ffmpeg not found, audio passes through")
        # a hung fetch holds the slot's serial fetch queue; segments arrive every 2s
        self._segment_client = httpx.AsyncClient(timeout=5)
        self.slot_relay = SlotRelay(
            self.slot_store,
            segments=SegmentProxy(self._fetch_segment, ffmpeg),
        )
        self.webui: WebUI | None = None
        self.webui_runner: web.AppRunner | None = None
        self.running = False
        self.processor: StreamProcessor | None = None
        # survives config reloads, so a save in the web UI does not reshuffle tiles
        self.quad_state = QuadState()
        self.overrides = Overrides()
        # channels per slot of the last quad QuadStream accepted; None until the
        # current login has accepted one
        self._pushed: tuple[str, ...] | None = None
        self.quadstream_client: QuadStreamClient | None = None
        self._config_hash: int | None = None
        # (username, secret) the current QuadStream client logged in with
        self._credentials: tuple[str | None, str | None] | None = None
        # channel -> its last-cycle candidate (raw priority), for channels on screen
        self._on_screen: dict[str, PrioritizedStream] = {}
        # relay clock when the slot store was last fed
        self._fed_at: float | None = None
        # every configured channel's outcome in the last refresh
        self.channel_statuses: list[ChannelStatus] = []
        # wall-clock time (epoch seconds) of that refresh; None before the first
        self.statuses_at: float | None = None

    async def _fetch_segment(self, uri: str) -> bytes:
        """Fetch one relay segment from its origin URI."""
        # streamed: resp.content would keep the body on a Response that sits in a
        # reference cycle until the cyclic GC runs, holding up to ~120 MiB of old segments
        async with (
            asyncio.timeout(SEGMENT_FETCH_WAIT),
            self._segment_client.stream("GET", uri) as resp,
        ):
            resp.raise_for_status()
            return b"".join([chunk async for chunk in resp.aiter_bytes()])

    async def start(self) -> None:
        """Start the daemon and optional health/webui servers."""
        if self.health_server:
            self.health_server.start()
        if self.enable_webui:
            # register /streams routes only when the local relay is served; with no
            # config yet the web UI still starts, so the user can write one
            try:
                mode = (await self.config_loader.load_or_cache()).mode
            except ConfigNotFoundError:
                mode = "both"
            self.webui = WebUI(
                self.config_loader,
                host=self.webui_host,
                port=self.webui_port,
                allowed_hosts=self.webui_allowed_hosts,
                slot_relay=self.slot_relay if mode in ("both", "local") else None,
            )
            self.webui_runner = await self.webui.start()
        self.running = True
        await self._main_loop()

    @staticmethod
    def _dropped_from_pool(
        previous_positions: dict[str, int],
        candidates: list[PrioritizedStream],
    ) -> list[str]:
        """Quad channels no longer in the live candidate pool.

        Offline/unresolved streams drop out silently, so surfacing this makes a
        "channel disappeared" event visible instead of looking like a re-rank.
        """
        live = {c.stream.channel for c in candidates}
        return sorted(c for c in previous_positions if c not in live)

    def _still_playing(self, candidates: list[PrioritizedStream]) -> list[PrioritizedStream]:
        """On-screen channels whose lookup failed but whose video still arrives.

        A failed lookup is weak evidence (it also fails on a transient Twitch or
        proxy error); new segments reaching the relay since the last cycle prove
        the channel is live, so it keeps its slot instead of swapping out and back.
        A channel that resolved but a filter rejected is not kept.
        """
        assert self.processor is not None
        if self._fed_at is None:
            return []
        positions = self.quad_state.positions
        kept = []
        for channel in self._dropped_from_pool(positions, candidates):
            previous = self._on_screen.get(channel)
            if previous is None or not self.processor.lookup_failed(channel):
                continue
            slot = positions[channel]
            if self.slot_relay.video_arrived_since(slot, channel, self._fed_at):
                logger.info(
                    "kept channel: lookup failed but video still arriving",
                    channel=channel,
                    slot=slot,
                )
                kept.append(previous)
        return kept

    async def _main_loop(self) -> None:
        """Main daemon loop - processes streams and updates quad repeatedly."""
        while self.running:
            try:
                delay = await self._cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.one_shot:
                    raise
                logger.error(
                    "unexpected error in main loop",
                    error=str(e),
                    exc_info=True,
                )
                delay = 30
            if self.one_shot:
                self.running = False
                break
            logger.debug("sleeping", interval=delay)
            await asyncio.sleep(delay)

    async def _cycle(self) -> float:
        """Run one cycle; return the seconds to wait before the next."""
        try:
            config = await self.config_loader.load_or_cache()
        except ConfigNotFoundError as e:
            # one-shot has nothing to wait for; a daemon waits for the web UI or an operator
            if self.one_shot:
                raise
            logger.warning("waiting for a config file", error=str(e))
            return self.interval

        # detect config changes and recreate components
        config_hash = hash(str(config.model_dump(exclude={"credentials"})))
        if self._config_hash != config_hash:
            if self._config_hash is not None:
                logger.info("config changed, reloading")
            self._config_hash = config_hash
            if self.processor is not None:
                self.processor.close()
            # a lookup slower than one cycle is useless: the next cycle is already due
            self.processor = StreamProcessor(config, lookup_timeout=self.interval)

        assert self.processor is not None

        push_remote = config.mode in ("both", "remote")
        serve_local = config.mode in ("both", "local")
        self.slot_relay.proxy_segments = config.relay_segments

        credentials = (
            (config.credentials.username, config.credentials.secret) if config.credentials else None
        )
        if self.quadstream_client and credentials != self._credentials:
            logger.info("quadstream credentials changed, logging in again")
            self.quadstream_client = None

        # log in to QuadStream only when we push to it (skipped in local)
        if push_remote and not self.quadstream_client:
            assert credentials is not None
            username, secret = credentials
            assert username is not None
            assert secret is not None
            client = QuadStreamClient(username=username, secret=secret)
            if not await client.login():
                logger.error("quadstream login failed, retrying in 30s")
                return 30
            self.quadstream_client = client
            self._credentials = credentials
            # a new login may be another account, which has not seen any quad
            self._pushed = None

        candidates = await self.processor.process_stream_groups()
        self.channel_statuses = self.processor.statuses()
        self.statuses_at = time.time()
        if serve_local:
            candidates += self._still_playing(candidates)

        if not candidates:
            logger.info("no stream candidates available")
            self._mark_cycle()
            return self.interval

        # surface quad channels that fell out of the live pool this
        # cycle (offline/unresolved), which otherwise looks like a re-rank
        dropped = self._dropped_from_pool(self.quad_state.positions, candidates)
        if dropped:
            logger.info(
                "quad channels dropped from live pool",
                dropped=dropped,
                candidates=len(candidates),
            )

        self.quad_state = state = compose(candidates, self.quad_state, self.overrides, config)
        quad = state.quad
        self._on_screen = {}
        for c in candidates:
            if c.stream.channel in state.positions:
                self._on_screen.setdefault(c.stream.channel, c)

        # feed the local relay every cycle (both/local modes)
        if serve_local:
            self.slot_store.update(quad.to_list(), list(state.channels))
            self._fed_at = self.slot_relay.clock()

        if quad.is_empty():
            logger.info("quad is empty, skipping update")
            self._mark_cycle()
            return self.interval

        # push until QuadStream holds this lineup (both/remote modes); compared by
        # channel, since every refresh re-mints the playlist URLs
        if push_remote and state.channels != self._pushed:
            assert self.quadstream_client is not None
            if not await self.quadstream_client.update_quad(quad):
                logger.error("quadstream update failed")
            else:
                self._pushed = state.channels
                if config.webhook.enabled and config.webhook.url:
                    await self.quadstream_client.send_webhook(config.webhook.url, quad)

        self._mark_cycle()
        return self.interval

    def _mark_cycle(self) -> None:
        if self.health_server:
            self.health_server.mark_cycle()


async def run_daemon(
    one_shot: bool = False,
    interval: int = 30,
    enable_health_server: bool = False,
    config_path: str | None = None,
    enable_webui: bool = False,
    webui_host: str = "0.0.0.0",
    webui_port: int = 8081,
    webui_allowed_hosts: list[str] | None = None,
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
        webui_allowed_hosts: Host names beyond loopback the web UI answers to.
    """
    daemon = Daemon(
        interval=interval,
        one_shot=one_shot,
        enable_health_server=enable_health_server,
        config_path=config_path,
        enable_webui=enable_webui,
        webui_host=webui_host,
        webui_port=webui_port,
        webui_allowed_hosts=webui_allowed_hosts,
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
        if daemon.processor:
            daemon.processor.close()
        await daemon.slot_relay.aclose()
        await daemon._segment_client.aclose()
