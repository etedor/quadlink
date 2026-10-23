"""Cut a continuous MPEG-TS stream into LL-HLS parts while it is still arriving.

A part ends at the first video frame whose DTS is PART_SPLIT seconds past the
part's first frame, so parts follow decode order and need not start on a
keyframe. A segment ends where Splitter would cut it: at the first video PES
whose PTS reaches the next key, which with -copyts is that segment's keyframe.
The PAT, PMT and SDT that ffmpeg writes before a keyframe move with it, so every
segment's first part starts decodable (EXT-X-PART INDEPENDENT=YES).
"""

from collections import deque
from dataclasses import dataclass

from quadlink.relay.convert import _last_packet, _pid_at
from quadlink.relay.tsinfo import (
    _VIDEO_TYPES,
    PACKET,
    PTS_WRAP,
    _packets,
    _timestamp,
    before,
    pes_pts,
    program,
)

PART_SPLIT = 0.5
_CLOCK = 90000
_PAT = b"\x47\x40\x00"
_SDT_PID = 0x11


@dataclass(frozen=True)
class Part:
    """One finished part of the segment that starts at video PTS `key`."""

    key: int
    data: bytes
    duration: float
    # the part opens its segment: it starts with PAT, PMT and the keyframe
    independent: bool
    # the part closes its segment
    last: bool


def _times_at(buf: bytearray, i: int) -> tuple[int, int] | None:
    """(PTS, DTS) of the PES that the packet at i starts; DTS defaults to PTS."""
    for _, _, payload in _packets(bytes(buf[i : i + PACKET])):
        pts = pes_pts(payload)
        if pts is None:
            return None
        if payload[7] & 0x40 and len(payload) >= 19:
            return pts, _timestamp(payload[14:19])
        return pts, pts
    return None


def _since(later: int, earlier: int) -> float:
    return ((later - earlier) % PTS_WRAP) / _CLOCK


class PartCutter:
    """Turns pushed MPEG-TS bytes into finished parts, segment by segment.

    expect(key) announces each segment by its first video PTS, in order. Output
    ahead of the first key is dropped; a key whose frames never show gets no parts.
    """

    def __init__(self, split: float = PART_SPLIT) -> None:
        self._split = split
        self._keys: deque[int] = deque()
        self._buf = bytearray()
        self._scanned = 0
        self._video: bytes | None = None
        self._pmt: bytes | None = None
        self._psi: set[int] = set()
        self._headers = b""
        # DTS of the open part's first frame; None until a keyed frame arrives
        self._start: int | None = None
        self._independent = True
        # spacing of the last two frames, to time a part closed by end()
        self._last_dts: int | None = None
        self._frame = 1 / 30

    def expect(self, key: int) -> None:
        self._keys.append(key)

    @property
    def keys(self) -> list[int]:
        return list(self._keys)

    def push(self, chunk: bytes) -> list[Part]:
        self._buf += chunk
        end = len(self._buf) - len(self._buf) % PACKET
        if self._video is None and not self._find_program(end):
            return []
        assert self._video is not None
        done: list[Part] = []
        pos = self._scanned
        while (i := self._buf.find(self._video, pos, end)) >= 0:
            if i % PACKET:
                pos = i + 1
                continue
            pos = i + PACKET
            times = _times_at(self._buf, i)
            if times is None or not self._keys:
                continue
            pts, dts = times
            if self._last_dts is not None and dts != self._last_dts:
                self._frame = _since(dts, self._last_dts)
            self._last_dts = dts
            if self._start is None:
                # the open part starts at the first frame after a key arrives
                if not before(pts, self._keys[0]):
                    self._start = dts
                continue
            if len(self._keys) >= 2 and not before(pts, self._keys[1]):
                shift = self._cut(i, done, dts, segment=True)
                pos, end = pos - shift, end - shift
                # a lost segment's frames never show; its key gets no parts
                while len(self._keys) >= 2 and not before(pts, self._keys[1]):
                    self._keys.popleft()
            elif _since(dts, self._start) >= self._split - self._frame / 2:
                shift = self._cut(i, done, dts, segment=False)
                pos, end = pos - shift, end - shift
        self._scanned = end
        if self._start is None:
            # nothing keyed yet: keep only the latest PAT and PMT for the first part
            self._keep_headers(end)
            del self._buf[:end]
            self._buf[0:0] = self._headers
            self._scanned = len(self._headers)
        return done

    def end(self) -> list[Part]:
        """Close the open part as its segment's last, timed to one frame past its last."""
        done: list[Part] = []
        if self._keys and self._start is not None and self._last_dts is not None:
            end = len(self._buf) - len(self._buf) % PACKET
            duration = _since(self._last_dts, self._start) + self._frame
            done.append(
                Part(self._keys[0], bytes(self._buf[:end]), duration, self._independent, True)
            )
        self._keys.clear()
        self._buf.clear()
        self._scanned = 0
        self._start = None
        self._independent = True
        return done

    def _find_program(self, end: int) -> bool:
        pmt, streams = program(bytes(self._buf[:end]))
        video = next((pid for pid, t in streams.items() if t in _VIDEO_TYPES), None)
        if pmt is None or video is None:
            return False
        self._video = bytes([0x47, 0x40 | video >> 8, video & 0xFF])
        self._pmt = bytes([0x47, 0x40 | pmt >> 8, pmt & 0xFF])
        self._psi = {0, _SDT_PID, pmt}
        return True

    def _keep_headers(self, hi: int) -> None:
        assert self._pmt is not None
        pat = _last_packet(self._buf, _PAT, hi)
        pmt = _last_packet(self._buf, self._pmt, hi)
        if pat is not None and pmt is not None:
            self._headers = pat + pmt

    def _cut(self, i: int, done: list[Part], dts: int, segment: bool) -> int:
        """Finish the open part before the frame at i; return how far _buf shifted."""
        assert self._start is not None
        cut = i
        while cut >= PACKET and _pid_at(self._buf, cut - PACKET) in self._psi:
            cut -= PACKET
        moved = {_pid_at(self._buf, p) for p in range(cut, i, PACKET)}
        self._keep_headers(cut)
        done.append(
            Part(
                self._keys[0],
                bytes(self._buf[:cut]),
                _since(dts, self._start),
                self._independent,
                segment,
            )
        )
        del self._buf[:cut]
        self._start = dts
        self._independent = segment
        if segment:
            self._keys.popleft()
            if not self._psi - {_SDT_PID} <= moved:
                self._buf[0:0] = self._headers
                return cut - len(self._headers)
        return cut
