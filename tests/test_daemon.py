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
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import structlog
from aiohttp.test_utils import TestClient, TestServer

from quadlink import __main__ as cli
from quadlink import logconfig
from quadlink.daemon import Daemon, run_daemon
from quadlink.dashboard import DashboardState, QuadStreamLogin
from quadlink.health import HealthServer
from quadlink.overrides import GRACE_REFRESHES, OverrideError
from quadlink.quad import Hold, Overrides, compose
from quadlink.stream.fetcher import LookupFailed
from quadlink.types import ChannelState, Metadata, Quad, Stream, channel_login
from quadlink.viewers import Stats
from quadlink.webui import TOKEN_HEADER, WebUI

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


class FakeStats:
    """Stands in for the viewer count provider."""

    def __init__(self) -> None:
        self.live: dict[str, Stats] = {}
        self.calls: list[list[str]] = []
        # while set and clear, fetches wait for it
        self.gate: asyncio.Event | None = None

    async def fetch(self, logins: list[str]) -> dict[str, Stats]:
        self.calls.append(logins)
        if self.gate is not None:
            await self.gate.wait()
        return {k: v for k, v in self.live.items() if k in logins}


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
        self.login_error: str | None = None

    async def login(self) -> bool:
        self.logged_in = self.service.accounts.get(self.username) == self.secret
        self.login_error = None if self.logged_in else "bad credentials"
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


@pytest.fixture
def stats() -> FakeStats:
    return FakeStats()


@pytest.fixture(autouse=True)
def fakes(twitch: Twitch, quadstream: QuadStream, stats: FakeStats) -> Any:
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
        patch("quadlink.daemon.provider_for", lambda config, client: stats),
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
        if daemon._viewers_task is not None:
            daemon._viewers_task.cancel()
            await asyncio.gather(daemon._viewers_task, return_exceptions=True)
        await daemon._stats_client.aclose()


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

    async def test_local_mode_feeds_the_relay_only(
        self, make_daemon, config_path, quadstream, twitch
    ):
        write_config(config_path, mode="local")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.go_offline("b")
            twitch.go_live("e")
            await loops.refresh()
            await loops.recompose()
        assert store_channels(daemon) == ["a", "e", "c", "d"]
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


E_OVER_B = {"1": {"holder": "e", "replaced": "b"}}
ACTIONS: dict[str, tuple[Any, Any, dict[str, Any], list[str]]] = {
    # name: (setup action, action, saved holds, slots after)
    "place": (None, lambda d: d.place("e", 1), E_OVER_B, ["a", "e", "c", "d"]),
    "swap": (None, lambda d: d.swap(0, 1), {}, ["b", "a", "c", "d"]),
    "unpin": (lambda d: d.place("e", 1), lambda d: d.unpin(1), {}, ["a", "b", "c", "d"]),
    "unpin_all": (lambda d: d.place("e", 1), lambda d: d.unpin_all(), {}, ["a", "b", "c", "d"]),
}


class TestOperatorActions:
    """Each action updates the overrides, saves them and requests one recompose."""

    @pytest.mark.parametrize("name", ACTIONS)
    async def test_action_recomposes_once_and_persists(self, make_daemon, twitch, state_file, name):
        setup, action, holds, slots = ACTIONS[name]
        twitch.go_live("e")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            if setup is not None:
                await setup(daemon)
                await settle()
            with patch("quadlink.daemon.compose", wraps=compose) as composed:
                await action(daemon)
                await settle()
            assert composed.call_count == 1
        assert store_channels(daemon) == slots
        saved = json.loads(state_file.read_text())
        assert saved["holds"] == holds
        assert saved["channels"] == slots

    async def test_invalid_action_raises_and_changes_nothing(self, make_daemon, state_file):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            saved = state_file.read_text()
            with pytest.raises(OverrideError, match="zz is not live"):
                await daemon.place("zz", 0)
            with pytest.raises(OverrideError, match="use a slot from 0 to 3"):
                await daemon.unpin(4)
            assert not daemon._recompose_requested.is_set()
        assert daemon.overrides == Overrides()
        assert state_file.read_text() == saved

    async def test_channel_actions_wait_for_a_config(self, make_daemon):
        daemon = make_daemon()
        with pytest.raises(OverrideError, match="no config is loaded yet"):
            await daemon.place("a", 0)

    async def test_restart_restores_overrides_and_slots(self, make_daemon, twitch):
        twitch.go_live("e")
        first = make_daemon()
        async with stepped(first) as loops:
            await loops.refresh()
            await first.swap(0, 3)
            await first.place("e", 1)
            await settle()
        assert first.quad_state.channels == ("d", "e", "c", "a")

        second = make_daemon()
        assert second.overrides == first.overrides
        assert second.quad_state.channels == ("d", "e", "c", "a")
        async with stepped(second) as loops:
            await loops.refresh()
        assert store_channels(second) == ["d", "e", "c", "a"]

    async def test_hold_ends_after_the_grace(self, make_daemon, twitch, state_file):
        twitch.go_live("e")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            await daemon.place("e", 1)
            twitch.go_offline("e")
            for absent in range(1, GRACE_REFRESHES + 1):
                await loops.refresh()
                assert daemon.overrides.holds == {1: Hold("e", "b")}
                assert daemon.overrides.absent == {"e": absent}
            with structlog.testing.capture_logs() as logs:
                await loops.refresh()
        assert daemon.overrides.holds == {}
        assert events(logs, "holds ended")[0]["channels"] == ["e"]
        # the tile goes back to the channel the drop put off it
        assert store_channels(daemon) == ["a", "b", "c", "d"]
        assert json.loads(state_file.read_text())["holds"] == {}

    async def test_holder_back_within_the_grace_reclaims_its_tile(self, make_daemon, twitch):
        twitch.go_live("e")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            await daemon.place("e", 3)
            twitch.go_offline("e")
            await loops.refresh()
            # D stays off: it is replaced, not merely out-ranked
            assert "d" not in store_channels(daemon)
            twitch.go_live("e")
            await loops.refresh()
        assert store_channels(daemon) == ["a", "b", "c", "e"]

    async def test_empty_pool_does_not_run_out_the_grace(self, make_daemon, twitch):
        twitch.go_live("e")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            await daemon.place("e", 0)
            twitch.go_offline("e", *CHANNELS)
            for _ in range(GRACE_REFRESHES + 2):
                await loops.refresh()
        assert daemon.overrides.holds == {0: Hold("e", "a")}

    async def test_channel_removed_from_the_config_clears_at_once(
        self, make_daemon, config_path, twitch
    ):
        twitch.go_live("e")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            await daemon.place("e", 0)
            write_config(config_path, channels=("b", "c", "d"))
            await loops.refresh()
        assert daemon.overrides == Overrides()

    async def test_snapshot(self, make_daemon, config_path, twitch):
        twitch.display["g"] = "Gamma"
        twitch.go_live("g")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            # G is live but off screen; the drop puts A off tile 0
            write_config(config_path, channels=("a", "b", "c", "d", "e", "f", "g"))
            await loops.refresh()
            await daemon.place("g", 0)
            twitch.live["f"] = LookupFailed("timed out")
            await loops.refresh()
        snap = daemon.snapshot()
        assert (snap.slots[0].channel, snap.slots[0].display_name) == ("g", "Gamma")
        assert snap.slots[0].pinned
        views = {c.channel: c for c in snap.channels}
        assert (views["a"].state, views["a"].live) == (DashboardState.LIVE, True)
        assert (views["g"].state, views["g"].pin_slot) == (DashboardState.IN_QUAD, 0)
        assert views["f"].state == DashboardState.LOOKUP_FAILED
        assert snap.refreshed_at == daemon.statuses_at

    async def test_snapshot_shows_a_tile_held_during_the_grace(self, make_daemon, twitch):
        twitch.go_live("e")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            await daemon.place("e", 3)
            twitch.go_offline("e")
            await loops.refresh()
        snap = daemon.snapshot()
        held = snap.slots[3]
        assert (held.pinned, held.reserved_for) == (False, "e")
        views = {c.channel: c for c in snap.channels}
        assert (views["e"].state, views["e"].pin_slot) == (DashboardState.OFFLINE, 3)

    async def test_swap_waits_for_a_live_pool(self, make_daemon, twitch):
        daemon = make_daemon()
        with pytest.raises(OverrideError, match="no live channels yet"):
            await daemon.swap(0, 1)
        twitch.go_offline(*CHANNELS)
        async with stepped(daemon) as loops:
            await loops.refresh()
            with pytest.raises(OverrideError, match="no live channels yet"):
                await daemon.swap(0, 1)
            assert not daemon._recompose_requested.is_set()

    async def test_snapshot_before_any_refresh(self, make_daemon):
        snap = make_daemon().snapshot()
        assert [s.channel for s in snap.slots] == [""] * 4
        assert snap.channels == []


class TestViewerCounts:
    """Each refresh fetches viewer counts beside the lookups, never waiting on them."""

    async def test_counts_reach_the_snapshot_for_live_channels(self, make_daemon, twitch, stats):
        started = datetime(2026, 10, 23, 17, 0, 48, tzinfo=UTC)
        # "e" is offline to the lookup, which wins over a lagging provider
        stats.live = {"a": Stats(1108, started), "e": Stats(5, started)}
        daemon = make_daemon()
        notices = 0

        def count() -> None:
            nonlocal notices
            notices += 1

        daemon.watch(count)
        async with stepped(daemon) as loops:
            await loops.refresh()
        assert stats.calls == [sorted(CONFIGURED)]
        # refresh, recompose, and the counts' arrival
        assert notices == 3
        views = {c.channel: c for c in daemon.snapshot().channels}
        assert (views["a"].viewers, views["a"].started_at) == (1108, started)
        assert (views["b"].viewers, views["b"].started_at) == (None, None)
        assert (views["e"].viewers, views["e"].started_at) == (None, None)

    async def test_a_slow_fetch_does_not_hold_the_quad(self, make_daemon, quadstream, stats):
        stats.gate = asyncio.Event()
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            assert quadstream.pushes == [("secret", CHANNELS)]
            assert store_channels(daemon) == list(CHANNELS)
            # a fetch still running when the next refresh starts is not doubled
            await loops.refresh()
            assert len(stats.calls) == 1
            stats.gate.set()
            await settle()
            await loops.refresh()
            assert len(stats.calls) == 2

    async def test_config_change_rebuilds_the_provider(self, make_daemon, config_path):
        built: list[Any] = []

        def provider_for(config: Any, client: Any) -> FakeStats:
            built.append(config.twitch_api)
            return FakeStats()

        daemon = make_daemon()
        with patch("quadlink.daemon.provider_for", provider_for):
            async with stepped(daemon) as loops:
                await loops.refresh()
                write_config(config_path, twitch_api={"client_id": "id", "client_secret": "s"})
                await loops.refresh()
        assert [api.client_id if api else None for api in built] == [None, "id"]


class TestDashboardFeed:
    """What the dashboard API reads: change notices, login state, the API itself."""

    async def test_refresh_recompose_and_actions_notify_watchers(self, make_daemon):
        daemon = make_daemon()
        notices = 0

        def count() -> None:
            nonlocal notices
            notices += 1

        daemon.watch(count)
        async with stepped(daemon) as loops:
            await loops.refresh()
            # the refresh, then the recompose it requested
            assert notices == 2
            await daemon.unpin_all()
            assert notices == 3
            await settle()
            assert notices == 4

    async def test_snapshot_shows_the_quadstream_login(self, make_daemon, config_path, quadstream):
        daemon = make_daemon()
        assert daemon.snapshot().quadstream == QuadStreamLogin.OFF
        async with stepped(daemon) as loops:
            await loops.refresh()
            assert daemon.snapshot().quadstream == QuadStreamLogin.LOGGED_IN
            write_config(config_path, secret="wrong")
            await loops.refresh()
            assert daemon.snapshot().quadstream == QuadStreamLogin.FAILED
            write_config(config_path, mode="local")
            await loops.refresh()
            assert daemon.snapshot().quadstream == QuadStreamLogin.OFF

    async def test_api_drives_the_daemon(self, make_daemon):
        daemon = make_daemon()
        webui = WebUI(daemon.config_loader, dashboard=daemon)
        headers = {TOKEN_HEADER: webui.token}
        async with stepped(daemon) as loops, TestClient(TestServer(webui.app)) as client:
            await loops.refresh()
            resp = await client.post(
                "/api/actions", json={"action": "swap", "a": 2, "b": 3}, headers=headers
            )
            assert resp.status == 200
            assert (await resp.json())["slots"][2]["channel"] == "d"
            resp = await client.post(
                "/api/actions",
                json={"action": "place", "channel": "zz", "slot": 0},
                headers=headers,
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "zz is not live or does not exist"
            events = await client.get(f"/api/events?token={webui.token}")
            first = await events.content.readuntil(b"\n\n")
            first = first.removeprefix(b"retry: 2000\n")
            assert json.loads(first[len(b"data: ") :])["slots"][0]["channel"] == "a"
            await daemon.swap(0, 1)
            await settle()
            moved = await events.content.readuntil(b"\n\n")
            assert json.loads(moved[len(b"data: ") :])["slots"][0]["channel"] == "b"
            events.close()
        assert daemon.overrides.holds == {}


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

    async def test_log_level_changes_on_reload(
        self, make_daemon, config_path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(logconfig, "_level_num", logconfig.LOG_LEVELS["info"])
        monkeypatch.setattr(logconfig, "_pinned", False)
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            assert logconfig._level_num == logconfig.LOG_LEVELS["info"]
            write_config(config_path, log_level="debug")
            await loops.refresh()
        assert logconfig._level_num == logconfig.LOG_LEVELS["debug"]

    async def test_pinned_log_level_is_not_overridden_by_config(
        self, make_daemon, config_path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(logconfig, "_level_num", logconfig.LOG_LEVELS["warning"])
        monkeypatch.setattr(logconfig, "_pinned", True)
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            write_config(config_path, log_level="debug")
            await loops.refresh()
        assert logconfig._level_num == logconfig.LOG_LEVELS["warning"]

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
                assert await loops.refresh() == daemon.interval
            (warning,) = events(logs, "quadstream login failed, retrying after each refresh")
            assert warning["reason"] == "bad credentials"
            assert daemon.quadstream_client is None
            quadstream.accounts["user"] = "rotated"
            await loops.refresh()
        assert daemon.quadstream_client is not old
        assert [c.secret for c in quadstream.clients] == ["secret", "rotated", "rotated"]
        assert quadstream.pushes == [("secret", CHANNELS), ("rotated", CHANNELS)]


class TestQuadStreamDown:
    """While quadstream.tv refuses the login, the relay keeps following the pool."""

    async def test_local_tiles_follow_the_pool_and_the_login_recovers(
        self, make_daemon, quadstream, twitch
    ):
        daemon = make_daemon()
        quadstream.accounts["user"] = "other"
        async with stepped(daemon) as loops:
            with structlog.testing.capture_logs() as down:
                await loops.refresh()
                assert store_channels(daemon) == list(CHANNELS)
                twitch.go_offline("b")
                twitch.go_live("e")
                await loops.refresh()
                assert store_channels(daemon) == ["a", "e", "c", "d"]
                await loops.refresh()
            quadstream.accounts["user"] = "secret"
            with structlog.testing.capture_logs() as up:
                await loops.refresh()
                await loops.refresh()
        assert twitch.lookups == 5 * len(CONFIGURED)
        (warning,) = events(down, "quadstream login failed, retrying after each refresh")
        assert warning["log_level"] == "warning"
        assert warning["reason"] == "bad credentials"
        (recovered,) = events(up, "quadstream login recovered")
        assert recovered["log_level"] == "info"
        # the quad on screen at recovery, pushed once
        assert quadstream.pushes == [("secret", ("a", "e", "c", "d"))]
        assert len(quadstream.clients) == 4

    async def test_one_login_attempt_per_refresh(self, make_daemon, quadstream):
        daemon = make_daemon()
        quadstream.accounts["user"] = "other"
        async with stepped(daemon) as loops:
            await loops.refresh()
            # web actions recompose without a refresh
            await loops.recompose()
            await loops.recompose()
            assert len(quadstream.clients) == 1
            await loops.refresh()
        assert len(quadstream.clients) == 2
        assert quadstream.pushes == []

    async def test_failed_push_after_recovery_is_retried(self, make_daemon, quadstream):
        daemon = make_daemon()
        quadstream.accounts["user"] = "other"
        async with stepped(daemon) as loops:
            await loops.refresh()
            quadstream.accounts["user"] = "secret"
            quadstream.refuse_pushes = 1
            await loops.refresh()
            assert quadstream.pushes == []
            await loops.refresh()
        # logged in once; the second push needs no new login
        assert len(quadstream.clients) == 2
        assert quadstream.pushes == [("secret", CHANNELS)]

    async def test_remote_mode_keeps_looking_up_while_down(
        self, make_daemon, config_path, quadstream, twitch
    ):
        write_config(config_path, mode="remote")
        daemon = make_daemon()
        quadstream.accounts["user"] = "other"
        async with stepped(daemon) as loops:
            await loops.refresh()
            twitch.go_offline("b")
            twitch.go_live("e")
            await loops.refresh()
            quadstream.accounts["user"] = "secret"
            await loops.refresh()
        assert twitch.lookups == 3 * len(CONFIGURED)
        assert quadstream.pushes == [("secret", ("a", "e", "c", "d"))]
        assert store_channels(daemon) == [None] * 4


class TestLoginAndReadiness:
    """/ready turns green once a refresh completes, whatever quadstream.tv does."""

    async def test_ready_after_a_completed_refresh(self, make_daemon):
        daemon = make_daemon(enable_health_server=True, interval=20)
        assert daemon.health_server is not None
        assert daemon.health_server.max_age == 60
        assert not daemon.health_server.is_ready()
        async with stepped(daemon) as loops:
            await loops.refresh()
        assert daemon.health_server.is_ready()

    async def test_failed_login_is_ready_and_retries(self, make_daemon, quadstream, twitch):
        daemon = make_daemon(enable_health_server=True)
        quadstream.accounts["user"] = "other"
        async with stepped(daemon) as loops:
            assert await loops.refresh() == daemon.interval
            assert daemon.health_server.is_ready()
            assert twitch.lookups == len(CONFIGURED)
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
        """The login follows the lookups, as in the daemon; the exit code still fails."""
        quadstream.accounts["user"] = "other"
        started = time.monotonic()
        with structlog.testing.capture_logs() as logs:
            assert self.run(config_path) == 1
        assert time.monotonic() - started < 5
        assert not [e for e in logs if "retrying" in e["event"]]
        (failed,) = events(logs, "daemon failed")
        assert "login" in failed["error"]
        assert twitch.lookups == len(CONFIGURED)
        assert len(quadstream.clients) == 1
        assert quadstream.pushes == []

    def test_failed_push_exits_one(self, config_path, quadstream, twitch):
        """A push QuadStream refuses fails one-shot exactly like a failed login."""
        quadstream.refuse_pushes = 1
        with structlog.testing.capture_logs() as logs:
            assert self.run(config_path) == 1
        (failed,) = events(logs, "daemon failed")
        assert "update" in failed["error"]
        assert quadstream.pushes == []

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


class TestOutsideChannels:
    """Channels outside the config, placed from Explore, hold a tile until the hold ends."""

    async def test_a_live_channel_holds_the_tile_and_stays_looked_up(self, make_daemon, twitch):
        twitch.go_live("zed")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            await daemon.place("Zed", 2)
            await settle()
            assert daemon.quad_state.channels[2] == "zed"
            assert {c.channel: c for c in daemon.snapshot().channels}["zed"].extra
            await loops.refresh()
            assert daemon.quad_state.channels[2] == "zed"

    async def test_an_offline_channel_or_a_bad_name_is_refused(self, make_daemon, twitch):
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            with pytest.raises(OverrideError, match="not live"):
                await daemon.place("zed", 2)
            with pytest.raises(OverrideError, match="not a Twitch channel name"):
                await daemon.place("no such name", 2)
        assert daemon.overrides.extras == frozenset()

    async def test_unpin_takes_it_off_screen_and_out_of_the_list(self, make_daemon, twitch):
        twitch.go_live("zed")
        daemon = make_daemon()
        async with stepped(daemon) as loops:
            await loops.refresh()
            await daemon.place("zed", 2)
            await settle()
            await daemon.unpin(2)
            await settle()
            assert "zed" not in daemon.quad_state.channels
            assert "zed" not in {c.channel for c in daemon.snapshot().channels}
            assert daemon.overrides.extras == frozenset()

    async def test_outside_holds_do_not_survive_a_restart(self, make_daemon, twitch):
        twitch.go_live("zed")
        first = make_daemon()
        async with stepped(first) as loops:
            await loops.refresh()
            await first.place("zed", 2)
            await settle()
        second = make_daemon()
        async with stepped(second) as loops:
            await loops.refresh()
            await settle()
        assert second.overrides.extras == frozenset()
        assert "zed" not in second.quad_state.channels
