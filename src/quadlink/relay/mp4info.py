"""Just enough fragmented-MP4 parsing to remux fMP4 relay segments (ISO/IEC 14496-12)."""

import struct
from collections.abc import Iterator
from dataclasses import dataclass

_CONTAINERS = {b"moov", b"trak", b"mdia", b"moof", b"traf"}


@dataclass(frozen=True)
class Track:
    track_id: int
    timescale: int


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


def video_track(init: bytes) -> Track | None:
    """The first video track of an fMP4 init segment."""
    try:
        moov = _first(init, 0, len(init), [b"moov"])
        if moov is None:
            return None
        for lo, hi in _find(init, *moov, [b"trak"]):
            tkhd = _first(init, lo, hi, [b"tkhd"])
            mdhd = _first(init, lo, hi, [b"mdia", b"mdhd"])
            hdlr = _first(init, lo, hi, [b"mdia", b"hdlr"])
            if tkhd is None or mdhd is None or hdlr is None:
                continue
            if init[hdlr[0] + 8 : hdlr[0] + 12] != b"vide":
                continue
            wide = 8 if init[tkhd[0]] == 1 else 0
            track_id = struct.unpack_from(">I", init, tkhd[0] + 12 + wide)[0]
            wide = 8 if init[mdhd[0]] == 1 else 0
            timescale = struct.unpack_from(">I", init, mdhd[0] + 12 + wide)[0]
            if timescale:
                return Track(track_id, timescale)
        return None
    except (struct.error, IndexError):
        # a box too short for its fields
        return None


def _offsets(data: bytes, lo: int, hi: int) -> Iterator[tuple[int | None, int]]:
    """(field position or None, composition offset) of each sample in one traf's truns."""
    for t_lo, t_hi in _find(data, lo, hi, [b"trun"]):
        version = data[t_lo]
        flags = int.from_bytes(data[t_lo + 1 : t_lo + 4], "big")
        count = struct.unpack_from(">I", data, t_lo + 4)[0]
        pos = t_lo + 8 + (4 if flags & 0x1 else 0) + (4 if flags & 0x4 else 0)
        fields = [f for f in (0x100, 0x200, 0x400, 0x800) if flags & f]
        for _ in range(count):
            if pos + 4 * len(fields) > t_hi:
                return
            found: tuple[int | None, int] = (None, 0)
            for f in fields:
                if f == 0x800:
                    found = (pos, struct.unpack_from(">i" if version else ">I", data, pos)[0])
                pos += 4
            yield found


def _video_moofs(data: bytes, video: Track) -> Iterator[list[tuple[int | None, int]]]:
    """The video samples of each moof that has any, as _offsets gives them."""
    for moof, moof_end in _find(data, 0, len(data), [b"moof"]):
        samples: list[tuple[int | None, int]] = []
        for lo, hi in _find(data, moof, moof_end, [b"traf"]):
            tfhd = _first(data, lo, hi, [b"tfhd"])
            if tfhd is None:
                continue
            if struct.unpack_from(">I", data, tfhd[0] + 4)[0] == video.track_id:
                samples += _offsets(data, lo, hi)
        if samples:
            yield samples


def lift_offsets(data: bytes, video: Track, lift: int) -> tuple[bytes, int]:
    """An fMP4 media segment that remuxes alone as it would after earlier segments.

    ffmpeg's mov demuxer raises each video PTS by the deepest negated composition
    offset (trun v1, e.g. CMAF) of the moofs it has read so far, so no PTS precedes its
    DTS; it reads a whole moof before any of its samples. `lift` is that depth after
    the earlier segments, in media ticks. Offsets are rewritten to include it, so none
    is negative and ffmpeg adds nothing. Returns the segment and the lift after it;
    a segment it cannot rewrite in place comes back unchanged.
    """
    try:
        moofs = list(_video_moofs(data, video))
    except (struct.error, IndexError):
        return data, lift
    own = 0
    targets: list[tuple[list[tuple[int | None, int]], int]] = []
    rewrite = False
    for samples in moofs:
        own = max(own, -min(offset for _, offset in samples))
        target = max(lift, own)
        rewrite = rewrite or target != own
        targets.append((samples, target))
    after = max(lift, own)
    if not rewrite:
        return data, after
    if any(pos is None for samples, _ in targets for pos, _ in samples):
        return data, after
    buf = bytearray(data)
    for samples, target in targets:
        for pos, offset in samples:
            assert pos is not None
            struct.pack_into(">I", buf, pos, offset + target)
    return bytes(buf), after
