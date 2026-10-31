"""Google Drive, through the OAuth session its own integration already holds.

Drive has no paths — a file knows only its parents — so the folder the user configured is
walked segment by segment and created where it is missing, and the resulting ids are cached
for the life of the uploader. Everything above this module still speaks in paths.

The integration's scope is `drive.file`, which grants access to what this OAuth client
created and nothing else. That is the whole of what an uploader needs, and it means Stamina
cannot see, list or delete a single file the user put in their own Drive.
"""

from __future__ import annotations

from collections.abc import AsyncIterable
import json
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .base import STREAM_CHUNK, DestinationError, content_type, gather, rechunk, streamed
from .oauth import OAuthDestination

_LOGGER = logging.getLogger(__name__)

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"

FOLDER_MIME = "application/vnd.google-apps.folder"

# A simple upload is documented up to 5 MB. Segments are a few dozen kilobytes; a committed
# video (about 70 MB an hour) past this goes up through a resumable session, in chunks of
# `STREAM_CHUNK` (Drive wants multiples of 256 KiB).
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024

_JSON = {"Content-Type": "application/json"}


def _quoted(value: str) -> str:
    """Escape one string for a Drive query literal.

    Names are sanitised for Windows-forbidden characters, which leaves the apostrophe
    — legal in a camera called `Bill's Gate` and the end of a Drive query that contains it.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _confirm(item: object, path: str, size: int) -> None:
    """Check Drive's answer to an upload: the file it stored, and its length."""
    if not isinstance(item, dict) or not item.get("id"):
        raise DestinationError(f"Google Drive did not confirm storing {path}")
    stored = item.get("size")
    if stored is not None and int(stored) != size:
        raise DestinationError(f"Google Drive stored {stored} bytes of {path}, not the {size} sent")


class GoogleDriveDestination(OAuthDestination):
    """Event folders this integration creates in the user's Drive.

    Deleting a folder deletes everything in it that this client owns, so removing a pending
    folder, after a commit or for a drop, is one request once its id is known.
    """

    service = "Google Drive"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Start with no folder resolved."""
        super().__init__(hass, entry)
        self._folders: dict[str, str] = {}

    async def _async_children(self, parent: str, query: str) -> list[dict]:
        """Return the non-trashed children of one folder matching an extra clause."""
        payload = await self._async_request(
            "GET",
            f"{DRIVE}/files",
            params={
                "q": f"{_quoted(parent)} in parents and trashed=false and {query}",
                "fields": "files(id,name,size,mimeType)",
                "pageSize": "200",
            },
        )
        if not isinstance(payload, dict):
            return []
        return list(payload.get("files", []))

    async def _async_folder_id(self, folder: str, *, create: bool = True) -> str | None:
        """Resolve the id of one folder path, creating the missing segments if asked to.

        Cached, because every segment resolves the same folder and Drive charges a request for
        each segment of it. `create` is off for reading: a startup reconcile of an uploader
        that has never uploaded anything should report an empty folder, not make one.
        """
        if (known := self._folders.get(folder)) is not None:
            return known

        parent = "root"
        for segment in folder.split("/"):
            if not segment:
                continue
            existing = await self._async_children(
                parent, f"name = {_quoted(segment)} and mimeType = {_quoted(FOLDER_MIME)}"
            )
            if existing:
                parent = str(existing[0]["id"])
                continue
            if not create:
                return None
            parent = await self._async_create(
                parent, {"name": segment, "mimeType": FOLDER_MIME, "parents": [parent]}
            )

        self._folders[folder] = parent
        return parent

    async def _async_create(self, parent: str, metadata: dict[str, object]) -> str:
        """Create one file or folder, without risking a second of the same name.

        Drive keys on ids, not names, so it will happily make a twin — and a create whose
        answer was lost on the way back is exactly what a retry would ask for again. One
        attempt only, then a look to see whether it in fact happened: an invisible twin is
        worse than a failure, because nothing indexes it and eviction may delete the real
        file instead of it.
        """
        name = str(metadata["name"])
        try:
            created = await self._async_request(
                "POST", f"{DRIVE}/files", data=json.dumps(metadata), headers=_JSON, attempts=1
            )
        except DestinationError:
            existing = await self._async_children(parent, f"name = {_quoted(name)}")
            if not existing:
                raise
            return str(existing[0]["id"])
        if not isinstance(created, dict) or not created.get("id"):
            raise DestinationError(f"Google Drive did not create {name}")
        return str(created["id"])

    async def _async_file_id(self, folder: str, name: str, *, create: bool) -> str | None:
        """Return the id of one file in a folder, or None when it is not there."""
        parent = await self._async_folder_id(folder, create=create)
        if parent is None:
            return None
        found = await self._async_children(parent, f"name = {_quoted(name)}")
        return str(found[0]["id"]) if found else None

    @staticmethod
    def _split(path: str) -> tuple[str, str]:
        """Split a path into the folder it lives in and its file name."""
        folder, _, name = path.rpartition("/")
        if not folder or not name:
            raise DestinationError(f"{path} is not a path inside a folder")
        return folder, name

    async def async_store(self, path: str, data: bytes) -> None:
        """Upload one file, and confirm from Drive's own answer that it landed.

        An existing file of the same name has its content replaced rather than a second copy
        made: Drive allows duplicate names, so a store retried after a timeout that in fact
        succeeded would otherwise leave a file the index does not know about and the quota
        never reclaims.
        """
        if len(data) > SIMPLE_UPLOAD_LIMIT:
            await self.async_store_stream(path, len(data), streamed(data))
            return
        file_id = await self._async_target(path)
        item = await self._async_request(
            "PATCH",
            f"{UPLOAD}/files/{file_id}",
            params={"uploadType": "media", "fields": "id,size"},
            data=data,
            headers={"Content-Type": content_type(path)},
        )
        _confirm(item, path, len(data))
        _LOGGER.debug("Uploaded %s (%s bytes) to %s", path, len(data), self.label)

    async def _async_target(self, path: str) -> str:
        """Return the id of the file at `path`, creating it (empty) when it is not there."""
        folder, name = self._split(path)
        file_id = await self._async_file_id(folder, name, create=True)
        if file_id is None:
            parent = await self._async_folder_id(folder)
            file_id = await self._async_create(
                str(parent), {"name": name, "parents": [parent], "mimeType": content_type(path)}
            )
        return file_id

    async def async_store_stream(self, path: str, size: int, chunks: AsyncIterable[bytes]) -> None:
        """Upload one file from a stream: one request when small, else a resumable session.

        The session replaces the content of a file created first (as `async_store` does), so
        a retried commit never leaves a twin. One chunk is held in memory at a time; each is
        retried on its own, and Drive answers the last with the file.
        """
        if size <= SIMPLE_UPLOAD_LIMIT:
            await self.async_store(path, await gather(chunks, size))
            return
        file_id = await self._async_target(path)
        session = await self._async_request(
            "PATCH",
            f"{UPLOAD}/files/{file_id}",
            params={"uploadType": "resumable", "fields": "id,size"},
            headers={
                **_JSON,
                "X-Upload-Content-Type": content_type(path),
                "X-Upload-Content-Length": str(size),
            },
            data="{}",
            location=True,
        )
        if not session:
            raise DestinationError(f"Google Drive did not open an upload session for {path}")
        item: object = None
        start = 0
        async for chunk in rechunk(chunks, STREAM_CHUNK):
            if start + len(chunk) > size:
                raise DestinationError(f"the stream for {path} is longer than {size} bytes")
            item = await self._async_request(
                "PUT",
                str(session),
                data=chunk,
                headers={"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{size}"},
            )
            start += len(chunk)
        if start != size:
            raise DestinationError(f"the stream for {path} gave {start} of {size} bytes")
        _confirm(item, path, size)
        _LOGGER.debug("Uploaded %s (%s bytes) to %s in a session", path, size, self.label)

    async def async_size(self, path: str) -> int | None:
        """Return one file's size, asking for that file alone."""
        folder, name = self._split(path)
        parent = await self._async_folder_id(folder, create=False)
        if parent is None:
            return None
        found = await self._async_children(parent, f"name = {_quoted(name)}")
        if not found or found[0].get("size") is None:
            return None
        return int(found[0]["size"])

    async def async_delete(self, path: str) -> None:
        """Remove one file, treating an already-missing file as success."""
        folder, name = self._split(path)
        file_id = await self._async_file_id(folder, name, create=False)
        if file_id is None:
            return
        await self._async_request("DELETE", f"{DRIVE}/files/{file_id}", allow_missing=True)

    async def async_list(self, folder: str) -> dict[str, int]:
        """Return what the destination holds under one folder."""
        found: dict[str, int] = {}
        parent = await self._async_folder_id(folder, create=False)
        if parent is None:
            return found  # the folder does not exist yet, which is not an error
        token: str | None = None
        while True:
            params = {
                "q": f"{_quoted(parent)} in parents and trashed=false",
                "fields": "nextPageToken,files(name,size,mimeType)",
                "pageSize": "200",
            }
            if token:
                params["pageToken"] = token
            payload = await self._async_request(
                "GET", f"{DRIVE}/files", params=params, allow_missing=True
            )
            if not isinstance(payload, dict):
                return found
            for item in payload.get("files", []):
                if item.get("mimeType") == FOLDER_MIME:
                    continue
                found[f"{folder}/{item['name']}"] = int(item.get("size") or 0)
            token = payload.get("nextPageToken")
            if not token:
                return found

    def _forget(self, folder: str) -> None:
        """Drop cached ids at and under a folder that has gone."""
        for known in [
            path for path in self._folders if path == folder or path.startswith(f"{folder}/")
        ]:
            del self._folders[known]

    async def async_read(self, path: str) -> bytes:
        """Return one file's content."""
        folder, name = self._split(path)
        file_id = await self._async_file_id(folder, name, create=False)
        if file_id is None:
            raise DestinationError(f"Google Drive has no {path}")
        data = await self._async_request("GET", f"{DRIVE}/files/{file_id}", params={"alt": "media"})
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return json.dumps(data).encode()

    async def async_delete_folder(self, folder: str) -> None:
        """Remove a folder and everything in it, in one request."""
        folder_id = await self._async_folder_id(folder, create=False)
        self._forget(folder)
        if folder_id is None:
            return
        await self._async_request("DELETE", f"{DRIVE}/files/{folder_id}", allow_missing=True)

    async def async_read_range(self, path: str, start: int, end: int) -> bytes:
        """Return bytes `start..end` (inclusive) of one file, with a `Range` request."""
        folder, name = self._split(path)
        file_id = await self._async_file_id(folder, name, create=False)
        if file_id is None:
            raise DestinationError(f"Google Drive has no {path}")
        data = await self._async_request(
            "GET",
            f"{DRIVE}/files/{file_id}",
            params={"alt": "media"},
            headers={"Range": f"bytes={start}-{end}"},
        )
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return json.dumps(data).encode()
