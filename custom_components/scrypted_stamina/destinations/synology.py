"""Synology DSM, through the File Station client its own integration already holds.

The closest thing to "record a copy to the NAS" for the boxes most people own: the Synology
integration is already there for its sensors, and its session is reused here rather than a
second login being asked for.

File Station addresses everything from a shared folder down, so the first segment of the
configured folder is the share — `home/reolink` writes to `/home/reolink`, and a share that
does not exist is an error the NAS reports rather than something this creates.

Nothing here imports `synology_dsm` at module level, so the destinations package stays
importable for the great majority of users who do not have a Synology.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant

from .base import (
    Destination,
    DestinationError,
    TransientError,
    async_attempt,
)

_LOGGER = logging.getLogger(__name__)

# File Station pages its listings and defaults to a hundred entries. A folder of segments can
# be far longer than that, and a page it did not ask for is a file the quota forgets.
PAGE = 500

_DELETE = "SYNO.FileStation.Delete"

# How much of a download is read at a time, while streaming up to a range.
_READ_CHUNK = 256 * 1024

# What DSM answers when the thing being deleted is not there: a File Station error (900)
# whose detail is "no such file or directory" (408).
_NO_SUCH_FILE = 900
_NO_SUCH_FILE_DETAIL = 408


class _NoSessionError(DestinationError):
    """The Synology integration exposes no DSM session for the APIs it does not wrap."""


def _is_missing(err: Exception) -> bool:
    """Return whether a DSM error means the thing asked for was not there.

    Two shapes, because the two APIs report it differently. Listing a folder that does not
    exist answers 408 outright; deleting a file that does not exist answers the batch
    failure 900, with the 408 one level down among the errors it collected.
    """
    args = err.args[0] if err.args else None
    if not isinstance(args, dict):
        return False
    code = int(args.get("code", 0))
    if code == _NO_SUCH_FILE_DETAIL:
        return True
    if code != _NO_SUCH_FILE:
        return False
    details = args.get("details")
    if not isinstance(details, list) or not details or not isinstance(details[0], dict):
        return False
    return int(details[0].get("code", 0)) == _NO_SUCH_FILE_DETAIL


class SynologyDestination(Destination):
    """Event folders in a File Station shared folder.

    The library the Synology integration uses wraps list, upload, download and a
    non-recursive delete. A recursive delete is a File Station API it does not wrap
    (`SYNO.FileStation.Delete` with `recursive`), so that goes through the same logged-in
    session's generic request; a NAS that does not answer it is emptied file by file.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Bind to a loaded Synology DSM config entry."""
        self._hass = hass
        self._entry = entry

    @property
    def label(self) -> str:
        """Return the NAS this uploads to."""
        return f"Synology ({self._entry.title})"

    @property
    def _file_station(self) -> Any:
        """Return the File Station client the Synology integration keeps on its entry.

        File Station is a package the user may not have installed, and the integration
        leaves the attribute unset when it is missing — which is worth saying plainly here,
        because the alternative is an uploader that fails once per file for a reason no log
        line explains.
        """
        if self._entry.state is not ConfigEntryState.LOADED:
            raise DestinationError(f"{self._entry.title} is not loaded")
        data = getattr(self._entry, "runtime_data", None)
        station = getattr(getattr(data, "api", None), "file_station", None)
        if station is None:
            raise DestinationError(
                f"{self._entry.title} has no File Station; install and enable it in DSM"
            )
        return station

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        """Split a path into the File Station folder it lives in and its name."""
        folder, _, name = path.rpartition("/")
        if not folder or not name:
            raise DestinationError(f"{path} is not a path inside a shared folder")
        return f"/{folder.strip('/')}", name

    async def _async_call[T](self, what: str, call: Awaitable[T]) -> T:
        """Await one File Station call, deciding whether its failure is worth a retry.

        The library raises one exception type for everything the API can report, so a
        connection that dropped and a share that does not exist arrive the same way. Both
        are retried; the second simply fails four times before it is reported, which costs
        a few seconds of a misconfiguration that a person has to fix anyway.
        """
        try:
            return await call
        except DestinationError:
            raise
        except Exception as err:
            raise TransientError(f"{type(err).__name__} during {what}: {err}") from err

    async def async_store(self, path: str, data: bytes) -> None:
        """Upload one file, and confirm from DSM's own answer that it landed."""
        folder, name = self._split(path)

        async def attempt() -> None:
            stored = await self._async_call(
                f"upload of {path}",
                self._file_station.upload_file(
                    path=folder, filename=name, source=data, create_parents=True
                ),
            )
            if not stored:
                raise DestinationError(f"Synology did not confirm storing {path}")

        await async_attempt(f"Synology upload of {path}", attempt)
        _LOGGER.debug("Uploaded %s (%s bytes) to %s", path, len(data), self.label)

    async def async_store_stream(self, path: str, size: int, chunks: AsyncIterable[bytes]) -> None:
        """Upload one file from a stream (File Station takes an async iterator as source).

        One attempt: a stream cannot be replayed, so the commit retries the whole file; the
        size is confirmed after (`async_store_video`).
        """
        folder, name = self._split(path)

        async def attempt() -> None:
            stored = await self._async_call(
                f"upload of {path}",
                self._file_station.upload_file(
                    path=folder, filename=name, source=chunks, create_parents=True
                ),
            )
            if not stored:
                raise DestinationError(f"Synology did not confirm storing {path}")

        await async_attempt(f"Synology upload of {path}", attempt, attempts=1)
        _LOGGER.debug("Uploaded %s (%s bytes) to %s, streamed", path, size, self.label)

    async def async_delete(self, path: str) -> None:
        """Remove one file, treating an already-missing file as success."""
        folder, name = self._split(path)

        async def attempt() -> None:
            # Resolved before the try: "File Station is not installed" is a settled fact
            # about the NAS, and retrying it four times only delays saying so.
            station = self._file_station
            try:
                await station.delete_file(path=folder, filename=name)
            except Exception as err:
                if _is_missing(err):
                    return
                raise TransientError(f"{type(err).__name__}: {err}") from err

        await async_attempt(f"Synology delete of {path}", attempt)

    async def async_list(self, folder: str) -> dict[str, int]:
        """Return what the destination holds under one folder."""
        remote = f"/{folder.strip('/')}"

        async def attempt() -> dict[str, int]:
            station = self._file_station
            found: dict[str, int] = {}
            offset = 0
            while True:
                try:
                    files = await station.get_files(path=remote, offset=offset, limit=PAGE)
                except Exception as err:
                    if _is_missing(err):
                        return found  # the folder does not exist yet, which is not an error
                    raise TransientError(f"{type(err).__name__}: {err}") from err
                if not files:
                    return found
                for item in files:
                    if item.is_dir:
                        continue
                    size = getattr(item.additional, "size", None)
                    found[f"{folder}/{item.name}"] = int(size or 0)
                if len(files) < PAGE:
                    return found
                offset += PAGE

        return await async_attempt(f"Synology listing of {folder}", attempt)

    @property
    def _dsm(self) -> Any:
        """Return the logged-in DSM session, for the File Station APIs not wrapped."""
        self._file_station  # noqa: B018 - raises the clear error when File Station is missing
        dsm = getattr(getattr(self._entry.runtime_data, "api", None), "dsm", None)
        if dsm is None:
            raise _NoSessionError(f"{self._entry.title} exposes no DSM session")
        return dsm

    async def async_read(self, path: str) -> bytes:
        """Return one file's content."""
        folder, name = self._split(path)

        async def attempt() -> bytes:
            reader = await self._async_call(
                f"download of {path}",
                self._file_station.download_file(path=folder, filename=name),
            )
            if reader is None or reader is True:
                raise DestinationError(f"Synology returned nothing for {path}")
            return bytes(await self._async_call(f"read of {path}", reader.read()))

        return await async_attempt(f"Synology read of {path}", attempt)

    async def async_read_range(self, path: str, start: int, end: int) -> bytes:
        """Return bytes `start..end` (inclusive) of one file, streaming it only that far.

        File Station's download answers 200 or fails, so a `Range` request is not an option:
        the download is read from the start, and let go of as soon as `end` has arrived.
        """
        folder, name = self._split(path)

        async def attempt() -> bytes:
            reader = await self._async_call(
                f"download of {path}",
                self._file_station.download_file(path=folder, filename=name),
            )
            if reader is None or reader is True:
                raise DestinationError(f"Synology returned nothing for {path}")
            data = bytearray()
            offset = 0
            while offset <= end:
                chunk = await self._async_call(f"read of {path}", reader.read(_READ_CHUNK))
                if not chunk:
                    break
                view = memoryview(chunk)
                if offset + len(view) > start:
                    data += view[max(0, start - offset) : end + 1 - offset]
                offset += len(view)
            return bytes(data)

        return await async_attempt(f"Synology read of {path}", attempt)

    async def async_delete_folder(self, folder: str) -> None:
        """Remove a folder with one recursive File Station delete."""
        try:
            await self._dsm.get(
                _DELETE, "delete", {"path": f"/{folder.strip('/')}", "recursive": "true"}
            )
        except _NoSessionError:
            await super().async_delete_folder(folder)
        except Exception as err:
            if _is_missing(err):
                return
            if "NotExists" in type(err).__name__:
                await super().async_delete_folder(folder)
                return
            raise DestinationError(f"Synology could not delete {folder}: {err}") from err
