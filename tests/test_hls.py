"""The HLS playlist builder: VOD fMP4 in order, with gaps as discontinuities."""

from custom_components.scrypted_stamina.hls import playlist


def test_playlist_is_vod_fmp4_in_order() -> None:
    text = playlist([2, 0, 1], durations={0: 2.05, 1: 2.0, 2: 1.4})
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U"
    assert '#EXT-X-MAP:URI="init.mp4"' in lines
    assert "#EXT-X-TARGETDURATION:3" in lines
    assert [line for line in lines if line.endswith(".m4s")] == [
        "00000.m4s",
        "00001.m4s",
        "00002.m4s",
    ]
    assert "#EXTINF:2.050," in lines
    assert lines[-1] == "#EXT-X-ENDLIST"
    assert "#EXT-X-DISCONTINUITY" not in text


def test_gaps_become_discontinuities() -> None:
    lines = playlist([0, 1, 4]).splitlines()
    at = lines.index("00004.m4s")
    assert lines[at - 2] == "#EXT-X-DISCONTINUITY"
