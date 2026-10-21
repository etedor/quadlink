"""Just enough fragmented-MP4 parsing to convert fMP4 relay segments (ISO/IEC 14496-12)."""

import struct
from collections.abc import Iterator
from dataclasses import dataclass

from quadlink.relay.tsinfo import PTS_WRAP

_CONTAINERS = {b"moov", b"trak", b"mdia", b"edts", b"mvex", b"moof", b"traf"}


@dataclass(frozen=True)
class Track:
    track_id: int
    timescale: int
    # media ticks ffmpeg subtracts from every timestamp; see _edit_shift
    shift: int = 0
    default_duration: int = 0


@dataclass(frozen=True)
class InitInfo:
    video: Track | None
    # media timescale of the first audio track, which is its sample rate
    audio_rate: int | None


def _boxes(data: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[bytes, int, int]]:
    """(type, body start, body end) of each box between start and end."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, pos)
        head = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            head = 16
        elif size == 0:
            size = end - pos
        if size < head or pos + size > end:
            return
        yield kind, pos + head, pos + size
        pos += size


def _find(data: bytes, start: int, end: int, path: list[bytes]) -> Iterator[tuple[int, int]]:
    """Body spans of every box at this path below start..end."""
    for kind, lo, hi in _boxes(data, start, end):
        if kind != path[0]:
            continue
        if len(path) == 1:
            yield lo, hi
        elif kind in _CONTAINERS:
            yield from _find(data, lo, hi, path[1:])


def _first(data: bytes, start: int, end: int, path: list[bytes]) -> tuple[int, int] | None:
    return next(_find(data, start, end, path), None)


def _timescale(data: bytes, lo: int) -> int:
    return int(struct.unpack_from(">I", data, lo + (20 if data[lo] == 1 else 12))[0])


def _edit_shift(data: bytes, lo: int, hi: int, timescale: int, movie_timescale: int) -> int:
    """Media ticks that ffmpeg's mov demuxer subtracts from every timestamp.

    Leading empty edits (media_time -1) delay the track by their duration, in the
    movie timescale; the first real edit's media_time is where presentation starts.
    """
    wide = data[lo] == 1
    count = struct.unpack_from(">I", data, lo + 4)[0]
    pos, size = lo + 8, 20 if wide else 12
    empty = 0
    for _ in range(count):
        if pos + size > hi:
            break
        if wide:
            duration, media_time = struct.unpack_from(">Qq", data, pos)
        else:
            duration, media_time = struct.unpack_from(">Ii", data, pos)
        pos += size
        if media_time == -1:
            empty += duration
            continue
        delay = (
            (empty * timescale + movie_timescale // 2) // movie_timescale if movie_timescale else 0
        )
        return int(media_time - delay)
    return 0


def _track(data: bytes, lo: int, hi: int, movie_timescale: int) -> tuple[bytes, Track] | None:
    tkhd = _first(data, lo, hi, [b"tkhd"])
    mdhd = _first(data, lo, hi, [b"mdia", b"mdhd"])
    hdlr = _first(data, lo, hi, [b"mdia", b"hdlr"])
    if tkhd is None or mdhd is None or hdlr is None:
        return None
    track_id = struct.unpack_from(">I", data, tkhd[0] + (20 if data[tkhd[0]] == 1 else 12))[0]
    timescale = _timescale(data, mdhd[0])
    if timescale == 0:
        return None
    handler = data[hdlr[0] + 8 : hdlr[0] + 12]
    elst = _first(data, lo, hi, [b"edts", b"elst"])
    shift = 0 if elst is None else _edit_shift(data, *elst, timescale, movie_timescale)
    return handler, Track(track_id, timescale, shift)


def init_info(init: bytes) -> InitInfo:
    """The video track and audio sample rate of an fMP4 init segment."""
    try:
        return _init_info(init)
    except (struct.error, IndexError):
        # a box too short for its fields
        return InitInfo(None, None)


def _init_info(init: bytes) -> InitInfo:
    video: Track | None = None
    audio: int | None = None
    moov = _first(init, 0, len(init), [b"moov"])
    if moov is None:
        return InitInfo(None, None)
    mvhd = _first(init, *moov, [b"mvhd"])
    movie_timescale = 0 if mvhd is None else _timescale(init, mvhd[0])
    for lo, hi in _find(init, *moov, [b"trak"]):
        found = _track(init, lo, hi, movie_timescale)
        if found is None:
            continue
        handler, track = found
        if handler == b"vide" and video is None:
            video = track
        elif handler == b"soun" and audio is None:
            audio = track.timescale
    if video is not None:
        for lo, _ in _find(init, *moov, [b"mvex", b"trex"]):
            if struct.unpack_from(">I", init, lo + 4)[0] == video.track_id:
                duration = struct.unpack_from(">I", init, lo + 12)[0]
                video = Track(video.track_id, video.timescale, video.shift, duration)
    return InitInfo(video, audio)


def _samples(data: bytes, lo: int, hi: int, default: int) -> Iterator[tuple[int, int]]:
    """(duration, composition offset) of each sample in one traf's truns."""
    tfhd = _first(data, lo, hi, [b"tfhd"])
    if tfhd is not None:
        flags = int.from_bytes(data[tfhd[0] + 1 : tfhd[0] + 4], "big")
        pos = tfhd[0] + 8 + (8 if flags & 0x1 else 0) + (4 if flags & 0x2 else 0)
        if flags & 0x8:
            default = struct.unpack_from(">I", data, pos)[0]
    for t_lo, t_hi in _find(data, lo, hi, [b"trun"]):
        version = data[t_lo]
        flags = int.from_bytes(data[t_lo + 1 : t_lo + 4], "big")
        count = struct.unpack_from(">I", data, t_lo + 4)[0]
        pos = t_lo + 8 + (4 if flags & 0x1 else 0) + (4 if flags & 0x4 else 0)
        fields = [f for f in (0x100, 0x200, 0x400, 0x800) if flags & f]
        for _ in range(count):
            if pos + 4 * len(fields) > t_hi:
                return
            duration, offset = default, 0
            for f in fields:
                if f == 0x100:
                    duration = struct.unpack_from(">I", data, pos)[0]
                elif f == 0x800:
                    offset = struct.unpack_from(">i" if version else ">I", data, pos)[0]
                pos += 4
            yield duration, offset


def fragment_video_pts(data: bytes, video: Track, lift: int | None = None) -> int | None:
    """Lowest video PTS of an fMP4 media segment, in 90 kHz ticks as ffmpeg outputs it.

    Reads the first fragment that holds video. `lift` is the composition offset lift
    ffmpeg applies to that fragment (see video_lift); None takes the segment's own, as
    a demuxer reading only this segment applies it.
    """
    try:
        return _fragment_video_pts(data, video, lift)
    except (struct.error, IndexError):
        return None


def video_lift(data: bytes, video: Track, first: bool = False) -> int:
    """Media ticks ffmpeg's mov demuxer adds to each video PTS once it has read this segment.

    The demuxer raises every PTS by the most negative composition offset it has read
    so far (trun v1, e.g. CMAF), so no PTS precedes its DTS; the lift only ever rises.
    `first` counts only the fragment holding the first video sample: the demuxer reads
    a whole moof before any of its samples, and later moofs only after them.
    """
    try:
        trafs = list(_video_trafs(data, video))
    except (struct.error, IndexError):
        return 0
    if first:
        trafs = [t for t in trafs if t[0] == trafs[0][0]]
    return _lift(trafs)


def _lift(trafs: list[tuple[int, int, list[tuple[int, int]]]]) -> int:
    return max([0, *(-offset for _, _, samples in trafs for _, offset in samples)])


def _video_trafs(data: bytes, video: Track) -> Iterator[tuple[int, int, list[tuple[int, int]]]]:
    """(moof start, base DTS less the edit shift, samples) of each video traf with a tfdt."""
    for moof, moof_end in _find(data, 0, len(data), [b"moof"]):
        for lo, hi in _find(data, moof, moof_end, [b"traf"]):
            tfhd = _first(data, lo, hi, [b"tfhd"])
            tfdt = _first(data, lo, hi, [b"tfdt"])
            if tfhd is None or tfdt is None:
                continue
            if struct.unpack_from(">I", data, tfhd[0] + 4)[0] != video.track_id:
                continue
            fmt = ">Q" if data[tfdt[0]] == 1 else ">I"
            dts = struct.unpack_from(fmt, data, tfdt[0] + 4)[0] - video.shift
            samples = list(_samples(data, lo, hi, video.default_duration))
            if samples:
                yield moof, dts, samples


def _fragment_video_pts(data: bytes, video: Track, lift: int | None) -> int | None:
    trafs = list(_video_trafs(data, video))
    if not trafs:
        return None
    if lift is None:
        lift = _lift(trafs)
    _, dts, samples = trafs[0]
    # negative offsets let any sample hold the lowest PTS, so all are scanned
    low = dts + samples[0][1]
    for duration, offset in samples:
        low = min(low, dts + offset)
        dts += duration
    low += lift
    # av_rescale_q rounds half away from zero
    scaled = (abs(low) * 90000 + video.timescale // 2) // video.timescale
    return (scaled if low >= 0 else -scaled) % PTS_WRAP
