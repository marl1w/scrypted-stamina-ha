"""Where an event lands in a destination (architecture §3.5).

    <folder>/pending/<area>/<YYMMDD_HHMMSS>_<camera>_<id>/init.mp4, 00000.m4s, …
    <folder>/events/<area>-<camera>-<YYMMDD_HHMMSS>.mp4
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


def event_folder_name(started: dt.datetime, camera: str, event_id: str) -> str:
    """Name one event's folder after its start (local time), camera and id."""
    stamp = started.strftime("%y%m%d_%H%M%S")
    return f"{stamp}_{camera_slug(camera)}_{safe_part(event_id, fallback='event')}"


_FOLDER_NAME = re.compile(r"^(\d{6}_\d{6})_(.+)$")


def event_file_name(area: str, folder: str, event_id: str) -> str:
    """Name a committed event's video: `<area>-<camera>-<YYMMDD_HHMMSS>.mp4`.

    The camera and the start are read back from the event's folder name, which fixed them
    (in local time) when the event began.
    """
    match = _FOLDER_NAME.match(folder)
    if match is None:
        return f"{safe_part(area, fallback=NO_AREA)}-{safe_part(folder, fallback='event')}.mp4"
    stamp, rest = match.groups()
    suffix = f"_{safe_part(event_id, fallback='event')}"
    camera = rest[: -len(suffix)] if rest.endswith(suffix) and rest != suffix else rest
    return f"{safe_part(area, fallback=NO_AREA)}-{safe_part(camera, fallback='camera')}-{stamp}.mp4"


def root_folder(folder: str) -> str:
    """Return the configured folder as a clean relative path."""
    parts = [safe_part(part) for part in folder.split("/") if part.strip(_TRIM)]
    return "/".join(parts or [DEFAULT_FOLDER])


def event_path(folder: str, kind: str, area: str, name: str) -> str:
    """Return the path of an event folder of one kind (`pending`, `events`, …)."""
    return "/".join([root_folder(folder), kind, safe_part(area, fallback=NO_AREA), name])


def event_file_path(folder: str, kind: str, area: str, name: str, event_id: str) -> str:
    """Return the path of a committed event's video (`events`, `unusual-unconfirmed`)."""
    return "/".join([root_folder(folder), kind, event_file_name(area, name, event_id)])


def segment_name(number: int) -> str:
    """Return a segment's file name: five digits, so a listing sorts in play order."""
    return f"{number:05d}.m4s"
