"""A client's distance behind live must stay bounded across gaps and channel switches.

QuadStream plays every served segment at real speed and never skips ahead, so
any content the relay serves beyond real time (a backlog after an outage, a new
channel's whole window) becomes permanent delay. Once the delay outgrows the
relay's window, the player's next segments roll off and it stutters.
"""

import httpx
import pytest
from aiohttp.test_utils import make_mocked_request

from quadlink.relay.server import SlotRelay
from quadlink.relay.store import SlotStore

SEGMENT = 2.0
TWITCH_WINDOW = 15
JOIN_BEHIND = 3 * SEGMENT
MAX_BEHIND = 12.0


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Source:
    """A Twitch channel: one 2s segment per 2s of clock, a rolling window."""

    def __init__(self, name: str, clock: Clock) -> None:
        self.name = name
        self.clock = clock
        self.start = clock.t - TWITCH_WINDOW * SEGMENT
        self.down = False

    def newest(self) -> int:
        return int((self.clock.t - self.start) // SEGMENT)

    def render(self) -> str:
        if self.down:
            request = httpx.Request("GET", self.name)
            raise httpx.HTTPStatusError(
                "404", request=request, response=httpx.Response(404, request=request)
            )
        newest = self.newest()
        first = max(0, newest - TWITCH_WINDOW + 1)
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
        for seq in range(first, newest + 1):
            lines += [f"#EXTINF:{SEGMENT:.3f},live", f"{self.name}/{seq}.ts"]
        return "\n".join(lines) + "\n"


class JitterySource(Source):
    """Publishes each segment up to 1.5s late, as Twitch's edge does."""

    def newest(self) -> int:
        seq = super().newest()
        late = (seq * 7919 % 16) / 10  # deterministic 0.0..1.5s
        published = self.start + (seq + 1) * SEGMENT + late
        return seq if self.clock.t >= published else seq - 1


class SlowSource(Source):
    """Publishes one 2s segment every 3s: video arrives slower than real time."""

    def newest(self) -> int:
        return int((self.clock.t - self.start) // 3.0)


class Client:
    """Plays the slot at real speed from JOIN_BEHIND behind the edge it joined at."""

    def __init__(self, relay: SlotRelay, clock: Clock) -> None:
        self.relay = relay
        self.clock = clock
        self.served: dict[int, float] = {}  # relay seq -> duration
        self.uris: dict[int, str] = {}  # relay seq -> segment URI
        self.joined_at: float | None = None
        self.content_at_join = 0.0
        self.worst = 0.0
        self.starved_at: float | None = None  # first clock time it ran out of content

    async def poll(self) -> None:
        request = make_mocked_request("GET", "/streams/1", match_info={"slot": "1"})
        resp = await self.relay.handle(request)
        assert resp.status == 200
        seq = duration = None
        for line in resp.text.splitlines():
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                seq = int(line.split(":", 1)[1])
            elif line.startswith("#EXTINF:"):
                duration = float(line[len("#EXTINF:") :].split(",", 1)[0])
            elif line and not line.startswith("#"):
                assert seq is not None and duration is not None
                self.served.setdefault(seq, duration)
                self.uris.setdefault(seq, line)
                seq += 1
        content = sum(self.served.values())
        if self.joined_at is None:
            self.joined_at = self.clock.t
            self.content_at_join = content - JOIN_BEHIND
        wanted = self.content_at_join + self.clock.t - self.joined_at
        if wanted > content and self.starved_at is None:
            self.starved_at = self.clock.t
        playhead = min(content, wanted)
        self.worst = max(self.worst, content - playhead)


async def _run(clock: Clock, client: Client, seconds: float, every: float = 1.0) -> None:
    end = clock.t + seconds
    while clock.t < end:
        await client.poll()
        clock.t += every


@pytest.mark.asyncio
async def test_client_stays_near_live_across_outages_and_switches():
    clock = Clock()
    sources = {"a": Source("a", clock)}
    store = SlotStore()
    store.update(["a", "", "", ""], ["chan-a", "", "", ""])

    async def fetch(url: str) -> str:
        return sources[url].render()

    relay = SlotRelay(store, fetch=fetch, cache_ttl=0.0, time_fn=clock)
    client = Client(relay, clock)
    await _run(clock, client, 30)

    for _ in range(3):
        # source outage the relay covers with filler; the source keeps producing
        sources["a"].down = True
        await _run(clock, client, 10)
        sources["a"].down = False
        await _run(clock, client, 30)

    for name in ("b", "c", "d"):
        # a channel switch lands on a full Twitch window of the new channel
        sources[name] = Source(name, clock)
        store.update([name, "", "", ""], [f"chan-{name}", "", "", ""])
        await _run(clock, client, 40)

    assert client.worst <= MAX_BEHIND, f"client fell {client.worst:.0f}s behind live"


@pytest.mark.asyncio
@pytest.mark.parametrize("every", [1.0, 2.0])  # AVPlayer reloads about every 2s
async def test_late_segments_are_never_skipped(every):
    clock = Clock()
    source = JitterySource("a", clock)
    store = SlotStore()
    store.update(["a", "", "", ""], ["chan-a", "", "", ""])

    async def fetch(url: str) -> str:
        return source.render()

    relay = SlotRelay(store, fetch=fetch, cache_ttl=0.0, time_fn=clock)
    client = Client(relay, clock)
    await _run(clock, client, 300, every)

    played = [int(uri.split("/")[1].removesuffix(".ts")) for _, uri in sorted(client.uris.items())]
    assert played == list(range(played[0], played[0] + len(played)))
    assert client.worst <= MAX_BEHIND


@pytest.mark.asyncio
async def test_slow_source_never_starves_the_client():
    clock = Clock()
    sources: dict[str, Source] = {"a": Source("a", clock)}
    store = SlotStore()
    store.update(["a", "", "", ""], ["chan-a", "", "", ""])

    async def fetch(url: str) -> str:
        return sources[url].render()

    relay = SlotRelay(store, fetch=fetch, cache_ttl=0.0, time_fn=clock)
    client = Client(relay, clock)
    await _run(clock, client, 30)

    slow = SlowSource("a", clock)
    # same channel, so its sequence must carry on past the watermark
    slow.start = clock.t - (sources["a"].newest() + 1) * 3.0
    sources["a"] = slow
    await _run(clock, client, 60)

    assert client.starved_at is None, f"client ran out of content at {client.starved_at:.0f}s"
    assert client.worst <= MAX_BEHIND
