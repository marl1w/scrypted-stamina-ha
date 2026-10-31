"""The destinations' operations beyond store: read, size, the video, folder delete."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigEntryState
import pytest

from custom_components.scrypted_stamina.destinations import (
    DestinationError,
    GoogleDriveDestination,
    OneDriveDestination,
    SftpDestination,
    SynologyDestination,
    WebDavDestination,
    async_create_destination,
    onedrive,
)
from custom_components.scrypted_stamina.destinations.base import content_type, streamed

from .conftest import FakeDestination

SOURCE = "scrypted-stamina/pending/garden/261031_210509_front-door_ev_1"
VIDEO = "scrypted-stamina/events/garden-front-door-261031_210509.mp4"


def loaded(domain: str, runtime=None) -> MagicMock:
    entry = MagicMock(
        domain=domain, title=domain, entry_id=f"{domain}-1", state=ConfigEntryState.LOADED
    )
    entry.runtime_data = runtime
    return entry


@pytest.mark.parametrize(
    ("domain", "cls"),
    [
        ("onedrive", OneDriveDestination),
        ("google_drive", GoogleDriveDestination),
        ("synology_dsm", SynologyDestination),
        ("webdav", WebDavDestination),
        ("sftp_storage", SftpDestination),
    ],
)
def test_every_provider_is_created_from_its_domain(domain, cls) -> None:
    assert isinstance(async_create_destination(MagicMock(), loaded(domain)), cls)


def test_content_types() -> None:
    assert content_type("x/00001.m4s") == "video/iso.segment"
    assert content_type("x/garden-front-door-261031_210509.mp4") == "video/mp4"


async def test_a_video_is_stored_and_its_size_confirmed() -> None:
    fake = FakeDestination()
    await fake.async_store_video(VIDEO, 5, streamed(b"video"))
    assert fake.files[VIDEO] == b"video"

    class Lossy(FakeDestination):
        async def async_store(self, path: str, data: bytes) -> None:
            await super().async_store(path, data[:2])

    with pytest.raises(DestinationError, match="2 bytes"):
        await Lossy().async_store_video(VIDEO, 5, streamed(b"video"))


async def test_webdav_deletes_a_folder_with_one_request() -> None:
    client = MagicMock()
    client.check = AsyncMock(return_value=True)
    client.clean = AsyncMock()
    destination = WebDavDestination(MagicMock(), loaded("webdav", client))
    await destination.async_delete_folder(SOURCE)
    client.clean.assert_awaited_once_with(SOURCE)


async def test_webdav_reads_a_file_back() -> None:
    async def chunks():
        yield b"ab"
        yield b"cd"

    client = MagicMock()
    client.download_iter = AsyncMock(return_value=chunks())
    destination = WebDavDestination(MagicMock(), loaded("webdav", client))
    assert await destination.async_read(f"{SOURCE}/init.mp4") == b"abcd"


async def test_sftp_removes_trees() -> None:
    sftp = MagicMock()
    sftp.isdir = AsyncMock(return_value=True)
    sftp.rmtree = AsyncMock()
    config = SimpleNamespace(backup_location="/backups")
    destination = SftpDestination(MagicMock(), loaded("sftp_storage", config))
    destination._sftp = sftp
    await destination.async_delete_folder(SOURCE)
    sftp.rmtree.assert_awaited_once_with(f"/backups/{SOURCE}")


async def test_onedrive_stores_a_small_video_in_one_request_and_asks_its_size() -> None:
    destination = OneDriveDestination(MagicMock(), loaded("onedrive"))
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "PUT":
            return {"id": "v", "size": len(kwargs["data"])}
        return {"size": 5}

    destination._async_request = request
    await destination.async_store_video(VIDEO, 5, streamed(b"video"))
    assert [c[0] for c in calls] == ["PUT", "GET"]
    assert calls[0][1].endswith(f"{VIDEO}:/content")
    assert calls[1][2]["params"] == {"$select": "size"}


async def test_onedrive_uploads_a_large_video_through_a_session(monkeypatch) -> None:
    monkeypatch.setattr(onedrive, "SIMPLE_UPLOAD_LIMIT", 4)
    monkeypatch.setattr(onedrive, "SESSION_CHUNK", 4)
    destination = OneDriveDestination(MagicMock(), loaded("onedrive"))
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            return {"uploadUrl": "https://upload.example/session"}
        if kwargs["headers"]["Content-Range"].endswith("9/10"):
            return {"id": "v", "size": 10}
        return {"nextExpectedRanges": ["4-"]}

    destination._async_request = request
    await destination.async_store(VIDEO, b"0123456789")
    assert calls[0][1].endswith(f"{VIDEO}:/createUploadSession")
    puts = [c for c in calls if c[0] == "PUT"]
    assert [c[2]["headers"]["Content-Range"] for c in puts] == [
        "bytes 0-3/10",
        "bytes 4-7/10",
        "bytes 8-9/10",
    ]
    assert [c[2]["data"] for c in puts] == [b"0123", b"4567", b"89"]
    # The session URL is its own credential: Graph refuses a bearer token on it.
    assert all(c[1] == "https://upload.example/session" and not c[2]["authorize"] for c in puts)


async def test_google_drive_asks_one_file_its_size() -> None:
    destination = GoogleDriveDestination(MagicMock(), loaded("google_drive"))
    destination._folders = {"scrypted-stamina/events": "events"}
    destination._async_request = AsyncMock(return_value={"files": [{"id": "v", "size": "7"}]})
    assert await destination.async_size(VIDEO) == 7
    _, kwargs = destination._async_request.await_args
    assert "'garden-front-door-261031_210509.mp4'" in kwargs["params"]["q"]
