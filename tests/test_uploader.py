"""The uploader against a fake destination: order, the committed video, gaps, eviction."""

from __future__ import annotations

import asyncio
import re

from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.uploader import video_playlist

from .conftest import event_id, subject, until
from .test_events import run_event

VIDEO = re.compile(r"^scrypted-stamina/events/no-area-front-door-\d{6}_\d{6}(-\d+)?\.mp4$")


async def committed(manager, eid: str) -> None:
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )


async def test_a_commit_joins_the_segments_into_one_video_and_deletes_pending(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(60)
    run_event(stamina, eid, "confirmed")
    await committed(manager, eid)

    fake = destinations["storage0"]
    assert fake.under("scrypted-stamina/") == fake.videos()
    [path] = fake.videos()
    assert VIDEO.match(path)
    init, first, second = (stamina.files[(eid, name)] for name in ("init.mp4", "0.m4s", "1.m4s"))
    assert fake.files[path] == init + first + second  # joined in play order, as uploaded
    assert [op for op, _ in fake.ops].count("read") == 3
    assert not fake.under("scrypted-stamina/pending/")
    [copy] = (await manager.index.async_copies(eid)).values()
    assert copy.playable and copy.video == path
    assert copy.ranges == {
        "init.mp4": [0, len(init)],
        "00000.m4s": [len(init), len(first)],
        "00001.m4s": [len(init) + len(first), len(second)],
    }
    assert copy.size == len(fake.files[path])  # the quota counts the video
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_expired_segment_is_a_gap_and_playback_skips_it(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(62)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    stamina.segment(eid, 1)
    stamina.segment(eid, 2)
    del stamina.files[(eid, "1.m4s")]  # expired in the plugin before it was fetched
    stamina.end(eid, subjects=[subject("s1", verdict="confirmed")], next_segment=3, init=True)
    await committed(manager, eid)

    record = manager.records[eid]
    assert record.gaps == [1]
    [copy] = (await manager.index.async_copies(eid)).values()
    assert set(copy.ranges) == {"init.mp4", "00000.m4s", "00002.m4s"}
    playlist = video_playlist(record, copy, "video.mp4")
    assert "#EXT-X-DISCONTINUITY" in playlist
    assert playlist.count("#EXT-X-BYTERANGE:") == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_segment_not_cut_yet_is_waited_for(hass, stamina, destinations, setup) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(63)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    stamina.not_ready.add((eid, "0.m4s"))
    await until(lambda: any(p.endswith("init.mp4") for p in destinations["storage0"].files))
    await asyncio.sleep(0.2)
    assert not any(p.endswith("00000.m4s") for p in destinations["storage0"].files)
    stamina.not_ready.clear()
    await until(lambda: any(p.endswith("00000.m4s") for p in destinations["storage0"].files))
    assert manager.records[eid].gaps == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_quota_evicts_the_oldest_committed_event(hass, stamina, destinations, setup) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    target = next(iter(manager.uploader.targets.values()))
    first, second = event_id(64), event_id(65)
    run_event(stamina, first, "confirmed")
    await committed(manager, first)
    size = target.usage.used
    # Room for one committed event and a little: the next one forces the first out.
    target.quota = size + 100
    run_event(stamina, second, "confirmed")
    await committed(manager, second)

    fake = destinations["storage0"]
    first_copy = next(iter((await manager.index.async_copies(first)).values()))
    second_copy = next(iter((await manager.index.async_copies(second)).values()))
    assert first_copy.deleted and ("delete", first_copy.video) in fake.ops
    assert fake.videos() == [second_copy.video]
    assert target.usage.get(first) is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_full_destination_keeps_pending_footage_over_quota(
    hass, stamina, destinations, setup
) -> None:
    """The quota is soft for pending footage: it goes over and a repair issue says so."""
    from homeassistant.helpers import issue_registry as ir

    entry = await setup()
    manager = entry.runtime_data.manager
    target = next(iter(manager.uploader.targets.values()))
    target.quota = 60  # init fits; nothing else does, and nothing committed can be evicted
    eid = event_id(66)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    stamina.segment(eid, 1)
    await until(lambda: len(destinations["storage0"].files) == 3)
    assert target.over_quota and target.usage.used > target.quota
    assert not target.degraded  # a quota shortfall is not a destination failure
    assert ir.async_get(hass).async_get_issue(
        "scrypted_stamina", f"over_quota_{target.subentry_id}"
    )
    assert not any(op == "delete_folder" for op, _ in destinations["storage0"].ops)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_every_kept_event_goes_to_every_destination(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup(destinations=2)
    manager = entry.runtime_data.manager
    eid = event_id(67)
    run_event(stamina, eid, "confirmed")
    await committed(manager, eid)
    for fake in destinations.values():
        assert len(fake.videos()) == 1 and not fake.under("scrypted-stamina/pending/")
    # Fetched from the plugin once, whatever the number of destinations.
    await hass.config_entries.async_unload(entry.entry_id)
