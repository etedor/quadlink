"""Pure HLS media-playlist parse + republish core.

No I/O and no aiohttp here — this is the testable heart of the relay. It turns
a Twitch source media playlist into our own continuously-live media playlist
with a monotonic MEDIA-SEQUENCE and DISCONTINUITY splices on source change.
"""

import math
from collections import deque
from dataclasses import dataclass, field

# a parsed source segment: (duration, uri, program_date_time, source_discontinuity)
ParsedSegment = tuple[float, str, "str | None", bool]


def parse_media_playlist(text: str) -> tuple[int, int, list[ParsedSegment]]:
    """Parse a Twitch media playlist.

    Returns (target_duration, media_sequence, segments). Unknown tags are
    ignored; EXTINF/PROGRAM-DATE-TIME/DISCONTINUITY attach to the next segment.
    """
    target_duration = 6
    media_sequence = 0
    segments: list[ParsedSegment] = []

    duration = 0.0
    pdt: str | None = None
    disc = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])
        elif line == "#EXT-X-DISCONTINUITY":
            disc = True
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            # value is an ISO timestamp that itself contains colons
            pdt = line.split(":", 1)[1]
        elif line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:") :].split(",", 1)[0])
        elif line.startswith("#"):
            continue  # ignore all other tags (DATERANGE, TWITCH-*, etc.)
        else:
            segments.append((duration, line, pdt, disc))
            duration, pdt, disc = 0.0, None, False

    return target_duration, media_sequence, segments


@dataclass
class Segment:
    """One republished segment in our own playlist."""

    seq: int
    uri: str
    duration: float
    program_date_time: str | None
    discontinuity: bool


@dataclass
class SlotState:
    """Rolling republish state for one slot."""

    window: int = 12
    segments: deque[Segment] = field(default_factory=deque)
    next_seq: int = 0
    discontinuity_seq: int = 0
    last_source_url: str | None = None
    last_source_seq: int | None = None
    pending_discontinuity: bool = False


def ingest(state: SlotState, source_text: str, source_url: str) -> None:
    """Fold a freshly-fetched source playlist into our rolling window."""
    _, media_sequence, segments = parse_media_playlist(source_text)

    # source change -> reset source-sequence tracking, splice a discontinuity
    if source_url != state.last_source_url:
        state.last_source_url = source_url
        state.last_source_seq = None
        if state.segments:  # only splice if we've already served something
            state.pending_discontinuity = True

    for index, (duration, uri, pdt, src_disc) in enumerate(segments):
        source_seq = media_sequence + index
        if state.last_source_seq is not None and source_seq <= state.last_source_seq:
            continue  # already ingested this segment
        disc = state.pending_discontinuity or src_disc
        state.pending_discontinuity = False
        state.segments.append(
            Segment(
                seq=state.next_seq,
                uri=uri,
                duration=duration,
                program_date_time=pdt,
                discontinuity=disc,
            )
        )
        state.next_seq += 1
        state.last_source_seq = source_seq

    # trim to window; count out any discontinuity segments that roll off
    while len(state.segments) > state.window:
        removed = state.segments.popleft()
        if removed.discontinuity:
            state.discontinuity_seq += 1


def render(state: SlotState) -> str:
    """Render our current window as a live HLS media playlist."""
    segments = list(state.segments)
    target = max(1, max((math.ceil(s.duration) for s in segments), default=1))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target}",
        f"#EXT-X-MEDIA-SEQUENCE:{segments[0].seq}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{state.discontinuity_seq}",
    ]
    for seg in segments:
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if seg.program_date_time:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{seg.program_date_time}")
        lines.append(f"#EXTINF:{seg.duration:.3f},")
        lines.append(seg.uri)
    return "\n".join(lines) + "\n"
