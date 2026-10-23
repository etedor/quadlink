"""Tests for the dashboard API: guards, snapshot, events, actions and stills."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient, TestServer

from quadlink import api
from quadlink.config.loader import ConfigLoader
from quadlink.dashboard import QuadStreamLogin, Snapshot, snapshot
from quadlink.overrides import OverrideError
from quadlink.quad import Overrides, QuadState
from quadlink.relay.playlist import Segment
from quadlink.relay.segments import SegmentProxy
from quadlink.types import ChannelState, ChannelStatus, Metadata, Stream
from quadlink.webui import TOKEN_HEADER, WebUI

# the relay's fixtures: real segments encoded by ffmpeg
from tests.test_relay.conftest import ffmpeg, ts_run  # noqa: F401

THUMB_A = "https://static-cdn.jtvnw.net/previews-ttv/live_user_a-320x180.jpg"


def _live(channel: str, name: str, title: str = "t", category: str = "Games") -> ChannelStatus:
    stream = Stream(url=f"https://twitch.tv/{channel}", metadata=Metadata(name, category, title))
    return ChannelStatus(channel, ChannelState.LIVE, [100], stream=stream, priority=100)


def _snapshot(slots: tuple[str, ...] = ("a", "", "b", "")) -> Snapshot:
    statuses = [
        _live("a", "Alpha", title="hello", category="Chess"),
        _live("b", "b"),
        ChannelStatus("c", ChannelState.OFFLINE, [50, 10]),
        ChannelStatus("d", ChannelState.REJECTED, [50], reason="category blocked"),
    ]
    overrides = Overrides(pins={"a": 0, "c": 1}, benches=frozenset({"d"}))
    return snapshot(
        statuses, QuadState(channels=slots), overrides, [], 1700000000.5, QuadStreamLogin.FAILED
    )


class FakeDashboard:
    def __init__(self, segments: SegmentProxy, ffmpeg_path: str | None) -> None:
        self.segments = segments
        self.ffmpeg = ffmpeg_path
        self.snap = _snapshot()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: OverrideError | None = None
        self.watchers: list[Any] = []

    def snapshot(self) -> Snapshot:
        return self.snap

    def watch(self, callback: Any) -> None:
        self.watchers.append(callback)

    def change(self, snap: Snapshot | None = None) -> None:
        if snap is not None:
            self.snap = snap
        for callback in self.watchers:
            callback()

    def _act(self, name: str, **params: Any) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((name, params))

    async def place(self, channel: str, slot: int) -> None:
        self._act("place", channel=channel, slot=slot)

    async def swap(self, a: int, b: int) -> None:
        self._act("swap", a=a, b=b)

    async def pin(self, slot: int) -> None:
        self._act("pin", slot=slot)

    async def unpin(self, slot: int) -> None:
        self._act("unpin", slot=slot)

    async def bench(self, channel: str) -> None:
        self._act("bench", channel=channel)

    async def unbench(self, channel: str) -> None:
        self._act("unbench", channel=channel)

    async def reset(self) -> None:
        self._act("reset")


async def _no_fetch(uri: str) -> bytes:  # pragma: no cover - guard
    raise AssertionError("fetch should not be called")


@pytest.fixture
async def proxy() -> AsyncIterator[SegmentProxy]:
    proxy = SegmentProxy(_no_fetch, None)
    yield proxy
    await proxy.aclose()


@pytest.fixture
def dashboard(proxy: SegmentProxy) -> FakeDashboard:
    return FakeDashboard(proxy, None)


@pytest.fixture
def webui(tmp_path: Path, dashboard: FakeDashboard) -> WebUI:
    config = tmp_path / "config.yaml"
    config.write_text("mode: local\npriorities:\n  1:\n    - urls: [a]\n")
    return WebUI(ConfigLoader(explicit_path=str(config)), dashboard=dashboard)


@pytest.fixture
async def client(webui: WebUI) -> AsyncIterator[TestClient]:
    async with TestClient(TestServer(webui.app)) as c:
        yield c


def _auth(webui: WebUI, **extra: str) -> dict[str, str]:
    return {TOKEN_HEADER: webui.token, **extra}


async def _event(resp: ClientResponse, timeout: float = 2.0) -> bytes:
    async with asyncio.timeout(timeout):
        return await resp.content.readuntil(b"\n\n")


def _data(event: bytes) -> dict[str, Any]:
    event = event.removeprefix(b"retry: 2000\n")
    assert event.startswith(b"data: ")
    return json.loads(event[len(b"data: ") :])


class TestGuards:
    async def test_state_needs_the_token_header(self, client: TestClient, webui: WebUI) -> None:
        assert (await client.get("/api/state")).status == 403
        assert (await client.get(f"/api/state?token={webui.token}")).status == 403
        assert (await client.get("/api/state", headers=_auth(webui))).status == 200

    async def test_actions_need_the_token_header(self, client: TestClient, webui: WebUI) -> None:
        resp = await client.post(f"/api/actions?token={webui.token}", json={"action": "reset"})
        assert resp.status == 403
        resp = await client.post(
            "/api/actions",
            data='{"action": "reset"}',
            headers=_auth(webui, **{"Content-Type": "text/plain"}),
        )
        assert resp.status == 415

    async def test_events_take_the_token_as_a_query_parameter(
        self, client: TestClient, webui: WebUI
    ) -> None:
        assert (await client.get("/api/events")).status == 403
        assert (await client.get("/api/events?token=wrong")).status == 403
        resp = await client.get(f"/api/events?token={webui.token}")
        assert resp.status == 200
        resp.close()

    @pytest.mark.parametrize(
        "headers",
        [
            {"Origin": "https://evil.example"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Host": "rebind.evil.example"},
        ],
    )
    async def test_events_still_check_host_and_origin(
        self, client: TestClient, webui: WebUI, headers: dict[str, str]
    ) -> None:
        resp = await client.get(f"/api/events?token={webui.token}", headers=headers)
        assert resp.status == 403

    @pytest.mark.parametrize("site", [None, "same-origin", "none"])
    async def test_still_needs_no_token(self, client: TestClient, site: str | None) -> None:
        headers = {} if site is None else {"Sec-Fetch-Site": site}
        resp = await client.get(
            "/api/slots/0/still.jpg?c=a", headers=headers, allow_redirects=False
        )
        assert resp.status == 302

    @pytest.mark.parametrize(
        "headers",
        [
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
            {"Origin": "https://evil.example"},
            {"Host": "rebind.evil.example"},
        ],
    )
    async def test_still_from_another_site_is_403(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        resp = await client.get(
            "/api/slots/0/still.jpg?c=a", headers=headers, allow_redirects=False
        )
        assert resp.status == 403


class TestState:
    async def test_snapshot_shape(self, client: TestClient, webui: WebUI) -> None:
        resp = await client.get("/api/state", headers=_auth(webui))
        assert resp.headers["Cache-Control"] == "no-store"
        body = await resp.json()
        assert body["v"] == 1
        assert body["refreshed_at"] == 1700000000.5
        assert body["quadstream"] == "failed"
        assert body["slots"] == [
            {
                "slot": 0,
                "channel": "a",
                "display_name": "Alpha",
                "title": "hello",
                "category": "Chess",
                "pinned": True,
                "reserved_for": None,
                "still": "/api/slots/0/still.jpg?c=a",
            },
            {
                "slot": 1,
                "channel": None,
                "display_name": None,
                "title": None,
                "category": None,
                "pinned": False,
                "reserved_for": "c",
                "still": None,
            },
            {
                "slot": 2,
                "channel": "b",
                "display_name": "b",
                "title": "t",
                "category": "Games",
                "pinned": False,
                "reserved_for": None,
                "still": "/api/slots/2/still.jpg?c=b",
            },
            {
                "slot": 3,
                "channel": None,
                "display_name": None,
                "title": None,
                "category": None,
                "pinned": False,
                "reserved_for": None,
                "still": None,
            },
        ]
        assert body["channels"][0] == {
            "channel": "a",
            "display_name": "Alpha",
            "title": "hello",
            "category": "Chess",
            "state": "in quad",
            "slot": 0,
            "pin_slot": 0,
            "benched": False,
            "priorities": [100],
            "priority": 100,
            "reason": None,
            "thumbnail": THUMB_A,
        }
        rows = [
            (c["channel"], c["state"], c["pin_slot"], c["benched"], c["priorities"], c["thumbnail"])
            for c in body["channels"][1:]
        ]
        assert rows == [
            ("b", "in quad", None, False, [100], THUMB_A.replace("_a-", "_b-")),
            ("c", "offline", 1, False, [50, 10], None),
            ("d", "benched", None, True, [50], None),
        ]
        assert body["channels"][3]["reason"] == "category blocked"
        assert body["channels"][2]["display_name"] == "c"

    async def test_state_is_built_per_request(
        self, client: TestClient, webui: WebUI, dashboard: FakeDashboard
    ) -> None:
        await client.get("/api/state", headers=_auth(webui))
        # changed with no notice to watchers
        dashboard.snap = _snapshot(("", "", "", "b"))
        body = await (await client.get("/api/state", headers=_auth(webui))).json()
        assert [s["channel"] for s in body["slots"]] == [None, None, None, "b"]


class TestEvents:
    async def _open(self, client: TestClient, webui: WebUI) -> ClientResponse:
        resp = await client.get(f"/api/events?token={webui.token}")
        assert resp.status == 200
        return resp

    async def test_sends_the_snapshot_on_connect_and_on_change(
        self, client: TestClient, webui: WebUI, dashboard: FakeDashboard
    ) -> None:
        resp = await self._open(client, webui)
        assert resp.headers["Content-Type"] == "text/event-stream"
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["X-Accel-Buffering"] == "no"
        raw = await _event(resp)
        # the reconnect delay comes once, with the first event
        assert raw.startswith(b"retry: 2000\ndata: ")
        assert _data(raw)["slots"][1]["channel"] is None
        dashboard.change(_snapshot(("a", "c", "b", "")))
        raw = await _event(resp)
        assert raw.startswith(b"data: ")
        assert _data(raw)["slots"][1]["channel"] == "c"
        resp.close()

    async def test_unchanged_snapshot_sends_nothing(
        self,
        client: TestClient,
        webui: WebUI,
        dashboard: FakeDashboard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(api, "HEARTBEAT", 0.3)
        resp = await self._open(client, webui)
        await _event(resp)
        dashboard.change()
        # the next thing on the stream is the heartbeat, not a repeat of the snapshot
        assert await _event(resp) == b": keepalive\n\n"
        resp.close()

    async def test_changes_coalesce(
        self,
        client: TestClient,
        webui: WebUI,
        dashboard: FakeDashboard,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(api, "EVENT_GAP", 1.0)
        resp = await self._open(client, webui)
        await _event(resp)
        for slots in (("a", "c", "b", ""), ("a", "", "b", "c"), ("", "", "", "c")):
            dashboard.change(_snapshot(slots))
            await asyncio.sleep(0)
        # the changes land within the gap after the first event: the next one carries
        # the newest state, and the states between are never sent
        newest = _data(await _event(resp))
        assert [s["channel"] for s in newest["slots"]] == [None, None, None, "c"]
        resp.close()

    async def test_heartbeat(
        self, client: TestClient, webui: WebUI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api, "HEARTBEAT", 0.05)
        resp = await self._open(client, webui)
        await _event(resp)
        assert await _event(resp) == b": keepalive\n\n"
        assert await _event(resp) == b": keepalive\n\n"
        resp.close()

    async def test_disconnect_removes_the_subscriber(
        self, client: TestClient, webui: WebUI, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api, "HEARTBEAT", 0.05)
        assert webui.api is not None
        resp = await self._open(client, webui)
        await _event(resp)
        assert len(webui.api._subscribers) == 1
        resp.close()
        async with asyncio.timeout(2):
            while webui.api._subscribers:
                await asyncio.sleep(0.01)

    async def test_shutdown_ends_open_streams(
        self, tmp_path: Path, dashboard: FakeDashboard
    ) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("mode: local\npriorities:\n  1:\n    - urls: [a]\n")
        webui = WebUI(ConfigLoader(explicit_path=str(config)), dashboard=dashboard)
        client = TestClient(TestServer(webui.app))
        await client.start_server()
        resp = await client.get(f"/api/events?token={webui.token}")
        await _event(resp)
        async with asyncio.timeout(2):
            await client.close()
        assert webui.api is not None and not webui.api._subscribers


class TestActions:
    async def _post(self, client: TestClient, webui: WebUI, body: Any) -> ClientResponse:
        return await client.post("/api/actions", json=body, headers=_auth(webui))

    @pytest.mark.parametrize(
        "body,call",
        [
            (
                {"action": "place", "channel": "a", "slot": 3},
                ("place", {"channel": "a", "slot": 3}),
            ),
            ({"action": "swap", "a": 0, "b": 2}, ("swap", {"a": 0, "b": 2})),
            ({"action": "pin", "slot": 1}, ("pin", {"slot": 1})),
            ({"action": "unpin", "slot": 1}, ("unpin", {"slot": 1})),
            ({"action": "bench", "channel": "b"}, ("bench", {"channel": "b"})),
            ({"action": "unbench", "channel": "b"}, ("unbench", {"channel": "b"})),
            ({"action": "reset"}, ("reset", {})),
        ],
    )
    async def test_action_calls_the_daemon_and_returns_the_snapshot(
        self,
        client: TestClient,
        webui: WebUI,
        dashboard: FakeDashboard,
        body: dict[str, Any],
        call: tuple[str, dict[str, Any]],
    ) -> None:
        resp = await self._post(client, webui, body)
        assert resp.status == 200
        assert (await resp.json())["v"] == 1
        assert dashboard.calls == [call]

    async def test_override_error_is_400_with_its_message(
        self, client: TestClient, webui: WebUI, dashboard: FakeDashboard
    ) -> None:
        dashboard.error = OverrideError("slot 2 is empty: there is no channel to pin")
        resp = await self._post(client, webui, {"action": "pin", "slot": 2})
        assert resp.status == 400
        assert await resp.json() == {"error": "slot 2 is empty: there is no channel to pin"}

    @pytest.mark.parametrize(
        "body,message",
        [
            ([], "must be a JSON object"),
            ({}, "does not exist"),
            ({"action": "explode"}, "does not exist"),
            ({"action": "pin"}, "takes slot"),
            ({"action": "pin", "slot": 1, "channel": "a"}, "takes slot"),
            ({"action": "reset", "slot": 1}, "takes no parameters"),
            ({"action": "pin", "slot": "1"}, "slot must be an integer"),
            ({"action": "pin", "slot": True}, "slot must be an integer"),
            ({"action": "pin", "slot": 1.0}, "slot must be an integer"),
            ({"action": "bench", "channel": 5}, "channel must be a string"),
        ],
    )
    async def test_invalid_body_is_400(
        self,
        client: TestClient,
        webui: WebUI,
        dashboard: FakeDashboard,
        body: Any,
        message: str,
    ) -> None:
        resp = await self._post(client, webui, body)
        assert resp.status == 400
        assert message in (await resp.json())["error"]
        assert dashboard.calls == []

    async def test_malformed_json_is_400(
        self, client: TestClient, webui: WebUI, dashboard: FakeDashboard
    ) -> None:
        resp = await client.post(
            "/api/actions",
            data="{nope",
            headers=_auth(webui, **{"Content-Type": "application/json"}),
        )
        assert resp.status == 400
        assert dashboard.calls == []


def _seg(seq: int) -> Segment:
    return Segment(
        seq=seq,
        uri=str(seq),
        duration=2.0,
        program_date_time=None,
        discontinuity=False,
        map_gen=None,
    )


class TestStills:
    @pytest.fixture
    async def fed(self, ffmpeg: str, ts_run: Any) -> AsyncIterator[FakeDashboard]:  # noqa: F811
        segs = ts_run(48000, 2)

        async def fetch(uri: str) -> bytes:
            return segs[int(uri)]

        proxy = SegmentProxy(fetch, ffmpeg)
        for seq in range(2):
            proxy.register(0, _seg(seq), "a")
        # the newest segment's converted output waits for a next one; its raw bytes do not
        await proxy.get(0, 0)
        async with asyncio.timeout(2):
            while proxy.latest(0, "a") is None:
                await asyncio.sleep(0.01)
        yield FakeDashboard(proxy, ffmpeg)
        await proxy.aclose()

    @pytest.fixture
    async def fed_client(self, tmp_path: Path, fed: FakeDashboard) -> AsyncIterator[TestClient]:
        config = tmp_path / "config.yaml"
        config.write_text("mode: local\npriorities:\n  1:\n    - urls: [a]\n")
        webui = WebUI(ConfigLoader(explicit_path=str(config)), dashboard=fed)
        async with TestClient(TestServer(webui.app)) as c:
            yield c

    async def test_still_from_a_cached_segment(
        self, fed_client: TestClient, ffmpeg: str  # noqa: F811
    ) -> None:
        resp = await fed_client.get("/api/slots/0/still.jpg?c=a")
        assert resp.status == 200
        assert resp.content_type == "image/jpeg"
        assert resp.headers["Cache-Control"] == "max-age=5"
        jpeg = await resp.read()
        assert jpeg[:2] == b"\xff\xd8"
        probe = await asyncio.create_subprocess_exec(
            str(Path(ffmpeg).with_name("ffprobe")),
            "-v",
            "error",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        out, _ = await probe.communicate(jpeg)
        assert out.decode().strip() == "480,270"

    async def test_viewers_share_one_decode(
        self, fed_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grabs = 0
        real = api.grab_frame

        async def counted(path: str, segment: bytes) -> bytes | None:
            nonlocal grabs
            grabs += 1
            return await real(path, segment)

        monkeypatch.setattr(api, "grab_frame", counted)
        resps = await asyncio.gather(
            *(fed_client.get("/api/slots/0/still.jpg?c=a") for _ in range(5))
        )
        bodies = {await r.read() for r in resps}
        assert [r.status for r in resps] == [200] * 5
        assert len(bodies) == 1
        assert grabs == 1

    async def test_cache_expires(
        self, fed_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(api, "STILL_TTL", 0.0)
        grabs = 0

        async def counted(path: str, segment: bytes) -> bytes | None:
            nonlocal grabs
            grabs += 1
            return b"\xff\xd8jpeg"

        monkeypatch.setattr(api, "grab_frame", counted)
        for _ in range(2):
            assert (await fed_client.get("/api/slots/0/still.jpg?c=a")).status == 200
        assert grabs == 2

    async def test_unwatched_slot_redirects_to_the_twitch_thumbnail(
        self, client: TestClient
    ) -> None:
        resp = await client.get("/api/slots/0/still.jpg?c=a", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == THUMB_A
        assert resp.headers["Cache-Control"] == "no-store"

    async def test_failed_decode_redirects(
        self, fed_client: TestClient, fed: FakeDashboard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fed.segments, "latest", lambda slot, channel: b"not a video")
        resp = await fed_client.get("/api/slots/0/still.jpg?c=a", allow_redirects=False)
        assert resp.status == 302
        assert resp.headers["Location"] == THUMB_A

    async def test_decode_timeout_gives_none(self, ffmpeg: str, ts_run: Any) -> None:  # noqa: F811
        segment = ts_run(48000, 1)[0]
        assert await api.grab_frame(ffmpeg, segment, timeout=0.0) is None

    @pytest.mark.parametrize("slot", ["1", "3"])
    async def test_empty_slot_is_404(self, client: TestClient, slot: str) -> None:
        resp = await client.get(f"/api/slots/{slot}/still.jpg?c=a", allow_redirects=False)
        assert resp.status == 404

    @pytest.mark.parametrize("slot", ["4", "-1", "01", "x"])
    async def test_unknown_slot_is_404(self, client: TestClient, slot: str) -> None:
        resp = await client.get(f"/api/slots/{slot}/still.jpg?c=a", allow_redirects=False)
        assert resp.status == 404

    async def test_channel_without_a_login_is_404(
        self, client: TestClient, dashboard: FakeDashboard
    ) -> None:
        channel = "youtube.com/watch?v=x"
        dashboard.snap = snapshot(
            [], QuadState(channels=(channel, "", "", "")), Overrides(), [], None
        )
        resp = await client.get(
            "/api/slots/0/still.jpg", params={"c": channel}, allow_redirects=False
        )
        assert resp.status == 404


class TestStillChannels:
    """A still URL names its channel; a slot never serves another channel's picture."""

    @pytest.fixture
    async def two(self, ffmpeg: str, ts_run: Any) -> AsyncIterator[FakeDashboard]:  # noqa: F811
        segs = ts_run(48000, 2)

        async def fetch(uri: str) -> bytes:
            return segs[int(uri[-1])]

        proxy = SegmentProxy(fetch, ffmpeg)
        for slot, channel in ((0, "a"), (1, "b")):
            for seq in range(2):
                proxy.register(slot, _seg(seq), channel)
        async with asyncio.timeout(5):
            while proxy.latest(0, "a") is None or proxy.latest(1, "b") is None:
                await asyncio.sleep(0.01)
        dashboard = FakeDashboard(proxy, ffmpeg)
        dashboard.snap = _snapshot(("a", "b", "", ""))
        yield dashboard
        await proxy.aclose()

    @pytest.fixture
    async def two_client(self, tmp_path: Path, two: FakeDashboard) -> AsyncIterator[TestClient]:
        config = tmp_path / "config.yaml"
        config.write_text("mode: local\npriorities:\n  1:\n    - urls: [a]\n")
        webui = WebUI(ConfigLoader(explicit_path=str(config)), dashboard=two)
        async with TestClient(TestServer(webui.app)) as c:
            yield c

    async def _get(self, client: TestClient, slot: int, channel: str) -> ClientResponse:
        return await client.get(
            f"/api/slots/{slot}/still.jpg", params={"c": channel}, allow_redirects=False
        )

    async def test_swap_never_serves_the_other_tiles_picture(
        self, two_client: TestClient, two: FakeDashboard
    ) -> None:
        # both stills are cached before the swap
        assert (await self._get(two_client, 0, "a")).status == 200
        assert (await self._get(two_client, 1, "b")).status == 200
        two.change(_snapshot(("b", "a", "", "")))
        for slot, channel in ((0, "b"), (1, "a"), (0, "a"), (1, "b")):
            resp = await self._get(two_client, slot, channel)
            assert resp.status == 302, (slot, channel)
            assert resp.headers["Location"] == THUMB_A.replace("_a-", f"_{channel}-")

    async def test_snapshot_still_urls_name_the_channel(self, two: FakeDashboard) -> None:
        slots = api.snapshot_json(two.snapshot())["slots"]
        assert [s["still"] for s in slots[:2]] == [
            "/api/slots/0/still.jpg?c=a",
            "/api/slots/1/still.jpg?c=b",
        ]

    @pytest.mark.parametrize("query", ["", "?c=", "?c=a%2Fb"])
    async def test_still_without_a_channel_login_is_404(
        self, client: TestClient, query: str
    ) -> None:
        resp = await client.get(f"/api/slots/0/still.jpg{query}", allow_redirects=False)
        assert resp.status == 404

    async def test_empty_slot_is_404_whatever_c_names(self, client: TestClient) -> None:
        resp = await client.get("/api/slots/1/still.jpg?c=c", allow_redirects=False)
        assert resp.status == 404

    async def test_missing_ffmpeg_gives_none(self) -> None:
        assert await api.grab_frame("/nonexistent/ffmpeg", b"x") is None

    async def test_missing_ffmpeg_failure_is_cached(
        self, two_client: TestClient, two: FakeDashboard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        starts = 0

        async def missing(path: str) -> Any:
            nonlocal starts
            starts += 1
            raise FileNotFoundError(path)

        monkeypatch.setattr(api, "_start_ffmpeg", missing)
        for _ in range(3):
            resp = await self._get(two_client, 0, "a")
            assert resp.status == 302
        assert starts == 1
