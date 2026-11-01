"""Lost-footage scenarios from the adversarial review, one test each."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import time

from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.scrypted_stamina import uploader as uploader_module
from custom_components.scrypted_stamina.const import CONF_PLAYBACK_ORDER, INDEX_FILENAME, NO_AREA
from custom_components.scrypted_stamina.decider import EventRecord, EventState
from custom_components.scrypted_stamina.index import Copy, EventIndex
from custom_components.scrypted_stamina.protocol import EventFacts
from custom_components.scrypted_stamina.relevance import RelevanceResult
from custom_components.scrypted_stamina.uploader import video_playlist

from .conftest import (
    CAMERA,
    event_id,
    event_object,
    joined,
    make_entry,
    subject,
    until,
)
from .test_events import GATE, OPTIONS, Clock, run_event, wire


@pytest.fixture(autouse=True)
def quick_breaker(monkeypatch):
    """A failed destination is retried within the test's patience."""
    monkeypatch.setattr(uploader_module, "BREAKER_SECONDS", 0.01)
    monkeypatch.setattr(uploader_module, "BREAKER_MAX_SECONDS", 0.05)


def held(manager, eid):
    return manager.records.get(eid)


def state_is(manager, eid, state):
    return lambda: (record := held(manager, eid)) is not None and record.state is state


def files(fake, prefix):
    """Files under `prefix`: pending files by name, and those joined into videos."""
    return [path.rsplit("/", 1)[1] for path in fake.under(prefix)] + sorted(joined(fake, prefix))


# ------------------------------------------------------------------ 1. orphans keep uploading


async def test_an_orphan_keeps_uploading_until_the_plugin_says_it_is_done(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(100)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    await until(lambda: "00000.m4s" in files(destinations["storage0"], "scrypted-stamina/"))

    stamina.stream_status = 500
    stamina.drop_streams()
    await until(lambda: not manager.link.connected)
    # Cut while the link was down: only the event's own state says they exist.
    stamina.cut(eid, 1)
    stamina.cut(eid, 2)
    stamina.event_state[eid] = {"segments": {"live": True, "next": 3, "init": True}}
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=125))
    await until(state_is(manager, eid, EventState.ORPHANED))

    await until(lambda: "00002.m4s" in files(destinations["storage0"], "scrypted-stamina/"))
    # Still live in the plugin: not committed, not forgotten.
    assert "orphan_complete" not in held(manager, eid).notes
    assert manager.uploader.tracking(eid)

    # Open in the plugin, a released segmenter is provisional: only a closed event ends it.
    stamina.event_state[eid] = {"segments": {"live": False, "next": 3}}
    await asyncio.sleep(0.3)
    assert "orphan_complete" not in held(manager, eid).notes
    stamina.event_state[eid] = {
        "event": event_object(eid, end=time.time()),
        "segments": {"live": False, "next": 3},
    }
    await until(lambda: eid not in manager.records)
    kept = files(destinations["storage0"], "scrypted-stamina/events/")
    assert {"init.mp4", "00000.m4s", "00001.m4s", "00002.m4s"} <= set(kept)
    record = await manager.index.async_get(eid)
    assert record.state is EventState.ORPHANED and "orphan_complete" in record.notes
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_playlist_lists_only_what_the_destination_holds(
    hass, stamina, destinations, setup
) -> None:
    destinations["storage0"].fail = {"00001.m4s"}
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(101)
    run_event(stamina, eid, "confirmed", segments=3)
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )
    record = manager.records[eid]
    [copy] = (await manager.index.async_copies(eid)).values()
    assert set(copy.ranges) == {"init.mp4", "00000.m4s", "00002.m4s"}
    played = video_playlist(record, copy, "video.mp4")
    assert played.count("#EXT-X-BYTERANGE:") == 2 and "#EXT-X-DISCONTINUITY" in played
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_refusal_before_the_next_fetch_does_not_stall_the_upload(
    hass, stamina, destinations, setup
) -> None:
    # The destination refuses 00000 while the fetcher, PREFETCH ahead, has not fetched 1 yet:
    # the degraded lane must read 1 itself, not wait for a fetcher that waits for it.
    destinations["storage0"].fail = {"00000.m4s"}
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(102)
    run_event(stamina, eid, "confirmed", segments=3)
    await until(
        lambda: eid in manager.records and manager.records[eid].state is EventState.COMMITTED
    )
    [copy] = (await manager.index.async_copies(eid)).values()
    assert set(copy.ranges) == {"init.mp4", "00001.m4s", "00002.m4s"}
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 2. no drop before the replay


async def test_nothing_is_dropped_before_the_replay_is_read(
    hass, stamina, destinations, setup
) -> None:
    clock = Clock()
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(110)
    run_event(stamina, eid, "vetoed")
    await until(state_is(manager, eid, EventState.FOLLOW_UP))
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)

    stamina.replay_gate = asyncio.Event()
    stamina.drop_streams()
    # While disconnected the plugin confirms the subject after all.
    stamina.verdict(eid, subject("s1", verdict="confirmed"))
    await until(lambda: manager.link.connected and not manager.link.healthy, timeout=10)

    await clock.advance(hass, 11 * 60)  # the window closes while the replay is held
    await asyncio.sleep(0.2)
    assert held(manager, eid).state is EventState.FOLLOW_UP
    assert destinations["storage0"].under("scrypted-stamina/pending/")

    stamina.replay_gate.set()
    await until(lambda: eid not in manager.records)
    assert (await manager.index.async_get(eid)).state is EventState.COMMITTED
    assert destinations["storage0"].under("scrypted-stamina/events/")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_gap_orphans_held_events_and_recovers_missed_ones(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, Clock())
    eid, missed = event_id(111), event_id(112)
    run_event(stamina, eid, "vetoed")
    await until(state_is(manager, eid, EventState.FOLLOW_UP))
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)

    stamina.recoverable = [
        event_object(
            missed,
            subjects=[subject("s1", verdict="confirmed")],
            end=time.time(),
            segments_state="ended",
        )
    ]
    stamina.gap_next = True
    stamina.drop_streams()
    await until(lambda: (r := held(manager, eid)) is None or "stream_gap" in r.notes, timeout=10)
    record = held(manager, eid) or await manager.index.async_get(eid)
    assert record.state is EventState.ORPHANED
    # Kept where it can be found, never deleted.
    await until(lambda: bool(destinations["storage0"].videos()))
    assert {"init.mp4", "00000.m4s", "00001.m4s"} <= joined(destinations["storage0"])

    await until(lambda: missed in manager.records)
    assert "recovered_after_gap" in manager.records[missed].notes
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_drop_rereads_the_event_and_keeps_it_if_confirmed(
    hass, stamina, destinations, setup
) -> None:
    clock = Clock()
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(113)
    run_event(stamina, eid, "vetoed")
    await until(state_is(manager, eid, EventState.FOLLOW_UP))
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)
    # The plugin knows better than the stream told us.
    stamina.event_state[eid] = {
        "event": event_object(eid, subjects=[subject("s1", verdict="confirmed")], end=time.time()),
        "segments": {"live": False},
    }
    await clock.advance(hass, 11 * 60)
    await until(lambda: eid not in manager.records)
    record = await manager.index.async_get(eid)
    assert record.state is EventState.COMMITTED
    assert "confirmed_before_drop" in record.notes
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 3. DROPPING listens


async def test_a_verdict_during_dropping_keeps_the_event(
    hass, stamina, destinations, setup
) -> None:
    clock = Clock()
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(120)
    run_event(stamina, eid, "vetoed")
    await until(state_is(manager, eid, EventState.FOLLOW_UP))
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)
    stamina.event_status = 500  # the re-read before deleting cannot be done: the drop waits
    await clock.advance(hass, 11 * 60)
    await until(state_is(manager, eid, EventState.DROPPING))
    assert destinations["storage0"].under("scrypted-stamina/pending/")

    stamina.verdict(eid, subject("s1", verdict="confirmed"))
    await until(lambda: eid not in manager.records)
    assert (await manager.index.async_get(eid)).state is EventState.COMMITTED
    assert destinations["storage0"].under("scrypted-stamina/events/")
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 4. cap and late evidence


async def test_the_cap_never_evicts_an_event_with_late_evidence(
    hass, stamina, destinations, setup
) -> None:
    hass.states.async_set(GATE, "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS, follow_up_cap_gb=150 / 1024**3)
    manager = await wire(entry, Clock())

    def never_unusual(record, *, now, include_late=True):
        return RelevanceResult(0.1, -1.0, 1.4, False, "usual", [])

    manager.analysis.score = never_unusual
    first, second = event_id(130), event_id(131)
    run_event(stamina, first, "vetoed")
    await until(state_is(manager, first, EventState.FOLLOW_UP))
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)
    hass.states.async_set(GATE, "on", {"device_class": "door"})
    await hass.async_block_till_done()
    assert manager.records[first].late

    run_event(stamina, second, "vetoed")
    await until(
        lambda: (r := held(manager, second)) is None or r.state is EventState.DROPPED, timeout=10
    )
    assert manager.records[first].state is EventState.FOLLOW_UP
    pending = destinations["storage0"].under("scrypted-stamina/pending/")
    assert pending and all(first in path for path in pending)
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 7. the tail


async def test_the_last_segment_is_awaited_on_errors_until_the_plugin_says_last(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    stamina.event_status = 500  # no event state: only `last: true` can end it
    eid = event_id(140)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    stamina.end(
        eid,
        subjects=[subject("s1", verdict="confirmed")],
        next_segment=1,
        init=True,
        segments_state="running",
    )
    await asyncio.sleep(0.5)
    assert manager.records[eid].state is EventState.KEEPING  # waiting for the tail
    stamina.segment(eid, 1, last=True)
    await until(state_is(manager, eid, EventState.COMMITTED))
    assert "00001.m4s" in files(destinations["storage0"], "scrypted-stamina/events/")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_tail_gives_up_only_after_the_ttl(hass, stamina, destinations, setup) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    stamina.event_status = 500
    eid = event_id(141)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    stamina.end(
        eid, subjects=[subject("s1", verdict="confirmed")], next_segment=1, segments_state="running"
    )
    await asyncio.sleep(0.5)
    assert manager.records[eid].state is EventState.KEEPING
    manager.uploader.monotonic = lambda: time.monotonic() + 700
    manager.uploader.poke(eid)
    await until(state_is(manager, eid, EventState.COMMITTED), timeout=10)
    assert "segments_unconfirmed_ttl" in manager.records[eid].notes
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 8. handler errors, resume


async def test_a_failed_message_is_replayed_not_skipped(hass, stamina, setup) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(150)
    stamina.start(eid, subjects=[subject("s1")])
    await until(lambda: eid in manager.records)
    failures = []
    original = manager.link._on_message

    async def flaky(message):
        if message.type == "event.segment" and not failures:
            failures.append(message.seq)
            raise RuntimeError("index busy")
        await original(message)

    manager.link._on_message = flaky
    stamina.segment(eid, 0, init=True)
    await until(lambda: manager.records[eid].facts.segments == 1, timeout=10)
    assert failures and manager.link.seq >= failures[0]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_ended_event_still_pending_is_decided_on_resume(hass, stamina, setup) -> None:
    entry = make_entry(hass, stamina)
    eid = event_id(151)
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    now = time.time()
    await index.async_save(
        EventRecord(
            facts=EventFacts(id=eid, camera=CAMERA, started=now - 60, ended=now - 30),
            folder=f"x_{eid}",
        )
    )
    await index.async_close()
    entry = await setup(entry=entry)
    record = entry.runtime_data.manager.records[eid]
    assert record.state is EventState.FOLLOW_UP and record.follow_up_until is not None
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 9. uploads off, unusual late


async def test_uploads_off_keeps_segments_until_the_window_decides(
    hass, stamina, destinations, setup
) -> None:
    clock = Clock()
    hass.states.async_set(GATE, "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    await manager.async_set_uploads("unusual", NO_AREA)
    eid = event_id(160)
    run_event(stamina, eid, "vetoed")
    await until(state_is(manager, eid, EventState.FOLLOW_UP))
    # Held in pending/ while the window runs: the plugin keeps segments only for its TTL.
    await until(lambda: len(destinations["storage0"].under("scrypted-stamina/pending/")) == 3)
    assert "held_while_off" in manager.records[eid].notes

    hass.states.async_set(GATE, "on", {"device_class": "door"})
    await hass.async_block_till_done()
    await clock.advance(hass, 11 * 60)
    await until(lambda: eid not in manager.records)
    assert (await manager.index.async_get(eid)).state is EventState.COMMITTED
    kept = files(destinations["storage0"], "scrypted-stamina/unusual-unconfirmed/")
    assert {"init.mp4", "00000.m4s", "00001.m4s"} <= set(kept)
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 11. restart blind spot


async def test_changes_while_restarting_count_as_late_evidence(
    hass, stamina, destinations, setup, reload
) -> None:
    clock = Clock()
    hass.states.async_set(GATE, "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(170)
    run_event(stamina, eid, "vetoed")
    await until(state_is(manager, eid, EventState.FOLLOW_UP))
    await hass.config_entries.async_unload(entry.entry_id)
    hass.states.async_set(GATE, "on", {"device_class": "door"})  # nobody is listening
    await reload(entry)
    late = entry.runtime_data.manager.records[eid].late
    assert late and late[0]["entity"] == GATE and late[0]["while_restarting"]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_restart_alone_is_not_late_evidence(
    hass, stamina, destinations, setup, reload
) -> None:
    """After a restart every sensor's `last_changed` is new; one in its old state did nothing."""
    clock = Clock()
    hass.states.async_set(GATE, "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS)
    manager = await wire(entry, clock)
    eid = event_id(171)
    run_event(stamina, eid, "vetoed")
    await until(state_is(manager, eid, EventState.FOLLOW_UP))
    await hass.config_entries.async_unload(entry.entry_id)
    hass.states.async_remove(GATE)  # its integration restarting: back, in the same state
    hass.states.async_set(GATE, "off", {"device_class": "door"})
    await reload(entry)
    assert entry.runtime_data.manager.records[eid].late == []
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 12. predecessor


async def test_events_are_registered_for_the_predecessor_at_start(hass, stamina, setup) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    eid = event_id(180)
    stamina.start(eid, subjects=[subject("s1")])
    await until(lambda: eid in manager.records)
    assert eid in manager.analysis._recent
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 13. late scrypted_mqtt


async def test_the_link_to_scrypted_mqtt_follows_a_device_that_appears_later(
    hass, stamina, setup
) -> None:
    entry = await setup()
    entities, devices = er.async_get(hass), dr.async_get(hass)
    ours = devices.async_get(entities.async_get("event.front_door_stamina").device_id)
    assert ours.via_device_id is None

    mqtt = MockConfigEntry(domain="scrypted_mqtt")
    mqtt.add_to_hass(hass)
    garden = ar.async_get(hass).async_create("Garden")
    camera = devices.async_get_or_create(
        config_entry_id=mqtt.entry_id, identifiers={("scrypted_mqtt", "scrypted:38")}, name="Gate"
    )
    devices.async_update_device(camera.id, area_id=garden.id)
    await hass.async_block_till_done()
    ours = devices.async_get(ours.id)
    assert ours.via_device_id == camera.id and ours.area_id == garden.id
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 23. independence


async def test_a_failing_destination_does_not_hold_the_others_back(
    hass, stamina, destinations, setup
) -> None:
    destinations["storage1"].fail = {".m4s"}
    entry = await setup(destinations=2)
    manager = await wire(entry, Clock())
    eid = event_id(190)
    run_event(stamina, eid, "confirmed", segments=3)
    await until(state_is(manager, eid, EventState.COMMITTED), timeout=10)
    good = files(destinations["storage0"], "scrypted-stamina/events/")
    assert {"00000.m4s", "00001.m4s", "00002.m4s"} <= set(good)
    # Each segment came from the plugin once, however often the bad destination retried.
    for n in range(3):
        assert stamina.fetches.count((eid, f"{n}.m4s")) == 1
    target = manager.uploader.targets[list(entry.subentries)[1]]
    assert target.last_error is not None
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 22, 28. index


async def test_final_events_store_their_features_and_old_rows_are_pruned(hass) -> None:
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    now = time.time()
    old_dropped = EventRecord(
        facts=EventFacts(id="ev_a", camera=CAMERA, started=now - 30 * 86400, ended=now),
        state=EventState.DROPPED,
        finalized=True,
    )
    past_history = EventRecord(
        facts=EventFacts(id="ev_d", camera=CAMERA, started=now - 200 * 86400, ended=now),
        state=EventState.DROPPED,
        finalized=True,
    )
    old_kept = EventRecord(
        facts=EventFacts(id="ev_b", camera=CAMERA, started=now - 400 * 86400, ended=now),
        state=EventState.COMMITTED,
        finalized=True,
    )
    recent = EventRecord(
        facts=EventFacts(id="ev_c", camera=CAMERA, started=now, ended=now),
        state=EventState.COMMITTED,
        finalized=True,
    )
    await index.async_save(old_dropped)
    await index.async_save(past_history)
    await index.async_save(old_kept)
    await index.async_save(recent, features=json.dumps({"x": 1}))
    await index.async_save_copy("ev_b", "dest", Copy(folder="f", files={"init.mp4": 3}))

    rows = await hass.async_add_executor_job(index.read_learning_rows, now - 1)
    assert rows == [("ev_c", True, '{"x": 1}', "", 1)]
    pruned = await index.async_prune(
        history_before=now - 180 * 86400,
        fresh_before=now - 30 * 86400,
        thumbnails_before=now - 30 * 86400,
        dropped_thumbnails_before=now - 7 * 86400,
    )
    assert pruned.deleted == 1
    assert await index.async_get("ev_d") is None
    assert await index.async_get("ev_a") is not None  # dropped history stays now
    assert await index.async_get("ev_b") is not None  # its files are still out there
    await index.async_close()


# ------------------------------------------------------------------ plugin final pass


async def test_a_released_segmenter_that_resumes_is_followed_with_a_new_init(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(200)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    # Released while the event is open: provisional, nothing is final yet.
    stamina.update(
        eid, subjects=[subject("s1")], next_segment=1, init=True, segments_state="released"
    )
    await until(lambda: "00000.m4s" in files(destinations["storage0"], "scrypted-stamina/"))
    assert not manager.records[eid].facts.segments_final
    # It resumes after an ffmpeg restart: a discontinuity, and a new init.
    stamina.restart_segmenter(eid, 1, k=1)
    stamina.end(eid, subjects=[subject("s1", verdict="confirmed")], next_segment=2, init=True)
    stamina.last(eid, 2)
    await until(state_is(manager, eid, EventState.COMMITTED))

    fake = destinations["storage0"]
    kept = files(fake, "scrypted-stamina/events/")
    assert {"init.mp4", "init-1.mp4", "00000.m4s", "00001.m4s"} <= set(kept)
    [copy] = (await manager.index.async_copies(eid)).values()
    # Joined in play order: each init before the segments that need it.
    order = sorted(copy.ranges, key=lambda name: copy.ranges[name][0])
    assert order == ["init.mp4", "00000.m4s", "init-1.mp4", "00001.m4s"]
    lines = video_playlist(manager.records[eid], copy, "v.mp4").splitlines()
    at = lines.index(
        f"#EXT-X-BYTERANGE:{copy.ranges['00001.m4s'][1]}@{copy.ranges['00001.m4s'][0]}"
    )
    offset, length, _ = copy.ranges["init-1.mp4"]
    assert lines[at - 3 : at - 1] == [
        "#EXT-X-DISCONTINUITY",
        f'#EXT-X-MAP:URI="v.mp4",BYTERANGE="{length}@{offset}"',
    ]
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_evicted_batch_becomes_gaps(hass, stamina, destinations, setup) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(201)
    stamina.start(eid, subjects=[subject("s1")])
    for n in range(4):
        stamina.cut(eid, n, init=n == 0)
    stamina.not_ready.update({(eid, "1.m4s"), (eid, "2.m4s")})
    for n in range(4):
        stamina.push(
            "event.segment",
            event_id=eid,
            camera_id=CAMERA,
            kind="cut",
            last=False,
            segment={"n": n, "init": 0, "discontinuity": False, "duration_s": 2.0},
        )
    await until(lambda: "00000.m4s" in files(destinations["storage0"], "scrypted-stamina/"))
    stamina.evict(eid, [1, 2])  # one batch, before they could be fetched
    stamina.not_ready.clear()
    stamina.end(eid, subjects=[subject("s1", verdict="confirmed")], next_segment=4, init=True)
    stamina.last(eid, 4)
    await until(state_is(manager, eid, EventState.COMMITTED))
    record = manager.records[eid]
    assert sorted(record.gaps) == [1, 2]
    kept = files(destinations["storage0"], "scrypted-stamina/events/")
    assert "00003.m4s" in kept and "00001.m4s" not in kept
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_stopped_playback_session_is_started_again(
    hass, hass_client_no_auth, stamina, setup
) -> None:
    from homeassistant.components.media_source import async_resolve_media
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "media_source", {})
    entry = await setup(options={CONF_PLAYBACK_ORDER: "recording_first"})
    manager = await wire(entry, Clock())
    eid = event_id(202)
    run_event(stamina, eid, "confirmed")
    await until(state_is(manager, eid, EventState.COMMITTED))
    played = await async_resolve_media(
        hass, f"media-source://scrypted_stamina/{CAMERA}/x/{eid}", None
    )
    client = await hass_client_no_auth()
    first = await client.get(played.url)
    assert first.status == 200 and "/playback/pb_" in str(first.url)
    session_url = str(first.url.relative())

    stamina.stopped_sessions.add(f"pb_{1:022d}")  # stopped after two minutes paused
    again = await client.get(session_url)
    assert again.status == 200
    assert f"pb_{2:022d}" in str(again.url)  # a new session, for the same event
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ panel support


async def test_a_page_of_events_is_filtered_and_ordered_in_sql(hass) -> None:
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    base = time.time()

    def item(n: int, camera: str, cls: str, unusual: bool, state: EventState, started: float):
        from custom_components.scrypted_stamina.protocol import Subject

        record = EventRecord(
            facts=EventFacts(
                id=f"ev_{n}",
                camera=camera,
                started=started,
                ended=started + 5,
                subjects={"s1": Subject(id="s1", cls=cls)},
            ),
            state=state,
            area="garden",
            unusual_final=unusual,
        )
        return record

    records = [
        item(1, "38", "person", True, EventState.COMMITTED, base - 50),
        item(2, "38", "vehicle", False, EventState.COMMITTED, base - 40),
        item(3, "40", "person", False, EventState.DROPPED, base - 30),
        item(4, "40", "person", False, EventState.COMMITTED, base - 20),
        item(5, "38", "person", False, EventState.COMMITTED, base - 20),  # same start as 4
    ]
    await index.async_save_many([(record, None) for record in records])
    await index.async_save_thumbnail("ev_5", b"jpeg")

    rows, cursor = await index.async_page(limit=2)
    assert [row.id for row in rows] == ["ev_5", "ev_4"] and cursor == (base - 20, "ev_4")
    assert rows[0].thumbnail and not rows[1].thumbnail
    assert rows[0].area == "garden" and rows[0].duration == 5 and rows[0].classes == ["person"]
    rows, cursor = await index.async_page(limit=2, before=cursor)
    assert [row.id for row in rows] == ["ev_2", "ev_1"]
    rows, _ = await index.async_page(limit=2, before=cursor)
    assert rows == []

    rows, _ = await index.async_page(include_dropped=True, cameras=["40"])
    assert [row.id for row in rows] == ["ev_4", "ev_3"]
    rows, _ = await index.async_page(unusual=True)
    assert [row.id for row in rows] == ["ev_1"]
    rows, _ = await index.async_page(cls="vehicle")
    assert [row.id for row in rows] == ["ev_2"]
    assert (await index.async_page(cameras=[]))[0] == []
    await index.async_close()


async def test_thumbnails_cache_privately_and_play_urls_are_short_lived(
    hass, hass_client_no_auth, stamina, setup
) -> None:
    from urllib.parse import parse_qs, urlsplit

    from custom_components.scrypted_stamina.playback import play_url, thumbnail_url

    entry = await setup()
    await entry.runtime_data.index.async_save_thumbnail("ev_x", b"jpeg")
    client = await hass_client_no_auth()
    response = await client.get(thumbnail_url(hass, entry.entry_id, "ev_x"))
    assert response.headers["Cache-Control"] == "private, max-age=3600"
    expires = int(parse_qs(urlsplit(play_url(hass, entry.entry_id, "ev_x")).query)["exp"][0])
    assert expires - time.time() <= 3600
    await hass.config_entries.async_unload(entry.entry_id)
