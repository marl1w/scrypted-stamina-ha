"""Where an event lands in a destination (architecture §3.5).

    <folder>/pending/<area>/<YYMMDD_HHMMSS>_<camera>_<id>/init.mp4, 00000.m4s, …
    <folder>/events/<area>-<camera>-<YYMMDD_HHMMSS>.mp4   (and -part2.mp4, … if it grew)
    <folder>/unusual-unconfirmed/<area>-<camera>-<YYMMDD_HHMMSS>.mp4

Segments stream into the pending folder while the event lasts; a committed event is one
video file, the pending folder's files joined in play order.

Ported from Reolink Stamina's `cloud/naming.py`: `safe_part` keeps every segment legal on
every provider (Windows-forbidden characters, trailing dots), and the configured folder is
sanitised segment by segment so neither a slash in a name nor a `..` typed into the form can
write outside where the user pointed. New here: the area and camera are slugified, because
they are folder names a person types into a file browser, and the timestamp leads so a plain
listing is chronological.
"""

from __future__ import annotations

import datetime as dt
import re

from homeassistant.util import slugify

from .const import DEFAULT_FOLDER, NO_AREA

# Forbidden by OneDrive, SharePoint, and Windows itself. Note the colon: the obvious
# HH:MM:SS in a file name cannot survive an upload.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# A name that ends in a dot or a space is silently mangled by Windows.
_TRIM = " ."


def safe_part(text: str, *, fallback: str = "unknown") -> str:
    """Make one path segment safe for every destination we support."""
    cleaned = _FORBIDDEN.sub("-", text).strip(_TRIM)
    # Collapse the runs of dashes that replacing several forbidden characters can leave.
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-" + _TRIM)
    # A segment of nothing but punctuation is legal and useless: "::" would survive as "-".
    if not any(character.isalnum() for character in cleaned):
        return fallback
    return cleaned


def area_slug(area: str | None) -> str:
    """Return the folder for a camera's area: its slug, or `no-area`."""
    if not area or not any(character.isalnum() for character in area):
        return NO_AREA
    return slugify(area, separator="-") or NO_AREA


def camera_slug(name: str | None, fallback: str = "camera") -> str:
    """Return a camera's device name as it appears in a folder name."""
    if not name or not any(character.isalnum() for character in name):
        return safe_part(fallback, fallback="camera")
    return slugify(name, separator="-") or safe_part(fallback, fallback="camera")


def camera_in_area(area: str | None, camera: str | None) -> str:
    """Return the camera's slug without its area's, when it repeats it.

    Area `area-2` and camera "Area 2 Gate" give `gate`, so a name reads
    `area-2-gate-…`, not `area-2-area-2-gate-…`. A camera named just like its area keeps
    its whole slug.
    """
    slug = camera_slug(camera)
    if area and area != NO_AREA and slug.startswith(f"{area}-"):
        return slug.removeprefix(f"{area}-")
    return slug


def event_folder_name(
    started: dt.datetime, camera: str, event_id: str, area: str | None = None
) -> str:
    """Name one event's folder after its start (local time), camera and id."""
    stamp = started.strftime("%y%m%d_%H%M%S")
    camera_part = camera_in_area(area, camera)
    return f"{stamp}_{camera_part}_{safe_part(event_id, fallback='event')}"


def event_file_name(started: dt.datetime, area: str, camera: str) -> str:
    """Name a committed event's video: `<area>-<camera>-<YYMMDD_HHMMSS>.mp4`.

    Computed once, when the event begins (local time), next to its folder name.
    """
    stamp = started.strftime("%y%m%d_%H%M%S")
    area = safe_part(area, fallback=NO_AREA)
    return f"{area}-{camera_in_area(area, camera)}-{stamp}.mp4"


def video_part_path(video: str, part: int) -> str:
    """Return the path of one file of a committed video: part 1 is the video itself.

    What an event receives after its commit (an orphan that kept going) is a file of its
    own, `<video>-part<n>.mp4`, so a committed file is never rewritten.
    """
    return video if part <= 1 else f"{video.removesuffix('.mp4')}-part{part}.mp4"


def root_folder(folder: str) -> str:
    """Return the configured folder as a clean relative path."""
    parts = [safe_part(part) for part in folder.split("/") if part.strip(_TRIM)]
    return "/".join(parts or [DEFAULT_FOLDER])


def event_path(folder: str, kind: str, area: str, name: str) -> str:
    """Return the path of an event folder of one kind (`pending`, `events`, …)."""
    return "/".join([root_folder(folder), kind, safe_part(area, fallback=NO_AREA), name])


def event_file_path(folder: str, kind: str, name: str) -> str:
    """Return the path of a committed event's video (`events`, `unusual-unconfirmed`)."""
    return "/".join([root_folder(folder), kind, safe_part(name, fallback="event.mp4")])


def segment_name(number: int) -> str:
    """Return a segment's file name: five digits, so a listing sorts in play order."""
    return f"{number:05d}.m4s"
