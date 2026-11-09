"""Tests for non-Twitch HLS sources: config, live checks, URI resolution and fixed slots."""

import httpx
import pytest
from pydantic import ValidationError

from quadlink.api import _thumbnail
from quadlink.config.models import Config, Source
from quadlink.dashboard import ChannelView, DashboardState
from quadlink.quad import Hold, Overrides, QuadState, compose
from quadlink.relay.playlist import absolutize, parse_media_playlist
from quadlink.stream.fetcher import LookupFailed
from quadlink.stream.source import SourceChecker
from quadlink.types import ChannelState, ChannelStatus, Metadata, PrioritizedStream, Stream

URL = "http://cam.lan:8095/live.m3u8"


def _config(**extra: object) -> Config:
    return Config(mode="local", priorities={}, **extra)  # type: ignore[arg-type]


class TestConfig:
    def test_a_source_needs_a_login_like_name_and_an_http_url(self):
        assert Source(name="skyview", url=URL).slot is None
        with pytest.raises(ValidationError):
            Source(name="Sky View", url=URL)
        with pytest.raises(ValidationError):
            Source(name="skyview", url="rtmp://cam.lan/live")

    @pytest.mark.parametrize("slot", [0, 5])
    def test_a_slot_is_a_tile_number(self, slot):
        with pytest.raises(ValidationError):
            Source(name="skyview", url=URL, slot=slot)

    def test_names_and_fixed_slots_must_differ(self):
        with pytest.raises(ValidationError, match="names"):
            _config(sources=[Source(name="sky", url=URL), Source(name="sky", url=URL)])
        with pytest.raises(ValidationError, match="slot"):
            _config(
                sources=[
                    Source(name="one", url=URL, slot=1),
                    Source(name="two", url=URL, slot=1),
                ]
            )

    def test_a_source_is_found_by_name(self):
        config = _config(sources=[Source(name="sky", url=URL, slot=2)])
        assert config.source("sky") == Source(name="sky", url=URL, slot=2)
        assert config.source("a") is None


def _playlist(seq: int, *, segments: int = 3, end: bool = False) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{seq}"]
    for n in range(seq, seq + segments):
        lines += ["#EXTINF:2.000000,", f"seg{n:06d}.ts"]
    if end:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _checker(*responses: httpx.Response | Exception) -> SourceChecker:
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return SourceChecker(httpx.AsyncClient(transport=httpx.MockTransport(handle)))


SKY = Source(name="sky", url=URL, title="The sky", category="Map", slot=1)


class TestChecker:
    async def test_a_moving_playlist_is_live(self):
        checker = _checker(
            httpx.Response(200, text=_playlist(5)), httpx.Response(200, text=_playlist(6))
        )
        stream = await checker.check(SKY)
        assert stream is not None and stream.source
        assert (stream.channel, stream.url, stream.master_url) == ("sky", URL, URL)
        assert stream.metadata == Metadata(author="sky", category="Map", title="The sky")
        assert await checker.check(SKY) is not None

    async def test_a_playlist_that_stops_moving_is_offline(self):
        checker = _checker(
            httpx.Response(200, text=_playlist(5)),
            httpx.Response(200, text=_playlist(5)),
            httpx.Response(200, text=_playlist(6)),
        )
        assert await checker.check(SKY) is not None
        assert await checker.check(SKY) is None
        # moving again, it is live again
        assert await checker.check(SKY) is not None

    @pytest.mark.parametrize(
        "response",
        [
            httpx.ConnectError("refused"),
            httpx.Response(404),
            httpx.Response(200, text=_playlist(5, end=True)),
            httpx.Response(200, text=_playlist(5, segments=0)),
        ],
    )
    async def test_down_ended_or_empty_is_offline(self, response):
        assert await _checker(response).check(SKY) is None

    async def test_a_master_playlist_fails_with_a_reason(self):
        master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvideo.m3u8\n"
        with pytest.raises(LookupFailed, match="media playlist"):
            await _checker(httpx.Response(200, text=master)).check(SKY)


class TestAbsolutize:
    def test_relative_segments_resolve_against_the_playlist(self):
        text = absolutize(_playlist(5, segments=1), URL)
        assert parse_media_playlist(text)[2][0][1] == "http://cam.lan:8095/seg000005.ts"

    def test_absolute_uris_stay(self):
        text = "#EXTINF:2.0,live\nhttps://cdn.example/a.ts\n"
        assert absolutize(text, URL) == text

    def test_attribute_and_prefetch_uris_resolve(self):
        text = (
            '#EXT-X-MAP:URI="init.mp4"\n'
            '#EXT-X-PART:DURATION=0.5,URI="part/1.0.ts"\n'
            "#EXT-X-TWITCH-PREFETCH:next.ts\n"
        )
        out = absolutize(text, "http://cam.lan/hls/live.m3u8")
        assert 'URI="http://cam.lan/hls/init.mp4"' in out
        assert 'URI="http://cam.lan/hls/part/1.0.ts"' in out
        assert "#EXT-X-TWITCH-PREFETCH:http://cam.lan/hls/next.ts" in out

    def test_a_decimal_target_duration_parses(self):
        assert parse_media_playlist("#EXT-X-TARGETDURATION:2.0\n")[0] == 2


def _stream(channel: str, priority: int, source: bool = False) -> PrioritizedStream:
    return PrioritizedStream(
        stream=Stream(
            url=f"https://twitch.tv/{channel}",
            metadata=Metadata(channel, f"cat-{channel}", "t"),
            channel=channel,
            source=source,
        ),
        priority=priority,
        tiebreaker=0.5,
    )


FIXED = _config(sources=[Source(name="sky", url=URL, slot=2)])


def _quad(
    pool: list[PrioritizedStream], state: QuadState, overrides: Overrides | None = None
) -> QuadState:
    return compose(pool, state, overrides or Overrides(), FIXED)


class TestFixedSlot:
    def test_a_live_fixed_source_takes_its_slot_whatever_its_priority(self):
        pool = [_stream(c, 500) for c in "abcd"] + [_stream("sky", 1, source=True)]
        state = _quad(pool, QuadState())
        assert state.channels[1] == "sky"
        assert len([c for c in state.channels if c]) == 4

    def test_an_offline_fixed_source_leaves_its_slot_to_rotation(self):
        pool = [_stream(c, 500) for c in "abcd"]
        state = _quad(pool, QuadState())
        assert "sky" not in state.channels and all(state.channels)

    def test_a_returning_fixed_source_takes_its_slot_back_at_once(self):
        state = _quad([_stream(c, 500) for c in "abcd"], QuadState())
        occupant = state.channels[1]
        back = _quad([_stream(c, 500) for c in "abcd"] + [_stream("sky", 1, True)], state)
        assert back.channels[1] == "sky"
        assert occupant not in back.channels or back.channels.index(occupant) != 1

    def test_a_manual_hold_on_the_slot_overrides_it(self):
        pool = [_stream(c, 500) for c in "abcde"] + [_stream("sky", 1, True)]
        held = Overrides(holds={1: Hold("e")})
        state = _quad(pool, QuadState(), held)
        assert state.channels[1] == "e"
        # with the hold gone, the source has its slot again
        assert _quad(pool, state).channels[1] == "sky"


class TestThumbnail:
    def _view(self, slot: int | None) -> ChannelView:
        stream = Stream(url=URL, metadata=Metadata("sky", "Map", ""), channel="sky", source=True)
        status = ChannelStatus("sky", ChannelState.LIVE, [1], stream=stream, priority=1)
        state = DashboardState.IN_QUAD if slot is not None else DashboardState.LIVE
        return ChannelView(
            channel="sky",
            display_name="sky",
            state=state,
            slot=slot,
            pin_slot=None,
            live=True,
            extra=False,
            status=status,
        )

    def test_an_on_screen_source_shows_its_relay_still(self):
        assert _thumbnail(self._view(1)) == "/api/slots/1/still.jpg?c=sky"

    def test_an_off_screen_source_has_no_twitch_preview(self):
        assert _thumbnail(self._view(None)) is None
