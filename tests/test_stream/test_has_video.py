"""Tests for spotting audio-only renditions from multivariant metadata."""

from types import SimpleNamespace

import pytest

from quadlink.stream.fetcher import _has_video


def _stream(resolution, codecs, uri="https://cdn/v.m3u8", url="https://cdn/v.m3u8"):
    info = SimpleNamespace(resolution=resolution, codecs=codecs)
    playlist = SimpleNamespace(uri=uri, stream_info=info)
    return SimpleNamespace(url=url, multivariant=SimpleNamespace(playlists=[playlist]))


@pytest.mark.parametrize(
    "resolution,codecs,expected",
    [
        (None, ["mp4a.40.2"], False),
        ((1920, 1080), ["avc1.640028", "mp4a.40.2"], True),
        (None, ["hvc1.2.4.L123", "mp4a.40.2"], True),
        (None, [], True),
    ],
)
def test_has_video(resolution, codecs, expected):
    assert _has_video(_stream(resolution, codecs)) is expected


def test_rendition_missing_from_multivariant_counts_as_video():
    assert _has_video(_stream(None, ["mp4a.40.2"], uri="https://cdn/other.m3u8"))


def test_stream_without_multivariant_counts_as_video():
    assert _has_video(SimpleNamespace(url="https://cdn/v.m3u8"))
