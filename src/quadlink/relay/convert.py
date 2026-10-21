"""One ffmpeg process per continuous run of MPEG-TS segments, converting audio to 48 kHz.

macOS AVPlayer resamples non-48 kHz audio with a small rate error, so a 44.1 kHz
channel drifts out of lip sync on machina; delivering 48 kHz removes the resampling.
A single long-running encoder is required: encoding segments separately leaves
audible seams, because independent AAC encoder states never agree at a boundary.
"""

import asyncio
import shutil
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from quadlink.relay.tsinfo import first_video_pts

logger = structlog.get_logger()

TARGET_RATE = 48000
# the native AAC encoder emits its first frame 1024 samples early; shifting input
# timestamps by the same amount keeps audio aligned with the copied video
_ENCODER_DELAY = 1024
# bounds out.m3u8 over a long run; the scan reads only names it has not seen, so
# any size works as long as the scan keeps up with ffmpeg
_LIST_SIZE = 40
_PTS_HZ = 90000
# EXTINF carries microseconds, so a piece's end lands within a tick of the next
# keyframe; any slack below one frame (750 ticks at 120 fps) is safe
_END_SLACK = 90


@dataclass
class _Range:
    """Output pieces of one fed segment, in order."""

    pieces: list[Path] = field(default_factory=list)
    dropped: bool = False


class AudioConverter:
    """Pipes consecutive TS segments through ffmpeg and maps outputs back to inputs.

    ffmpeg cuts an output piece at every keyframe. Twitch puts one at each segment
    start, and some channels add more inside a segment, so an input segment's output
    is every piece whose first video PTS lies between its own and the next segment's.
    """

    def __init__(self, ffmpeg: str, workdir: Path) -> None:
        self.ffmpeg = ffmpeg
        self.workdir = workdir
        self._proc: asyncio.subprocess.Process | None = None
        # first video PTS of each fed segment, increasing; keys of _ranges
        self._bounds: list[int] = []
        self._ranges: dict[int, _Range] = {}
        # index of the next o<N>.ts to scan
        self._next = 0
        # range of the newest scanned piece; a piece without video joins it
        self._last: int | None = None
        # PTS where the newest scanned piece ends, i.e. where ffmpeg cut next
        self._end: int | None = None
        # after EOF ffmpeg writes no more pieces, so every range is complete
        self._finished = False

    async def start(self) -> None:
        # a run dir left by a crashed daemon holds an out.m3u8 the scan would trust
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir.mkdir(parents=True)
        self._proc = await asyncio.create_subprocess_exec(
            self.ffmpeg,
            "-v",
            "error",
            "-copyts",
            # the default probe holds a new run's first output until a third segment
            # is in; 0.5s finds both streams within the first, so it lags by one
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
            # a tiny hls_time makes ffmpeg cut at every keyframe, so every input
            # segment boundary is also an output piece boundary
            "-f",
            "hls",
            "-hls_time",
            "0.1",
            "-hls_list_size",
            str(_LIST_SIZE),
            "-hls_segment_filename",
            str(self.workdir / "o%d.ts"),
            "-muxdelay",
            "0",
            "-muxpreload",
            "0",
            str(self.workdir / "out.m3u8"),
            stdin=asyncio.subprocess.PIPE,
        )

    async def feed(self, data: bytes) -> int | None:
        """Write one input segment; return the video PTS its output is looked up by.

        None means the segment has no output of its own: without video PTS (or with
        one not after the previous segment's) its content folds into the range before.
        """
        assert self._proc is not None and self._proc.stdin is not None
        pts = first_video_pts(data)
        if pts is not None and (not self._bounds or pts > self._bounds[-1]):
            self._bounds.append(pts)
            self._ranges[pts] = _Range()
        else:
            pts = None
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()
        return pts

    def _scan(self) -> None:
        # the workdir can vanish under a reader once close() runs; treat that as no output
        try:
            text = (self.workdir / "out.m3u8").read_text()
        except OSError:
            return
        durations: dict[str, float] = {}
        extinf: float | None = None
        for ln in text.splitlines():
            if ln.startswith("#EXTINF:"):
                extinf = float(ln[len("#EXTINF:") :].split(",")[0])
            elif ln.endswith(".ts") and extinf is not None:
                durations[ln] = extinf
        if not durations:
            return
        # outputs are o<N>.ts in order and listed only once complete
        newest = max(int(name[1:-3]) for name in durations)
        while self._next <= newest:
            name = f"o{self._next}.ts"
            path = self.workdir / name
            try:
                pts = first_video_pts(path.read_bytes())
            except FileNotFoundError:
                self._next += 1
                continue
            except OSError:
                return
            self._next += 1
            if pts is not None:
                i = bisect_right(self._bounds, pts) - 1
                self._last = self._bounds[i] if i >= 0 else None
                if name in durations:
                    self._end = pts + round(durations[name] * _PTS_HZ)
            rng = self._ranges.get(self._last) if self._last is not None else None
            if rng is None or rng.dropped:
                path.unlink(missing_ok=True)
            else:
                rng.pieces.append(path)
        self._prune()

    def _complete(self, i: int) -> bool:
        """Whether range i can gain no more pieces: ffmpeg has cut at the next boundary."""
        if self._finished:
            return True
        if i + 1 >= len(self._bounds):
            return False
        return self._end is not None and self._end >= self._bounds[i + 1] - _END_SLACK

    def _prune(self) -> None:
        # only a complete range can go: a later piece could still map into it
        while self._bounds and self._ranges[self._bounds[0]].dropped and self._complete(0):
            del self._ranges[self._bounds.pop(0)]

    def _ready(self, video_pts: int) -> _Range | None:
        rng = self._ranges.get(video_pts)
        if rng is None or rng.dropped or not rng.pieces:
            return None
        if not self._complete(bisect_left(self._bounds, video_pts)):
            return None
        return rng

    def discard(self, video_pts: int) -> None:
        """Delete every output piece for this input's video PTS, now and once written."""
        self._scan()
        rng = self._ranges.get(video_pts)
        if rng is None:
            return
        for path in rng.pieces:
            path.unlink(missing_ok=True)
        rng.pieces.clear()
        rng.dropped = True
        self._prune()

    def has_output(self, video_pts: int) -> bool:
        """Whether the whole output of the input segment at this video PTS exists."""
        self._scan()
        return self._ready(video_pts) is not None

    async def output_for(self, video_pts: int, timeout: float) -> bytes | None:
        """Converted bytes of the input segment starting at this video PTS, once written.

        MPEG-TS concatenates byte-wise, so the pieces join into one segment.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self._scan()
            rng = self._ready(video_pts)
            if rng is not None:
                try:
                    return b"".join(path.read_bytes() for path in rng.pieces)
                except OSError:
                    pass
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def finish(self) -> None:
        """End the input so ffmpeg writes its last segment; outputs stay until close()."""
        proc, self._proc = self._proc, None
        if proc is not None:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        self._finished = True

    async def close(self) -> None:
        await self.finish()
        shutil.rmtree(self.workdir, ignore_errors=True)
        self._bounds.clear()
        self._ranges.clear()
