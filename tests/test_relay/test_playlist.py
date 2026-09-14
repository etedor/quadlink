"""Tests for the HLS media-playlist parse + republish core."""

import math

from quadlink.relay.playlist import Segment, SlotState, ingest, parse_media_playlist, render

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


def _src(media_seq: int, uris: list[str]) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6", f"#EXT-X-MEDIA-SEQUENCE:{media_seq}"]
    for uri in uris:
        lines.append("#EXTINF:2.000,live")
        lines.append(uri)
    return "\n".join(lines) + "\n"


def test_ingest_first_fetch_assigns_monotonic_seq():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "chan-A")
    assert [s.seq for s in state.segments] == [0, 1]
    assert all(s.discontinuity is False for s in state.segments)


def test_ingest_dedupes_on_refetch():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "chan-A")
    ingest(state, _src(100, ["a.ts", "b.ts"]), "chan-A")
    assert [s.uri for s in state.segments] == ["a.ts", "b.ts"]
    assert [s.seq for s in state.segments] == [0, 1]


def test_ingest_appends_new_segments():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "chan-A")
    ingest(state, _src(101, ["b.ts", "c.ts"]), "chan-A")
    assert [s.uri for s in state.segments] == ["a.ts", "b.ts", "c.ts"]
    assert [s.seq for s in state.segments] == [0, 1, 2]


def test_ingest_same_identity_url_refresh_does_not_splice():
    # a Twitch token/edge refresh re-mints the URL but the channel is unchanged;
    # the media-sequence keeps climbing, so nothing should splice a discontinuity
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "chan-A")
    ingest(state, _src(102, ["c.ts", "d.ts"]), "chan-A")  # same channel, fresh fetch
    assert [s.uri for s in state.segments] == ["a.ts", "b.ts", "c.ts", "d.ts"]
    assert all(s.discontinuity is False for s in state.segments)


def test_ingest_marks_discontinuity_on_identity_change():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "chan-A")
    ingest(state, _src(500, ["x.ts", "y.ts"]), "chan-B")
    x = next(s for s in state.segments if s.uri == "x.ts")
    y = next(s for s in state.segments if s.uri == "y.ts")
    assert x.discontinuity is True
    assert y.discontinuity is False


def test_ingest_identity_change_resets_watermark_even_if_seq_lower():
    # a new channel can have a LOWER media-sequence than the old one; resetting
    # the watermark on identity change ensures its segments still get appended
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "chan-A")  # watermark climbs to 101
    ingest(state, _src(50, ["p.ts", "q.ts"]), "chan-B")  # lower seq, different channel
    assert [s.uri for s in state.segments] == ["a.ts", "b.ts", "p.ts", "q.ts"]
    p = next(s for s in state.segments if s.uri == "p.ts")
    assert p.discontinuity is True


def test_ingest_trims_to_window_and_counts_discontinuity_sequence():
    state = SlotState(window=3)
    ingest(state, _src(0, ["a.ts", "b.ts"]), "chan-A")
    # switch channel -> c.ts carries a discontinuity, then push past the window
    ingest(state, _src(0, ["c.ts", "d.ts", "e.ts"]), "chan-B")
    assert len(state.segments) == 3
    # a.ts and b.ts evicted; the discontinuity on c.ts is still in-window
    assert [s.uri for s in state.segments] == ["c.ts", "d.ts", "e.ts"]
    assert state.discontinuity_seq == 0
    # push more so c.ts (the discontinuity segment) rolls out (same channel now)
    ingest(state, _src(3, ["f.ts", "g.ts", "h.ts"]), "chan-B")
    assert state.discontinuity_seq == 1


def test_render_has_required_headers_and_no_endlist():
    state = SlotState()
    ingest(state, _src(100, ["a.ts", "b.ts"]), "chan-A")
    out = render(state)
    assert out.startswith("#EXTM3U")
    assert "#EXT-X-VERSION:6" in out
    assert "#EXT-X-MEDIA-SEQUENCE:0" in out
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:0" in out
    assert "#EXT-X-TARGETDURATION:2" in out
    assert "#EXTINF:2.000," in out
    assert "a.ts" in out and "b.ts" in out
    assert "#EXT-X-ENDLIST" not in out


def test_render_emits_discontinuity_tag_before_segment():
    state = SlotState()
    ingest(state, _src(100, ["a.ts"]), "chan-A")
    ingest(state, _src(0, ["x.ts"]), "chan-B")
    out = render(state)
    lines = out.splitlines()
    x_index = lines.index("x.ts")
    # the line two before the URI (EXTINF is directly before) should include the tag
    assert "#EXT-X-DISCONTINUITY" in lines[x_index - 2 : x_index]
