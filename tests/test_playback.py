"""Playback through Home Assistant: the signed proxy, the cloud copy, the media source."""

from __future__ import annotations

from homeassistant.components.media_source import async_browse_media, async_resolve_media
from homeassistant.setup import async_setup_component

from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.playback import proxy_url

from .conftest import TOKEN, event_id, until
from .test_events import run_event


async def test_proxy_follows_the_session_redirect_and_signs_every_uri(
    hass, hass_client_no_auth, stamina, setup
) -> None:
    entry = await setup()
    client = await hass_client_no_auth()
    url = proxy_url(hass, entry.entry_id, "/v1/cameras/38/playback.m3u8?t=1000&d=30")
    response = await client.get(url)
    assert response.status == 200
    text = await response.text()
    assert TOKEN not in text and "127.0.0.1" not in text
    segment = next(line for line in text.splitlines() if line.endswith("") and "0.m4s" in line)
    assert "/playback/pb_" in segment and "sig=" in segment
    media = await client.get(segment)
    assert media.status == 200
    assert await media.read() == b"media-0.m4s"

    tampered = url.replace("d=30", "d=31")
    assert (await client.get(tampered)).status == 403
    assert proxy_url(hass, entry.entry_id, "/v1/health") is None
    assert proxy_url(hass, entry.entry_id, "/v1/cameras/38/../../health") is None
    await hass.config_entries.async_unload(entry.entry_id)


def fake_video(destinations) -> bytes:
    [path] = destinations["storage0"].videos()
    return destinations["storage0"].files[path]


async def test_media_source_browses_and_resolves(
    hass, hass_client_no_auth, stamina, destinations, setup
) -> None:
    assert await async_setup_component(hass, "media_source", {})
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(80)
    stamina.thumbnails[eid] = b"jpeg"
    run_event(stamina, eid, "confirmed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )

    root = await async_browse_media(hass, "media-source://scrypted_stamina")
    camera = root.children[0]
    assert camera.title == "Front door"
    days = await async_browse_media(hass, f"media-source://scrypted_stamina/{camera.identifier}")
    events = await async_browse_media(
        hass, f"media-source://scrypted_stamina/{days.children[0].identifier}"
    )
    item = events.children[0]
    assert item.can_play
    client = await hass_client_no_auth()
    assert await (await client.get(item.thumbnail)).read() == b"jpeg"

    # A camera with a recorder plays from the recorder, through the proxy.
    uri = f"media-source://scrypted_stamina/{item.identifier}"
    played = await async_resolve_media(hass, uri, None)
    playlist = await (await client.get(played.url)).text()
    assert "/api/scrypted_stamina/proxy/" in playlist and "/playback/pb_" in playlist

    # Both sources are offered explicitly too.
    sources = await async_browse_media(hass, uri)
    assert [child.title for child in sources.children] == ["Recording", "Cloud copy"]

    # Older than the recorder keeps footage: straight to the cloud copy, recorder not asked.
    manager.recorder_retention_days = -1
    played = await async_resolve_media(hass, uri, None)
    assert "/api/scrypted_stamina/cloud/" in await (await client.get(played.url)).text()
    manager.recorder_retention_days = 30

    # The recorder failing falls back to the committed cloud copy.
    stamina.playback_status = 502
    played = await async_resolve_media(hass, uri, None)
    playlist = await (await client.get(played.url)).text()
    assert "/api/scrypted_stamina/cloud/" in playlist

    # Without a recorder at all, the cloud copy plays too.
    manager.cameras["38"].info.capabilities["video_recorder"] = False
    playlist = await (await client.get(played.url)).text()
    lines = playlist.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("#EXT-X-BYTERANGE:"))
    length, offset = (int(n) for n in lines[at].split(":")[1].split("@"))
    first = lines[at + 1]
    assert first.startswith("/api/scrypted_stamina/cloud/") and "/video.mp4?" in first
    # The player asks for each segment as a range of the one video.
    response = await client.get(first, headers={"Range": f"bytes={offset}-{offset + length - 1}"})
    assert response.status == 206
    assert response.headers["Content-Range"].endswith(f"/{len(fake_video(destinations))}")
    assert await response.read() == fake_video(destinations)[offset : offset + length]
    assert await response.read() == stamina.files[(eid, "0.m4s")]
    whole = await client.get(first)
    assert whole.status == 200 and await whole.read() == fake_video(destinations)
    await hass.config_entries.async_unload(entry.entry_id)
