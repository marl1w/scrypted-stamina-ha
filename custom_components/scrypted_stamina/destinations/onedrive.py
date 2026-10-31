"""OneDrive, through the OAuth session its own integration already holds.

The cost of borrowing that session is the scope the integration holds —
`Files.ReadWrite.AppFolder` — so event folders live inside Home Assistant's own app folder
rather than at the root of the drive.

Graph moves a folder by patching its `parentReference` (one request), deletes a folder with
everything in it (one request), and creates missing parents on a path-addressed upload, so
every operation the uploader needs is native.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from .base import DestinationError, UploadTooLargeError, content_type
from .oauth import OAuthDestination

_LOGGER = logging.getLogger(__name__)

# Microsoft Graph. The app folder is addressed by name, so nothing here needs the drive id.
GRAPH = "https://graph.microsoft.com/v1.0"
APPROOT = f"{GRAPH}/me/drive/special/approot"

# Graph accepts a single PUT up to 250 MB; a segment is a few hundred kilobytes.
SIMPLE_UPLOAD_LIMIT = 240 * 1024 * 1024

_JSON = {"Content-Type": "application/json"}


class OneDriveDestination(OAuthDestination):
    """Event folders in Home Assistant's OneDrive app folder."""

    service = "OneDrive"
    can_move = True

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
            raise UploadTooLargeError(
                f"{len(data)} bytes exceeds the {SIMPLE_UPLOAD_LIMIT} byte single-request limit"
            )
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

    async def _async_folder_id(self, folder: str) -> str:
        """Return the id of a folder, creating it and its parents where missing.

        Each segment is read by path first and only created when missing: creating one that
        exists answers 409 in a subfolder but 400 directly under the app folder. It is created
        with `conflictBehavior: fail` ("replace" would empty it), and a creation that loses a
        race is read by path again.
        """
        parent_url = APPROOT
        walked: list[str] = []
        folder_id: str | None = None
        for segment in [part for part in folder.split("/") if part]:
            walked.append(segment)
            item = await self._async_request(
                "GET", self._item("/".join(walked)), allow_missing=True
            )
            if isinstance(item, dict) and item.get("id"):
                folder_id = str(item["id"])
                parent_url = f"{GRAPH}/me/drive/items/{folder_id}"
                continue
            item = await self._async_request(
                "POST",
                f"{parent_url}/children",
                data=json.dumps(
                    {
                        "name": segment,
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "fail",
                    }
                ),
                headers=_JSON,
                allow_conflict=True,
            )
            if not isinstance(item, dict) or not item.get("id"):
                item = await self._async_request("GET", self._item("/".join(walked)))
            if not isinstance(item, dict) or not item.get("id"):
                raise DestinationError(f"OneDrive could not resolve {folder}")
            folder_id = str(item["id"])
            parent_url = f"{GRAPH}/me/drive/items/{folder_id}"
        if folder_id is None:
            raise DestinationError("OneDrive will not move into the app folder root")
        return folder_id

    async def async_move(self, source: str, target: str) -> None:
        """Move a folder by patching its parent and name."""
        parent, _, name = target.rpartition("/")
        parent_id = await self._async_folder_id(parent)
        item = await self._async_request(
            "PATCH",
            self._item(source),
            data=json.dumps({"parentReference": {"id": parent_id}, "name": name}),
            headers=_JSON,
        )
        if not isinstance(item, dict) or not item.get("id"):
            raise DestinationError(f"OneDrive did not confirm moving {source}")
