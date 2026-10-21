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
def ts_run(ffmpeg: str, tmp_path_factory: pytest.TempPathFactory) -> Callable[..., list[bytes]]:
    """Consecutive 2s TS segments with continuous timestamps, like one Twitch broadcast.

    `extra_keyframes` adds keyframes at these offsets (seconds) inside every segment,
    as some Twitch channels do; segments still start at every 2s keyframe. `offset`
    is the first timestamp in seconds; past 95443.7 the 33-bit PTS wraps. `open_gop`
    encodes 640x360 with B-frames and open GOPs, as many broadcasters' x264 does: a
    segment's leading B-frames show before its keyframe and reference the previous GOP.
    """

    def make(
        rate: int,
        count: int,
        extra_keyframes: tuple[float, ...] = (),
        offset: float = 10,
        open_gop: bool = False,
    ) -> list[bytes]:
        out = tmp_path_factory.mktemp(f"ts{rate}")
        size = "640x360" if open_gop else "320x180"
        gop = ["-bf", "3", "-x264-params", "open-gop=1"] if open_gop else []
        times = [2 * i + off for i in range(count) for off in (0.0, *extra_keyframes)]
        force = (
            ["-force_key_frames", ",".join(f"{t:.3f}" for t in times)] if extra_keyframes else []
        )
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=s={size}:r=30",
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
                *force,
                *gop,
                "-c:a",
                "aac",
                "-ar",
                str(rate),
                "-ac",
                "2",
                "-output_ts_offset",
                str(offset),
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


def _top_boxes(data: bytes) -> list[tuple[bytes, bytes]]:
    boxes, pos = [], 0
    while pos + 8 <= len(data):
        size = int.from_bytes(data[pos : pos + 4], "big")
        boxes.append((data[pos + 4 : pos + 8], data[pos : pos + size]))
        pos += size
    return boxes


@pytest.fixture(scope="session")
def fmp4_run(
    ffmpeg: str, tmp_path_factory: pytest.TempPathFactory
) -> Callable[..., tuple[bytes, list[bytes]]]:
    """(init, media segments) of a 2s-segment fMP4 HLS broadcast with B-frames.

    `chunked` cuts every segment into 0.25s fragments, as Twitch's CMAF segments are;
    otherwise each segment is one fragment, as ffmpeg's HLS muxer writes them. `offset`
    is the first timestamp in seconds, which ffmpeg stores as an edit list delay.
    `negative_cts` writes chunked fragments with signed composition offsets (trun v1),
    as CMAF muxers do, so B-frame offsets go below zero instead of an edit list.
    `bframes` sets the B-frame depth, and with it the most negative offset.
    """

    def make(
        rate: int,
        count: int,
        offset: float = 10,
        chunked: bool = False,
        negative_cts: bool = False,
        bframes: int = 3,
    ) -> tuple[bytes, list[bytes]]:
        out = tmp_path_factory.mktemp(f"fmp4{rate}")
        chunked = chunked or negative_cts
        if chunked:
            # one fragmented file, cut at the fragments holding each 2s keyframe below
            container = [
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof"
                + ("+negative_cts_offsets" if negative_cts else ""),
                "-frag_duration",
                "250000",
                "-f",
                "mp4",
                str(out / "all.mp4"),
            ]
        else:
            container = [
                "-f",
                "hls",
                "-hls_segment_type",
                "fmp4",
                "-hls_time",
                "2",
                "-hls_list_size",
                "0",
                "-hls_fmp4_init_filename",
                "init.mp4",
                "-hls_segment_filename",
                str(out / "s%02d.m4s"),
                str(out / "p.m3u8"),
            ]
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=s=640x360:r=30",
                "-f",
                "lavfi",
                "-i",
                f"sine=f=440:r={rate}",
                "-t",
                str(2 * count),
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c:v",
                "libx264",
                "-bf",
                str(bframes),
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
                str(offset),
                *container,
            ],
            check=True,
        )
        if not chunked:
            segs = [p.read_bytes() for p in sorted(out.glob("s*.m4s"))]
            return (out / "init.mp4").read_bytes(), segs[:count]
        boxes = _top_boxes((out / "all.mp4").read_bytes())
        init = b"".join(box for kind, box in boxes if kind in (b"ftyp", b"moov"))
        fragments = [
            boxes[i][1] + boxes[i + 1][1] for i, (kind, _) in enumerate(boxes) if kind == b"moof"
        ]
        # 30 fps, 0.25s per fragment: a 2s keyframe opens every 8th fragment's group
        per = len(fragments) // count
        return init, [b"".join(fragments[i * per : (i + 1) * per]) for i in range(count)]

    return make
