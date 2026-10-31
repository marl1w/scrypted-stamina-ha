"""Whole events through the manager: commit, follow-up window, drop, cap, restart."""

from __future__ import annotations

import json
import time

from homeassistant.core import HomeAssistant
import pytest

from custom_components.scrypted_stamina.const import (
    CONF_CAMERAS,
    CONF_FOLLOW_UP_MINUTES,
    CONF_LINKED,
)
from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.index import ACTION_DROP, Copy, EventIndex
from custom_components.scrypted_stamina.relevance import RelevanceResult

from .conftest import CAMERA, Clock, event_id, subject, until

GATE = "binary_sensor.gate"
OPTIONS = {CONF_CAMERAS: {CAMERA: {CONF_LINKED: [GATE], CONF_FOLLOW_UP_MINUTES: 10}}}


def late_makes_unusual(record, *, now, include_late=True) -> RelevanceResult:
    """A model that finds an event unusual only when late evidence came."""
    unusual = include_late and bool(record.late)
    return RelevanceResult(
        score=0.95 if unusual else 0.2,
        surprisal=3.0 if unusual else -1.0,
        threshold=1.4,
        unusual=unusual,
        reason="Person with the gate opening after" if unusual else "usual",
        reasons=[],
    )


async def wire(entry, clock: Clock):
    manager = entry.runtime_data.manager
    manager.now = clock
    manager.analysis.score = late_makes_unusual
    return manager


def run_event(stamina, eid: str, verdict: str, segments: int = 2) -> None:
    stamina.start(eid, subjects=[subject("s1")])
    for n in range(segments):
        stamina.segment(eid, n, init=n == 0)
    stamina.verdict(eid, subject("s1", verdict=verdict))
    stamina.end(eid, subjects=[subject("s1", verdict=verdict)], next_segment=segments, init=True)


async def state_of(manager, eid: str) -> EventState:
    record = manager.records.get(eid) or await manager.index.async_get(eid)
    return record.state


async def test_confirmed_event_commits_with_manifest_and_details(
    hass: HomeAssistant, stamina, destinations, setup
) -> None:
    entry = await setup(destinations=2)
    manager = await wire(entry, Clock())
    fired = []
    hass.bus.async_listen("state_changed", lambda e: fired.append(e.data["entity_id"]))
    eid = event_id(10)
    run_event(stamina, eid, "confirmed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )

    for fake in destinations.values():
        files = fake.under("scrypted-stamina/events/no-area/")
        names = [path.rsplit("/", 1)[1] for path in files]
        assert names == ["00000.m4s", "00001.m4s", "event.json", "index.m3u8", "init.mp4"]
        assert not fake.under("scrypted-stamina/pending/")
        details = json.loads(next(fake.files[p] for p in files if p.endswith("event.json")))
        assert details["state"] == "keeping"  # written before the move
        assert details["keep_reason"] == "confirmed"
        assert "camera_event_time" in details["latency"]
        assert "first_upload" in details["latency"]
        assert TOKEN_FREE(details)
    assert ("ev_" + "0" * 20 + "10", 1) in stamina.acks
    state = hass.states.get("event.front_door_stamina")
    assert state.attributes["event_type"] == "normal"
    assert state.attributes["decision"] in ("keeping", "committed")
    # Entity updates are coalesced over a second.
    await until(lambda: hass.states.get("sensor.stamina_latency").state != "unknown")
    await hass.config_entries.async_unload(entry.entry_id)


def TOKEN_FREE(details: dict) -> bool:
    from .conftest import TOKEN

    return TOKEN not in json.dumps(details)


async def test_segments_upload_in_order(hass, stamina, destinations, setup) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(11)
    run_event(stamina, eid, "confirmed", segments=4)
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )
    stores = [path.rsplit("/", 1)[1] for op, path in destinations["storage0"].ops if op == "store"]
    assert stores[:5] == ["init.mp4", "00000.m4s", "00001.m4s", "00002.m4s", "00003.m4s"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_vetoed_event_is_held_then_dropped_after_the_window(
    hass, stamina, destinations, setup
) -> None:
    clock = Clock()
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(12)
    run_event(stamina, eid, "vetoed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.FOLLOW_UP
    )
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)

    await clock.advance(hass, 5 * 60)
    assert manager.records[eid].state is EventState.FOLLOW_UP  # still inside the window
    assert destinations["storage0"].under("scrypted-stamina/pending/")

    await clock.advance(hass, 6 * 60)
    await until(lambda: eid not in manager.records)
    record = await manager.index.async_get(eid)
    assert record.state is EventState.DROPPED
    assert record.finalized and record.unusual_final is False
    assert not destinations["storage0"].under("scrypted-stamina/")
    assert (eid, 1) in stamina.acks
    assert await manager.index.async_actions() == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_late_door_open_makes_it_unusual_and_moves_it(
    hass, stamina, destinations, setup
) -> None:
    clock = Clock()
    hass.states.async_set(GATE, "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    events = []
    hass.bus.async_listen(
        "state_changed",
        lambda e: (
            events.append(e.data["new_state"])
            if e.data["entity_id"] == "event.front_door_stamina"
            else None
        ),
    )
    eid = event_id(13)
    run_event(stamina, eid, "vetoed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.FOLLOW_UP
    )

    hass.states.async_set(GATE, "on", {"device_class": "door"})
    await hass.async_block_till_done()
    assert manager.records[eid].late[0]["entity"] == GATE
    assert manager.records[eid].late[0]["kind"] == "door"
    assert manager.records[eid].late[0]["to"] == "on"

    await clock.advance(hass, 11 * 60)
    await until(lambda: eid not in manager.records)
    record = await manager.index.async_get(eid)
    assert record.state is EventState.COMMITTED
    assert record.unusual_provisional is False and record.unusual_final is True
    files = destinations["storage0"].under("scrypted-stamina/unusual-unconfirmed/no-area/")
    assert any(path.endswith("index.m3u8") for path in files)
    assert not destinations["storage0"].under("scrypted-stamina/pending/")
    details = json.loads(
        next(destinations["storage0"].files[p] for p in files if p.endswith("event.json"))
    )
    assert details["late"][0]["entity"] == GATE
    assert details["score"]["final"] == 0.95 and details["score"]["provisional"] == 0.2

    last = events[-1]
    assert last.attributes["event_type"] == "unusual"
    assert last.attributes["late"] is True
    await until(lambda: hass.states.get("binary_sensor.front_door_unusual").state == "on")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_confirmed_event_is_rescored_after_the_window_without_moving(
    hass, stamina, destinations, setup
) -> None:
    clock = Clock()
    hass.states.async_set(GATE, "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(14)
    run_event(stamina, eid, "confirmed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )
    hass.states.async_set(GATE, "on", {"device_class": "door"})
    await hass.async_block_till_done()
    await clock.advance(hass, 11 * 60)
    await until(lambda: eid not in manager.records)
    files = destinations["storage0"].under("scrypted-stamina/events/")
    details = json.loads(
        next(destinations["storage0"].files[p] for p in files if p.endswith("event.json"))
    )
    assert details["score"]["final"] == 0.95
    assert not destinations["storage0"].under("scrypted-stamina/unusual-unconfirmed/")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_late_verdict_upgrade_keeps_the_event(hass, stamina, destinations, setup) -> None:
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, Clock())
    eid = event_id(15)
    run_event(stamina, eid, "vetoed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.FOLLOW_UP
    )
    stamina.verdict(eid, subject("s1", verdict="confirmed"))
    await until(lambda: manager.records[eid].state is EventState.COMMITTED)
    assert destinations["storage0"].under("scrypted-stamina/events/")
    assert "confirmed_after_end" in manager.records[eid].notes
    await hass.config_entries.async_unload(entry.entry_id)


async def test_follow_up_cap_drops_the_oldest_held_event(
    hass, stamina, destinations, setup
) -> None:
    """Each event holds init (30 B) and two segments (2 x 40 B): 110 B. The cap is 150 B."""
    entry = await setup(options=OPTIONS, follow_up_cap_gb=150 / 1024**3)
    manager = await wire(entry, Clock())
    first, kept, second = event_id(20), event_id(21), event_id(22)
    run_event(stamina, first, "vetoed")
    await until(
        lambda: first in manager.records and manager.records[first].state is EventState.FOLLOW_UP
    )
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)
    run_event(stamina, kept, "confirmed")
    await until(
        lambda: kept in manager.records and manager.records[kept].state is EventState.COMMITTED
    )
    run_event(stamina, second, "vetoed")
    await until(
        lambda: second in manager.records and manager.records[second].state is EventState.FOLLOW_UP
    )

    await until(
        lambda: (
            manager.records.get(first) is None or manager.records[first].state is EventState.DROPPED
        )
    )
    assert await state_of(manager, first) is EventState.DROPPED
    assert "dropped_for_follow_up_cap" in manager.records[first].notes
    assert await state_of(manager, second) is EventState.FOLLOW_UP
    assert await state_of(manager, kept) is EventState.COMMITTED
    pending = destinations["storage0"].under("scrypted-stamina/pending/")
    assert pending and all(second in path for path in pending)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_restart_in_the_middle_of_the_window(
    hass, stamina, destinations, setup, reload
) -> None:
    clock = Clock()
    hass.states.async_set(GATE, "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(30)
    run_event(stamina, eid, "vetoed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.FOLLOW_UP
    )
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)
    until_before = manager.records[eid].follow_up_until

    await clock.advance(hass, 4 * 60)
    await reload(entry)
    manager = await wire(entry, clock)
    record = manager.records[eid]
    assert record.state is EventState.FOLLOW_UP
    assert record.follow_up_until == until_before  # the window end survived the restart

    # Evidence after the restart is still collected, and evaluation resumes on time.
    hass.states.async_set(GATE, "on", {"device_class": "door"})
    await hass.async_block_till_done()
    assert record.late
    await clock.advance(hass, 7 * 60)
    await until(lambda: eid not in manager.records)
    assert (await manager.index.async_get(eid)).state is EventState.COMMITTED
    assert destinations["storage0"].under("scrypted-stamina/unusual-unconfirmed/")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_no_drop_while_the_link_is_down(hass, stamina, destinations, setup) -> None:
    """The window closes with the link down: the drop waits for the link."""
    clock = Clock()
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(31)
    run_event(stamina, eid, "vetoed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.FOLLOW_UP
    )
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)

    stamina.stream_status = 500
    stamina.drop_streams()
    await until(lambda: not manager.link_healthy)
    await clock.advance(hass, 11 * 60)
    record = manager.records[eid]
    assert record.finalized and record.state is EventState.FOLLOW_UP
    assert destinations["storage0"].under("scrypted-stamina/pending/")

    stamina.stream_status = 200
    await until(lambda: manager.link_healthy, timeout=40)
    await until(lambda: eid not in manager.records)
    assert (await manager.index.async_get(eid)).state is EventState.DROPPED
    assert not destinations["storage0"].under("scrypted-stamina/pending/")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_uploads_off_skips_ordinary_events_but_not_unusual_ones(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.stamina_uploads"}, blocking=True
    )
    assert manager.uploads_enabled is False
    eid = event_id(40)
    run_event(stamina, eid, "confirmed")
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )
    assert "uploads_off" in manager.records[eid].notes
    assert not destinations["storage0"].files

    def provisional_unusual(record, *, now, include_late=True):
        return RelevanceResult(0.9, 3.0, 1.4, True, "rare", [])

    manager.analysis.score = provisional_unusual
    other = event_id(41)
    run_event(stamina, other, "vetoed")
    await until(
        lambda: other in manager.records and manager.records[other].state is EventState.COMMITTED
    )
    assert destinations["storage0"].under("scrypted-stamina/unusual-unconfirmed/")
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("action", [True, False])
async def test_restart_replays_a_decided_drop(hass, stamina, destinations, setup, action) -> None:
    """A DROPPING decision written down before a restart is carried out at startup."""
    from custom_components.scrypted_stamina.const import INDEX_FILENAME
    from custom_components.scrypted_stamina.decider import EventRecord
    from custom_components.scrypted_stamina.protocol import EventFacts

    from .conftest import make_entry

    entry = make_entry(hass, stamina)
    subentry = next(iter(entry.subentries))
    eid = event_id(50)
    folder = f"scrypted-stamina/pending/no-area/x_{eid}"
    destinations["storage0"].files[f"{folder}/init.mp4"] = b"init"
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    await index.async_save(
        EventRecord(
            facts=EventFacts(
                id=eid, camera=CAMERA, started=time.time() - 900, ended=time.time() - 880
            ),
            state=EventState.DROPPING,
            finalized=True,
            folder=f"x_{eid}",
            area="no-area",
        )
    )
    await index.async_save_copy(eid, subentry, Copy(folder=folder, files={"init.mp4": 4}))
    if action:
        await index.async_add_action(eid, ACTION_DROP, time.time())
    await index.async_close()

    entry = await setup(entry=entry)
    manager = entry.runtime_data.manager
    # Carried out once the link is healthy, after re-reading the event from the plugin.
    await until(lambda: eid not in manager.records)
    assert (await manager.index.async_get(eid)).state is EventState.DROPPED
    assert not destinations["storage0"].under("scrypted-stamina/pending/")
    assert ("delete_folder", folder) in destinations["storage0"].ops
    assert await manager.index.async_actions() == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_restart_orphans_events_that_can_no_longer_complete(hass, stamina, setup) -> None:
    from custom_components.scrypted_stamina.const import INDEX_FILENAME
    from custom_components.scrypted_stamina.decider import EventRecord
    from custom_components.scrypted_stamina.protocol import EventFacts

    eid = event_id(51)
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    await index.async_save(
        EventRecord(
            facts=EventFacts(id=eid, camera=CAMERA, started=time.time() - 3600),
            state=EventState.PENDING,
            folder=f"x_{eid}",
        )
    )
    await index.async_close()
    entry = await setup()
    record = await entry.runtime_data.manager.index.async_get(eid)
    assert record.state is EventState.ORPHANED
    assert "expired_during_restart" in record.notes
    await hass.config_entries.async_unload(entry.entry_id)
