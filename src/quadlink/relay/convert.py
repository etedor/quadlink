"""One ffmpeg process per continuous run of segments, converting audio to 48 kHz.

macOS AVPlayer resamples non-48 kHz audio with a small rate error, so a 44.1 kHz
channel drifts out of lip sync on machina; delivering 48 kHz removes the resampling.
A single long-running encoder is required: encoding segments separately leaves
audible seams, because independent AAC encoder states never agree at a boundary.

ffmpeg reads the run's segments on stdin and writes one MPEG-TS stream to stdout,
which Splitter cuts back into one output per input segment. Nothing touches disk.
fMP4 segments reach it as MPEG-TS too, each remuxed on its own by Remuxer.
"""

import asyncio
from collections import deque
from collections.abc import Callable, Iterable

import structlog

from quadlink.relay.mp4info import lift_offsets, video_track
from quadlink.relay.tsinfo import (
    _VIDEO_TYPES,
    PACKET,
    _packets,
    before,
    continue_stream,
    first_video_pts,
    pes_pts,
    program,
)

logger = structlog.get_logger()

TARGET_RATE = 48000
# the native AAC encoder emits its first frame 1024 samples early; shifting input
# timestamps by the same amount keeps audio aligned with the copied video
_ENCODER_DELAY = 1024
_READ = 1 << 16
_PAT = b"\x47\x40\x00"
_SDT_PID = 0x11
# seconds ffmpeg gets to drain after EOF before it is killed
_FINISH_WAIT = 5.0
# bytes of ffmpeg output that may wait for a cut: ~8 segments at Twitch's top
# bitrate; more means no cut is coming, and the run must end before it exhausts memory
MAX_PENDING = 16 << 20

OnOutput = Callable[[int, bytes | None], None]


def _pid_at(buf: bytearray, i: int) -> int:
    return ((buf[i + 1] & 0x1F) << 8) | buf[i + 2]


def _pts_at(buf: bytearray, i: int) -> int | None:
    for _, _, payload in _packets(bytes(buf[i : i + PACKET])):
        return pes_pts(payload)
    return None


def _last_packet(buf: bytearray, header: bytes, hi: int) -> bytes | None:
    i = buf.rfind(header, 0, hi)
    while i > 0 and i % PACKET:
        i = buf.rfind(header, 0, i)
    return bytes(buf[i : i + PACKET]) if i >= 0 and i % PACKET == 0 else None


class Splitter:
    """Cuts ffmpeg's continuous MPEG-TS output back into one output per fed segment.

    Each fed segment is known by its first video PTS (its key). The output for key k
    runs up to the TS packet that starts the first video PES at or past key k+1,
    which with -copyts is that segment's first keyframe; audio stays where ffmpeg
    interleaved it. Cuts are whole packets, so outputs concatenate back into the
    exact stream: continuity counters run on across outputs as in any HLS TS
    segmenter, and a player starting at any output only sees a fresh start.

    ffmpeg writes SDT, PAT and PMT right before each keyframe, so those packets
    move to the output they introduce. If a cut has none, copies of the latest PAT
    and PMT go first: same bytes and continuity counter, so a player reading
    across the cut sees a legal duplicate packet (ISO/IEC 13818-1 2.4.3.3).
    """

    def __init__(self) -> None:
        # keys of fed segments whose output is not complete; the head is being written
        self._keys: deque[int] = deque()
        self._buf = bytearray()
        # packets of _buf before this offset hold no cut
        self._scanned = 0
        # first 3 bytes of a TS packet that starts a video PES
        self._video: bytes | None = None
        self._pmt: bytes | None = None
        self._psi: set[int] = set()
        self._headers = b""

    @property
    def pending(self) -> list[int]:
        return list(self._keys)

    @property
    def size(self) -> int:
        return len(self._buf)

    def expect(self, key: int) -> None:
        self._keys.append(key)

    def push(self, chunk: bytes) -> list[tuple[int, bytes]]:
        """Take more ffmpeg output; return (key, output) of each output now complete."""
        self._buf += chunk
        end = len(self._buf) - len(self._buf) % PACKET
        if self._video is None and not self._find_program(end):
            return []
        assert self._video is not None
        done: list[tuple[int, bytes]] = []
        pos = self._scanned
        while (i := self._buf.find(self._video, pos, end)) >= 0:
            if i % PACKET:
                pos = i + 1
                continue
            pos = i + PACKET
            if len(self._keys) < 2:
                continue
            pts = _pts_at(self._buf, i)
            if pts is None or before(pts, self._keys[1]):
                continue
            shift = self._cut(i, done)
            pos, end = pos - shift, end - shift
            # a lost segment's frames never show; its key gets no output
            while len(self._keys) >= 2 and not before(pts, self._keys[1]):
                done.append((self._keys.popleft(), b""))
        self._scanned = end
        if not self._keys:
            # output of a segment without video PTS, ahead of any key; its PAT and PMT
            # stay, so the first keyed output still starts with them
            self._keep_headers(end)
            del self._buf[:end]
            self._buf[0:0] = self._headers
            self._scanned = 0
        return done

    def _keep_headers(self, hi: int) -> None:
        assert self._pmt is not None
        pat = _last_packet(self._buf, _PAT, hi)
        pmt = _last_packet(self._buf, self._pmt, hi)
        if pat is not None and pmt is not None:
            self._headers = pat + pmt

    def _find_program(self, end: int) -> bool:
        pmt, streams = program(bytes(self._buf[:end]))
        video = next((pid for pid, t in streams.items() if t in _VIDEO_TYPES), None)
        if pmt is None or video is None:
            return False
        self._video = bytes([0x47, 0x40 | video >> 8, video & 0xFF])
        self._pmt = bytes([0x47, 0x40 | pmt >> 8, pmt & 0xFF])
        self._psi = {0, _SDT_PID, pmt}
        return True

    def _cut(self, i: int, done: list[tuple[int, bytes]]) -> int:
        """Complete the head output before packet i; return how far _buf shifted."""
        assert self._pmt is not None
        cut = i
        while cut >= PACKET and _pid_at(self._buf, cut - PACKET) in self._psi:
            cut -= PACKET
        moved = {_pid_at(self._buf, p) for p in range(cut, i, PACKET)}
        self._keep_headers(cut)
        with memoryview(self._buf) as view:
            done.append((self._keys.popleft(), bytes(view[:cut])))
        del self._buf[:cut]
        if not self._psi - {_SDT_PID} <= moved:
            self._buf[0:0] = self._headers
            return cut - len(self._headers)
        return cut

    def end(self) -> list[tuple[int, bytes]]:
        """Complete every output at EOF: the head gets the rest of the stream."""
        done: list[tuple[int, bytes]] = []
        if self._keys:
            end = len(self._buf) - len(self._buf) % PACKET
            done.append((self._keys.popleft(), bytes(self._buf[:end])))
        done += [(key, b"") for key in self._keys]
        self._keys.clear()
        self._buf.clear()
        return done

    def abandon(self) -> list[int]:
        """Drop every incomplete output; return their keys."""
        keys = list(self._keys)
        self._keys.clear()
        self._buf.clear()
        return keys


def command(ffmpeg: str) -> list[str]:
    """ffmpeg arguments for one run: MPEG-TS in on stdin, 48 kHz AAC MPEG-TS out on stdout."""
    return [
        ffmpeg,
        "-v",
        "error",
        "-copyts",
        # the default probe holds a new run's first output until a third segment
        # is in; 0.5s finds both streams within the first
        "-analyzeduration",
        "500000",
        "-f",
        "mpegts",
        "-i",
        "pipe:0",
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-c:v",
        "copy",
        "-af",
        f"aresample={TARGET_RATE},asetpts=PTS+{_ENCODER_DELAY}",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        # one stereo AAC encode needs no worker threads; each one counts toward TasksMax
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-f",
        "mpegts",
        # a run starting under 60s before the 33-bit PTS wrap reads as negative
        # timestamps; shifting them would move every output off its input's PTS
        "-avoid_negative_ts",
        "disabled",
        # each packet reaches the pipe as soon as it is muxed, so a segment's
        # output completes as soon as the next segment's keyframe is fed
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "-flush_packets",
        "1",
        "pipe:1",
    ]


class AudioConverter:
    """Pipes consecutive TS segments through ffmpeg and hands back one output per input.

    on_output(key, data) runs once per segment that register() gave a key: with the
    converted bytes once complete, or with None when the run failed before that.
    """

    def __init__(self, ffmpeg: str, on_output: OnOutput) -> None:
        self.ffmpeg = ffmpeg
        self._on_output = on_output
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._splitter = Splitter()
        self._last: int | None = None
        # set once EOF is ours, so the reader leaves the tail to finish()
        self._finishing = False
        # set once the reader has given up; later feeds fail at once
        self.failed = False

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *command(self.ffmpeg),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._read())

    def continues(self, data: bytes) -> bool:
        """Whether the segment can join this run.

        A video PTS not after the run's last key (a step back, or a jump past half
        the PTS range) would never close the head output; such a segment starts a new
        run. Segments without video PTS always join.
        """
        if self.failed:
            return False
        pts = first_video_pts(data)
        return pts is None or self._last is None or before(self._last, pts)

    def register(self, data: bytes) -> int | None:
        """Note the next input segment; return the key its output is delivered under.

        None means the segment gets no output of its own: without a video PTS (or
        with one not after the previous segment's) its content joins the one before.
        """
        pts = first_video_pts(data)
        if pts is None or (self._last is not None and not before(self._last, pts)):
            return None
        self._last = pts
        self._splitter.expect(pts)
        return pts

    async def feed(self, data: bytes) -> None:
        """Write one input segment, after register()."""
        if self.failed:
            raise RuntimeError("relay conversion already failed")
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    def _emit(self, outputs: Iterable[tuple[int, bytes | None]]) -> None:
        for key, data in outputs:
            self._on_output(key, data or None)

    async def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stdout = proc.stdout
        try:
            while chunk := await stdout.read(_READ):
                self._emit(self._splitter.push(chunk))
                if self._splitter.size > MAX_PENDING:
                    raise BufferError(f"{self._splitter.size} bytes of output with no cut")
        except Exception as e:
            logger.warning("relay conversion output failed", error=repr(e))
            self.failed = True
            # nothing reads stdout now; a live ffmpeg would block the next feed until FEED_WAIT
            if proc.returncode is None:
                proc.kill()
            self._emit((key, None) for key in self._splitter.abandon())
            return
        if not self._finishing:
            # ffmpeg died: what it wrote of the head output ends mid-segment
            logger.warning("relay ffmpeg exited mid-run", returncode=proc.returncode)
            self.failed = True
            self._emit((key, None) for key in self._splitter.abandon())

    async def finish(self) -> None:
        """End the input; deliver every remaining output once ffmpeg has drained."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        self._finishing = True
        if proc.stdin is not None:
            proc.stdin.close()
        assert self._reader is not None
        try:
            async with asyncio.timeout(_FINISH_WAIT):
                await self._reader
                await proc.wait()
        except TimeoutError:
            await self._kill(proc)
        if proc.returncode == 0:
            self._emit(self._splitter.end())
        else:
            self._emit((key, None) for key in self._splitter.abandon())

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)

    async def close(self) -> None:
        """Stop ffmpeg at once; outputs not yet delivered never are."""
        proc, self._proc = self._proc, None
        self._finishing = True
        if proc is not None:
            await self._kill(proc)
        self._splitter.abandon()
        # the proxy's callback refers back to this converter; drop the cycle
        self._on_output = _ignore


def _ignore(key: int, data: bytes | None) -> None:
    pass


async def remux(ffmpeg: str, init: bytes, data: bytes, timeout: float) -> bytes:
    """One fMP4 media segment as MPEG-TS with its streams copied.

    Timestamps stay the source's, so the output fits between converted neighbours.
    Raises RuntimeError, with ffmpeg's stderr, if it exits non-zero or writes nothing.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-v",
        "error",
        "-copyts",
        "-f",
        "mp4",
        "-i",
        "pipe:0",
        "-map",
        "0:v",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-f",
        "mpegts",
        "-avoid_negative_ts",
        "disabled",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            out, err = await proc.communicate(init + data)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}: {err.decode(errors='replace')}")
    return out


class Remuxer:
    """Remuxes the media segments of one fMP4 init to MPEG-TS that reads as one stream.

    Each segment gets its own short ffmpeg: one long-lived mov demuxer keeps an index
    entry for every sample it has read, ~20 MiB an hour. Joined, the outputs match a
    single remux of all segments fed so far, so they feed a converter like TS segments:
    each video PTS gets the lift one demuxer would have reached (see lift_offsets),
    and continuity counters run on.

    `broke_run` is set by remux() whenever the segment it just returned could not be
    lifted to continue the stream so far: the caller must start a new converter run
    at it instead of feeding it on as a continuation.
    """

    def __init__(self, ffmpeg: str, init: bytes) -> None:
        self.ffmpeg = ffmpeg
        self.init = init
        self._video = video_track(init)
        self._lift = 0
        self._counters: dict[int, int] = {}
        self.broke_run = False

    async def remux(self, data: bytes, timeout: float) -> bytes:
        """One media segment as MPEG-TS. Raises on failure. Call in stream order."""
        lift = self._lift
        broke = False
        if self._video is not None:
            data, lift, broke = lift_offsets(data, self._video, lift)
        # remux() bounds only its pipe; this also covers starting ffmpeg
        async with asyncio.timeout(timeout):
            out = await remux(self.ffmpeg, self.init, data, timeout)
        self._lift = lift
        self.broke_run = broke
        return continue_stream(out, self._counters)
