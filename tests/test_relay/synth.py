"""Hand-built MPEG-TS packets for parser tests that need exact timestamps."""

PMT_PID = 0x1000
VIDEO_PID = 0x100
AUDIO_PID = 0x101


def _packet(pid: int, payload: bytes, start: bool, cc: int = 0) -> bytes:
    flags = 0x40 if start else 0
    assert len(payload) <= 184
    if len(payload) == 184:
        return bytes([0x47, flags | pid >> 8, pid & 0xFF, 0x10 | cc & 0x0F]) + payload
    # pad with an adaptation field of stuffing bytes
    fill = 183 - len(payload)
    adapt = bytes([fill]) + (b"\x00" + b"\xff" * (fill - 1) if fill else b"")
    return bytes([0x47, flags | pid >> 8, pid & 0xFF, 0x30 | cc & 0x0F]) + adapt + payload


def _section(table_id: int, body: bytes) -> bytes:
    length = len(body) + 5 + 4
    head = bytes([table_id, 0xB0 | length >> 8, length & 0xFF, 0, 1, 0xC1, 0, 0])
    # CRC is not checked by the parsers under test
    return b"\x00" + head + body + b"\0\0\0\0"


def pat(cc: int = 0) -> bytes:
    return _packet(0, _section(0, bytes([0, 1, 0xE0 | PMT_PID >> 8, PMT_PID & 0xFF])), True, cc)


def pmt(cc: int = 0) -> bytes:
    body = bytes([0xE0 | VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0xF0, 0])
    for stype, pid in ((0x1B, VIDEO_PID), (0x0F, AUDIO_PID)):
        body += bytes([stype, 0xE0 | pid >> 8, pid & 0xFF, 0xF0, 0])
    return _packet(PMT_PID, _section(2, body), True, cc)


def _ts(prefix: int, value: int) -> bytes:
    value %= 1 << 33
    return bytes(
        [
            prefix << 4 | (value >> 30 & 0x07) << 1 | 1,
            value >> 22 & 0xFF,
            (value >> 15 & 0x7F) << 1 | 1,
            value >> 7 & 0xFF,
            (value & 0x7F) << 1 | 1,
        ]
    )


def pes(pid: int, pts: int, dts: int | None = None, cc: int = 0) -> bytes:
    """One TS packet starting a PES packet with these timestamps."""
    if dts is None:
        header = bytes([0x80, 0x80, 5]) + _ts(2, pts)
    else:
        header = bytes([0x80, 0xC0, 10]) + _ts(3, pts) + _ts(1, dts)
    stream_id = 0xE0 if pid == VIDEO_PID else 0xC0
    return _packet(pid, b"\0\0\1" + bytes([stream_id, 0, 0]) + header, True, cc)


def cont(pid: int, cc: int = 0) -> bytes:
    """A TS packet continuing a PES packet."""
    return _packet(pid, b"\xaa" * 184, False, cc)
