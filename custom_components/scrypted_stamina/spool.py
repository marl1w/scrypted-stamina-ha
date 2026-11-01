"""A local copy of each event's segments, so a commit never reads them back.

    <config>/.cache/scrypted_stamina_spool/<event id>/init.mp4, 00000.m4s, …

Under `.cache`, because Home Assistant leaves `.cache/*` out of its backups (both the
Supervisor's and Core's own): footage of the last hours has no place in a backup. A spool
from before, at `<config>/scrypted_stamina_spool`, is moved here at startup.

Every segment fetched from the plugin is written here as it goes to the destinations, and a
commit streams the video from these files: no download from the destination, and never more
than a chunk in memory. An event's folder goes once it is committed everywhere, or dropped.

It is a cache, not the record: a segment missing here (the box was replaced, the spool was
trimmed) is read back from the destination instead. So it is bounded — by size (512 MB),
oldest event first, and by age (two days) — and cleared at startup of every event the index
no longer needs. A trim that cannot get under the size (one long event) is not repeated at
every segment: the next waits for another quarter of the bound to be written.

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

SPOOL_DIR = ".cache/scrypted_stamina_spool"
# Where the spool was before it moved under `.cache`, out of backups.
OLD_SPOOL_DIR = "scrypted_stamina_spool"
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
        self._old_root = Path(hass.config.path(OLD_SPOOL_DIR))
        self.max_bytes = max_bytes
        self.max_age = max_age
        # Bytes written since the size was last measured, plus that measure; and the size at
        # which the next trim runs (raised after a trim that could not get under the bound).
        self._bytes = 0
        self._trim_at: int | None = None

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
        if self._bytes > (self._trim_at or self.max_bytes):
            self._measured(await self._hass.async_add_executor_job(self._trim, None))

    def _measured(self, total: int) -> None:
        """Take a trim's result: what is left, and when the next trim is worth running."""
        self._bytes = total
        self._trim_at = None if total <= self.max_bytes * _TRIM_TO else total + self.max_bytes // 4

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
        self._measured(await self._hass.async_add_executor_job(self._trim, keep))

    def _migrate(self) -> None:
        """Move a spool from its old place, out of backups; remove it if it cannot move."""
        try:
            if self.root.exists():
                raise OSError(f"{self.root} exists already")
            self.root.parent.mkdir(parents=True, exist_ok=True)
            self._old_root.rename(self.root)
            _LOGGER.debug("Spool moved to %s", self.root)
        except OSError as err:
            _LOGGER.debug("Spool not moved (%s); removing %s", err, self._old_root)
            shutil.rmtree(self._old_root, ignore_errors=True)

    def _trim(self, keep: set[str] | None) -> int:
        """Apply the bounds; with `keep`, remove every event not in it. Return what is left.

        Without `keep` (after a write) it bounds size and age only; a clean passes the events
        the index still needs, and the rest go.
        """
        if self._old_root.is_dir():
            self._migrate()
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
