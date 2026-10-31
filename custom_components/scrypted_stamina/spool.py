"""A local copy of each event's segments, so a commit never reads them back.

    <config>/scrypted_stamina_spool/<event id>/init.mp4, 00000.m4s, …

Every segment fetched from the plugin is written here as it goes to the destinations, and a
commit streams the video from these files: no download from the destination, and never more
than a chunk in memory. An event's folder goes once it is committed everywhere, or dropped.

It is a cache, not the record: a segment missing here (the box was replaced, the spool was
trimmed) is read back from the destination instead. So it is bounded — by size, oldest
event first, and by age — and cleared at startup of every event the index no longer needs.

All file work runs in the executor; nothing here blocks the event loop.
"""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
import time

from homeassistant.core import HomeAssistant

from .naming import safe_part

_LOGGER = logging.getLogger(__name__)

SPOOL_DIR = "scrypted_stamina_spool"
# About seven hours of footage at 70 MB an hour; events are minutes long.
SPOOL_MAX_BYTES = 512 * 1024 * 1024
# An event folder untouched this long belongs to something that will not commit.
SPOOL_MAX_AGE = 2 * 86400
# Trimmed to this share of the maximum, so it is not trimmed again at the next segment.
_TRIM_TO = 0.75


class Spool:
    """Segments on the box's own disk, one folder per event."""

    def __init__(
        self, hass: HomeAssistant, max_bytes: int = SPOOL_MAX_BYTES, max_age: float = SPOOL_MAX_AGE
    ) -> None:
        """Place the spool in Home Assistant's config directory; nothing is touched yet."""
        self._hass = hass
        self.root = Path(hass.config.path(SPOOL_DIR))
        self.max_bytes = max_bytes
        self.max_age = max_age
        # Bytes written since the size was last measured, plus that measure.
        self._bytes = 0

    def folder(self, event_id: str) -> Path:
        """Return one event's folder."""
        return self.root / safe_part(event_id, fallback="event")

    async def async_write(self, event_id: str, name: str, data: bytes) -> None:
        """Keep one segment. A failure (a full disk) is logged, never raised."""
        try:
            await self._hass.async_add_executor_job(self._write, event_id, name, data)
        except OSError as err:
            _LOGGER.debug("Could not spool %s of %s: %s", name, event_id, err)
            return
        self._bytes += len(data)
        if self._bytes > self.max_bytes:
            self._bytes = await self._hass.async_add_executor_job(self._trim, None)

    def _write(self, event_id: str, name: str, data: bytes) -> None:
        folder = self.folder(event_id)
        folder.mkdir(parents=True, exist_ok=True)
        # Written aside and renamed, so a crash never leaves a short file under the name.
        partial = folder / f".{name}.part"
        partial.write_bytes(data)
        partial.replace(folder / name)

    async def async_sizes(self, event_id: str) -> dict[str, int]:
        """Return the segments held for one event, with their sizes."""
        return await self._hass.async_add_executor_job(self._sizes, event_id)

    def _sizes(self, event_id: str) -> dict[str, int]:
        folder = self.folder(event_id)
        if not folder.is_dir():
            return {}
        return {
            entry.name: entry.stat().st_size
            for entry in folder.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        }

    async def async_read(self, event_id: str, name: str) -> bytes:
        """Return one spooled segment (OSError when it is gone)."""
        return await self._hass.async_add_executor_job((self.folder(event_id) / name).read_bytes)

    async def async_discard(self, event_id: str) -> None:
        """Remove one event's folder."""
        await self._hass.async_add_executor_job(
            lambda: shutil.rmtree(self.folder(event_id), ignore_errors=True)
        )

    async def async_clean(self, keep: set[str] | None = None) -> None:
        """Apply the size and age bounds and, given `keep`, remove every other event."""
        self._bytes = await self._hass.async_add_executor_job(self._trim, keep)

    def _trim(self, keep: set[str] | None) -> int:
        """Apply the bounds; with `keep`, remove every event not in it. Return what is left.

        Without `keep` (after a write) it bounds size and age only; a clean passes the events
        the index still needs, and the rest go.
        """
        if not self.root.is_dir():
            return 0
        known = {self.folder(event_id).name for event_id in keep or ()}
        now = time.time()
        folders: list[tuple[float, int, Path]] = []
        for folder in self.root.iterdir():
            if not folder.is_dir():
                continue
            stats = [entry.stat() for entry in folder.iterdir() if entry.is_file()]
            touched = max((stat.st_mtime for stat in stats), default=folder.stat().st_mtime)
            if (keep is not None and folder.name not in known) or now - touched > self.max_age:
                shutil.rmtree(folder, ignore_errors=True)
                continue
            folders.append((touched, sum(stat.st_size for stat in stats), folder))
        total = sum(size for _, size, _ in folders)
        # Oldest first, and never the event written last: it is the one most likely to commit.
        for _, size, folder in sorted(folders)[:-1]:
            if total <= self.max_bytes * _TRIM_TO:
                break
            _LOGGER.debug("Spool over its bound: removing %s", folder.name)
            shutil.rmtree(folder, ignore_errors=True)
            total -= size
        return total
