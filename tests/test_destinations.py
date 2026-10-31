"""The ported destinations' new operations: move, read and folder delete (spike S4)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.config_entries import ConfigEntryState
import pytest

from custom_components.scrypted_stamina.destinations import (
    GoogleDriveDestination,
    MoveUnsupportedError,
    OneDriveDestination,
    SftpDestination,
    SynologyDestination,
    WebDavDestination,
    async_create_destination,
)
from custom_components.scrypted_stamina.destinations.base import content_type

SOURCE = "scrypted-stamina/pending/garden/261031_210509_front-door_ev_1"
TARGET = "scrypted-stamina/events/garden/261031_210509_front-door_ev_1"


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
def test_every_ported_provider_can_move(domain, cls) -> None:
    destination = async_create_destination(MagicMock(), loaded(domain))
    assert isinstance(destination, cls)
    assert destination.can_move


def test_content_types() -> None:
    assert content_type("x/00001.m4s") == "video/iso.segment"
    assert content_type("x/index.m3u8") == "application/vnd.apple.mpegurl"
    assert content_type("x/event.json") == "application/json"


async def test_webdav_moves_with_one_request_after_making_the_parent() -> None:
    client = MagicMock()
    client.check = AsyncMock(return_value=False)
    client.mkdir = AsyncMock(return_value=True)
    client.move = AsyncMock()
    client.clean = AsyncMock()
    destination = WebDavDestination(MagicMock(), loaded("webdav", client))
    await destination.async_move(SOURCE, TARGET)
    client.move.assert_awaited_once_with(SOURCE, TARGET, overwrite=False)
    assert client.mkdir.await_args_list[-1].args[0] == "scrypted-stamina/events/garden"

    client.check = AsyncMock(return_value=True)
    await destination.async_delete_folder(SOURCE)
    client.clean.assert_awaited_once_with(SOURCE)


async def test_webdav_reads_a_file_back() -> None:
    async def chunks():
        yield b"ab"
        yield b"cd"

    client = MagicMock()
    client.download_iter = AsyncMock(return_value=chunks())
    destination = WebDavDestination(MagicMock(), loaded("webdav", client))
    assert await destination.async_read(f"{TARGET}/init.mp4") == b"abcd"


async def test_sftp_renames_and_removes_trees() -> None:
    sftp = MagicMock()
    sftp.makedirs = AsyncMock()
    sftp.rename = AsyncMock()
    sftp.isdir = AsyncMock(return_value=True)
    sftp.rmtree = AsyncMock()
    config = SimpleNamespace(backup_location="/backups")
    destination = SftpDestination(MagicMock(), loaded("sftp_storage", config))
    destination._sftp = sftp
    await destination.async_move(SOURCE, TARGET)
    sftp.rename.assert_awaited_once_with(f"/backups/{SOURCE}", f"/backups/{TARGET}")
    sftp.makedirs.assert_awaited_once_with("/backups/scrypted-stamina/events/garden", exist_ok=True)
    await destination.async_delete_folder(SOURCE)
    sftp.rmtree.assert_awaited_once_with(f"/backups/{SOURCE}")


async def test_onedrive_moves_by_patching_the_parent() -> None:
    destination = OneDriveDestination(MagicMock(), loaded("onedrive"))
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            return None  # 409: the folder exists
        if method == "GET":
            return {"id": f"id-{len(calls)}"}
        return {"id": "moved"}

    destination._async_request = request
    await destination.async_move(SOURCE, TARGET)
    method, url, kwargs = calls[-1]
    assert method == "PATCH" and url.endswith(f"{SOURCE}:")
    assert '"name": "261031_210509_front-door_ev_1"' in kwargs["data"]
    assert '"parentReference": {"id": "id-6"}' in kwargs["data"]
    assert all(c[2].get("allow_conflict") for c in calls if c[0] == "POST")


async def test_google_drive_moves_by_swapping_parents() -> None:
    destination = GoogleDriveDestination(MagicMock(), loaded("google_drive"))
    destination._folders = {
        SOURCE: "event-folder",
        "scrypted-stamina/pending/garden": "pending-garden",
        "scrypted-stamina/events/garden": "events-garden",
    }
    destination._async_request = AsyncMock(return_value={"id": "event-folder"})
    await destination.async_move(SOURCE, TARGET)
    _, kwargs = destination._async_request.await_args
    assert kwargs["params"]["addParents"] == "events-garden"
    assert kwargs["params"]["removeParents"] == "pending-garden"
    assert SOURCE not in destination._folders


async def test_synology_moves_with_copymove_and_waits_for_it() -> None:
    dsm = MagicMock()
    dsm.get = AsyncMock(
        side_effect=[
            {"success": True},  # CreateFolder
            {"data": {"taskid": "t1"}},  # CopyMove start
            {"data": {"finished": False}},
            {"data": {"finished": True}},
        ]
    )
    runtime = SimpleNamespace(api=SimpleNamespace(file_station=MagicMock(), dsm=dsm))
    destination = SynologyDestination(MagicMock(), loaded("synology_dsm", runtime))
    await destination.async_move(SOURCE, TARGET)
    start = dsm.get.await_args_list[1]
    assert start.args[:2] == ("SYNO.FileStation.CopyMove", "start")
    assert start.args[2]["dest_folder_path"] == "/scrypted-stamina/events/garden"
    assert start.args[2]["remove_src"] == "true"


async def test_synology_without_copymove_asks_for_the_copy_fallback() -> None:
    class SynologyDSMAPINotExistsException(Exception):
        pass

    dsm = MagicMock()
    dsm.get = AsyncMock(side_effect=SynologyDSMAPINotExistsException("SYNO.FileStation.CopyMove"))
    runtime = SimpleNamespace(api=SimpleNamespace(file_station=MagicMock(), dsm=dsm))
    destination = SynologyDestination(MagicMock(), loaded("synology_dsm", runtime))
    with pytest.raises(MoveUnsupportedError):
        await destination.async_move(SOURCE, TARGET)
