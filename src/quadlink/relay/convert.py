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


class AudioConverter:
    """Pipes consecutive TS segments through ffmpeg; output segments match inputs 1:1."""

    def __init__(self, ffmpeg: str, workdir: Path) -> None:
        self.ffmpeg = ffmpeg
        self.workdir = workdir
        self._proc: asyncio.subprocess.Process | None = None
        self._by_pts: dict[int, Path] = {}
        self._listed = 0

    async def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
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
            # a tiny hls_time makes ffmpeg cut at every keyframe, which Twitch places
            # at each segment start, so outputs line up with inputs
            "-f",
            "hls",
            "-hls_time",
            "0.1",
            "-hls_list_size",
            "0",
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
            names = [
                ln
                for ln in (self.workdir / "out.m3u8").read_text().splitlines()
                if ln.endswith(".ts")
            ]
            for name in names[self._listed :]:
                path = self.workdir / name
                pts = first_video_pts(path.read_bytes())
                if pts is not None:
                    self._by_pts[pts] = path
                self._listed += 1
        except OSError:
            return

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
