"""Tests for the Explore directory over Twitch GQL."""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from quadlink.explore import TOP_FETCH, TOP_LIMIT, Category, DirectoryError, GQLDirectory, Sort


def _node(id: str, name: str, viewers: int | None, live: int | None = 3) -> dict[str, Any]:
    return {
        "id": id,
        "name": name,
        "viewersCount": viewers,
        "broadcastersCount": live,
        "boxArtURL": f"https://static-cdn.jtvnw.net/ttv-boxart/{id}-52x70.jpg",
    }


STREAM = {
    "title": "speedrun",
    "viewersCount": 42,
    "createdAt": "2026-10-24T16:09:40Z",
    "language": "en",
    "previewImageURL": "https://static-cdn.jtvnw.net/previews-ttv/live_user_zed-440x248.jpg",
    "broadcaster": {"login": "zed", "displayName": "Zed"},
    "game": {"name": "Destiny 2"},
    "freeformTags": [{"name": "English"}, {"name": "Chill"}],
}


class Twitch:
    """A GQL endpoint answering by the query's root field."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.errors: list[Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.status != 200:
            return httpx.Response(self.status)
        if self.errors is not None:
            return httpx.Response(200, json={"errors": self.errors, "data": None})
        query, variables = body["query"], body["variables"]
        assert request.headers["Client-ID"]
        if "searchCategories" in query:
            nodes = [
                _node("1", "Destiny", 12),
                _node("2", "Destiny 2", 1907),
                _node("3", "Dead", None),
            ]
            data = {"searchCategories": {"edges": [{"node": n} for n in nodes]}}
        elif "games(" in query:
            assert variables == {"first": TOP_FETCH}
            counts = [90, 70, 80, *range(TOP_FETCH - 3)]
            nodes = [_node(str(i), f"c{i}", v) for i, v in enumerate(counts)]
            data = {"games": {"edges": [{"node": n} for n in nodes]}}
        elif query.startswith("query($first"):
            edges = [{"node": STREAM}]
            data = {"streams": {"edges": edges, "pageInfo": {"hasNextPage": False}}}
        elif "streams(" in query:
            if variables["id"] == "404":
                data = {"game": None}
            else:
                edges = [{"node": STREAM}, {"node": {**STREAM, "broadcaster": None}}]
                data = {
                    "game": {
                        **_node(variables["id"], "Destiny 2", 1907, 249),
                        "streams": {"edges": edges, "pageInfo": {"hasNextPage": True}},
                    }
                }
        else:
            raise AssertionError(query)
        return httpx.Response(200, json={"data": data})


@pytest.fixture
def twitch() -> Twitch:
    return Twitch()


@pytest.fixture
async def directory(twitch: Twitch) -> Any:
    async with httpx.AsyncClient(transport=httpx.MockTransport(twitch.handler)) as client:
        yield GQLDirectory(client)


async def test_search_puts_live_categories_first(directory: GQLDirectory) -> None:
    found = await directory.search(" destiny ")
    assert [c.name for c in found] == ["Destiny 2", "Destiny", "Dead"]
    assert found[0] == Category(
        "2", "Destiny 2", "https://static-cdn.jtvnw.net/ttv-boxart/2-52x70.jpg", 1907, 3
    )


async def test_empty_search_asks_nothing(directory: GQLDirectory, twitch: Twitch) -> None:
    assert await directory.search("  ") == []
    assert twitch.requests == []


async def test_top_is_the_most_watched_by_the_reported_counts(directory: GQLDirectory) -> None:
    top = [c.viewers for c in await directory.top()]
    assert top[:4] == [90, 80, 70, TOP_FETCH - 4]
    assert top == sorted(top, reverse=True) and len(top) == TOP_LIMIT


async def test_streams(directory: GQLDirectory, twitch: Twitch) -> None:
    listing = await directory.streams("497057", Sort.NEW)
    assert listing is not None
    assert (listing.category.name, listing.category.live, listing.more) == ("Destiny 2", 249, True)
    # a stream without a broadcaster (a banned account) is left out
    [s] = listing.streams
    assert (s.login, s.display_name, s.title, s.viewers, s.language) == (
        "zed",
        "Zed",
        "speedrun",
        42,
        "EN",
    )
    assert s.started_at == datetime(2026, 10, 24, 16, 9, 40, tzinfo=UTC)
    assert s.tags == ("English", "Chill")
    assert s.category == "Destiny 2"
    assert twitch.requests[0]["variables"] == {
        "id": "497057",
        "first": 100,
        "sort": "RECENT",
        "languages": None,
    }


async def test_streams_in_a_language(directory: GQLDirectory, twitch: Twitch) -> None:
    await directory.streams("497057", Sort.VIEWERS, "DE")
    await directory.streams("497057", Sort.VIEWERS)
    assert [r["variables"]["languages"] for r in twitch.requests] == [["DE"], None]


async def test_streams_of_all_categories(directory: GQLDirectory, twitch: Twitch) -> None:
    listing = await directory.streams(None, Sort.VIEWERS, "EN")
    assert listing is not None
    assert (listing.category, [s.login for s in listing.streams], listing.more) == (
        None,
        ["zed"],
        False,
    )
    assert twitch.requests[0]["variables"] == {
        "sort": "VIEWER_COUNT",
        "languages": ["EN"],
        "first": 30,
    }


async def test_unknown_category_is_none(directory: GQLDirectory) -> None:
    assert await directory.streams("404", Sort.VIEWERS) is None


async def test_answers_are_cached_per_arguments(directory: GQLDirectory, twitch: Twitch) -> None:
    await directory.streams("1", Sort.VIEWERS)
    await directory.streams("1", Sort.VIEWERS)
    await directory.streams("1", Sort.NEW)
    await directory.search("Destiny")
    await directory.search("destiny")
    assert len(twitch.requests) == 3


@pytest.mark.parametrize("failure", ["status", "errors"])
async def test_failures_raise_directory_error(
    directory: GQLDirectory, twitch: Twitch, failure: str
) -> None:
    if failure == "status":
        twitch.status = 500
    else:
        twitch.errors = [{"message": "failed integrity check"}]
    with pytest.raises(DirectoryError):
        await directory.top()
    # a failure is not cached
    twitch.status, twitch.errors = 200, None
    assert await directory.top()
