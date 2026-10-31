"""The playlist a committed event's video plays through.

HLS with fMP4 segments (RFC 8216 §4.3.2.5 `EXT-X-MAP`), every init and segment a byte range
(`EXT-X-BYTERANGE`, §4.3.2.2) of the video's files (one, or more once it grew), so a player
starts on the first segment instead of downloading the whole event, in Safari, VLC, hls.js
and Home Assistant's player.

Only segments the destination actually holds are listed. One that never arrived (expired
in the plugin, refused by the destination) is left out and the next one is preceded by
`EXT-X-DISCONTINUITY`, so a player jumps the gap.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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
    ranges: Mapping[str, Sequence[int | str]] | None = None,
    uri: str | None = None,
) -> str:
    """Return a VOD playlist over the given segment numbers, in order.

    `discontinuities` are segments the plugin marked as starting afresh (its ffmpeg
    restarted mid-event); they get a discontinuity tag like a gap does. `init_of` names the
    init a segment needs where it is not `init.mp4` (`init-<k>.mp4`), and a new
    `EXT-X-MAP` goes before the first segment that needs a different one.

    With `ranges` (file name to `[offset, length]`, or `[offset, length, uri]` for a video
    of several files) and `uri`, every file is that range of `uri` rather than a file of
    its own.
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
            if ranges is not None and uri is not None:
                offset, length, *at = ranges[name]
                lines.append(
                    f'#EXT-X-MAP:URI="{at[0] if at else uri}",BYTERANGE="{length}@{offset}"'
                )
            else:
                lines.append(f'#EXT-X-MAP:URI="{name}"')
            current_init = needed
        lines.append(f"#EXTINF:{lengths[number]:.3f},")
        if ranges is not None and uri is not None:
            offset, length, *at = ranges[segment_name(number)]
            lines.append(f"#EXT-X-BYTERANGE:{length}@{offset}")
            lines.append(str(at[0] if at else uri))
        else:
            lines.append(segment_name(number))
        previous = number
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
