"""Main daemon loop for stream monitoring and quad selection."""

import asyncio
import os
import shutil
import signal
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import structlog
from aiohttp import web

from quadlink import overrides as actions
from quadlink.config.loader import ConfigLoader, ConfigNotFoundError
from quadlink.config.models import Config
from quadlink.dashboard import QuadStreamLogin, Snapshot, snapshot
from quadlink.explore import Directory, GQLDirectory
from quadlink.health import HealthServer
from quadlink.logconfig import apply_config_level
from quadlink.overrides import OverrideError
from quadlink.quad import Overrides, QuadState, compose
from quadlink.quadstream import QuadStreamClient
from quadlink.relay.segments import SegmentProxy
from quadlink.relay.server import SlotRelay
from quadlink.relay.store import SlotStore
from quadlink.state import DEFAULT_STATE_PATH, StateStore
from quadlink.stream.processor import EXTRA_PRIORITY, StreamProcessor
from quadlink.types import ChannelState, ChannelStatus, PrioritizedStream, channel_login
from quadlink.viewers import GQLStats, ViewerCounts, provider_for
from quadlink.webui import WebUI

logger = structlog.get_logger()

# seconds one segment fetch may take in all; httpx's timeout bounds each read only,
# so a trickling origin could hold the slot's worker, and every tile behind it, for longer
SEGMENT_FETCH_WAIT = 8.0


class Daemon:
    """Main daemon for Quadlink stream curation.

    Two loops share the event loop. Refresh (every interval) reloads the config,
    looks up every channel and caches the live pool. Recompose (on request)
    composes the quad from that pool, feeds the relay, then logs in to QuadStream
    if needed and pushes. A refresh requests a recompose; so can anything else,
    via request_recompose(). Neither loop waits on quadstream.tv: while it refuses
    the login, the relay keeps following the pool.

    Attributes:
        interval: Seconds between refreshes.
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
        state_path: str | None = None,
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
            state_path: File that keeps pins, benches and slot positions across
                restarts (default: $QL_STATE_FILE, else /var/lib/quadlink/state.json).
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
        self.ffmpeg = shutil.which("ffmpeg")
        if self.ffmpeg is None:
            logger.warning("relay ffmpeg not found, audio passes through")
        # a hung fetch holds the slot's serial fetch queue; segments arrive every 2s
        self._segment_client = httpx.AsyncClient(timeout=5)
        self.segments = SegmentProxy(self._fetch_segment, self.ffmpeg)
        self.slot_relay = SlotRelay(self.slot_store, segments=self.segments)
        self.webui: WebUI | None = None
        self.webui_runner: web.AppRunner | None = None
        self.running = False
        self.processor: StreamProcessor | None = None
        self.state_store = StateStore(
            Path(state_path or os.environ.get("QL_STATE_FILE") or DEFAULT_STATE_PATH)
        )
        # survives config reloads, so a save in the web UI does not reshuffle tiles
        self.overrides, self.quad_state = self.state_store.load()
        # channels per slot of the last quad QuadStream accepted; None until the
        # current login has accepted one
        self._pushed: tuple[str, ...] | None = None
        self.quadstream_client: QuadStreamClient | None = None
        self._config_hash: int | None = None
        # (username, secret) the current QuadStream client logged in with
        self._credentials: tuple[str | None, str | None] | None = None
        # one login attempt per refresh, so web-requested recomposes do not hammer
        # quadstream.tv while it refuses the login
        self._login_due = False
        # the last login attempt failed; the warning is logged once per streak
        self._login_failing = False
        # channel -> its last-cycle candidate (raw priority), for channels on screen
        self._on_screen: dict[str, PrioritizedStream] = {}
        # relay clock at the end of the last refresh; the next refresh keeps an
        # on-screen channel whose lookup failed if its video arrived since
        self._refreshed_at: float | None = None
        # config and live pool of the last completed refresh, which recompose reads
        self._config: Config | None = None
        self._pool: list[PrioritizedStream] = []
        self._recompose_requested = asyncio.Event()
        # every configured channel's outcome in the last refresh
        self.channel_statuses: list[ChannelStatus] = []
        # wall-clock time (epoch seconds) of that refresh; None before the first
        self.statuses_at: float | None = None
        # last display name seen live per channel: an offline channel has no stream to name it
        self.display_names: dict[str, str] = {}
        # called after anything the snapshot shows may have changed
        self._watchers: list[Callable[[], None]] = []
        self._stats_client = httpx.AsyncClient()
        self.viewers = ViewerCounts(GQLStats(self._stats_client))
        self.directory: Directory = GQLDirectory(self._stats_client)
        # runs beside the lookups, never awaited by a refresh: the quad does not wait on it
        self._viewers_task: asyncio.Task[None] | None = None

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
                dashboard=self,
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
        if self._refreshed_at is None:
            return []
        positions = self.quad_state.positions
        kept = []
        for channel in self._dropped_from_pool(positions, candidates):
            previous = self._on_screen.get(channel)
            if previous is None or not self.processor.lookup_failed(channel):
                continue
            slot = positions[channel]
            if self.slot_relay.video_arrived_since(slot, channel, self._refreshed_at):
                logger.info(
                    "kept channel: lookup failed but video still arriving",
                    channel=channel,
                    slot=slot,
                )
                kept.append(previous)
        return kept

    def request_recompose(self) -> None:
        """Compose the quad again from the cached pool, without new lookups.

        Requests made while a recompose runs are served by one more recompose.
        """
        self._recompose_requested.set()

    async def _main_loop(self) -> None:
        """Run the refresh loop, with the recompose loop beside it until it ends."""
        recomposer = None if self.one_shot else asyncio.create_task(self._recompose_loop())
        try:
            await self._refresh_loop()
        finally:
            tasks = [t for t in (recomposer, self._viewers_task) if t is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _refresh_loop(self) -> None:
        while self.running:
            try:
                delay = await (self._cycle() if self.one_shot else self._refresh())
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

    async def _recompose_loop(self) -> None:
        # the only caller of _recompose while the daemon runs, so it never overlaps itself
        while True:
            await self._recompose_requested.wait()
            self._recompose_requested.clear()
            try:
                await self._recompose()
            except Exception as e:
                logger.error("unexpected error in recompose", error=str(e), exc_info=True)
            self._notify()

    async def _cycle(self) -> float:
        """Run one refresh and the recompose it requests; return the seconds to wait."""
        delay = await self._refresh()
        if self._recompose_requested.is_set():
            self._recompose_requested.clear()
            await self._recompose()
        return delay

    async def _refresh(self) -> float:
        """Look up every channel and cache the live pool; return the seconds to wait."""
        try:
            config = await self.config_loader.load_or_cache()
        except ConfigNotFoundError as e:
            # one-shot has nothing to wait for; a daemon waits for the web UI or an operator
            if self.one_shot:
                raise
            logger.warning("waiting for a config file", error=str(e))
            return self.interval

        apply_config_level(config.log_level)

        # detect config changes and recreate components
        config_hash = hash(str(config.model_dump(exclude={"credentials"})))
        if self._config_hash != config_hash:
            if self._config_hash is not None:
                logger.info("config changed, reloading")
            self._config_hash = config_hash
            if self.processor is not None:
                self.processor.close()
            self.processor = StreamProcessor(config, lookup_timeout=config.lookup_timeout)
            self.viewers.provider = provider_for(config, self._stats_client)

        assert self.processor is not None

        serve_local = config.mode in ("both", "local")
        self.slot_relay.proxy_segments = config.relay_segments

        if self.quadstream_client and self._credentials_of(config) != self._credentials:
            logger.info("quadstream credentials changed, logging in again")
            self.quadstream_client = None
        if not self.one_shot:
            self._start_viewer_counts(config)
        looked_up = self.overrides.extras
        self.processor.extras = looked_up
        candidates = await self.processor.process_stream_groups()
        # a channel placed from Explore while these lookups ran is not among them: the lookup
        # it got at its placement stands until the next refresh, or it would read as offline
        late = self.overrides.extras - looked_up
        self.channel_statuses = [
            *(s for s in self.processor.statuses() if s.channel not in late),
            *(s for s in self.channel_statuses if s.channel in late),
        ]
        candidates += [c for c in self._pool if c.stream.channel in late]
        self.statuses_at = time.time()
        self.display_names |= {
            s.channel: s.stream.metadata.author for s in self.channel_statuses if s.stream
        }
        if serve_local:
            candidates += self._still_playing(candidates)
        self._refreshed_at = self.slot_relay.clock()

        if not candidates:
            logger.info("no stream candidates available")
        else:
            # surface quad channels that fell out of the live pool this
            # cycle (offline/unresolved), which otherwise looks like a re-rank
            dropped = self._dropped_from_pool(self.quad_state.positions, candidates)
            if dropped:
                logger.info(
                    "quad channels dropped from live pool",
                    dropped=dropped,
                    candidates=len(candidates),
                )

        overrides, state = actions.drop_unconfigured(
            self.overrides, self.quad_state, configured_channels(config) | self.overrides.extras
        )
        # an empty pool usually means every lookup failed (network or proxy down),
        # which must not run out the grace of every hold
        if candidates:
            overrides, state = actions.age(overrides, state, {c.stream.channel for c in candidates})
        if overrides != self.overrides or state != self.quad_state:
            ended = sorted(self.overrides.pins.keys() - overrides.pins.keys())
            if ended:
                logger.info("holds ended", channels=ended)
            self.overrides, self.quad_state = overrides, state
            self._save_state()

        self._config = config
        self._pool = candidates
        # set with the config, so the login uses this refresh's credentials
        self._login_due = config.mode in ("both", "remote")
        self.request_recompose()
        self._mark_cycle()
        self._notify()
        return self.interval

    def _start_viewer_counts(self, config: Config) -> None:
        # FETCH_WAIT ends every fetch well within an interval; a fetch still running
        # then has its result arrive late rather than twice
        if self._viewers_task is not None and not self._viewers_task.done():
            return
        channels = sorted(configured_channels(config) | self.overrides.extras)
        self._viewers_task = asyncio.create_task(self._viewer_counts(channels))

    async def _viewer_counts(self, channels: list[str]) -> None:
        before = self.viewers.stats
        await self.viewers.refresh(channels)
        if self.viewers.stats != before:
            self._notify()

    async def _recompose(self) -> None:
        """Compose the quad from the cached pool, feed the relay and push when needed.

        The only writer of the slot store and the QuadStream push, and of
        quad_state except for swap, which moves channels ahead of it.
        """
        config, pool = self._config, self._pool
        # an empty pool keeps the quad on screen as it is
        if config is None or not pool:
            return

        self.quad_state = state = compose(pool, self.quad_state, self.overrides, config)
        overrides = actions.lose(self.overrides, state, {c.stream.channel for c in pool})
        if overrides != self.overrides:
            lost = sorted(self.overrides.pins.keys() - overrides.pins.keys())
            logger.info("soft holds lost", channels=lost)
            self._set_overrides(overrides)
        self._save_state()
        quad = state.quad
        self._on_screen = {}
        for c in pool:
            if c.stream.channel in state.positions:
                self._on_screen.setdefault(c.stream.channel, c)

        if config.mode in ("both", "local"):
            self.slot_store.update(quad.to_list(), list(state.channels))

        if config.mode not in ("both", "remote"):
            return
        client = self.quadstream_client or await self._login(config)
        # push until QuadStream holds this lineup; compared by channel, since every
        # refresh re-mints the playlist URLs. A failed push is sent again by the next
        # recompose, at the latest after the next refresh.
        if client and state.channels != self._pushed:
            if not await client.update_quad(quad):
                # one-shot has no later refresh to retry the push; it exits 1
                if self.one_shot:
                    raise RuntimeError("QuadStream did not accept the quad update")
                logger.error("quadstream update failed")
                return
            # a refresh that logged in again during the push reset _pushed for the
            # new account, which has not seen this quad
            if client is self.quadstream_client:
                self._pushed = state.channels
            if config.webhook.enabled and config.webhook.url:
                await client.send_webhook(config.webhook.url, quad)

    async def place(self, channel: str, slot: int) -> None:
        """Make channel hold the tile; what it showed is put off it (see overrides.place).

        A channel outside the config must be live with video; it is looked up
        until its hold ends. Every operator action raises OverrideError for
        invalid input, and otherwise saves the overrides and requests one recompose.
        """
        channel = channel.strip().lower()
        configured = self._configured()
        if channel not in configured and channel not in self.overrides.extras:
            await self._look_up_outside(channel)
        # reversed: the pool is best first, and a channel's first entry is the one that counts
        priorities = {c.stream.channel: c.priority for c in reversed(self._pool)}
        self._apply(
            *actions.place(self.overrides, self.quad_state, channel, slot, configured, priorities)
        )

    async def swap(self, a: int, b: int) -> None:
        """Swap the channels of two slots; holds move with their holders."""
        # with nothing to recompose from, the moved quad_state would never reach the relay
        if self._config is None or not self._pool:
            raise OverrideError("no live channels yet: wait for a refresh that finds one")
        self._apply(*actions.swap(self.overrides, self.quad_state, a, b))

    async def unpin(self, slot: int) -> None:
        """End the tile's hold; the channel it replaced gets the tile back."""
        self._apply(*actions.unpin(self.overrides, self.quad_state, slot))

    async def unpin_all(self) -> None:
        """End every hold."""
        self._apply(*actions.unpin_all(self.overrides, self.quad_state))

    async def lock(self, slot: int, soft: bool) -> None:
        """Make the tile's hold soft or hard; a tile with no hold gets one for its channel."""
        self._apply(*actions.lock(self.overrides, self.quad_state, slot, soft))

    async def _look_up_outside(self, channel: str) -> None:
        """Check a channel outside the config is live with video, and pool it at once."""
        # a bad name fails before the lookup, which takes seconds
        if not actions.is_login(channel):
            raise OverrideError(f"{channel!r} is not a Twitch channel name")
        assert self.processor is not None
        try:
            stream = await self.processor.look_up(channel)
        except Exception as e:
            raise OverrideError(f"the Twitch lookup of {channel} failed: {e}") from e
        if stream is None:
            raise OverrideError(f"{channel} is not live or does not exist")
        if not stream.has_video:
            raise OverrideError(f"{channel} is live without video")
        # pooled now instead of after the next refresh's lookups
        self._pool = [*self._pool, PrioritizedStream(stream, EXTRA_PRIORITY, 0.0)]
        self.channel_statuses = [
            *self.channel_statuses,
            ChannelStatus(channel, ChannelState.LIVE, [], stream=stream, priority=EXTRA_PRIORITY),
        ]

    def snapshot(self) -> Snapshot:
        """The quad and every configured channel's dashboard state."""
        return snapshot(
            self.channel_statuses,
            self.quad_state,
            self.overrides,
            self.statuses_at,
            self._quadstream_login(),
            self.viewers.stats,
            self.display_names,
        )

    def watch(self, callback: Callable[[], None]) -> None:
        """Call callback (on the event loop) whenever the snapshot may have changed."""
        self._watchers.append(callback)

    def _notify(self) -> None:
        for callback in self._watchers:
            callback()

    def _quadstream_login(self) -> QuadStreamLogin:
        if self._config is None or self._config.mode not in ("both", "remote"):
            return QuadStreamLogin.OFF
        if self.quadstream_client is not None:
            return QuadStreamLogin.LOGGED_IN
        if self._login_failing:
            return QuadStreamLogin.FAILED
        return QuadStreamLogin.PENDING

    def _configured(self) -> set[str]:
        if self._config is None or self.processor is None:
            raise OverrideError("no config is loaded yet: wait for the first refresh")
        return configured_channels(self._config)

    def _apply(self, overrides: Overrides, state: QuadState | None = None) -> None:
        # swap moves channels between slots ahead of the recompose that seats them
        if state is not None:
            self.quad_state = state
        self._set_overrides(overrides)
        self._save_state()
        self.request_recompose()
        self._notify()

    def _set_overrides(self, overrides: Overrides) -> None:
        """Take overrides; outside channels that no longer hold a tile leave the pool."""
        if gone := self.overrides.extras - overrides.extras:
            self._pool = [c for c in self._pool if c.stream.channel not in gone]
            self.channel_statuses = [s for s in self.channel_statuses if s.channel not in gone]
        self.overrides = overrides
        if self.processor is not None:
            self.processor.extras = overrides.extras

    def _save_state(self) -> None:
        self.state_store.save(self.overrides, self.quad_state)

    @staticmethod
    def _credentials_of(config: Config) -> tuple[str | None, str | None] | None:
        if config.credentials is None:
            return None
        return config.credentials.username, config.credentials.secret

    async def _login(self, config: Config) -> QuadStreamClient | None:
        """Log in to QuadStream, at most once per refresh; return the client or None."""
        if not self._login_due:
            return None
        self._login_due = False
        credentials = self._credentials_of(config)
        assert credentials is not None
        username, secret = credentials
        assert username is not None
        assert secret is not None
        client = QuadStreamClient(username=username, secret=secret)
        if not await client.login():
            # one-shot has no later refresh to try again; it exits 1 after the local work
            if self.one_shot:
                raise RuntimeError("QuadStream did not accept the login")
            if not self._login_failing:
                logger.warning(
                    "quadstream login failed, retrying after each refresh",
                    reason=client.login_error,
                )
                self._login_failing = True
            return None
        if self._login_failing:
            logger.info("quadstream login recovered")
            self._login_failing = False
        self.quadstream_client = client
        self._credentials = credentials
        # a new login may be another account, which has not seen any quad
        self._pushed = None
        return client

    def _mark_cycle(self) -> None:
        if self.health_server:
            self.health_server.mark_cycle()


def configured_channels(config: Config) -> set[str]:
    """Channel key of every configured URL."""
    return {
        channel_login(url)
        for groups in config.priorities.values()
        for group in groups
        for url in group.urls
    }


async def run_daemon(
    one_shot: bool = False,
    interval: int = 30,
    enable_health_server: bool = False,
    config_path: str | None = None,
    enable_webui: bool = False,
    webui_host: str = "0.0.0.0",
    webui_port: int = 8081,
    webui_allowed_hosts: list[str] | None = None,
    state_path: str | None = None,
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
        state_path: File that keeps pins, benches and slot positions (see Daemon).
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
        state_path=state_path,
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
        await daemon._stats_client.aclose()
