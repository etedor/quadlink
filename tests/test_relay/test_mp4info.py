"""Tests for the fMP4 parsing behind fMP4 conversion."""

import asyncio
import random

import pytest

from quadlink.relay.convert import remux
from quadlink.relay.mp4info import fragment_video_pts, init_info
from quadlink.relay.tsinfo import first_video_pts


@pytest.mark.parametrize("rate", [44100, 48000])
def test_init_info_finds_video_and_audio_rate(fmp4_run, rate):
    init, _ = fmp4_run(rate, 1)
    info = init_info(init)
    assert info.video is not None and info.video.timescale > 0
    assert info.audio_rate == rate


def test_init_info_without_moov():
    assert init_info(b"") == init_info(b"\x00\x00\x00\x08free")
    assert init_info(b"").video is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    [{}, {"chunked": True}, {"offset": 95440}],
    ids=["edit-list", "chunked", "pts-wrap"],
)
async def test_fragment_key_is_the_pts_ffmpeg_outputs(ffmpeg, fmp4_run, shape):
    init, segs = fmp4_run(44100, 4, **shape)
    video = init_info(init).video
    assert video is not None
    keys = [fragment_video_pts(seg, video) for seg in segs]
    outs = await asyncio.gather(*(remux(ffmpeg, init, seg, 10) for seg in segs))
    assert keys == [first_video_pts(out or b"") for out in outs]
    assert len(set(keys)) == len(keys)


def test_fragment_without_video_has_no_key(fmp4_run):
    init, _ = fmp4_run(44100, 1)
    video = init_info(init).video
    assert video is not None
    assert fragment_video_pts(b"not an fMP4 fragment", video) is None


def test_corrupt_boxes_never_raise(fmp4_run):
    init, segs = fmp4_run(44100, 1)
    video = init_info(init).video
    assert video is not None
    rng = random.Random(7)
    for _ in range(300):
        bad_init, bad_seg = bytearray(init), bytearray(segs[0][:4096])
        for buf in (bad_init, bad_seg):
            for _ in range(4):
                buf[rng.randrange(len(buf))] = rng.randrange(256)
        init_info(bytes(bad_init))
        fragment_video_pts(bytes(bad_seg), video)
