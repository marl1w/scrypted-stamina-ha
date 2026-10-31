"""The SSE link: resume by seq/epoch, epoch change, gaps, and the 2-minute loss timer."""

from __future__ import annotations

from datetime import timedelta
import time

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.scrypted_stamina.const import INDEX_FILENAME
from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.index import EventIndex

from .conftest import EPOCH, event_id, event_object, subject, until

record_stub = type("Stub", (), {"notes": ["orphan_complete"]})()


async def test_resume_from_the_last_seq_without_duplicates(hass, stamina, setup) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(1)
    stamina.start(eid, subjects=[subject("s1")])
    seq = stamina.segment(eid, 0, init=True)
    await until(lambda: manager.link.seq == seq)

    stamina.drop_streams()
    await until(lambda: len(stamina.stream_requests) >= 2 and manager.link_healthy)
    assert stamina.stream_requests[-1] == {"since": str(seq), "epoch": EPOCH}
    assert stamina.last_event_ids[-1] == f"{EPOCH}:{seq}"

    seq2 = stamina.segment(eid, 1)
    await until(lambda: manager.link.seq == seq2)
    assert manager.records[eid].facts.segments == 2
    # Where the stream is, written down (at the latest) when the integration stops.
    await hass.config_entries.async_unload(entry.entry_id)
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    assert await index.async_get_meta("seq") == str(seq2)
    await index.async_close()


async def test_a_new_epoch_orphans_open_events(hass, stamina, destinations, setup) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(2)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    await until(
        lambda: (
            "first_upload" in manager.records.get(eid, None).latency
            if eid in manager.records
            else False
        )
    )

    stamina.restart("ep_second")
    await until(lambda: manager.link.epoch == "ep_second")
    # Kept and still uploading what it can, then committed where it is.
    await until(
        lambda: (
            "orphan_complete" in manager.records.get(eid, record_stub).notes
            or eid not in manager.records
        )
    )
    record = await manager.index.async_get(eid)
    assert record.state is EventState.ORPHANED
    assert "plugin_restarted" in record.notes
    # Kept: what was uploaded is committed where it is, nothing deleted.
    kept = destinations["storage0"].under("scrypted-stamina/events/")
    assert any(path.endswith("00000.m4s") for path in kept)
    assert not any(op == "delete_folder" for op, _ in destinations["storage0"].ops)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_gap_in_the_stream_orphans_open_events(hass, stamina, setup) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(3)
    seq = stamina.start(eid, subjects=[subject("s1")])
    await until(lambda: manager.link.seq == seq)
    stamina.gap_next = True
    stamina.drop_streams()
    await until(
        lambda: (
            manager.records.get(eid) is None or manager.records[eid].state is EventState.ORPHANED
        )
    )
    record = await manager.index.async_get(eid)
    assert record.state is EventState.ORPHANED
    assert "stream_gap" in record.notes
    await hass.config_entries.async_unload(entry.entry_id)


async def test_link_lost_for_two_minutes_orphans_and_keeps(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(4)
    stamina.start(eid, subjects=[subject("s1", verdict="vetoed")])
    stamina.segment(eid, 0, init=True)
    await until(lambda: eid in manager.records and "first_upload" in manager.records[eid].latency)

    stamina.stream_status = 500
    stamina.drop_streams()
    await until(lambda: not manager.link_healthy)
    # One minute is not enough.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()
    assert manager.records[eid].state is EventState.PENDING

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=125))
    await hass.async_block_till_done()
    await until(
        lambda: eid not in manager.records or manager.records[eid].state is EventState.ORPHANED
    )
    record = await manager.index.async_get(eid)
    assert record.state is EventState.ORPHANED
    assert "link_lost" in record.notes
    # Orphaned is "never drop", not "stop uploading": once the plugin says the event is
    # over, what was uploaded is committed where it can be found.
    stamina.event_state[eid] = {
        "event": event_object(eid, end=time.time()),
        "segments": {"live": False, "next": 1},
    }
    await until(lambda: bool(destinations["storage0"].under("scrypted-stamina/events/")))
    # A vetoed subject, but no decision could be made: nothing is deleted.
    assert not any(op == "delete_folder" for op, _ in destinations["storage0"].ops)
    assert destinations["storage0"].under("scrypted-stamina/events/")
    await hass.config_entries.async_unload(entry.entry_id)
