"""Tests for the viewer count providers and the per-refresh tracker; no network."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import structlog

from quadlink import viewers
from quadlink.config.models import Config
from quadlink.viewers import (
    STALE_REFRESHES,
    FallbackStats,
    GQLStats,
    HelixStats,
    Stats,
    ViewerCounts,
    provider_for,
)

T0 = datetime(2026, 10, 23, 17, 0, 48, tzinfo=UTC)
LIVE = Stats(1108, T0)

Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def gql_users(logins: list[str], live: dict[str, int]) -> dict[str, Any]:
    """A response shaped as gql.twitch.tv sent it: unknown logins are null."""
    users: list[dict[str, Any] | None] = []
    for login in logins:
        if login.startswith("unknown"):
            users.append(None)
        elif login in live:
            stream = {"viewersCount": live[login], "createdAt": "2026-10-23T17:00:48Z"}
            users.append({"login": login, "stream": stream})
        else:
            users.append({"login": login, "stream": None})
    return {"data": {"users": users}, "extensions": {"durationMilliseconds": 28}}


class TestGQL:
    async def test_parses_live_channels_only(self) -> None:
        sent: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Client-ID"] == viewers.GQL_CLIENT_ID
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json=gql_users(body["variables"]["logins"], {"a": 1108}))

        async with client(handler) as c:
            found = await GQLStats(c).fetch(["a", "b", "unknown1"])
        assert found == {"a": LIVE}
        assert len(sent) == 1
        assert "users(logins: $logins)" in sent[0]["query"]

    async def test_chunks_by_100(self) -> None:
        batches: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            logins = json.loads(request.content)["variables"]["logins"]
            batches.append(len(logins))
            return httpx.Response(200, json=gql_users(logins, {"c0": 1, "c150": 2}))

        async with client(handler) as c:
            found = await GQLStats(c).fetch([f"c{i}" for i in range(250)])
        assert batches == [100, 100, 50]
        assert {k: v.viewers for k, v in found.items()} == {"c0": 1, "c150": 2}

    async def test_no_logins_sends_nothing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - guard
            raise AssertionError("no request expected")

        async with client(handler) as c:
            assert await GQLStats(c).fetch([]) == {}

    async def test_errors_without_data_raise(self) -> None:
        body = {"errors": [{"message": "service timeout"}]}
        async with client(lambda r: httpx.Response(200, json=body)) as c:
            with pytest.raises(viewers.StatsError, match="service timeout"):
                await GQLStats(c).fetch(["a"])

    async def test_http_error_raises(self) -> None:
        async with client(lambda r: httpx.Response(500)) as c:
            with pytest.raises(httpx.HTTPStatusError):
                await GQLStats(c).fetch(["a"])


class Helix:
    """Fake id.twitch.tv and api.twitch.tv."""

    def __init__(self, live: dict[str, int]) -> None:
        self.live = live
        self.tokens_minted = 0
        self.valid: set[str] = set()
        self.stream_requests: list[list[str]] = []
        self.token_status = 200
        self.expires_in = 5000000

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            assert form == {
                "client_id": "id",
                "client_secret": "secret",
                "grant_type": "client_credentials",
            }
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"message": "secret echoed"})
            self.tokens_minted += 1
            token = f"t{self.tokens_minted}"
            self.valid.add(token)
            return httpx.Response(
                200,
                json={"access_token": token, "expires_in": self.expires_in, "token_type": "bearer"},
            )
        assert request.url.path == "/helix/streams"
        assert request.headers["Client-Id"] == "id"
        if request.headers["Authorization"].removeprefix("Bearer ") not in self.valid:
            return httpx.Response(401, json={"error": "Unauthorized"})
        logins = request.url.params.get_list("user_login")
        assert request.url.params["first"] == "100"
        self.stream_requests.append(logins)
        data = [
            {
                "user_login": login,
                "type": "live",
                "viewer_count": self.live[login],
                "started_at": "2026-10-23T17:00:48Z",
            }
            for login in logins
            if login in self.live
        ]
        return httpx.Response(200, json={"data": data, "pagination": {}})


class TestHelix:
    async def test_parses_streams(self) -> None:
        api = Helix({"a": 1108})
        async with client(api) as c:
            assert await HelixStats(c, "id", "secret").fetch(["a", "b"]) == {"a": LIVE}

    async def test_chunks_by_100(self) -> None:
        api = Helix({"c0": 1, "c120": 2})
        async with client(api) as c:
            found = await HelixStats(c, "id", "secret").fetch([f"c{i}" for i in range(201)])
        assert [len(r) for r in api.stream_requests] == [100, 100, 1]
        assert set(found) == {"c0", "c120"}

    async def test_token_is_cached(self) -> None:
        api = Helix({"a": 1})
        async with client(api) as c:
            helix = HelixStats(c, "id", "secret")
            await helix.fetch(["a"])
            await helix.fetch(["a"])
        assert api.tokens_minted == 1

    async def test_expired_token_is_replaced(self) -> None:
        api = Helix({"a": 1})
        # within TOKEN_MARGIN of expiry counts as expired
        api.expires_in = 30
        async with client(api) as c:
            helix = HelixStats(c, "id", "secret")
            await helix.fetch(["a"])
            await helix.fetch(["a"])
        assert api.tokens_minted == 2

    async def test_401_gets_a_new_token_and_retries_once(self) -> None:
        api = Helix({"a": 1})
        async with client(api) as c:
            helix = HelixStats(c, "id", "secret")
            await helix.fetch(["a"])
            api.valid.clear()
            assert await helix.fetch(["a"]) == {"a": Stats(1, T0)}
        assert api.tokens_minted == 2

    async def test_refused_token_raises_without_the_body(self) -> None:
        api = Helix({"a": 1})
        api.token_status = 403
        async with client(api) as c:
            with pytest.raises(viewers.StatsError) as caught:
                await HelixStats(c, "id", "secret").fetch(["a"])
        assert "403" in str(caught.value)
        assert "secret" not in str(caught.value)


class Fake:
    def __init__(self, result: dict[str, Stats] | Exception) -> None:
        self.result = result
        self.calls = 0

    async def fetch(self, logins: list[str]) -> dict[str, Stats]:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Hang:
    async def fetch(self, logins: list[str]) -> dict[str, Stats]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def events(logs: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == event]


class TestFallback:
    async def test_primary_wins_when_it_works(self) -> None:
        primary, fallback = Fake({"a": LIVE}), Fake({})
        assert await FallbackStats(primary, fallback).fetch(["a"]) == {"a": LIVE}
        assert fallback.calls == 0

    async def test_fallback_on_primary_failure_logs_once_per_streak(self) -> None:
        primary, fallback = Fake(RuntimeError("down")), Fake({"a": LIVE})
        both = FallbackStats(primary, fallback)
        with structlog.testing.capture_logs() as logs:
            assert await both.fetch(["a"]) == {"a": LIVE}
            assert await both.fetch(["a"]) == {"a": LIVE}
            primary.result = {}
            assert await both.fetch(["a"]) == {}
        failed = events(logs, "Helix viewer counts failed, using Twitch GQL")
        assert [(e["log_level"], e["error"]) for e in failed] == [("warning", "down")]
        assert [e["log_level"] for e in events(logs, "Helix viewer counts recovered")] == ["info"]

    async def test_both_failing_raises(self) -> None:
        both = FallbackStats(Fake(RuntimeError("helix")), Fake(RuntimeError("gql")))
        with pytest.raises(RuntimeError, match="gql"):
            await both.fetch(["a"])

    def test_provider_for_config(self, tmp_path: Any) -> None:
        base: dict[str, Any] = {"mode": "local", "priorities": {1: [{"urls": ["a"]}]}}
        c = httpx.AsyncClient()
        assert isinstance(provider_for(Config(**base), c), GQLStats)
        secret = tmp_path / "secret"
        secret.write_text("s3cret\n")
        config = Config(**base, twitch_api={"client_id": "id", "client_secret_file": str(secret)})
        chosen = provider_for(config, c)
        assert isinstance(chosen, FallbackStats)
        assert isinstance(chosen.primary, HelixStats)
        assert chosen.primary.client_secret == "s3cret"
        assert isinstance(chosen.fallback, GQLStats)


class TestViewerCounts:
    async def test_keeps_only_twitch_logins(self) -> None:
        seen: list[list[str]] = []

        class Spy:
            async def fetch(self, logins: list[str]) -> dict[str, Stats]:
                seen.append(logins)
                return {}

        await ViewerCounts(Spy()).refresh(["a", "twitch.tv/videos/123", "b_2"])
        assert seen == [["a", "b_2"]]

    async def test_failure_keeps_stale_counts_then_clears(self) -> None:
        provider = Fake({"a": LIVE})
        counts = ViewerCounts(provider)
        await counts.refresh(["a"])
        provider.result = RuntimeError("down")
        for _ in range(STALE_REFRESHES):
            await counts.refresh(["a"])
            assert counts.stats == {"a": LIVE}
        await counts.refresh(["a"])
        assert counts.stats == {}

    async def test_failure_streak_logs_a_warning_then_info(self) -> None:
        provider = Fake(RuntimeError("down"))
        counts = ViewerCounts(provider)
        with structlog.testing.capture_logs() as logs:
            await counts.refresh(["a"])
            await counts.refresh(["a"])
            provider.result = {"a": LIVE}
            await counts.refresh(["a"])
            provider.result = RuntimeError("again")
            await counts.refresh(["a"])
        assert [(e["log_level"], e["error"]) for e in events(logs, "viewer counts failed")] == [
            ("warning", "down"),
            ("warning", "again"),
        ]
        recovered = events(logs, "viewer counts recovered")
        assert [(e["log_level"], e["failures"]) for e in recovered] == [("info", 2)]
        assert counts.stats == {"a": LIVE}

    async def test_a_hung_provider_times_out(self) -> None:
        counts = ViewerCounts(Hang())
        counts.stats = {"a": LIVE}
        with (
            patch.object(viewers, "FETCH_WAIT", 0.01),
            structlog.testing.capture_logs() as logs,
        ):
            await counts.refresh(["a"])
        assert [e["error"] for e in events(logs, "viewer counts failed")] == ["TimeoutError"]
        assert counts.stats == {"a": LIVE}
