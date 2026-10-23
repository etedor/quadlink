"""Viewer counts and start times of live Twitch channels, for the dashboard."""

import asyncio
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx
import structlog

from quadlink.config.models import Config

logger = structlog.get_logger()

GQL_URL = "https://gql.twitch.tv/gql"
# Twitch's own web client ID; streamlink and the Twitch plugin send it too
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX_STREAMS_URL = "https://api.twitch.tv/helix/streams"

# Helix takes at most 100 user_login values; GQL has taken 500 in one query,
# but has no documented limit, so it gets the same chunks
BATCH = 100
# seconds one HTTP request may take, short enough that a failed Helix call leaves
# GQL time inside FETCH_WAIT
REQUEST_WAIT = 2.0
# seconds one refresh's fetch may take in all
FETCH_WAIT = 5.0
# failed fetches in a row that keep the last counts; one more clears them
STALE_REFRESHES = 3
# seconds before a Helix token's expiry that it counts as expired
TOKEN_MARGIN = 60.0

_LOGIN = re.compile(r"[a-z0-9_]+")

_GQL_QUERY = (
    "query($logins: [String!]) "
    "{ users(logins: $logins) { login stream { viewersCount createdAt } } }"
)


@dataclass(frozen=True)
class Stats:
    """A live channel's numbers.

    Attributes:
        viewers: Current viewer count.
        started_at: When the stream started (UTC).
    """

    viewers: int
    started_at: datetime


class ViewerStats(Protocol):
    """A source of viewer counts."""

    async def fetch(self, logins: list[str]) -> dict[str, Stats]:
        """Stats of every live channel among logins; offline and unknown ones are absent."""
        ...


class StatsError(Exception):
    """A provider's response could not be used."""


def _chunks(logins: list[str]) -> Iterator[list[str]]:
    for i in range(0, len(logins), BATCH):
        yield logins[i : i + BATCH]


def _time(value: str) -> datetime:
    # Twitch sends "2026-10-23T17:00:48Z"
    return datetime.fromisoformat(value)


class GQLStats:
    """Twitch's public GQL API; needs no registration."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def fetch(self, logins: list[str]) -> dict[str, Stats]:
        """Stats of the live channels among logins, one request per 100 logins."""
        found: dict[str, Stats] = {}
        for chunk in _chunks(logins):
            found |= await self._fetch(chunk)
        return found

    async def _fetch(self, logins: list[str]) -> dict[str, Stats]:
        resp = await self.client.post(
            GQL_URL,
            json={"query": _GQL_QUERY, "variables": {"logins": logins}},
            headers={"Client-ID": GQL_CLIENT_ID},
            timeout=REQUEST_WAIT,
        )
        resp.raise_for_status()
        body = resp.json()
        users = (body.get("data") or {}).get("users")
        if not isinstance(users, list):
            raise StatsError(f"Twitch GQL sent no users: {body.get('errors')}")
        found = {}
        # an unknown login comes back as null
        for user in users:
            stream = (user or {}).get("stream")
            if stream:
                found[user["login"]] = Stats(stream["viewersCount"], _time(stream["createdAt"]))
        return found


class HelixStats:
    """Twitch's official Helix API, with an app access token (client credentials)."""

    def __init__(self, client: httpx.AsyncClient, client_id: str, client_secret: str) -> None:
        self.client = client
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        # time.monotonic() after which the token counts as expired
        self._expires = 0.0

    async def fetch(self, logins: list[str]) -> dict[str, Stats]:
        """Stats of the live channels among logins, one request per 100 logins."""
        found: dict[str, Stats] = {}
        for chunk in _chunks(logins):
            found |= await self._fetch(chunk)
        return found

    async def _fetch(self, logins: list[str]) -> dict[str, Stats]:
        params = httpx.QueryParams({"user_login": logins, "first": BATCH})
        resp = await self._get(params, await self._access_token())
        # Twitch revokes app tokens before their expiry at times
        if resp.status_code == 401:
            self._token = None
            resp = await self._get(params, await self._access_token())
        resp.raise_for_status()
        return {
            s["user_login"]: Stats(s["viewer_count"], _time(s["started_at"]))
            for s in resp.json()["data"]
        }

    async def _get(self, params: httpx.QueryParams, token: str) -> httpx.Response:
        return await self.client.get(
            HELIX_STREAMS_URL,
            params=params,
            headers={"Client-Id": self.client_id, "Authorization": f"Bearer {token}"},
            timeout=REQUEST_WAIT,
        )

    async def _access_token(self) -> str:
        if self._token is not None and time.monotonic() < self._expires:
            return self._token
        resp = await self.client.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=REQUEST_WAIT,
        )
        if resp.status_code != 200:
            # status only: the body can echo the client secret
            raise StatsError(f"Twitch refused the Helix token request (HTTP {resp.status_code})")
        body = resp.json()
        self._token = str(body["access_token"])
        self._expires = time.monotonic() + float(body["expires_in"]) - TOKEN_MARGIN
        return self._token


class _Streak:
    """Logs a failure once per streak: a warning at its start, info at recovery."""

    def __init__(self, failed: str, recovered: str) -> None:
        self.failed_event = failed
        self.recovered_event = recovered
        self.count = 0

    def failed(self, error: BaseException) -> None:
        self.count += 1
        if self.count == 1:
            logger.warning(self.failed_event, error=_describe(error))

    def ok(self) -> None:
        if self.count:
            logger.info(self.recovered_event, failures=self.count)
        self.count = 0


def _describe(error: BaseException) -> str:
    return str(error) or type(error).__name__


class FallbackStats:
    """The primary provider, and the fallback whenever the primary fails."""

    def __init__(self, primary: ViewerStats, fallback: ViewerStats) -> None:
        self.primary = primary
        self.fallback = fallback
        self._streak = _Streak(
            "Helix viewer counts failed, using Twitch GQL",
            "Helix viewer counts recovered",
        )

    async def fetch(self, logins: list[str]) -> dict[str, Stats]:
        """Stats from the primary provider, else from the fallback."""
        try:
            found = await self.primary.fetch(logins)
        except Exception as e:
            self._streak.failed(e)
            return await self.fallback.fetch(logins)
        self._streak.ok()
        return found


def provider_for(config: Config, client: httpx.AsyncClient) -> ViewerStats:
    """Helix with GQL as fallback when config has Twitch API credentials, else GQL."""
    gql = GQLStats(client)
    api = config.twitch_api
    if api is None or api.client_secret is None:
        return gql
    return FallbackStats(HelixStats(client, api.client_id, api.client_secret), gql)


class ViewerCounts:
    """The latest stats of the configured channels, refreshed once per refresh.

    Attributes:
        provider: Where the stats come from; the daemon replaces it on a config change.
        stats: Login -> stats of each channel live at the last successful fetch.
    """

    def __init__(self, provider: ViewerStats) -> None:
        self.provider = provider
        self.stats: dict[str, Stats] = {}
        self._streak = _Streak("viewer counts failed", "viewer counts recovered")

    async def refresh(self, channels: list[str]) -> None:
        """Fetch the stats of channels' Twitch logins; never raises.

        A failed fetch keeps the last stats for STALE_REFRESHES fetches, then clears them.
        """
        logins = [c for c in channels if _LOGIN.fullmatch(c)]
        try:
            async with asyncio.timeout(FETCH_WAIT):
                found = await self.provider.fetch(logins)
        except Exception as e:
            self._streak.failed(e)
            if self._streak.count > STALE_REFRESHES:
                self.stats = {}
            return
        self._streak.ok()
        self.stats = found
