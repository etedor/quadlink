"""Dashboard API: state snapshot, live events, operator actions, slot stills and Explore.

The web UI's request guard covers every route here; nothing in this module checks
the caller.
"""

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import quote

import structlog
from aiohttp import web

from quadlink.dashboard import Snapshot
from quadlink.explore import Category, Directory, DirectoryError, Sort, Stream
from quadlink.overrides import OverrideError
from quadlink.relay.segments import SegmentProxy
from quadlink.types import thumbnail_url

logger = structlog.get_logger()

API_VERSION = 2
EVENTS_PATH = "/api/events"
STILL_PATH = re.compile(r"/api/slots/[^/]+/still\.jpg")

# seconds without an event before a comment line keeps proxies from closing the stream
HEARTBEAT = 15.0
# seconds between two events to one subscriber; changes in between merge into the next
EVENT_GAP = 0.25
# seconds a slot's still (or its failure to decode) serves every viewer
STILL_TTL = 5.0
# seconds one ffmpeg frame grab may take
STILL_WAIT = 3.0

# Twitch logins; any other channel key is a URL, which has no preview image
_LOGIN = re.compile(r"[a-z0-9_]+")

# action -> its parameters and their types
ACTIONS: dict[str, dict[str, type]] = {
    "place": {"channel": str, "slot": int},
    "swap": {"a": int, "b": int},
    "unpin": {"slot": int},
    "put_back": {"channel": str},
    "unpin_all": {},
    "add": {"channel": str},
    "remove": {"channel": str},
}


class Dashboard(Protocol):
    """What the API reads and drives: the daemon."""

    segments: SegmentProxy
    ffmpeg: str | None
    directory: Directory

    def snapshot(self) -> Snapshot: ...

    def watch(self, callback: Callable[[], None]) -> None: ...

    async def place(self, channel: str, slot: int) -> None: ...

    async def swap(self, a: int, b: int) -> None: ...

    async def unpin(self, slot: int) -> None: ...

    async def put_back(self, channel: str) -> None: ...

    async def unpin_all(self) -> None: ...

    async def add(self, channel: str) -> None: ...

    async def remove(self, channel: str) -> None: ...


def snapshot_json(snap: Snapshot) -> dict[str, Any]:
    """The snapshot as the API sends it; bump API_VERSION when the shape changes."""
    views = {c.channel: c for c in snap.channels}

    def about(channel: str) -> dict[str, Any]:
        view = views.get(channel)
        stream = view.status.stream if view is not None else None
        numbers = {
            "viewers": view.viewers if view is not None else None,
            # epoch seconds, like refreshed_at
            "started_at": (
                int(view.started_at.timestamp())
                if view is not None and view.started_at is not None
                else None
            ),
        }
        if stream is None:
            return {"display_name": channel, "title": None, "category": None, **numbers}
        meta = stream.metadata
        return {
            "display_name": meta.author,
            "title": meta.title,
            "category": meta.category,
            **numbers,
        }

    slots = []
    for s in snap.slots:
        entry: dict[str, Any] = {"slot": s.slot, "channel": s.channel or None}
        if s.channel:
            entry |= about(s.channel)
            entry["display_name"] = s.display_name
        else:
            entry |= {
                "display_name": None,
                "title": None,
                "category": None,
                "viewers": None,
                "started_at": None,
            }
        entry |= {
            "pinned": s.pinned,
            "reserved_for": s.reserved_for,
            "returns": s.returns,
            # the channel in the URL keeps a browser's cached still from outliving a swap
            "still": f"/api/slots/{s.slot}/still.jpg?c={quote(s.channel)}" if s.channel else None,
        }
        slots.append(entry)
    channels = [
        {
            "channel": c.channel,
            **about(c.channel),
            "state": str(c.state),
            "slot": c.slot,
            "pin_slot": c.pin_slot,
            "replaced_slot": c.replaced_slot,
            "replaced_by": c.replaced_by,
            "live": c.live,
            "extra": c.extra,
            "priorities": c.status.priorities,
            "priority": c.status.priority,
            "reason": c.status.reason,
            "thumbnail": c.status.thumbnail_url,
        }
        for c in snap.channels
    ]
    return {
        "v": API_VERSION,
        "refreshed_at": snap.refreshed_at,
        "quadstream": str(snap.quadstream),
        "slots": slots,
        "channels": channels,
    }


def parse_action(body: Any) -> tuple[str, dict[str, Any]]:
    """The action name and its parameters; OverrideError when the body is not valid."""
    if not isinstance(body, dict):
        raise OverrideError("the request body must be a JSON object")
    action = body.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise OverrideError(
            f"the action {action!r} does not exist: use one of {', '.join(ACTIONS)}"
        )
    spec = ACTIONS[action]
    params = {k: v for k, v in body.items() if k != "action"}
    if params.keys() != spec.keys():
        needed = ", ".join(spec) or "no parameters"
        raise OverrideError(f"the action {action} takes {needed}")
    for name, kind in spec.items():
        value = params[name]
        # bool is an int subclass, and JSON true must not mean slot 1
        if not isinstance(value, kind) or isinstance(value, bool):
            noun = "an integer" if kind is int else "a string"
            raise OverrideError(f"the parameter {name} must be {noun}")
    return action, params


async def grab_frame(ffmpeg: str, segment: bytes, timeout: float = STILL_WAIT) -> bytes | None:
    """A JPEG (480 px wide) of the first keyframe in an MPEG-TS segment, or None."""
    try:
        proc = await _start_ffmpeg(ffmpeg)
    except OSError as e:
        logger.warning("still frame grab could not start ffmpeg", error=str(e))
        return None
    try:
        async with asyncio.timeout(timeout):
            out, err = await proc.communicate(segment)
    except TimeoutError:
        logger.warning("still frame grab timed out")
        return None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0 or not out:
        logger.warning(
            "still frame grab failed", code=proc.returncode, error=err.decode(errors="replace")
        )
        return None
    return out


async def _start_ffmpeg(ffmpeg: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        ffmpeg,
        "-v",
        "error",
        "-skip_frame",
        "nokey",
        "-i",
        "pipe:0",
        "-an",
        "-frames:v",
        "1",
        "-vf",
        "scale=480:-2",
        "-q:v",
        "5",
        "-f",
        "image2pipe",
        "-c:v",
        "mjpeg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


class DashboardAPI:
    """Routes under /api for the dashboard and for scripts."""

    def __init__(self, dashboard: Dashboard) -> None:
        self.dashboard = dashboard
        # one per open event stream: set when the snapshot may have changed
        self._subscribers: set[asyncio.Event] = set()
        self._closing = False
        # (version, JSON text) of the newest snapshot built for the event streams,
        # which share one build per change
        self._version = 0
        self._built: tuple[int, str] | None = None
        # slot -> (channel, loop time, JPEG or None for a failed grab)
        self._stills: dict[int, tuple[str, float, bytes | None]] = {}
        self._still_locks = {slot: asyncio.Lock() for slot in range(4)}
        dashboard.watch(self._changed)

    def register(self, app: web.Application) -> None:
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_get(EVENTS_PATH, self._handle_events)
        app.router.add_post("/api/actions", self._handle_action)
        app.router.add_get("/api/slots/{slot}/still.jpg", self._handle_still)
        app.router.add_get("/api/explore/categories", self._handle_categories)
        app.router.add_get("/api/explore/streams", self._handle_streams)
        app.on_shutdown.append(self._shutdown)

    def _changed(self) -> None:
        self._version += 1
        for event in self._subscribers:
            event.set()

    def _snapshot_text(self) -> str:
        return json.dumps(snapshot_json(self.dashboard.snapshot()), separators=(",", ":"))

    def _shared_snapshot_text(self) -> str:
        if self._built is None or self._built[0] != self._version:
            self._built = (self._version, self._snapshot_text())
        return self._built[1]

    async def _shutdown(self, app: web.Application) -> None:
        # event streams never end on their own, and the server waits for every handler
        self._closing = True
        for event in self._subscribers:
            event.set()

    async def _handle_state(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._snapshot_text(),
            content_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_events(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)
        loop = asyncio.get_running_loop()
        # a size-one queue that keeps only the newest state: the event says "changed"
        # and the handler reads the snapshot when it gets to it
        changed = asyncio.Event()
        changed.set()
        self._subscribers.add(changed)
        sent: str | None = None
        last_write = loop.time()
        try:
            while not self._closing:
                try:
                    async with asyncio.timeout_at(last_write + HEARTBEAT):
                        await changed.wait()
                except TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    last_write = loop.time()
                    continue
                if self._closing:
                    break
                changed.clear()
                text = self._shared_snapshot_text()
                if text != sent:
                    # retry: the delay before EventSource reconnects after a drop
                    retry = "retry: 2000\n" if sent is None else ""
                    await resp.write(f"{retry}data: {text}\n\n".encode())
                    sent, last_write = text, loop.time()
                    await asyncio.sleep(EVENT_GAP)
        except ConnectionError:
            pass
        finally:
            self._subscribers.discard(changed)
        return resp

    async def _handle_action(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return web.json_response({"error": "the request body is not valid JSON"}, status=400)
        try:
            action, params = parse_action(body)
            await getattr(self.dashboard, action)(**params)
        except OverrideError as e:
            return web.json_response({"error": str(e)}, status=400)
        logger.info("dashboard action", action=action, **params)
        return await self._handle_state(request)

    async def _handle_still(self, request: web.Request) -> web.Response:
        raw = request.match_info["slot"]
        if raw not in ("0", "1", "2", "3"):
            return web.json_response({"error": "the slot does not exist"}, status=404)
        slot = int(raw)
        channel = self.dashboard.snapshot().slots[slot].channel
        if not channel:
            return web.json_response({"error": f"slot {slot} is empty"}, status=404)
        requested = request.query.get("c", "")
        if requested != channel:
            # a URL from before a swap: never the picture of the channel there now
            if _LOGIN.fullmatch(requested):
                return _redirect(thumbnail_url(requested))
            return web.json_response(
                {"error": f"the c parameter must name the channel in slot {slot}"}, status=404
            )
        jpeg = await self._still(slot, channel)
        if jpeg is not None:
            return web.Response(
                body=jpeg, content_type="image/jpeg", headers={"Cache-Control": "max-age=5"}
            )
        if not _LOGIN.fullmatch(channel):
            return web.json_response({"error": f"slot {slot} has no picture"}, status=404)
        return _redirect(thumbnail_url(channel))

    async def _still(self, slot: int, channel: str) -> bytes | None:
        """Channel's picture in slot, decoded at most once per STILL_TTL."""
        loop = asyncio.get_running_loop()
        async with self._still_locks[slot]:
            cached = self._stills.get(slot)
            if cached is not None and cached[0] == channel and loop.time() - cached[1] < STILL_TTL:
                return cached[2]
            ffmpeg = self.dashboard.ffmpeg
            segment = self.dashboard.segments.latest(slot, channel)
            jpeg = None
            if ffmpeg is not None and segment is not None:
                jpeg = await grab_frame(ffmpeg, segment)
            self._stills[slot] = (channel, loop.time(), jpeg)
            return jpeg

    async def _handle_categories(self, request: web.Request) -> web.Response:
        query = request.query.get("q", "").strip()
        directory = self.dashboard.directory
        try:
            found = await directory.search(query) if query else await directory.top()
        except DirectoryError as e:
            logger.warning("explore categories failed", query=query, error=str(e))
            return web.json_response({"error": "Twitch did not answer"}, status=502)
        return web.json_response(
            {"categories": [_category_json(c) for c in found]},
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_streams(self, request: web.Request) -> web.Response:
        category = request.query.get("category", "")
        try:
            sort = Sort(request.query.get("sort", Sort.VIEWERS))
        except ValueError:
            return web.json_response({"error": "sort must be viewers or new"}, status=400)
        if not category.isdigit():
            return web.json_response({"error": "category must be a Twitch category ID"}, status=400)
        try:
            listing = await self.dashboard.directory.streams(category, sort)
        except DirectoryError as e:
            logger.warning("explore streams failed", category=category, error=str(e))
            return web.json_response({"error": "Twitch did not answer"}, status=502)
        if listing is None:
            return web.json_response({"error": "the category does not exist"}, status=404)
        return web.json_response(
            {
                "category": _category_json(listing.category),
                "streams": [_stream_json(s) for s in listing.streams],
                "more": listing.more,
            },
            headers={"Cache-Control": "no-store"},
        )


def _category_json(c: Category) -> dict[str, Any]:
    return {"id": c.id, "name": c.name, "box_art": c.box_art, "viewers": c.viewers, "live": c.live}


def _stream_json(s: Stream) -> dict[str, Any]:
    return {
        "channel": s.login,
        "display_name": s.display_name,
        "title": s.title,
        "viewers": s.viewers,
        # epoch seconds, like the snapshot's started_at
        "started_at": int(s.started_at.timestamp()),
        "language": s.language,
        "tags": list(s.tags),
        "thumbnail": s.thumbnail,
    }


def _redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location, "Cache-Control": "no-store"})
