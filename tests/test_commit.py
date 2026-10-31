"""The commit path: streamed from the spool, per destination, never rewriting a video."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
import json
import os
import sqlite3
import time
from unittest.mock import MagicMock

import pytest

from custom_components.scrypted_stamina.decider import EventRecord, EventState
from custom_components.scrypted_stamina.destinations import (
    DestinationError,
    OneDriveDestination,
    UploadTooLargeError,
    onedrive,
)
from custom_components.scrypted_stamina.destinations.base import gather
from custom_components.scrypted_stamina.index import Copy, EventIndex
from custom_components.scrypted_stamina.protocol import EventFacts
from custom_components.scrypted_stamina.spool import Spool
from custom_components.scrypted_stamina.uploader import Target, Uploader, video_playlist

from .conftest import CAMERA, FakeDestination
from .test_destinations import loaded

EVENT = "ev_commit"
NAME = "garden-front-door-261031_211349.mp4"
VIDEO = f"f/events/{NAME}"
PENDING = "f/pending/garden/261031_211349_front-door_ev_commit"
SEGMENTS = {"init.mp4": b"init", "00000.m4s": b"seg0", "00001.m4s": b"seg1"}


def make_record() -> EventRecord:
    return EventRecord(
        facts=EventFacts(id=EVENT, camera=CAMERA, started=time.time(), ended=time.time()),
        state=EventState.KEEPING,
        area="garden",
        camera_name="Front door",
        folder="261031_211349_front-door_ev_commit",
        file_name=NAME,
    )


def make_target(fake: FakeDestination, subentry_id: str = "a") -> Target:
    return Target(
        subentry_id=subentry_id,
        destination=fake,
        folder="f",
        quota=10**9,
        retention_days=0,
        follow_up_cap=10**9,
    )


@pytest.fixture
async def index(hass, tmp_path):
    index = EventIndex(hass, tmp_path / "index.db")
    await index.async_open()
    yield index
    await index.async_close()


async def prepared(
    hass, index: EventIndex, fakes: dict[str, FakeDestination], segments=SEGMENTS
) -> Uploader:
    """An uploader with every segment uploaded to every destination and spooled."""
    uploader = Uploader(
        hass,
        None,
        index,
        {sid: make_target(fake, sid) for sid, fake in fakes.items()},
        on_change=lambda: None,
        on_uploaded=lambda record: None,
        on_latency=lambda latency: None,
    )
    for name, data in segments.items():
        await uploader.spool.async_write(EVENT, name, data)
    for sid, fake in fakes.items():
        for name, data in segments.items():
            fake.files[f"{PENDING}/{name}"] = data
        await index.async_save_copy(
            EVENT,
            sid,
            Copy(folder=PENDING, files={name: len(data) for name, data in segments.items()}),
        )
    return uploader


async def copy_of(index: EventIndex, sid: str = "a") -> Copy:
    return (await index.async_copies(EVENT))[sid]


class Gate(FakeDestination):
    """A destination whose next streamed upload waits until it is let through."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def async_store_stream(self, path: str, size: int, chunks: AsyncIterable[bytes]) -> None:
        self.started.set()
        await self.release.wait()
        await super().async_store_stream(path, size, chunks)


# ------------------------------------------------------------------ streamed, no read-back


async def test_a_commit_streams_from_the_spool_and_reads_nothing_back(hass, index) -> None:
    fake = FakeDestination()
    uploader = await prepared(hass, index, {"a": fake})
    record = make_record()
    await uploader.async_commit(record)
    assert fake.files[VIDEO] == b"initseg0seg1"
    assert not any(op == "read" for op, _ in fake.ops)
    assert not fake.under(PENDING)
    assert not uploader.spool.folder(EVENT).exists()  # the spool goes with the commit
    copy = await copy_of(index)
    assert copy.video == VIDEO and copy.parts == [1] and copy.part_size(1) == 12
    assert record.video_path == f"events/{NAME}"


async def test_without_the_spool_each_segment_is_read_back_once(hass, index) -> None:
    fake = FakeDestination()
    uploader = await prepared(hass, index, {"a": fake})
    await uploader.spool.async_discard(EVENT)  # a new box: nothing local
    await uploader.async_commit(make_record())
    assert fake.files[VIDEO] == b"initseg0seg1"
    assert sorted(path for op, path in fake.ops if op == "read") == sorted(
        f"{PENDING}/{name}" for name in SEGMENTS
    )


# ------------------------------------------------------------------ append after commit


async def test_what_arrives_after_a_commit_is_a_new_file_and_the_video_is_untouched(
    hass, index
) -> None:
    fake = FakeDestination()
    uploader = await prepared(hass, index, {"a": fake})
    record = make_record()
    await uploader.async_commit(record)
    first = fake.files[VIDEO]

    # An orphan's later segment: uploaded to the pending folder and spooled again.
    fake.files[f"{PENDING}/00002.m4s"] = b"seg2"
    await index.async_add_file(EVENT, "a", "00002.m4s", 4)
    await uploader.spool.async_write(EVENT, "00002.m4s", b"seg2")
    fake.ops.clear()
    await uploader.async_commit(record)

    part2 = VIDEO.removesuffix(".mp4") + "-part2.mp4"
    assert fake.files[VIDEO] == first and fake.files[part2] == b"seg2"
    assert [path for op, path in fake.ops if op in ("store", "read")] == [part2]
    copy = await copy_of(index)
    assert copy.ranges["00002.m4s"] == [0, 4, 2] and copy.parts == [1, 2]
    text = video_playlist(record, copy, lambda part: f"video-{part}")
    assert "#EXT-X-BYTERANGE:4@0\nvideo-2" in text and "#EXT-X-BYTERANGE:4@4\nvideo-1" in text


# ------------------------------------------------------------------ crashes


async def test_a_crash_mid_upload_loses_nothing_and_the_retry_completes(hass, index) -> None:
    class Crashing(FakeDestination):
        crash = True

        async def async_store_stream(self, path, size, chunks) -> None:
            if self.crash:
                self.files[path] = (await gather(chunks, size))[:5]  # a torn upload
                raise DestinationError("connection lost")
            await super().async_store_stream(path, size, chunks)

    fake = Crashing()
    uploader = await prepared(hass, index, {"a": fake})
    record = make_record()
    with pytest.raises(DestinationError):
        await uploader.async_commit(record)
    copy = await copy_of(index)
    assert copy.video == VIDEO and not copy.committed and not copy.ranges  # reserved only
    assert len(fake.under(PENDING)) == 3 and uploader.spool.folder(EVENT).exists()

    fake.crash = False
    await uploader.async_commit(record)
    assert fake.files[VIDEO] == b"initseg0seg1" and not fake.under(PENDING)
    assert (await copy_of(index)).committed


async def test_a_crash_before_recording_rewrites_only_the_file_not_recorded(hass, index) -> None:
    fake = FakeDestination()
    uploader = await prepared(hass, index, {"a": fake})
    record = make_record()
    await uploader.async_commit(record)
    first = fake.files[VIDEO]

    await index.async_add_file(EVENT, "a", "00002.m4s", 4)
    fake.files[f"{PENDING}/00002.m4s"] = b"seg2"
    await uploader.spool.async_write(EVENT, "00002.m4s", b"seg2")
    saved = index.async_save_copy

    async def crash(*args):
        raise RuntimeError("power cut")

    index.async_save_copy = crash
    uploader._pumps.clear()
    with pytest.raises(RuntimeError):
        await uploader.async_commit(record)
    index.async_save_copy = saved
    part2 = VIDEO.removesuffix(".mp4") + "-part2.mp4"
    fake.files[part2] = b"torn"  # whatever the crash left there was never recorded
    await uploader.async_commit(record)
    assert fake.files[VIDEO] == first and fake.files[part2] == b"seg2"
    assert (await copy_of(index)).ranges["00002.m4s"] == [0, 4, 2]


# ------------------------------------------------------------------ per destination


async def test_one_destination_failing_does_not_hold_back_another(hass, index) -> None:
    good, bad = FakeDestination(name="good"), FakeDestination(name="bad")
    bad.fail = {"/events/"}
    uploader = await prepared(hass, index, {"a": good, "b": bad})
    record = make_record()
    with pytest.raises(DestinationError, match="bad"):
        await uploader.async_commit(record)
    assert good.files[VIDEO] == b"initseg0seg1" and not good.under(PENDING)
    assert (await copy_of(index, "a")).committed and not (await copy_of(index, "b")).committed
    assert uploader.spool.folder(EVENT).exists()  # still needed for the one that failed
    assert uploader.targets["b"].last_error

    bad.fail = set()
    good.ops.clear()
    await uploader.async_commit(record)
    assert bad.files[VIDEO] == b"initseg0seg1"
    assert not any(op in ("store", "read") for op, _ in good.ops)  # nothing done twice
    assert not uploader.spool.folder(EVENT).exists()


async def test_a_video_a_destination_can_never_take_is_not_retried(hass, index) -> None:
    class Small(FakeDestination):
        async def async_store_stream(self, path, size, chunks) -> None:
            self.ops.append(("stream", path))
            raise UploadTooLargeError(f"{size} bytes is too large")

    small, good = Small(name="small"), FakeDestination(name="good")
    uploader = await prepared(hass, index, {"a": small, "b": good})
    record = make_record()
    await uploader.async_commit(record)  # not raised: retrying would not help

    copy = await copy_of(index, "a")
    target = uploader.targets["a"]
    assert copy.refused and "too large" in copy.refused and not copy.committed
    assert not target.healthy and "too large" in str(target.last_error)
    assert small.under(PENDING)  # the segments stay where they are
    assert good.files[VIDEO] == b"initseg0seg1"
    small.ops.clear()
    await uploader.async_commit(record)
    assert not small.ops


# ------------------------------------------------------------------ eviction


async def test_an_eviction_during_an_upload_leaves_no_file_and_stays_deleted(hass, index) -> None:
    fake = Gate()
    uploader = await prepared(hass, index, {"a": fake})
    target = uploader.targets["a"]
    commit = asyncio.create_task(uploader.async_commit(make_record()))
    await fake.started.wait()
    # The lock is free while the upload runs, so the eviction is not held up by it.
    await asyncio.wait_for(uploader._async_evict(target, EVENT), 1)
    assert (await copy_of(index)).deleted
    fake.release.set()
    await commit
    assert not fake.videos("f/events/")  # the late upload was removed, not orphaned
    copy = await copy_of(index)
    assert copy.deleted and not copy.ranges  # and the copy was not brought back
    assert not await index.async_video_taken("a", VIDEO, "another")


async def test_an_eviction_deletes_every_file_of_a_video(hass, index) -> None:
    fake = FakeDestination()
    uploader = await prepared(hass, index, {"a": fake})
    record = make_record()
    await uploader.async_commit(record)
    await index.async_add_file(EVENT, "a", "00002.m4s", 4)
    fake.files[f"{PENDING}/00002.m4s"] = b"seg2"
    await uploader.async_commit(record)
    assert len(fake.videos("f/events/")) == 2
    await uploader._async_evict(uploader.targets["a"], EVENT)
    assert not fake.videos("f/events/")


# ------------------------------------------------------------------ good segments only


async def test_a_short_or_failed_segment_is_not_joined(hass, index, caplog) -> None:
    fake = FakeDestination()
    uploader = await prepared(hass, index, {"a": fake})
    # 00000.m4s was recorded at 4 bytes; the spool and the destination hold 2.
    await uploader.spool.async_write(EVENT, "00000.m4s", b"se")
    fake.files[f"{PENDING}/00000.m4s"] = b"se"
    # 00001.m4s is one this destination gave up on.
    copy = await copy_of(index)
    copy.failed.append(1)
    await index.async_save_copy_header(EVENT, "a", copy)
    # And one the index never heard of.
    fake.files[f"{PENDING}/00002.m4s"] = b"seg2"

    await uploader.async_commit(make_record())
    assert fake.files[VIDEO] == b"initseg2"
    assert set((await copy_of(index)).ranges) == {"init.mp4", "00002.m4s"}
    assert "not the 4 uploaded" in caplog.text and "does not know" in caplog.text
    assert fake.under(PENDING)  # the short one was never joined: its folder is kept


# ------------------------------------------------------------------ the spool


async def test_the_spool_is_bounded_and_cleaned(hass, tmp_path) -> None:
    spool = Spool(hass, max_bytes=1000)
    for event in ("ev_a", "ev_b", "ev_c"):
        await spool.async_write(event, "init.mp4", b"x" * 30)
    await spool.async_clean({"ev_a", "ev_b", "ev_c"})
    assert sorted(path.name for path in spool.root.iterdir()) == ["ev_a", "ev_b", "ev_c"]

    await spool.async_clean({"ev_b", "ev_c"})  # at startup: only what is still unfinished
    assert not spool.folder("ev_a").exists() and spool.folder("ev_b").exists()

    old = time.time() - spool.max_age - 60
    os.utime(spool.folder("ev_b") / "init.mp4", (old, old))
    await spool.async_clean()
    assert not spool.folder("ev_b").exists() and spool.folder("ev_c").exists()

    spool.max_bytes = 100
    for n in range(4):  # past the size bound: the oldest event goes, never the newest
        await spool.async_write("ev_d", f"{n:05d}.m4s", b"y" * 30)
    assert not spool.folder("ev_c").exists()
    assert (await spool.async_sizes("ev_d")) == {f"{n:05d}.m4s": 30 for n in range(4)}


# ------------------------------------------------------------------ the index


async def test_a_name_is_taken_through_an_indexed_column(hass, index, tmp_path) -> None:
    await index.async_save_copy("e1", "a", Copy(folder="p", video=VIDEO))
    assert await index.async_video_taken("a", VIDEO, "e2")
    assert not await index.async_video_taken("a", VIDEO, "e1")
    assert not await index.async_video_taken("b", VIDEO, "e2")
    deleted = Copy(folder="p", video=VIDEO, deleted=True)
    await index.async_save_copy("e1", "a", deleted)
    assert not await index.async_video_taken("a", VIDEO, "e2")  # a deleted copy frees it
    # A deleted copy is never brought back by a late write.
    assert not await index.async_save_copy("e1", "a", Copy(folder="p", video=VIDEO))
    assert (await index.async_copies("e1"))["a"].deleted
    db = sqlite3.connect(index.path)
    plan = db.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM copies WHERE destination = 'a' AND video = 'x'"
    ).fetchall()
    db.close()
    assert "copies_video" in str(plan)


async def test_an_old_index_gains_the_video_column(hass, tmp_path) -> None:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE copies (
            event_id TEXT NOT NULL, destination TEXT NOT NULL, copy TEXT NOT NULL,
            PRIMARY KEY (event_id, destination)
        );
        """
    )
    for event, deleted in (("e1", False), ("e2", True)):
        db.execute(
            "INSERT INTO copies VALUES (?, 'a', ?)",
            (event, json.dumps({"folder": "p", "video": f"{event}.mp4", "deleted": deleted})),
        )
    db.commit()
    db.close()
    index = EventIndex(hass, path)
    await index.async_open()
    assert await index.async_video_taken("a", "e1.mp4", "x")
    assert not await index.async_video_taken("a", "e2.mp4", "x")
    copy = (await index.async_copies("e1"))["a"]
    assert copy.parts == [] and copy.video_size == 0
    await index.async_close()


# ------------------------------------------------------------------ streaming uploads


async def test_onedrive_streams_a_video_through_a_session_one_chunk_at_a_time(
    monkeypatch,
) -> None:
    monkeypatch.setattr(onedrive, "SIMPLE_UPLOAD_LIMIT", 4)
    monkeypatch.setattr(onedrive, "SESSION_CHUNK", 4)
    destination = OneDriveDestination(MagicMock(), loaded("onedrive"))
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "POST":
            return {"uploadUrl": "https://upload.example/session"}
        if method == "GET":
            return {"size": 10}
        if kwargs["headers"]["Content-Range"].endswith("9/10"):
            return {"id": "v", "size": 10}
        return {"nextExpectedRanges": ["4-"]}

    pulled = []

    async def chunks():
        for piece in (b"012", b"3456", b"789"):  # uneven: re-cut to the session's size
            pulled.append(piece)
            yield piece

    destination._async_request = request
    await destination.async_store_video(VIDEO, 10, chunks())
    puts = [c for c in calls if c[0] == "PUT"]
    assert [c[2]["data"] for c in puts] == [b"0123", b"4567", b"89"]
    assert [c[2]["headers"]["Content-Range"] for c in puts] == [
        "bytes 0-3/10",
        "bytes 4-7/10",
        "bytes 8-9/10",
    ]
    assert calls[-1][0] == "GET"  # the size confirmed after


async def test_onedrive_refuses_a_stream_shorter_than_announced(monkeypatch) -> None:
    monkeypatch.setattr(onedrive, "SIMPLE_UPLOAD_LIMIT", 4)
    destination = OneDriveDestination(MagicMock(), loaded("onedrive"))

    async def request(method, url, **kwargs):
        return {"uploadUrl": "https://upload.example/session"} if method == "POST" else {}

    async def chunks():
        yield b"0123456"

    destination._async_request = request
    with pytest.raises(DestinationError, match="7 of 10"):
        await destination.async_store_stream(VIDEO, 10, chunks())
