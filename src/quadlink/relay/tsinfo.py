"""Just enough MPEG-TS parsing to route and convert relay segments (ISO/IEC 13818-1)."""

from collections.abc import Iterator

PACKET = 188
_VIDEO_TYPES = {0x1B, 0x24}  # H.264, HEVC
_AAC_ADTS = 0x0F
# ISO/IEC 14496-3 sampling_frequency_index
_ADTS_RATES = [
    96000,
    88200,
    64000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    12000,
    11025,
    8000,
    7350,
]


def _packets(data: bytes) -> Iterator[tuple[int, bool, bytes]]:
    """(pid, payload_unit_start, payload) for each well-formed packet."""
    for i in range(0, len(data) - PACKET + 1, PACKET):
        pkt = data[i : i + PACKET]
        if pkt[0] != 0x47:
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        start = bool(pkt[1] & 0x40)
        control = (pkt[3] >> 4) & 0x3
        if control in (0, 2):
            continue
        offset = 4 + (1 + pkt[4] if control == 3 else 0)
        if offset < PACKET:
            yield pid, start, pkt[offset:]


def _section(payload: bytes) -> bytes:
    pointer = payload[0]
    return payload[1 + pointer :]


def _streams(data: bytes) -> dict[int, int]:
    """pid -> stream_type from the first PAT/PMT."""
    pmt_pid = None
    for pid, start, payload in _packets(data):
        if pid == 0 and start and pmt_pid is None:
            sec = _section(payload)
            if len(sec) >= 12:
                pmt_pid = ((sec[10] & 0x1F) << 8) | sec[11]
        elif pmt_pid is not None and pid == pmt_pid and start:
            sec = _section(payload)
            length = ((sec[1] & 0x0F) << 8) | sec[2]
            info = ((sec[10] & 0x0F) << 8) | sec[11]
            pos, end = 12 + info, 3 + length - 4
            streams: dict[int, int] = {}
            while pos + 5 <= end:
                stype = sec[pos]
                epid = ((sec[pos + 1] & 0x1F) << 8) | sec[pos + 2]
                streams[epid] = stype
                pos += 5 + (((sec[pos + 3] & 0x0F) << 8) | sec[pos + 4])
            return streams
    return {}


def _pes_starts(data: bytes, wanted: int) -> Iterator[tuple[int | None, bytes]]:
    """(PTS, first payload bytes) of each PES packet on one PID."""
    for pid, start, payload in _packets(data):
        if pid != wanted or not start or payload[:3] != b"\x00\x00\x01":
            continue
        pts = None
        if len(payload) >= 14 and payload[7] & 0x80:
            p = payload[9:14]
            pts = (
                ((p[0] >> 1) & 0x07) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1
            )
        yield pts, payload[9 + payload[8] :] if len(payload) > 8 else b""


def _pid(data: bytes, types: set[int]) -> int | None:
    return next((pid for pid, t in _streams(data).items() if t in types), None)


def first_video_pts(data: bytes) -> int | None:
    pid = _pid(data, _VIDEO_TYPES)
    if pid is None:
        return None
    values = [pts for pts, _ in _pes_starts(data, pid) if pts is not None]
    return min(values) if values else None


def audio_pts(data: bytes) -> list[int]:
    pid = _pid(data, {_AAC_ADTS})
    if pid is None:
        return []
    return sorted(pts for pts, _ in _pes_starts(data, pid) if pts is not None)


def audio_sample_rate(data: bytes) -> int | None:
    pid = _pid(data, {_AAC_ADTS})
    if pid is None:
        return None
    for _, body in _pes_starts(data, pid):
        if len(body) >= 3 and body[0] == 0xFF and body[1] & 0xF0 == 0xF0:
            index = (body[2] >> 2) & 0x0F
            return _ADTS_RATES[index] if index < len(_ADTS_RATES) else None
    return None
