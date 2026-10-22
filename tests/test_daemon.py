"""Tests for the daemon: refresh and recompose loops, pushes, logins, reloads, one-shot.

The daemon runs with real config files, the real stream processor and compose; only
Twitch (the stream fetcher) and quadstream.tv (the QuadStream client) are faked.
"""

import asyncio
import itertools
import json
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import structlog

from quadlink import __main__ as cli
from quadlink.daemon import Daemon, run_daemon
from quadlink.health import HealthServer
from quadlink.quad import compose
from quadlink.stream.fetcher import LookupFailed
from quadlink.types import ChannelState, Metadata, Quad, Stream, channel_login

CHANNELS = ("a", "b", "c", "d")
# "e" is configured but offline until a test brings it live
CONFIGURED = (*CHANNELS, "e")


class Twitch:
    """Fake Twitch: what a lookup of each channel finds.

    Every lookup mints a new playlist URL, as Twitch does.
    """

    def __init__(self) -> None:
        # channel -> (category, title), or the exception its lookup raises
        self.live: dict[str, tuple[str, str] | Exception] = {}
        self.lookups = 0
        self.minted = 0
        # while set and clear, lookups wait for it
        self.gate: asyncio.Event | None = None
        self.fetchers: list[FakeFetcher] = []
        # channel -> display name, when it differs from the login
        self.display: dict[str, str] = {}

    def go_live(self, *channels: str, category: str = "Games", title: str = "t") -> None:
        for channel in channels:
            self.live[channel] = (category, title)

    def go_offline(self, *channels: str) -> None:
        for channel in channels:
            self.live.pop(channel, None)

    async def lookup(self, url: str) -> Stream | None:
        self.lookups += 1
        if self.gate is not None:
            await self.gate.wait()
        channel = channel_login(url)
        found = self.live.get(channel)
        if isinstance(found, Exception):
            raise found
        if found is None:
            return None
        self.minted += 1
        category, title = found
        return Stream(
            url=f"https://twitch.tv/{channel}",
            metadata=Metadata(
                author=self.display.get(channel, channel), category=category, title=title
            ),
            master_url=f"https://edge.example/{channel}.m3u8?token={self.minted}",
        )


class FakeExecutor:
    def __init__(self) -> None:
        self.shut_down = False

    def shutdown(self, **_: Any) -> None:
        self.shut_down = True


class FakeFetcher:
    """Stands in for StreamlinkFetcher inside the real StreamProcessor."""

    def __init__(self, twitch: Twitch, timeout: float | None = None) -> None:
        self.fetch_stream_info = twitch.lookup
        self.executor = FakeExecutor()
        self.timeout = timeout


def slot_channels(quad: Quad) -> tuple[str, ...]:
    """Channel per slot of a quad of fake playlist URLs."""
    return tuple(url.rsplit("/", 1)[1].split(".")[0] if url else "" for url in quad.slots)


class QuadStream:
    """Fake quadstream.tv: accounts, accepted pushes and webhooks."""

    def __init__(self) -> None:
        self.accounts = {"user": "secret"}
        self.clients: list[FakeClient] = []
        # (secret of the account, channel per slot) of every accepted push
        self.pushes: list[tuple[str, tuple[str, ...]]] = []
        self.refuse_pushes = 0
        self.webhooks: list[tuple[str, tuple[str, ...]]] = []
        # while set and clear, pushes wait for it
        self.gate: asyncio.Event | None = None
        self.in_flight = 0
        self.peak = 0

    def client(self, username: str, secret: str) -> "FakeClient":
        client = FakeClient(self, username, secret)
        self.clients.append(client)
        return client


class FakeClient:
    """Stands in for QuadStreamClient."""

    def __init__(self, service: QuadStream, username: str, secret: str) -> None:
        self.service = service
        self.username = username
        self.secret = secret
        self.logged_in = False

    async def login(self) -> bool:
        self.logged_in = self.service.accounts.get(self.username) == self.secret
        return self.logged_in

    async def update_quad(self, quad: Quad) -> bool:
        assert self.logged_in
        service = self.service
        service.in_flight += 1
        service.peak = max(service.peak, service.in_flight)
        try:
            if service.gate is not None:
                await service.gate.wait()
        finally:
            service.in_flight -= 1
        if service.refuse_pushes:
            service.refuse_pushes -= 1
            return False
        service.pushes.append((self.secret, slot_channels(quad)))
        return True

    async def send_webhook(self, url: str, quad: Quad | None = None) -> bool:
        assert quad is not None
        self.service.webhooks.append((url, slot_channels(quad)))
        return True


def write_config(
    path: Path,
    channels: tuple[str, ...] = CONFIGURED,
    mode: str = "both",
    secret: str = "secret",
    **extra: Any,
) -> None:
    """Write a config file (JSON is YAML) with all channels in one group."""
    data: dict[str, Any] = {"mode": mode, "priorities": {100: [{"urls": list(channels)}]}}
    if mode != "local":
        data["credentials"] = {"username": "user", "secret": secret}
    data.update(extra)
    path.write_text(json.dumps(data))


async def settle() -> None:
    """Let every ready task run until none is (the fakes never sleep)."""
    for _ in range(20):
        await asyncio.sleep(0)


def recompose_loops() -> list[asyncio.Task]:
    return [
        t
        for t in asyncio.all_tasks()
        if t.get_coro().__qualname__ == "Daemon._recompose_loop" and not t.done()
    ]


def events(logs: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == event]


async def wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


@pytest.fixture
def twitch() -> Twitch:
    twitch = Twitch()
    twitch.go_live(*CHANNELS)
    return twitch


@pytest.fixture
def quadstream() -> QuadStream:
    return QuadStream()


@pytest.fixture(autouse=True)
def fakes(twitch: Twitch, quadstream: QuadStream) -> Any:
    """Replace Twitch and quadstream.tv for every test; nothing reaches the network."""

    def fetcher(timeout: float | None = None, **_: Any) -> FakeFetcher:
        made = FakeFetcher(twitch, timeout)
        twitch.fetchers.append(made)
        return made

    # tiebreakers fall in lookup order, so equal-priority channels rank in config order
    order = itertools.count()
    tiebreakers = SimpleNamespace(random=lambda: 1 / (2 + next(order)))
    with (
        patch("quadlink.stream.processor.StreamlinkFetcher", fetcher),
        patch("quadlink.stream.processor.random", tiebreakers),
        patch("quadlink.daemon.QuadStreamClient", quadstream.client),
    ):
        yield


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    write_config(path)
    return path


@pytest.fixture
async def make_daemon(config_path: Path) -> AsyncIterator[Callable[..., Daemon]]:
    made: list[Daemon] = []

    def make(**kwargs: Any) -> Daemon:
        kwargs.setdefault("config_path", str(config_path))
        daemon = Daemon(**kwargs)
        daemon.running = True
        made.append(daemon)
        return daemon

    yield make
    for daemon in made:
        if daemon.processor is not None:
            daemon.processor.close()
        await daemon.slot_relay.aclose()
        await daemon._segment_client.aclose()


class Stepper:
    """The daemon's real recompose loop, with refreshes stepped by the test."""

    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon

    async def refresh(self) -> float:
        delay = await self.daemon._refresh()
        await settle()
        return delay

    async def recompose(self) -> None:
        self.daemon.request_recompose()
        await settle()


@asynccontextmanager
async def stepped(daemon: Daemon) -> AsyncIterator[Stepper]:
    task = asyncio.create_task(daemon._recompose_loop())
    try:
        yield Stepper(daemon)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def store_channels(daemon: Daemon) -> list[str | None]:
    entries = [daemon.slot_store.get(slot) for slot in range(4)]
    return [e.identity if e else None for e in entries]


class TestRefreshAndRecompose:
    """A refresh looks every channel up; the recompose it requests composes and pushes."""

    async def test_refresh_feeds_the_relay_and_pushes(self, make_daemon, quadstream):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            delay = await loops.refresh()
        assert delay == daemon.interval
        assert store_channels(daemon) == list(CHANNELS)
        assert quadstream.pushes == [("secret", CHANNELS)]
        assert [(s.channel, s.state) for s in daemon.channel_statuses] == [
            *((c, ChannelState.LIVE) for c in CHANNELS),
            ("e", ChannelState.OFFLINE),
        ]
        assert daemon.statuses_at is not None

    async def test_unchanged_lineup_is_pushed_once(self, make_daemon, quadstream, twitch):
        """Every refresh mints new URLs; the push compares channels, not URLs."""
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            for _ in range(3):
                await loops.refresh()
        assert twitch.minted == 12
        assert quadstream.pushes == [("secret", CHANNELS)]

    async def test_failed_push_is_retried_after_the_next_refresh(self, make_daemon, quadstream):
        daemon = make_daemon()
        quadstream.refuse_pushes = 1
        async with stepped(daemon) as loops:
            with structlog.testing.capture_logs() as logs:
                await loops.refresh()
            assert quadstream.pushes == []
            assert events(logs, "quadstream update failed")
            await loops.refresh()
            assert quadstream.pushes == [("secret", CHANNELS)]
            await loops.refresh()
        assert quadstream.pushes == [("secret", CHANNELS)]

    async def test_request_recompose_retries_a_push_without_a_lookup(
        self, make_daemon, quadstream, twitch
    ):
        daemon = make_daemon()
        quadstream.refuse_pushes = 1
        async with stepped(daemon) as loops:
            await loops.refresh()
            lookups = twitch.lookups
            await loops.recompose()
        assert twitch.lookups == lookups
        assert quadstream.pushes == [("secret", CHANNELS)]

    async def test_request_recompose_before_any_refresh_does_nothing(self, make_daemon, quadstream):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.recompose()
        assert store_channels(daemon) == [None] * 4
        assert quadstream.clients == []

    async def test_requests_during_a_push_get_one_more_recompose(
        self, make_daemon, quadstream, twitch
    ):
        daemon = make_daemon()
        quadstream.gate = asyncio.Event()
        async with stepped(daemon) as loops:
            await loops.refresh()
            assert quadstream.in_flight == 1
            # a refresh with a new lineup and two web actions while the push hangs
            twitch.go_offline("d")
            twitch.go_live("e")
            await loops.refresh()
            daemon.request_recompose()
            daemon.request_recompose()
            await settle()
            quadstream.gate.set()
            await settle()
        assert quadstream.peak == 1
        assert quadstream.pushes == [("secret", CHANNELS), ("secret", ("a", "b", "c", "e"))]

    async def test_recompose_error_keeps_the_loop_running(self, make_daemon):
        daemon = make_daemon()
        failures = [RuntimeError("boom")]

        def flaky(*args: Any) -> Any:
            if failures:
                raise failures.pop()
            return compose(*args)

        with patch("quadlink.daemon.compose", flaky):
            async with stepped(daemon) as loops:
                with structlog.testing.capture_logs() as logs:
                    await loops.refresh()
                assert events(logs, "unexpected error in recompose")
                assert store_channels(daemon) == [None] * 4
                await loops.recompose()
        assert store_channels(daemon) == list(CHANNELS)

    async def test_empty_pool_keeps_the_quad(self, make_daemon, quadstream, twitch):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.go_offline(*CHANNELS)
            with structlog.testing.capture_logs() as logs:
                await loops.refresh()
        assert events(logs, "no stream candidates available")
        assert store_channels(daemon) == list(CHANNELS)
        assert daemon.quad_state.channels == CHANNELS
        assert len(quadstream.pushes) == 1

    async def test_dropped_channel_is_logged_and_its_slot_refilled(self, make_daemon, twitch):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.go_offline("b")
            twitch.go_live("e")
            with structlog.testing.capture_logs() as logs:
                await loops.refresh()
        (dropped,) = events(logs, "quad channels dropped from live pool")
        assert dropped["log_level"] == "info"
        assert dropped["dropped"] == ["b"]
        assert store_channels(daemon) == ["a", "e", "c", "d"]

    async def test_statuses_follow_the_latest_refresh(self, make_daemon, twitch):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            with patch("quadlink.daemon.time.time", side_effect=[1000.0, 1030.0]):
                await loops.refresh()
                assert daemon.statuses_at == 1000.0
                twitch.go_offline("a")
                twitch.live["b"] = LookupFailed("timed out")
                await loops.refresh()
        assert daemon.statuses_at == 1030.0
        states = {s.channel: (s.state, s.reason) for s in daemon.channel_statuses}
        assert states["a"] == (ChannelState.OFFLINE, None)
        assert states["b"] == (ChannelState.LOOKUP_FAILED, "timed out")
        assert states["c"] == (ChannelState.LIVE, None)

    async def test_webhook_follows_an_accepted_push(self, make_daemon, config_path, quadstream):
        write_config(config_path, webhook={"enabled": True, "url": "https://hooks.example/q"})
        daemon = make_daemon()
        quadstream.refuse_pushes = 1
        async with stepped(daemon) as loops:
            await loops.refresh()
            assert quadstream.webhooks == []
            await loops.refresh()
        assert quadstream.webhooks == [("https://hooks.example/q", CHANNELS)]

    async def test_local_mode_feeds_the_relay_only(self, make_daemon, config_path, quadstream):
        write_config(config_path, mode="local")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
        assert store_channels(daemon) == list(CHANNELS)
        assert quadstream.clients == []

    async def test_remote_mode_pushes_only(self, make_daemon, config_path, quadstream):
        write_config(config_path, mode="remote")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
        assert quadstream.pushes == [("secret", CHANNELS)]
        assert store_channels(daemon) == [None] * 4


class TestStillPlaying:
    """An on-screen channel whose lookup failed keeps its slot while its video arrives."""

    @staticmethod
    def video_from(*channels: str) -> Callable[[int, str, float], bool]:
        return lambda slot, channel, since: channel in channels

    async def test_failed_lookup_with_video_arriving_keeps_the_slot(self, make_daemon, twitch):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.go_live("e")
            twitch.live["a"] = LookupFailed("proxy error")
            daemon.slot_relay.video_arrived_since = self.video_from("a")
            with structlog.testing.capture_logs() as logs:
                await loops.refresh()
        assert events(logs, "kept channel: lookup failed but video still arriving")
        assert store_channels(daemon) == list(CHANNELS)

    async def test_failed_lookup_without_new_video_loses_the_slot(self, make_daemon, twitch):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.go_live("e")
            twitch.live["a"] = LookupFailed("proxy error")
            daemon.slot_relay.video_arrived_since = self.video_from()
            await loops.refresh()
        assert store_channels(daemon) == ["e", "b", "c", "d"]

    async def test_channel_a_filter_rejects_loses_the_slot(self, make_daemon, config_path, twitch):
        rulesets = [{"name": "no-reruns", "filters": {"block_titles": ["rerun"]}}]
        write_config(config_path, rulesets=rulesets)
        config = json.loads(config_path.read_text())
        config["priorities"]["100"][0]["rulesets"] = ["no-reruns"]
        config_path.write_text(json.dumps(config))
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.go_live("e")
            twitch.go_live("a", title="rerun")
            daemon.slot_relay.video_arrived_since = self.video_from("a")
            await loops.refresh()
        assert store_channels(daemon) == ["e", "b", "c", "d"]


class TestLoginKeying:
    """Channels are keyed by their lowercase login, never by their display name."""

    async def test_display_name_differing_from_login(self, make_daemon, twitch):
        twitch.display["a"] = "Streamer A"
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            assert store_channels(daemon) == list(CHANNELS)
            assert daemon.quad_state.positions["a"] == 0
            (row,) = [s for s in daemon.channel_statuses if s.channel == "a"]
            assert row.state is ChannelState.LIVE
            assert row.stream is not None
            assert row.stream.metadata.author == "Streamer A"
            assert Daemon._dropped_from_pool(daemon.quad_state.positions, daemon._pool) == []

            # a failed lookup is matched to arriving video by login
            twitch.live["a"] = LookupFailed("proxy error")
            twitch.go_live("e")
            daemon.slot_relay.video_arrived_since = (  # type: ignore[method-assign]
                lambda slot, channel, since: channel == "a"
            )
            with structlog.testing.capture_logs() as logs:
                await loops.refresh()
        assert events(logs, "kept channel: lookup failed but video still arriving")
        assert store_channels(daemon) == list(CHANNELS)

    async def test_renamed_display_name_keeps_the_slot(self, make_daemon, twitch):
        twitch.display["a"] = "Streamer A"
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.display["a"] = "A Renamed"
            twitch.go_live("e")
            with structlog.testing.capture_logs() as logs:
                await loops.refresh()
        assert not events(logs, "quad channels dropped from live pool")
        assert store_channels(daemon) == list(CHANNELS)
        (row,) = [s for s in daemon.channel_statuses if s.channel == "a"]
        assert row.stream is not None
        assert row.stream.metadata.author == "A Renamed"


class TestFirstRefresh:
    async def test_still_playing_keeps_nothing_before_the_first_refresh(self, make_daemon, twitch):
        """No relay clock reading exists yet, so arriving video proves nothing."""
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.live["a"] = LookupFailed("proxy error")
            assert daemon.processor is not None
            pool = await daemon.processor.process_stream_groups()
        daemon.slot_relay.video_arrived_since = (  # type: ignore[method-assign]
            lambda slot, channel, since: True
        )
        daemon._refreshed_at = None
        assert daemon._still_playing(pool) == []
        # the same state after a refresh keeps the channel
        daemon._refreshed_at = 0.0
        assert [c.stream.channel for c in daemon._still_playing(pool)] == ["a"]


class TestConfigReload:
    async def test_reload_keeps_slots_and_replaces_the_processor(
        self, make_daemon, config_path, quadstream, twitch
    ):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            first = daemon.processor
            write_config(config_path, channels=tuple(reversed(CONFIGURED)), diversity_bonus=10)
            with structlog.testing.capture_logs() as logs:
                await loops.refresh()
        assert events(logs, "config changed, reloading")
        assert daemon.processor is not first
        assert twitch.fetchers[0].executor.shut_down
        assert not twitch.fetchers[1].executor.shut_down
        assert store_channels(daemon) == list(CHANNELS)
        assert daemon.quad_state.channels == CHANNELS
        assert quadstream.pushes == [("secret", CHANNELS)]

    async def test_config_change_during_a_refresh_applies_on_the_next(
        self, make_daemon, config_path, twitch
    ):
        daemon = make_daemon()
        twitch.gate = asyncio.Event()
        async with stepped(daemon) as loops:
            refresh = asyncio.create_task(loops.refresh())
            await settle()
            write_config(config_path, diversity_bonus=10)
            twitch.gate.set()
            await refresh
            # the refresh in progress finished with the config it started with
            assert daemon._config is not None
            assert daemon._config.diversity_bonus == 25
            assert len(twitch.fetchers) == 1
            await loops.refresh()
        assert daemon._config.diversity_bonus == 10
        assert len(twitch.fetchers) == 2
        assert store_channels(daemon) == list(CHANNELS)

    async def test_lookup_timeout_comes_from_the_config(self, make_daemon, config_path, twitch):
        daemon = make_daemon(interval=60)
        async with stepped(daemon) as loops:
            await loops.refresh()
            write_config(config_path, lookup_timeout=4)
            await loops.refresh()
        assert [f.timeout for f in twitch.fetchers] == [10, 4]

    async def test_credential_change_logs_in_again(
        self, make_daemon, config_path, quadstream, twitch
    ):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            processor = daemon.processor
            quadstream.accounts["user"] = "rotated"
            write_config(config_path, secret="rotated")
            await loops.refresh()
        assert [c.secret for c in quadstream.clients] == ["secret", "rotated"]
        # the new login gets the quad although the lineup did not change
        assert quadstream.pushes == [("secret", CHANNELS), ("rotated", CHANNELS)]
        # credentials are not part of the config hash: no processor rebuild
        assert daemon.processor is processor
        assert len(twitch.fetchers) == 1

    async def test_failed_relogin_drops_the_old_client_and_retries(
        self, make_daemon, config_path, quadstream
    ):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            old = daemon.quadstream_client
            # the config has the new secret before the account does
            write_config(config_path, secret="rotated")
            with structlog.testing.capture_logs() as logs:
                assert await loops.refresh() == 30
            assert events(logs, "quadstream login failed, retrying in 30s")
            assert daemon.quadstream_client is None
            quadstream.accounts["user"] = "rotated"
            await loops.refresh()
        assert daemon.quadstream_client is not old
        assert [c.secret for c in quadstream.clients] == ["secret", "rotated", "rotated"]
        assert quadstream.pushes == [("secret", CHANNELS), ("rotated", CHANNELS)]


class TestLoginAndReadiness:
    """/ready turns green only once a refresh completes."""

    async def test_ready_after_a_completed_refresh(self, make_daemon):
        daemon = make_daemon(enable_health_server=True, interval=20)
        assert daemon.health_server is not None
        assert daemon.health_server.max_age == 60
        assert not daemon.health_server.is_ready()
        async with stepped(daemon) as loops:
            await loops.refresh()
        assert daemon.health_server.is_ready()

    async def test_failed_login_is_not_ready_and_retries(self, make_daemon, quadstream, twitch):
        daemon = make_daemon(enable_health_server=True)
        quadstream.accounts["user"] = "other"
        async with stepped(daemon) as loops:
            assert await loops.refresh() == 30
            assert not daemon.health_server.is_ready()
            assert twitch.lookups == 0
            quadstream.accounts["user"] = "secret"
            assert await loops.refresh() == daemon.interval
        assert daemon.health_server.is_ready()
        assert quadstream.pushes == [("secret", CHANNELS)]

    async def test_missing_config_is_not_ready_and_waits(self, make_daemon, tmp_path):
        daemon = make_daemon(enable_health_server=True, config_path=str(tmp_path / "none.yaml"))
        async with stepped(daemon) as loops:
            with structlog.testing.capture_logs() as logs:
                assert await loops.refresh() == daemon.interval
        assert events(logs, "waiting for a config file")
        assert not daemon.health_server.is_ready()


class TestRunningLoops:
    """The two loops run together until the daemon stops or is cancelled."""

    async def test_stopping_ends_both_loops(self, make_daemon, quadstream):
        daemon = make_daemon(interval=0.01)
        task = asyncio.create_task(daemon._main_loop())
        await wait_for(lambda: bool(quadstream.pushes))
        assert len(recompose_loops()) == 1
        daemon.running = False
        await asyncio.wait_for(task, 1)
        assert recompose_loops() == []
        assert quadstream.pushes == [("secret", CHANNELS)]

    @pytest.mark.parametrize("during", ["wait", "lookup"])
    async def test_cancel_ends_both_loops(self, make_daemon, twitch, during):
        daemon = make_daemon(interval=0.01)
        task = asyncio.create_task(daemon._main_loop())
        await wait_for(lambda: daemon.slot_store.get(0) is not None)
        if during == "lookup":
            twitch.gate = asyncio.Event()
            lookups = twitch.lookups
            await wait_for(lambda: twitch.lookups > lookups)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.done()
        assert recompose_loops() == []

    async def test_unexpected_refresh_error_is_logged_and_the_loop_survives(
        self, make_daemon, quadstream
    ):
        daemon = make_daemon(interval=0.01)

        async def broken() -> float:
            raise RuntimeError("boom")

        daemon._refresh = broken  # type: ignore[method-assign]
        with structlog.testing.capture_logs() as logs:
            task = asyncio.create_task(daemon._main_loop())
            await wait_for(lambda: bool(events(logs, "unexpected error in main loop")))
        # the loop waits 30 s before the next refresh instead of ending
        await settle()
        assert not task.done()
        assert len(recompose_loops()) == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert quadstream.pushes == []

    async def test_webui_starts_without_a_config_and_waits_for_one(
        self, make_daemon, tmp_path, quadstream
    ):
        path = tmp_path / "later.yaml"
        daemon = make_daemon(
            config_path=str(path),
            interval=0.01,
            enable_webui=True,
            webui_host="127.0.0.1",
            webui_port=0,
        )
        task = asyncio.create_task(daemon.start())
        try:
            await wait_for(lambda: daemon.webui_runner is not None)
            assert daemon.webui is not None
            assert daemon.webui.slot_relay is daemon.slot_relay
            host, port = daemon.webui_runner.addresses[0][:2]
            async with httpx.AsyncClient() as http:
                page = await http.get(f"http://{host}:{port}/")
            assert page.status_code == 200
            write_config(path)
            await wait_for(lambda: bool(quadstream.pushes))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if daemon.webui_runner is not None:
                await daemon.webui_runner.cleanup()
        assert store_channels(daemon) == list(CHANNELS)


class TestOneShot:
    """`quadlink --one-shot` runs one refresh and recompose, then exits."""

    @staticmethod
    def run(config: Path, *extra: str) -> int:
        """Run the CLI; return its exit code."""
        argv = ["quadlink", "--one-shot", "--config", str(config), *extra]
        with patch.object(sys, "argv", argv), patch.object(cli, "setup_logging"):
            try:
                cli.main()
            except SystemExit as e:
                return int(e.code or 0)
        return 0

    def test_pushes_once_and_exits_without_waiting(self, config_path, quadstream, twitch):
        started = time.monotonic()
        assert self.run(config_path) == 0
        assert time.monotonic() - started < 5
        assert twitch.lookups == len(CONFIGURED)
        assert quadstream.pushes == [("secret", CHANNELS)]
        # the processor's lookup threads are released
        assert twitch.fetchers[0].executor.shut_down

    def test_no_live_channel_exits_zero(self, config_path, quadstream, twitch):
        twitch.go_offline(*CHANNELS)
        started = time.monotonic()
        assert self.run(config_path) == 0
        assert time.monotonic() - started < 5
        assert quadstream.pushes == []

    def test_missing_config_exits_one(self, tmp_path):
        with structlog.testing.capture_logs() as logs:
            assert self.run(tmp_path / "none.yaml") == 1
        (failed,) = events(logs, "daemon failed")
        assert "none.yaml" in failed["error"]

    def test_invalid_config_exits_one(self, config_path, twitch):
        config_path.write_text(json.dumps({"priorities": {}}))
        with structlog.testing.capture_logs() as logs:
            assert self.run(config_path) == 1
        assert events(logs, "daemon failed")
        assert twitch.lookups == 0

    def test_failed_login_exits_one_without_retrying(self, config_path, quadstream, twitch):
        quadstream.accounts["user"] = "other"
        started = time.monotonic()
        with structlog.testing.capture_logs() as logs:
            assert self.run(config_path) == 1
        assert time.monotonic() - started < 5
        assert not [e for e in logs if "retrying" in e["event"]]
        (failed,) = events(logs, "daemon failed")
        assert "login" in failed["error"]
        assert twitch.lookups == 0

    def test_unexpected_error_exits_one(self, config_path):
        with (
            patch("quadlink.daemon.compose", side_effect=RuntimeError("boom")),
            structlog.testing.capture_logs() as logs,
        ):
            assert self.run(config_path) == 1
        (failed,) = events(logs, "daemon failed")
        assert failed["error"] == "boom"


class TestRunDaemon:
    @pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
    async def test_signal_stops_the_daemon_and_releases_resources(
        self, config_path, quadstream, twitch, sig
    ):
        made: list[Daemon] = []

        class Recorded(Daemon):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                made.append(self)

        with (
            patch("quadlink.daemon.Daemon", Recorded),
            patch("quadlink.daemon.HealthServer", partial(HealthServer, host="127.0.0.1", port=0)),
            structlog.testing.capture_logs() as logs,
        ):
            task = asyncio.create_task(
                run_daemon(
                    config_path=str(config_path),
                    enable_health_server=True,
                    enable_webui=True,
                    webui_host="127.0.0.1",
                    webui_port=0,
                )
            )
            await wait_for(lambda: bool(quadstream.pushes))
            (daemon,) = made
            assert daemon.health_server is not None
            assert daemon.health_server.server is not None
            assert daemon.webui_runner is not None
            assert daemon.webui_runner.addresses
            os.kill(os.getpid(), sig)
            await asyncio.wait_for(task, 2)
        assert daemon.running is False
        assert events(logs, "shutting down")[0]["signal"] == sig.name
        assert daemon.health_server.server is None
        assert not daemon.webui_runner.addresses
        assert daemon._segment_client.is_closed
        assert twitch.fetchers[0].executor.shut_down
        assert recompose_loops() == []


def test_missing_ffmpeg_is_logged():
    with (
        patch("quadlink.daemon.shutil.which", return_value=None),
        structlog.testing.capture_logs() as logs,
    ):
        Daemon()
    assert events(logs, "relay ffmpeg not found, audio passes through")


class TestFetchSegment:
    async def test_returns_the_body_and_raises_on_error(self, make_daemon):
        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/gone.ts":
                return httpx.Response(404)
            return httpx.Response(200, content=b"x" * 100_000)

        daemon = make_daemon()
        await daemon._segment_client.aclose()
        daemon._segment_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        assert await daemon._fetch_segment("http://cdn/a.ts") == b"x" * 100_000
        with pytest.raises(httpx.HTTPStatusError):
            await daemon._fetch_segment("http://cdn/gone.ts")

    async def test_bounds_a_trickling_body(self, make_daemon):
        async def trickle() -> AsyncIterator[bytes]:
            while True:
                yield b"x"
                await asyncio.sleep(0.05)

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=trickle())

        daemon = make_daemon()
        await daemon._segment_client.aclose()
        daemon._segment_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch("quadlink.daemon.SEGMENT_FETCH_WAIT", 0.3):
            with pytest.raises(TimeoutError):
                await daemon._fetch_segment("http://cdn/slow.ts")
