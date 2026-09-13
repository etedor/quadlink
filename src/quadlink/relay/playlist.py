"""Pure HLS media-playlist parse + republish core.

No I/O and no aiohttp here — this is the testable heart of the relay. It turns
a Twitch source media playlist into our own continuously-live media playlist
with a monotonic MEDIA-SEQUENCE and DISCONTINUITY splices on source change.
"""

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
