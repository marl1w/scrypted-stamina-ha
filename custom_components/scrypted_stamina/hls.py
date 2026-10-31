"""The `index.m3u8` written into a committed event folder.

HLS with fMP4 segments (RFC 8216 §4.3.2.5 `EXT-X-MAP`): the folder plays as it stands, in
Safari, VLC, hls.js and Home Assistant's own player, with no server beyond a file listing.

Only segments the destination actually holds are listed. One that never arrived (expired
in the plugin, refused by the destination) is left out and the next one is preceded by
`EXT-X-DISCONTINUITY`, so a player jumps the gap instead of stalling on a 404.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math

from .naming import segment_name

# The plugin cuts 2 s segments (architecture §2.3). Used when it did not report a duration.
DEFAULT_SEGMENT_SECONDS = 2.0


def playlist(
    segments: Iterable[int],
    *,
    durations: Mapping[int, float] | None = None,
    discontinuities: Iterable[int] = (),
    init_of: Mapping[int, int] | None = None,
) -> str:
    """Return a VOD playlist over the given segment numbers, in order.

    `discontinuities` are segments the plugin marked as starting afresh (its ffmpeg
    restarted mid-event); they get a discontinuity tag like a gap does. `init_of` names the
    init a segment needs where it is not `init.mp4` (`init-<k>.mp4`), and a new
    `EXT-X-MAP` goes before the first segment that needs a different one.
    """
    init_of = init_of or {}
    breaks = set(discontinuities)
    durations = durations or {}
    numbers = sorted(set(segments))
    lengths = {n: float(durations.get(n) or DEFAULT_SEGMENT_SECONDS) for n in numbers}
    target = max([math.ceil(value) for value in lengths.values()] or [2])

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    previous: int | None = None
    current_init: int | None = None
    for number in numbers:
        if previous is not None and (number != previous + 1 or number in breaks):
            lines.append("#EXT-X-DISCONTINUITY")
        needed = init_of.get(number, 0)
        if needed != current_init:
            name = "init.mp4" if needed == 0 else f"init-{needed}.mp4"
            lines.append(f'#EXT-X-MAP:URI="{name}"')
            current_init = needed
        lines.append(f"#EXTINF:{lengths[number]:.3f},")
        lines.append(segment_name(number))
        previous = number
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
