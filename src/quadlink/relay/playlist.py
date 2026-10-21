"""Pure HLS media-playlist parse + republish core.

No I/O and no aiohttp here — this is the testable heart of the relay. It turns
a Twitch source media playlist into our own continuously-live media playlist
with a monotonic MEDIA-SEQUENCE and DISCONTINUITY splices on source change.
"""

import math
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

# a parsed source segment: (duration, uri, program_date_time, source_discontinuity, ext_map)
# ext_map is the raw #EXT-X-MAP line in effect (fMP4 init segment), or None for TS
ParsedSegment = tuple[float, str, str | None, bool, str | None]


def parse_media_playlist(text: str) -> tuple[int, int, list[ParsedSegment]]:
    """Parse a Twitch media playlist.

    Returns (target_duration, media_sequence, segments). Unknown tags are
    ignored; EXTINF/PROGRAM-DATE-TIME/DISCONTINUITY attach to the next segment.
    EXT-X-MAP (fMP4 init segment, used by Twitch enhanced broadcasting) persists
    across segments until it changes.
    """
    target_duration = 6
    media_sequence = 0
    segments: list[ParsedSegment] = []

    duration = 0.0
    pdt: str | None = None
    disc = False
    ext_map: str | None = None  # persists across segments until re-specified

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
        elif line.startswith("#EXT-X-MAP:"):
            ext_map = line  # carried verbatim; Twitch's URI is absolute
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            # value is an ISO timestamp that itself contains colons
            pdt = line.split(":", 1)[1]
        elif line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:") :].split(",", 1)[0])
        elif line.startswith("#"):
            continue  # ignore all other tags (DATERANGE, TWITCH-*, etc.)
        else:
            segments.append((duration, line, pdt, disc, ext_map))
            duration, pdt, disc = 0.0, None, False  # ext_map persists

    return target_duration, media_sequence, segments


_MAP_URI_RE = re.compile(r'#EXT-X-MAP:[^\n]*URI="([^"]+)"')
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')

# served in place of source video while a slot has none; see add_filler
FILLER_DURATION = 2.0
FILLER_TS = "/streams/filler/2s.ts"
FILLER_FMP4 = "/streams/filler/2s.m4s"
FILLER_FMP4_INIT = "/streams/filler/init.mp4"


def ad_segment_uris(text: str) -> set[str]:
    """URIs of Twitch-stitched ad segments in a source playlist.

    Same test as the streamlink-ttvlol plugin: an "Amazon" EXTINF title, or a
    PROGRAM-DATE-TIME inside a twitch-stitched-ad DATERANGE.
    """
    ranges: list[tuple[datetime, float]] = []
    for line in text.splitlines():
        if not line.startswith("#EXT-X-DATERANGE:"):
            continue
        attrs = {k: v.strip('"') for k, v in _ATTR_RE.findall(line.split(":", 1)[1])}
        if attrs.get("CLASS") != "twitch-stitched-ad" and not attrs.get("ID", "").startswith(
            "stitched-ad-"
        ):
            continue
        start = _parse_pdt(attrs.get("START-DATE"))
        try:
            duration = float(attrs.get("DURATION", ""))
        except ValueError:
            continue
        if start is not None:
            ranges.append((start, duration))

    ads: set[str] = set()
    title = ""
    pdt: datetime | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pdt = _parse_pdt(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            title = line.split(",", 1)[1] if "," in line else ""
        elif line and not line.startswith("#"):
            in_range = pdt is not None and any(
                0 <= (pdt - start).total_seconds() < duration for start, duration in ranges
            )
            if "Amazon" in title or in_range:
                ads.add(line)
            title, pdt = "", None
    return ads


def has_live_segment(text: str) -> bool:
    """Whether a source playlist holds any segment that is not an ad."""
    _, _, segments = parse_media_playlist(text)
    ads = ad_segment_uris(text)
    return any(uri not in ads for _, uri, _, _, _ in segments)


def _parse_pdt(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def first_map_uri(text: str) -> str | None:
    """Return the first EXT-X-MAP init-segment URI in a source playlist, if any."""
    m = _MAP_URI_RE.search(text)
    return m.group(1) if m else None


@dataclass
class Segment:
    """One republished segment in our own playlist."""

    seq: int
    uri: str
    duration: float
    program_date_time: str | None
    discontinuity: bool
    map_gen: int | None  # init-segment generation for fMP4, None for TS
    filler: bool = False


@dataclass
class SlotState:
    """Rolling republish state for one slot."""

    window: int = 12
    segments: deque[Segment] = field(default_factory=deque)
    next_seq: int = 0
    discontinuity_seq: int = 0
    last_identity: str | None = None
    last_source_seq: int | None = None
    last_source_time: datetime | None = None  # PROGRAM-DATE-TIME of last_source_seq
    pending_discontinuity: bool = False
    map_generation: int = 0  # bumps at each new fMP4 init run, for a stable init URL
    # relay clock time of the last new video, or of the last filler; filler starts
    # once real time runs STALL_AFTER past it
    served_until: float | None = None
    # relay clock time at which a client playing at real speed finishes the served
    # content; the relay keeps it within MAX_LEAD ahead of real time and adds
    # filler once it falls SLOW_AFTER behind
    edge: float | None = None


def _advance_watermark(state: SlotState, source_seq: int, source_time: datetime | None) -> None:
    state.last_source_seq = source_seq
    if source_time is not None:
        state.last_source_time = source_time


def ingest(state: SlotState, source_text: str, identity: str, budget: float | None = None) -> float:
    """Fold a freshly-fetched source playlist into our rolling window.

    `identity` is the stable channel identity, NOT the playlist URL. Twitch
    re-mints the URL every cycle for an unchanged stream, so keying on the URL
    would splice a spurious discontinuity each cycle. Keying on identity means a
    token refresh for the same channel keeps the sequence watermark and dedups
    cleanly, while a real channel switch resets it and splices a discontinuity.

    `budget` caps the seconds of new content appended, keeping the newest
    segments and splicing over the older ones; None appends everything.
    Returns the seconds appended.
    """
    _, media_sequence, segments = parse_media_playlist(source_text)
    ads = ad_segment_uris(source_text)

    # channel change -> reset source-sequence tracking, splice a discontinuity
    if identity != state.last_identity:
        state.last_identity = identity
        state.last_source_seq = None
        state.last_source_time = None
        if state.segments:  # only splice if we've already served something
            state.pending_discontinuity = True

    # (index, source_time, restarted) of each segment not yet ingested; the
    # watermark moves past each one as it is found, as if it were appended
    fresh: list[tuple[int, datetime | None, bool]] = []
    mark_seq, mark_time = state.last_source_seq, state.last_source_time
    for index, (_, uri, pdt, _, _) in enumerate(segments):
        source_seq = media_sequence + index
        source_time = _parse_pdt(pdt)
        restarted = False
        if mark_seq is not None and source_seq <= mark_seq:
            # within one broadcast seq and PROGRAM-DATE-TIME both rise, so a seq at or
            # below the watermark with a later time is a restarted broadcast (its
            # seq starts over). a lagging edge repeats old segments with their old
            # times and is skipped. without times, skip: there is no proof of restart.
            restarted = (
                source_time is not None and mark_time is not None and source_time > mark_time
            )
            if not restarted:
                continue  # already ingested this segment
        mark_seq = source_seq
        if source_time is not None:
            mark_time = source_time

        if uri in ads:
            # the gap an ad leaves is covered by filler; mark the watermark so the
            # ad is not reconsidered on the next fetch
            _advance_watermark(state, source_seq, source_time)
            if state.segments:
                state.pending_discontinuity = True
            continue
        fresh.append((index, source_time, restarted))

    if budget is not None:
        # a client plays every served second at real speed and never skips, so
        # content beyond the budget would become permanent delay behind live
        while fresh and sum(segments[i][0] for i, _, _ in fresh) > budget:
            index, source_time, _ = fresh.pop(0)
            _advance_watermark(state, media_sequence + index, source_time)
            if state.segments:
                state.pending_discontinuity = True

    appended = 0.0
    for index, source_time, restarted in fresh:
        duration, uri, pdt, src_disc, source_map = segments[index]
        source_seq = media_sequence + index
        if restarted and state.segments:
            state.pending_discontinuity = True

        # a format flip (fMP4 <-> MPEG-TS) cannot coexist in one continuous
        # playlist: EXT-X-MAP persists with no "unset", so TS segments would
        # inherit a prior fMP4 init and fail to decode. Drop the old-format
        # window so the served playlist stays a single format across the switch.
        prev_is_fmp4 = bool(state.segments) and state.segments[-1].map_gen is not None
        new_is_fmp4 = source_map is not None
        if state.segments and prev_is_fmp4 != new_is_fmp4:
            for old in state.segments:
                if old.discontinuity:
                    state.discontinuity_seq += 1
            state.segments.clear()
            state.pending_discontinuity = True
            prev_is_fmp4 = False

        disc = state.pending_discontinuity or src_disc
        state.pending_discontinuity = False

        # bump the init generation at each run boundary (first fMP4 segment or a
        # discontinuity) so the served EXT-X-MAP URL is stable within a run but
        # changes when the init actually changes. Token churn within a run keeps
        # the same generation, so AVPlayer never needlessly re-inits, and the
        # relay serves a fresh init on each fetch so the token can't go stale.
        if new_is_fmp4:
            if not prev_is_fmp4 or disc:
                state.map_generation += 1
            map_gen: int | None = state.map_generation
        else:
            map_gen = None

        state.segments.append(
            Segment(
                seq=state.next_seq,
                uri=uri,
                duration=duration,
                program_date_time=pdt,
                discontinuity=disc,
                map_gen=map_gen,
            )
        )
        state.next_seq += 1
        appended += duration
        _advance_watermark(state, source_seq, source_time)

    _trim(state)
    return appended


def _trim(state: SlotState) -> None:
    """Trim to the window, counting out discontinuity segments that roll off."""
    while len(state.segments) > state.window:
        removed = state.segments.popleft()
        if removed.discontinuity:
            state.discontinuity_seq += 1


def add_filler(state: SlotState) -> None:
    """Append one filler segment so the served playlist keeps advancing.

    The filler takes the format of the window: EXT-X-MAP has no "unset", so TS
    cannot follow fMP4. Each filler repeats the same timestamps, so every one
    starts a discontinuity.
    """
    fmp4 = bool(state.segments) and state.segments[-1].map_gen is not None
    state.segments.append(
        Segment(
            seq=state.next_seq,
            uri=FILLER_FMP4 if fmp4 else FILLER_TS,
            duration=FILLER_DURATION,
            program_date_time=None,
            discontinuity=True,
            map_gen=0 if fmp4 else None,
            filler=True,
        )
    )
    state.next_seq += 1
    state.pending_discontinuity = True
    _trim(state)


def render(
    state: SlotState,
    slot: int,
    uri_for: Callable[[Segment], str] | None = None,
    ready: Callable[[Segment], bool] | None = None,
) -> str:
    """Render our current window as a live HLS media playlist.

    `ready` holds back trailing segments a client could not fetch yet (a converted
    segment exists only once ffmpeg has seen the next keyframe); earlier ones stay.
    """
    segments = list(state.segments)
    if ready is not None:
        while len(segments) > 1 and not segments[-1].filler and not ready(segments[-1]):
            segments.pop()
    target = max(1, max((math.ceil(s.duration) for s in segments), default=1))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{target}",
        f"#EXT-X-MEDIA-SEQUENCE:{segments[0].seq}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{state.discontinuity_seq}",
    ]
    last_map: str | None = None
    for seg in segments:
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        # point fMP4 (Twitch enhanced broadcasting) segments at a relay-served
        # init URL that is stable within a run (keyed on the run generation) but
        # changes at real init boundaries; the relay resolves it to a fresh
        # Twitch init on each fetch, so the init token never goes stale
        if seg.map_gen is not None:
            map_uri = FILLER_FMP4_INIT if seg.filler else f"/streams/{slot}/init/{seg.map_gen}"
            if map_uri != last_map:
                lines.append(f'#EXT-X-MAP:URI="{map_uri}"')
                last_map = map_uri
        if seg.program_date_time:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{seg.program_date_time}")
        lines.append(f"#EXTINF:{seg.duration:.3f},")
        lines.append(uri_for(seg) if uri_for is not None and not seg.filler else seg.uri)
    return "\n".join(lines) + "\n"
