"""Shared relay fixtures: real MPEG-TS segments made with ffmpeg."""

import shutil
import subprocess
from collections.abc import Callable

import pytest


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        pytest.skip("ffmpeg not on PATH")
    return path


@pytest.fixture(scope="session")
def ts_run(
    ffmpeg: str, tmp_path_factory: pytest.TempPathFactory
) -> Callable[[int, int], list[bytes]]:
    """Consecutive 2s TS segments with continuous timestamps, like one Twitch broadcast."""

    def make(rate: int, count: int) -> list[bytes]:
        out = tmp_path_factory.mktemp(f"ts{rate}")
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=s=320x180:r=30",
                "-f",
                "lavfi",
                "-i",
                f"sine=f=440:r={rate}",
                "-t",
                str(2 * count),
                "-map",
                "1:a",
                "-map",
                "0:v",
                "-c:v",
                "libx264",
                "-g",
                "60",
                "-keyint_min",
                "60",
                "-sc_threshold",
                "0",
                "-c:a",
                "aac",
                "-ar",
                str(rate),
                "-ac",
                "2",
                "-output_ts_offset",
                "10",
                "-f",
                "segment",
                "-segment_time",
                "2",
                "-segment_format",
                "mpegts",
                "-reset_timestamps",
                "0",
                str(out / "s%02d.ts"),
            ],
            check=True,
        )
        return [p.read_bytes() for p in sorted(out.glob("s*.ts"))][:count]

    return make
