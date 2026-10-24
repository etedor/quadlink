"""Twitch categories and their live streams, for the dashboard's Explore view."""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypeVar

import httpx

from quadlink.viewers import GQL_CLIENT_ID, GQL_URL

# seconds one GQL request may take
REQUEST_WAIT = 4.0
# GQL refuses a second page without a browser integrity token, so one request is the
# whole listing; 100 is the largest page it serves
STREAMS_LIMIT = 100
TOP_LIMIT = 40
# Twitch ranks its top categories by a count older than the one it reports, so a few
# extra let the reported order pick the top ones
TOP_FETCH = 48
SEARCH_LIMIT = 10
# seconds an answer serves every request with the same arguments
SEARCH_TTL = 300.0
LISTING_TTL = 60.0

BOX_ART = (52, 70)
THUMBNAIL = (440, 248)

T = TypeVar("T")


class Sort(StrEnum):
    VIEWERS = "viewers"
    NEW = "new"


_GQL_SORT = {Sort.VIEWERS: "VIEWER_COUNT", Sort.NEW: "RECENT"}

_CATEGORY_FIELDS = (
    f"id name viewersCount broadcastersCount boxArtURL(width: {BOX_ART[0]}, height: {BOX_ART[1]})"
)

_TOP_QUERY = (
    "query($first: Int) { games(first: $first, options: {sort: VIEWER_COUNT}) "
    f"{{ edges {{ node {{ {_CATEGORY_FIELDS} }} }} }} }}"
)

_SEARCH_QUERY = (
    "query($query: String!, $first: Int) { searchCategories(query: $query, first: $first) "
    f"{{ edges {{ node {{ {_CATEGORY_FIELDS} }} }} }} }}"
)

_STREAMS_QUERY = (
    "query($id: ID, $first: Int, $sort: StreamSort, $languages: [Language!]) { game(id: $id) { "
    f"{_CATEGORY_FIELDS} "
    # Twitch filters by the broadcaster's language setting, which a stream's own
    # language tag disagrees with about one time in ten
    "streams(first: $first, options: {sort: $sort, broadcasterLanguages: $languages}) "
    "{ edges { node { "
    "title viewersCount createdAt language "
    f"previewImageURL(width: {THUMBNAIL[0]}, height: {THUMBNAIL[1]}) "
    "broadcaster { login displayName } freeformTags { name } } } "
    "pageInfo { hasNextPage } } } }"
)


@dataclass(frozen=True)
class Category:
    """A Twitch category.

    Attributes:
        id: Twitch's category ID.
        name: Display name, as stream metadata reports it.
        box_art: Cover image URL.
        viewers: Viewers across its live streams.
        live: Live streams in it.
    """

    id: str
    name: str
    box_art: str
    viewers: int
    live: int


@dataclass(frozen=True)
class Stream:
    """A live stream in a category listing."""

    login: str
    display_name: str
    title: str
    viewers: int
    started_at: datetime
    language: str
    tags: tuple[str, ...]
    thumbnail: str


@dataclass(frozen=True)
class Listing:
    """A category's live streams.

    Attributes:
        category: The category.
        streams: Its streams in the requested order, at most STREAMS_LIMIT.
        more: Whether Twitch has more than the listing holds.
    """

    category: Category
    streams: list[Stream]
    more: bool


class DirectoryError(Exception):
    """Twitch's answer could not be used."""


class Directory(Protocol):
    """A source of Twitch categories and their streams."""

    async def top(self) -> list[Category]:
        """The categories with the most viewers, most first."""
        ...

    async def search(self, query: str) -> list[Category]:
        """Categories matching query, live ones first."""
        ...

    async def streams(
        self, category_id: str, sort: Sort, language: str | None = None
    ) -> Listing | None:
        """The category's live streams, in one language when given (a Twitch code such as
        "EN"), or None when the category does not exist."""
        ...


class GQLDirectory:
    """Twitch's public GQL API, with each answer kept for a short while."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        # (kind, arguments) -> (monotonic expiry, answer)
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}

    async def top(self) -> list[Category]:
        return await self._cached(("top",), LISTING_TTL, self._top)

    async def search(self, query: str) -> list[Category]:
        query = query.strip()
        if not query:
            return []
        return await self._cached(
            ("search", query.lower()), SEARCH_TTL, lambda: self._search(query)
        )

    async def streams(
        self, category_id: str, sort: Sort, language: str | None = None
    ) -> Listing | None:
        return await self._cached(
            ("streams", category_id, sort, language),
            LISTING_TTL,
            lambda: self._streams(category_id, sort, language),
        )

    async def _cached(
        self, key: tuple[Any, ...], ttl: float, fetch: Callable[[], Awaitable[T]]
    ) -> T:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            answer: T = hit[1]
            return answer
        answer = await fetch()
        self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
        self._cache[key] = (now + ttl, answer)
        return answer

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.client.post(
                GQL_URL,
                json={"query": query, "variables": variables},
                headers={"Client-ID": GQL_CLIENT_ID},
                timeout=REQUEST_WAIT,
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise DirectoryError(str(e) or type(e).__name__) from e
        if body.get("errors") or not isinstance(body.get("data"), dict):
            raise DirectoryError(f"Twitch GQL refused the query: {body.get('errors')}")
        data: dict[str, Any] = body["data"]
        return data

    async def _top(self) -> list[Category]:
        data = await self._query(_TOP_QUERY, {"first": TOP_FETCH})
        found = [_category(e["node"]) for e in data["games"]["edges"]]
        return sorted(found, key=lambda c: c.viewers, reverse=True)[:TOP_LIMIT]

    async def _search(self, query: str) -> list[Category]:
        data = await self._query(_SEARCH_QUERY, {"query": query, "first": SEARCH_LIMIT})
        found = [_category(e["node"]) for e in data["searchCategories"]["edges"]]
        # Twitch ranks by name match; a live category beats a dead namesake
        return sorted(found, key=lambda c: c.viewers, reverse=True)

    async def _streams(self, category_id: str, sort: Sort, language: str | None) -> Listing | None:
        data = await self._query(
            _STREAMS_QUERY,
            {
                "id": category_id,
                "first": STREAMS_LIMIT,
                "sort": _GQL_SORT[sort],
                "languages": [language] if language else None,
            },
        )
        game = data.get("game")
        if not game:
            return None
        page = game["streams"]
        streams = [_stream(e["node"]) for e in page["edges"] if e["node"].get("broadcaster")]
        return Listing(_category(game), streams, bool(page["pageInfo"]["hasNextPage"]))


def _category(node: dict[str, Any]) -> Category:
    return Category(
        id=str(node["id"]),
        name=node["name"],
        box_art=node.get("boxArtURL") or "",
        viewers=node.get("viewersCount") or 0,
        live=node.get("broadcastersCount") or 0,
    )


def _stream(node: dict[str, Any]) -> Stream:
    who = node["broadcaster"]
    return Stream(
        login=who["login"],
        display_name=who.get("displayName") or who["login"],
        title=node.get("title") or "",
        viewers=node.get("viewersCount") or 0,
        # Twitch sends "2026-10-23T17:00:48Z"
        started_at=datetime.fromisoformat(node["createdAt"]),
        language=(node.get("language") or "").upper(),
        tags=tuple(t["name"] for t in node.get("freeformTags") or []),
        thumbnail=node.get("previewImageURL") or "",
    )
