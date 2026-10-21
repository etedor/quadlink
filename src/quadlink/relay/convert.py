"""One ffmpeg process per continuous run of MPEG-TS segments, converting audio to 48 kHz.

macOS AVPlayer resamples non-48 kHz audio with a small rate error, so a 44.1 kHz
channel drifts out of lip sync on machina; delivering 48 kHz removes the resampling.
A single long-running encoder is required: encoding segments separately leaves
audible seams, because independent AAC encoder states never agree at a boundary.
"""

import asyncio
import shutil
from pathlib import Path

import structlog

from quadlink.relay.tsinfo import first_video_pts

logger = structlog.get_logger()

TARGET_RATE = 48000
# the native AAC encoder emits its first frame 1024 samples early; shifting input
# timestamps by the same amount keeps audio aligned with the copied video
_ENCODER_DELAY = 1024
# bounds out.m3u8 over a long run; the scan reads only the newest name, so any size works
_LIST_SIZE = 40


class AudioConverter:
    """Pipes consecutive TS segments through ffmpeg; output segments match inputs 1:1."""

    def __init__(self, ffmpeg: str, workdir: Path) -> None:
        self.ffmpeg = ffmpeg
        self.workdir = workdir
        self._proc: asyncio.subprocess.Process | None = None
        self._by_pts: dict[int, Path] = {}
        # index of the next o<N>.ts to scan
        self._next = 0
        # discarded before their output was scanned; deleted once it appears
        self._dropped: set[int] = set()

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
            # a tiny hls_time makes ffmpeg cut at every keyframe, which Twitch places
            # at each segment start, so outputs line up with inputs
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

    async def feed(self, data: bytes) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(data)
        await self._proc.stdin.drain()

    def _scan(self) -> None:
        # the workdir can vanish under a reader once close() runs; treat that as no output
        try:
            text = (self.workdir / "out.m3u8").read_text()
        except OSError:
            return
        names = [ln for ln in text.splitlines() if ln.endswith(".ts")]
        if not names:
            return
        # outputs are o<N>.ts in order and listed only once complete
        newest = int(names[-1][1:-3])
        while self._next <= newest:
            path = self.workdir / f"o{self._next}.ts"
            try:
                pts = first_video_pts(path.read_bytes())
            except FileNotFoundError:
                pts = None
            except OSError:
                return
            self._next += 1
            if pts is None:
                continue
            if pts in self._dropped:
                self._dropped.discard(pts)
                path.unlink(missing_ok=True)
            else:
                self._by_pts[pts] = path

    def discard(self, video_pts: int) -> None:
        """Delete the output for this input's video PTS; no reader may ask for it again."""
        self._scan()
        path = self._by_pts.pop(video_pts, None)
        if path is None:
            self._dropped.add(video_pts)
        else:
            path.unlink(missing_ok=True)

    def has_output(self, video_pts: int) -> bool:
        """Whether a finished output segment already exists for this input's video PTS."""
        self._scan()
        return video_pts in self._by_pts

    async def output_for(self, video_pts: int, timeout: float) -> bytes | None:
        """Converted bytes of the input segment starting at this video PTS, once written."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self._scan()
            path = self._by_pts.get(video_pts)
            if path is not None:
                try:
                    return path.read_bytes()
                except OSError:
                    del self._by_pts[video_pts]
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

    async def close(self) -> None:
        await self.finish()
        shutil.rmtree(self.workdir, ignore_errors=True)
