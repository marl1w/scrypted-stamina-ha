"""OneDrive, through the OAuth session its own integration already holds.

The cost of borrowing that session is the scope the integration holds —
`Files.ReadWrite.AppFolder` — so event folders live inside Home Assistant's own app folder
rather than at the root of the drive.

Graph deletes a folder with everything in it (one request), creates missing parents on a
path-addressed upload, and answers a file's size by path, so every operation the uploader
needs is native. A file over the single-request limit (a
committed video of several hours) goes up through an upload session, in chunks.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from .base import DestinationError, content_type
from .oauth import OAuthDestination

_LOGGER = logging.getLogger(__name__)

# Microsoft Graph. The app folder is addressed by name, so nothing here needs the drive id.
GRAPH = "https://graph.microsoft.com/v1.0"
APPROOT = f"{GRAPH}/me/drive/special/approot"

# Graph accepts a single PUT up to 250 MB; a segment is a few dozen kilobytes, and a
# committed video about 70 MB an hour. Past the limit, an upload session takes it in chunks,
# which Graph wants in multiples of 320 KiB.
SIMPLE_UPLOAD_LIMIT = 240 * 1024 * 1024
SESSION_CHUNK = 32 * 320 * 1024

_JSON = {"Content-Type": "application/json"}


class OneDriveDestination(OAuthDestination):
    """Event folders in Home Assistant's OneDrive app folder."""

    service = "OneDrive"

    @staticmethod
    def _item(path: str) -> str:
        """Address one item inside the app folder by path."""
        # Graph's path addressing is `approot:/a/b/c:`; the colons are part of the syntax,
        # which is why a name containing one would break the URL rather than the file.
        return f"{APPROOT}:/{quote(path)}:"

    async def async_store(self, path: str, data: bytes) -> None:
        """Upload one file, and confirm from Graph's own answer that it landed.

        Graph returns the stored item, so success is checked rather than assumed: an upload
        is only reported as done once the service has named the item it created and agreed on
        its length. Anything less and the caller would record a file it does not have, which
        is worse than a reported failure — a failure gets retried.
        """
        if len(data) > SIMPLE_UPLOAD_LIMIT:
            item = await self._async_upload_session(path, data)
        else:
            item = await self._async_request(
                "PUT",
                f"{self._item(path)}/content",
                data=data,
                headers={"Content-Type": content_type(path)},
            )
        if not isinstance(item, dict) or not item.get("id"):
            raise DestinationError(f"OneDrive did not confirm storing {path}")
        stored = item.get("size")
        if stored is not None and int(stored) != len(data):
            raise DestinationError(
                f"OneDrive stored {stored} bytes of {path}, not the {len(data)} sent"
            )
        _LOGGER.debug("Uploaded %s (%s bytes) to %s", path, len(data), self.label)

    async def _async_upload_session(self, path: str, data: bytes) -> object:
        """Upload a large file through a Graph upload session; return the stored item.

        The session URL carries its own authorisation, and Graph refuses a bearer token on
        it. Each chunk is retried on its own; the last one answers with the item.
        """
        session = await self._async_request(
            "POST",
            f"{self._item(path)}/createUploadSession",
            data=json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}),
            headers=_JSON,
        )
        upload_url = session.get("uploadUrl") if isinstance(session, dict) else None
        if not upload_url:
            raise DestinationError(f"OneDrive did not open an upload session for {path}")
        item: object = None
        total = len(data)
        for start in range(0, total, SESSION_CHUNK):
            chunk = data[start : start + SESSION_CHUNK]
            item = await self._async_request(
                "PUT",
                str(upload_url),
                data=chunk,
                headers={"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"},
                authorize=False,
            )
        return item

    async def async_size(self, path: str) -> int | None:
        """Return one file's size, from its own item."""
        item = await self._async_request(
            "GET", self._item(path), params={"$select": "size"}, allow_missing=True
        )
        if not isinstance(item, dict) or item.get("size") is None:
            return None
        return int(item["size"])

    async def async_delete(self, path: str) -> None:
        """Remove one file or folder, treating an already-missing one as success."""
        await self._async_request("DELETE", self._item(path), allow_missing=True)

    async def async_delete_folder(self, folder: str) -> None:
        """Remove a folder and everything in it, in one request."""
        await self.async_delete(folder)

    async def async_list(self, folder: str) -> dict[str, int]:
        """Return what the destination holds under one folder."""
        found: dict[str, int] = {}
        url = f"{self._item(folder)}/children?$select=name,size,folder&$top=200"
        while url:
            payload = await self._async_request("GET", url, allow_missing=True)
            if payload is None:
                return found  # the folder does not exist yet, which is not an error
            for item in payload.get("value", []):
                if "folder" in item:
                    continue
                found[f"{folder}/{item['name']}"] = int(item.get("size") or 0)
            url = payload.get("@odata.nextLink")
        return found

    async def async_read(self, path: str) -> bytes:
        """Return one file's content (Graph redirects to a pre-authenticated download)."""
        data = await self._async_request("GET", f"{self._item(path)}/content")
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return json.dumps(data).encode()
