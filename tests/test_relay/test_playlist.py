"""Tests for the HLS media-playlist parse + republish core."""

from quadlink.relay.playlist import parse_media_playlist

SAMPLE = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DATERANGE:ID="x",CLASS="timestamp",START-DATE="2026-08-14T15:02:00.893Z"
#EXT-X-PROGRAM-DATE-TIME:2026-08-14T15:14:32.243Z
#EXTINF:2.000,live
https://cdn.example/seg100.ts?dna=aaa
#EXT-X-PROGRAM-DATE-TIME:2026-08-14T15:14:34.243Z
#EXTINF:2.000,live
https://cdn.example/seg101.ts?dna=bbb
"""


def test_parse_target_duration_and_media_sequence():
    td, seq, segs = parse_media_playlist(SAMPLE)
    assert td == 6
    assert seq == 100
    assert len(segs) == 2


def test_parse_segment_fields():
    _, _, segs = parse_media_playlist(SAMPLE)
    duration, uri, pdt, disc = segs[0]
    assert duration == 2.0
    assert uri == "https://cdn.example/seg100.ts?dna=aaa"
    assert pdt == "2026-08-14T15:14:32.243Z"
    assert disc is False


def test_parse_ignores_unknown_tags():
    # the EXT-X-DATERANGE line must not become a segment
    _, _, segs = parse_media_playlist(SAMPLE)
    assert all(uri.startswith("https://") for _, uri, _, _ in segs)


def test_parse_source_discontinuity_flag():
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:5\n"
        "#EXTINF:2.000,live\nhttps://cdn.example/a.ts\n"
        "#EXT-X-DISCONTINUITY\n#EXTINF:2.000,live\nhttps://cdn.example/b.ts\n"
    )
    _, _, segs = parse_media_playlist(text)
    assert segs[0][3] is False
    assert segs[1][3] is True


def test_parse_discontinuity_sequence_tag_not_treated_as_discontinuity():
    text = (
        "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:5\n"
        "#EXT-X-DISCONTINUITY-SEQUENCE:2\n"
        "#EXTINF:2.000,live\nhttps://cdn.example/a.ts\n"
    )
    _, _, segs = parse_media_playlist(text)
    assert segs[0][3] is False
