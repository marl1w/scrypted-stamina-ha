"""A committed video is played by range: only the bytes a player asks for are read."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.scrypted_stamina import playback
from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.destinations import (
    DestinationError,
    OneDriveDestination,
)

from .conftest import event_id, until
from .test_destinations import loaded
from .test_events import run_event

VIDEO = "scrypted-stamina/events/garden-front-door-261031_210509.mp4"
URL = "https://download.example/abc"


def _onedrive(answer):
    hass = MagicMock()
    hass.async_create_task = lambda coro, name=None: asyncio.get_running_loop().create_task(coro)
    destination = OneDriveDestination(hass, loaded("onedrive"))
    calls: list[tuple[str, dict]] = []

    async def request(method, url, **kwargs):
        calls.append((url, kwargs))
        return await answer(url, kwargs)

    destination._async_request = request
    return destination, calls


async def test_onedrive_reads_a_range_from_its_download_url_asked_once() -> None:
    async def answer(url, kwargs):
        if url == URL:
            first, last = kwargs["headers"]["Range"][6:].split("-")
            return b"0123456789"[int(first) : int(last) + 1]
        return {"id": "1", "@microsoft.graph.downloadUrl": URL}

    destination, calls = _onedrive(answer)
    assert await destination.async_read_range(VIDEO, 0, 3) == b"0123"
    assert await destination.async_read_range(VIDEO, 4, 9) == b"456789"
    lookups = [kwargs for url, kwargs in calls if url != URL]
    assert len(lookups) == 1
    assert lookups[0]["params"] == {"$select": "id,@microsoft.graph.downloadUrl"}
    ranged = [kwargs for url, kwargs in calls if url == URL]
    assert [k["headers"]["Range"] for k in ranged] == ["bytes=0-3", "bytes=4-9"]
    assert all(k["authorize"] is False for k in ranged)


async def test_onedrive_asks_again_for_a_url_that_stopped_working() -> None:
    expired = True

    async def answer(url, kwargs):
        nonlocal expired
        if url == URL:
            if expired:
                expired = False
                raise DestinationError("HTTP 401")
            return b"ok"
        return {"@microsoft.graph.downloadUrl": URL}

    destination, calls = _onedrive(answer)
    assert await destination.async_read_range(VIDEO, 0, 1) == b"ok"
    assert len([url for url, _ in calls if url != URL]) == 2


async def test_cancelling_one_reader_does_not_fail_the_others() -> None:
    release = asyncio.Event()

    async def answer(url, kwargs):
        if url == URL:
            return b"data"
        await release.wait()
        return {"@microsoft.graph.downloadUrl": URL}

    destination, calls = _onedrive(answer)
    first = asyncio.create_task(destination.async_read_range(VIDEO, 0, 3))
    second = asyncio.create_task(destination.async_read_range(VIDEO, 0, 3))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == b"data"
    # The shared lookup survived the cancel and was made once.
    assert len([url for url, _ in calls if url != URL]) == 1


async def test_the_cloud_view_reads_only_the_range_asked_for(
    hass, hass_client_no_auth, stamina, destinations, setup, monkeypatch
) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(81)
    run_event(stamina, eid, "confirmed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )
    client = await hass_client_no_auth()
    played = playback.play_url(hass, entry.entry_id, eid, "cloud")
    lines = (await (await client.get(played)).text()).splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("#EXT-X-BYTERANGE:"))
    length, offset = (int(n) for n in lines[at].split(":")[1].split("@"))
    uri = lines[at + 1]
    fake = destinations["storage0"]
    [path] = fake.videos()
    video = fake.files[path]
    fake.ops.clear()

    response = await client.get(uri, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
    assert response.status == 206
    assert await response.read() == video[offset : offset + length]
    assert fake.ops == [("read_range", path)]  # never the whole video

    # A whole read goes out in chunks, each a range of its own.
    monkeypatch.setattr(playback, "VIDEO_CHUNK", 7)
    fake.ops.clear()
    whole = await client.get(uri)
    assert whole.status == 200 and await whole.read() == video
    assert len(fake.ops) == -(-len(video) // 7)
    assert {op for op, _ in fake.ops} == {"read_range"}

    # A destination that fails before anything is sent is a clean 502.
    fake.files.pop(path)
    playback._LOOKUPS._items.clear()
    assert (await client.get(uri, headers={"Range": "bytes=0-1"})).status == 502
    await hass.config_entries.async_unload(entry.entry_id)
