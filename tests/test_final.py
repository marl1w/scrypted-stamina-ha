"""Final review round: one test per remaining finding."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from custom_components.scrypted_stamina import uploader as uploader_module
from custom_components.scrypted_stamina.const import INDEX_FILENAME
from custom_components.scrypted_stamina.decider import EventRecord, EventState
from custom_components.scrypted_stamina.destinations import DestinationError
from custom_components.scrypted_stamina.index import Copy, EventIndex
from custom_components.scrypted_stamina.protocol import EventFacts
from custom_components.scrypted_stamina.relevance import RelevanceResult
from custom_components.scrypted_stamina.uploader import Target, Uploader, video_playlist

from .conftest import (
    CAMERA,
    FakeDestination,
    event_id,
    event_object,
    joined,
    make_entry,
    subject,
    until,
)
from .test_events import Clock, run_event, wire


@pytest.fixture(autouse=True)
def quick_breaker(monkeypatch):
    monkeypatch.setattr(uploader_module, "BREAKER_SECONDS", 0.05)
    monkeypatch.setattr(uploader_module, "BREAKER_MAX_SECONDS", 0.1)


def state_is(manager, eid, state):
    return lambda: (r := manager.records.get(eid)) is not None and r.state is state


def names(fake, prefix):
    """Files under `prefix`: pending files by name, and those joined into videos."""
    return {path.rsplit("/", 1)[1] for path in fake.under(prefix)} | joined(fake, prefix)


# ------------------------------------------------------------------ 2, 3: ring and breaker


def test_the_breaker_counts_only_closed_failures_and_probes_once() -> None:
    target = Target("x", FakeDestination(), "f", 1, 0, 1)
    assert target.admit(0.0) == "closed"
    assert target.failed(Exception("down"), "closed", 0.0)
    assert target.degraded and target.admit(0.0) is None  # open
    # A failure of an attempt started before it opened does not count again.
    assert not target.failed(Exception("down"), None, 0.0)
    assert target.failures == 1
    later = target.degraded_until + 1
    assert target.admit(later) == "probe"
    assert target.admit(later) is None  # one probe at a time
    target.succeeded()
    assert target.admit(later) == "closed"


async def test_a_lagging_destination_catches_up_from_the_plugin(
    hass, stamina, destinations, setup, monkeypatch
) -> None:
    """The plugin is the buffer: a destination that was down gets everything afterwards."""
    monkeypatch.setattr(uploader_module, "BREAKER_SECONDS", 1.0)
    monkeypatch.setattr(uploader_module, "BREAKER_MAX_SECONDS", 1.0)
    destinations["storage1"].fail = {".m4s"}
    entry = await setup(destinations=2)
    manager = await wire(entry, Clock())
    eid = event_id(300)
    run_event(stamina, eid, "confirmed", segments=4)
    await until(lambda: len(names(destinations["storage0"], "scrypted-stamina/")) >= 5)
    assert not [ack for ack in stamina.acks if ack[0] == eid]  # held for the slow one
    destinations["storage1"].fail = set()
    await until(state_is(manager, eid, EventState.COMMITTED), timeout=10)
    for fake in destinations.values():
        assert {f"0000{n}.m4s" for n in range(4)} <= names(fake, "scrypted-stamina/events/")
    copies = await manager.index.async_copies(eid)
    assert all(not copy.failed for copy in copies.values())
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 5: orphans


async def test_an_orphan_not_known_closed_is_never_ended_by_silence(
    hass, stamina, destinations, setup
) -> None:
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(310)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    await until(lambda: "00000.m4s" in names(destinations["storage0"], "scrypted-stamina/"))
    stamina.stream_status = 500
    stamina.event_status = 500
    stamina.drop_streams()
    await until(lambda: not manager.link.connected)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=125))
    await until(state_is(manager, eid, EventState.ORPHANED))
    manager.uploader.monotonic = lambda: time.monotonic() + 3600  # an hour of silence
    manager.uploader.poke(eid)
    await asyncio.sleep(0.3)
    record = manager.records[eid]
    assert not record.facts.segments_final and "orphan_complete" not in record.notes
    assert destinations["storage0"].under("scrypted-stamina/pending/")  # kept, uncommitted
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_committed_orphan_that_gets_more_segments_is_committed_again(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(311)
    stamina.start(eid, subjects=[subject("s1")])
    stamina.segment(eid, 0, init=True)
    await until(lambda: "00000.m4s" in names(destinations["storage0"], "scrypted-stamina/"))
    stamina.event_state[eid] = {
        "event": event_object(eid, end=time.time()),
        "segments": {"live": False, "next": 1},
    }
    stamina.gap_next = True
    stamina.drop_streams()
    await until(lambda: eid not in manager.records, timeout=10)  # orphaned, committed, done
    fake = destinations["storage0"]
    [video] = fake.videos()
    first = fake.files[video]

    before = len(fake.ops)
    stamina.segment(eid, 1)  # the plugin had more after all
    await until(lambda: "00001.m4s" in names(fake, "scrypted-stamina/events/"))
    await until(lambda: eid not in manager.records, timeout=10)
    # The committed video is never rewritten: what came after is a file of its own.
    part2 = video.removesuffix(".mp4") + "-part2.mp4"
    assert sorted(fake.videos()) == sorted([video, part2])
    assert fake.files[video] == first and ("store", video) not in fake.ops[before:]
    assert fake.files[part2] == stamina.files[(eid, "1.m4s")]
    assert not fake.under("scrypted-stamina/pending/")
    assert not any(op == "read" for op, _ in fake.ops)  # streamed from the spool
    [copy] = (await manager.index.async_copies(eid)).values()
    assert copy.ranges["00001.m4s"] == [0, len(stamina.files[(eid, "1.m4s")]), 2]
    assert copy.part_path(2) == part2 and copy.part_size(1) == len(first)
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 6: resume, skipped


async def test_a_record_that_fails_to_resume_is_kept_as_orphaned(hass, stamina, setup) -> None:
    from unittest.mock import patch

    entry = make_entry(hass, stamina)
    eid = event_id(320)
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    now = time.time()
    await index.async_save(
        EventRecord(facts=EventFacts(id=eid, camera=CAMERA, started=now - 30), folder="x")
    )
    await index.async_close()
    with (
        patch(
            "custom_components.scrypted_stamina.manager.StaminaManager._catch_up_late",
            side_effect=RuntimeError("bad row"),
        ),
        patch(
            "custom_components.scrypted_stamina.manager.StaminaManager._async_resume_record",
            side_effect=RuntimeError("bad row"),
        ),
    ):
        entry = await setup(entry=entry)
    record = await entry.runtime_data.index.async_get(eid)
    assert record.state is EventState.ORPHANED and "resume_failed" in record.notes
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_skipped_message_orphans_its_event(hass, stamina, setup) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    eid = event_id(321)
    stamina.start(eid, subjects=[subject("s1")])
    await until(lambda: eid in manager.records)
    original = manager.link._on_message

    async def broken(message):
        if message.type == "event.segment":
            raise RuntimeError("always")
        await original(message)

    manager.link._on_message = broken
    stamina.segment(eid, 0, init=True)
    await until(state_is(manager, eid, EventState.ORPHANED), timeout=15)
    assert "message_skipped" in manager.records[eid].notes
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 7, 8, 18: the index


async def test_records_and_position_land_together_on_the_index_thread(hass) -> None:
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    record = EventRecord(facts=EventFacts(id="ev_p", camera=CAMERA, started=1.0))
    await index.async_save_with_position([(record, None)], {"seq": "41", "epoch": "ep"})
    assert await index.async_get("ev_p") is not None
    assert await index.async_get_meta("seq") == "41"
    thread = await index._run(lambda db: threading.current_thread().name)
    assert thread.startswith("scrypted_stamina_db")
    await index.async_close()


async def test_legacy_rows_get_their_features_written_back(hass, stamina, setup) -> None:
    entry = make_entry(hass, stamina)
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    record = EventRecord(
        facts=EventFacts(id="ev_old", camera=CAMERA, started=time.time() - 3600, ended=time.time()),
        state=EventState.COMMITTED,
        finalized=True,
    )
    await index.async_save(record)  # no features: written before they were stored
    await index.async_close()
    entry = await setup(entry=entry)
    manager = entry.runtime_data.manager
    await manager.analysis.async_rebuild()
    rows = await hass.async_add_executor_job(manager.index.read_learning_rows, 0)
    assert rows[0][0] == "ev_old" and rows[0][2] is not None
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 9: orphan retention


async def test_retention_never_removes_an_unsettled_orphan(hass) -> None:
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    fake = FakeDestination()
    target = Target("dest", fake, "f", 10**9, 0, 10**9, orphan_retention_days=1)
    old = time.time() - 5 * 86400
    for eid, committed in (("ev_pending", False), ("ev_done", True)):
        await index.async_save(
            EventRecord(
                facts=EventFacts(id=eid, camera=CAMERA, started=old),
                state=EventState.ORPHANED,
            )
        )
        await index.async_save_copy(
            eid, "dest", Copy(folder=f"f/{eid}", files={"a": 1}, committed=committed)
        )
        target.usage.set(eid, old, EventState.ORPHANED, 1)
    uploader = Uploader(
        hass,
        None,
        index,
        {"dest": target},
        on_change=lambda: None,
        on_uploaded=lambda r: None,
        on_latency=print,
    )

    async def settled(event_id):
        return event_id == "ev_done"

    await uploader.async_retention(time.time(), settled)
    assert target.usage.get("ev_pending") is not None
    assert target.usage.get("ev_done") is None
    await index.async_close()


# ------------------------------------------------------------------ 11: manifest, copy


def test_the_playlist_leaves_out_segments_without_their_init() -> None:
    record = EventRecord(facts=EventFacts(id="e", camera=CAMERA, started=1.0, init_of={1: 1}))
    copy = Copy(
        folder="f",
        video="f/events/v.mp4",
        committed=True,
        ranges={"init.mp4": [0, 4], "00000.m4s": [4, 10], "00001.m4s": [14, 10]},
    )
    text = video_playlist(record, copy, "video.mp4")
    assert '#EXT-X-MAP:URI="video.mp4",BYTERANGE="4@0"' in text
    assert "#EXT-X-BYTERANGE:10@4" in text and "@14" not in text  # init-1.mp4 is not there


class SavedCopies:
    """The index calls a commit makes, remembered."""

    def __init__(self) -> None:
        self.saved: list[Copy] = []
        self.deleted = False  # an eviction got there first

    async def async_save_copy(self, event_id: str, destination: str, copy: Copy) -> bool:
        if self.deleted:
            return False
        self.saved.append(Copy.from_header(copy.header(), dict(copy.files)))
        return True

    async_save_copy_header = async_save_copy

    async def async_video_taken(self, destination: str, path: str, event_id: str) -> bool:
        return False


def bare_uploader(hass, index=None) -> Uploader:
    return Uploader(
        hass, None, index, {}, on_change=lambda: None, on_uploaded=lambda r: None, on_latency=print
    )


def fake_target(fake: FakeDestination) -> Target:
    return Target(
        subentry_id="dest",
        destination=fake,
        folder="f",
        quota=10**9,
        retention_days=0,
        follow_up_cap=10**9,
    )


async def test_a_commit_joins_everything_in_the_pending_folder(hass) -> None:
    fake = FakeDestination()
    index = SavedCopies()
    record = EventRecord(facts=EventFacts(id="e", camera=CAMERA, started=1.0, ended=2.0))
    source, video = "f/pending/a/x", "f/events/a-cam-261031_211349.mp4"
    fake.files[f"{source}/00001.m4s"] = b"t"  # uploaded, but its row never written
    fake.files[f"{source}/00000.m4s"] = b"s"
    fake.files[f"{source}/init.mp4"] = b"i"
    fake.files[f"{source}/notes.txt"] = b"?"  # not footage: not joined
    copy = Copy(folder=source, files={"init.mp4": 1, "00000.m4s": 1}, video=video)
    await bare_uploader(hass, index)._async_commit_copy(fake_target(fake), record, copy)
    assert fake.files[video] == b"ist" and not fake.under(source)
    assert copy.files["00001.m4s"] == 1 and copy.committed and copy.video == video
    assert index.saved[-1].ranges == copy.ranges  # recorded before the folder went


async def test_a_commit_repeated_after_a_crash_does_not_join_twice(hass) -> None:
    fake = FakeDestination()
    record = EventRecord(facts=EventFacts(id="e", camera=CAMERA, started=1.0, ended=2.0))
    source, video = "f/pending/a/x", "f/events/a-cam-261031_211349.mp4"
    for name, data in (("init.mp4", b"i"), ("00000.m4s", b"s")):
        fake.files[f"{source}/{name}"] = data
    copy = Copy(folder=source, files={"init.mp4": 1, "00000.m4s": 1}, video=video)
    uploader, target = bare_uploader(hass, SavedCopies()), fake_target(fake)
    # Stored in part, then a crash before the index recorded it: written again, whole.
    fake.files[video] = b"i"
    await uploader._async_commit_copy(target, record, copy)
    assert fake.files[video] == b"is"
    # Recorded, then a crash before the folder was deleted: nothing joined twice.
    fake.files[f"{source}/00000.m4s"] = b"s"
    fake.ops.clear()
    await uploader._async_commit_copy(target, record, copy)
    assert fake.files[video] == b"is" and not fake.under(source)
    assert not any(op in ("store", "read") for op, _ in fake.ops)
    # More of an orphan arrives after its commit: a file of its own, the video untouched.
    fake.files[f"{source}/00001.m4s"] = b"t"
    copy.files["00001.m4s"] = 1
    fake.ops.clear()
    await uploader._async_commit_copy(target, record, copy)
    part2 = "f/events/a-cam-261031_211349-part2.mp4"
    assert fake.files[video] == b"is" and fake.files[part2] == b"t"
    assert [path for op, path in fake.ops if op == "store"] == [part2]
    assert ("read", video) not in fake.ops
    assert copy.ranges == {
        "init.mp4": [0, 1, 1],
        "00000.m4s": [1, 1, 1],
        "00001.m4s": [0, 1, 2],
    }


async def test_a_pending_folder_missing_an_uploaded_file_is_kept(hass) -> None:
    fake = FakeDestination()
    record = EventRecord(facts=EventFacts(id="e", camera=CAMERA, started=1.0, ended=2.0))
    source = "f/pending/a/x"
    fake.files[f"{source}/init.mp4"] = b"i"
    # The index says 00000.m4s was stored, but the listing does not show it.
    copy = Copy(folder=source, files={"init.mp4": 1, "00000.m4s": 1}, video="f/events/v.mp4")
    await bare_uploader(hass, SavedCopies())._async_commit_copy(fake_target(fake), record, copy)
    assert fake.files["f/events/v.mp4"] == b"i"
    assert not any(op == "delete_folder" for op, _ in fake.ops)


async def test_a_video_that_did_not_land_whole_is_not_committed(hass) -> None:
    class Short(FakeDestination):
        async def async_store(self, path: str, data: bytes) -> None:
            await super().async_store(path, data[:-1])

    fake = Short()
    record = EventRecord(facts=EventFacts(id="e", camera=CAMERA, started=1.0, ended=2.0))
    source = "f/pending/a/x"
    fake.files[f"{source}/init.mp4"] = b"i"
    fake.files[f"{source}/00000.m4s"] = b"s"
    copy = Copy(folder=source, files={"init.mp4": 1, "00000.m4s": 1}, video="f/events/v.mp4")
    with pytest.raises(DestinationError):
        await bare_uploader(hass, SavedCopies())._async_commit_copy(fake_target(fake), record, copy)
    assert fake.under(source) and not copy.committed  # the segments are still there


async def test_two_events_starting_in_the_same_second_get_two_videos(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = await wire(entry, Clock())
    first, second = event_id(320), event_id(321)
    start = time.time()
    for eid in (first, second):
        stamina.start(eid, subjects=[subject("s1")], start=start)
        stamina.segment(eid, 0, init=True)
        stamina.verdict(eid, subject("s1", verdict="confirmed"))
        stamina.end(eid, subjects=[subject("s1", verdict="confirmed")], next_segment=1, init=True)
    for eid in (first, second):
        await until(state_is(manager, eid, EventState.COMMITTED))
    fake = destinations["storage0"]
    videos = fake.videos()
    one = next(video for video in videos if not video.endswith("-2.mp4"))
    assert sorted(videos) == sorted([one, one.removesuffix(".mp4") + "-2.mp4"])
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 13: derived URLs


async def test_derived_urls_never_outlive_their_parent(
    hass, hass_client_no_auth, stamina, setup
) -> None:
    from urllib.parse import parse_qs, urlsplit

    from custom_components.scrypted_stamina.playback import proxy_url, sign

    entry = await setup()
    soon = int(time.time()) + 60
    parent = proxy_url(hass, entry.entry_id, "/v1/cameras/38/playback.m3u8?t=1", cap=soon)
    assert int(parse_qs(urlsplit(parent).query)["exp"][0]) == soon
    client = await hass_client_no_auth()
    text = await (await client.get(parent)).text()
    for line in text.splitlines():
        if line.startswith("/api/"):
            assert int(parse_qs(urlsplit(line).query)["exp"][0]) <= soon
    expired = sign(
        hass,
        f"/api/scrypted_stamina/proxy/{entry.entry_id}/v1/cameras/38/playback.m3u8",
        "t=1",
        lifetime=-1,
    )
    assert (await client.get(expired)).status == 403
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 20: truncated streams


async def test_a_cut_upstream_body_is_not_served_as_a_complete_200(
    hass, hass_client_no_auth, stamina, setup
) -> None:
    import aiohttp

    from custom_components.scrypted_stamina.playback import proxy_url

    entry = await setup()
    client = await hass_client_no_auth()
    url = proxy_url(hass, entry.entry_id, f"/v1/cameras/38/playback/pb_{'a' * 22}/cut.m4s")
    with pytest.raises((aiohttp.ClientError, asyncio.TimeoutError)):
        response = await client.get(url)
        await response.read()
    await hass.config_entries.async_unload(entry.entry_id)


# ------------------------------------------------------------------ 19, 21: churn


async def test_segments_do_not_rescore_and_unusual_turns_on_at_once(hass, stamina, setup) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    calls = []

    def unusual(record, *, now, include_late=True):
        calls.append(record.id)
        return RelevanceResult(0.9, 3.0, 1.4, True, "rare", [])

    manager.analysis.score = unusual
    await asyncio.sleep(1.2)  # idle: the next change is flushed on its leading edge
    eid = event_id(330)
    stamina.start(eid, subjects=[subject("s1")])
    await until(lambda: eid in manager.records)
    await until(lambda: hass.states.get("binary_sensor.front_door_unusual").state == "on", 0.5)
    before = len(calls)
    for n in range(3):
        stamina.segment(eid, n, init=n == 0)
    await until(lambda: manager.records[eid].facts.segments == 3)
    assert len(calls) == before
    await hass.config_entries.async_unload(entry.entry_id)


async def test_backoff_never_sleeps_inside_an_upload_slot() -> None:
    """Segment stores make one attempt each: the lane retries with its slot released."""
    from custom_components.scrypted_stamina.destinations.base import (
        DestinationError,
        TransientError,
        async_attempt,
        attempts_limited,
    )

    calls = []

    async def flaky():
        calls.append(1)
        raise TransientError("busy")

    with attempts_limited(1), pytest.raises(DestinationError):
        await async_attempt("store", flaky)
    assert calls == [1]
