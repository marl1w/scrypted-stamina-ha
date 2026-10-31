"""The uploader against a fake destination: order, commit, move fallbacks, gaps, eviction."""

from __future__ import annotations

import asyncio
import json

from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.destinations import MoveUnsupportedError

from .conftest import FakeDestination, event_id, subject, until
from .test_events import run_event


async def committed(manager, eid: str) -> None:
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )


async def test_commit_copies_then_deletes_where_rename_is_unsupported(
    hass, stamina, destinations, setup
) -> None:
    destinations["storage0"].can_move = False
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(60)
    run_event(stamina, eid, "confirmed")
    await committed(manager, eid)

    fake = destinations["storage0"]
    ops = [op for op, _ in fake.ops]
    assert "move" not in ops
    assert ops.count("read") == 5  # init, two segments, manifest, details
    assert ("delete_folder", next(p for o, p in fake.ops if o == "delete_folder")) in fake.ops
    assert not fake.under("scrypted-stamina/pending/")
    assert len(fake.under("scrypted-stamina/events/")) == 5
    await hass.config_entries.async_unload(entry.entry_id)


class NoMoveAfterAll(FakeDestination):
    """Advertises a move the server then turns out not to have (a Synology without CopyMove)."""

    async def async_move(self, source: str, target: str) -> None:
        raise MoveUnsupportedError("no CopyMove here")


async def test_a_move_the_server_refuses_falls_back_to_copying(
    hass, stamina, destinations, setup
) -> None:
    destinations["storage0"] = NoMoveAfterAll(name="NAS 0")
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(61)
    run_event(stamina, eid, "confirmed")
    await committed(manager, eid)
    assert len(destinations["storage0"].under("scrypted-stamina/events/")) == 5
    assert not destinations["storage0"].under("scrypted-stamina/pending/")
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

    fake = destinations["storage0"]
    files = fake.under("scrypted-stamina/events/")
    manifest = next(fake.files[p] for p in files if p.endswith("index.m3u8")).decode()
    assert "00001.m4s" not in manifest and "#EXT-X-DISCONTINUITY" in manifest
    details = json.loads(next(fake.files[p] for p in files if p.endswith("event.json")))
    assert details["gaps"] == [1]
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
    assert not any(first in path for path in fake.files)
    assert any(second in path for path in fake.files)
    assert target.usage.get(first) is None
    assert (
        "delete_folder",
        next(p for o, p in fake.ops if o == "delete_folder" and first in p),
    ) in fake.ops
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
        assert len(fake.under("scrypted-stamina/events/")) == 5
    # Fetched from the plugin once, whatever the number of destinations.
    await hass.config_entries.async_unload(entry.entry_id)
