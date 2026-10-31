"""The index's history: tiers, thumbnail ages, the horizon, the size cap and thinning."""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.scrypted_stamina import index as index_module
from custom_components.scrypted_stamina.const import (
    FRESH_DAYS,
    RATE_HALF_LIFE_DAYS,
    RELEVANCE_HISTORY_DAYS,
    THUMBNAIL_DROPPED_DAYS,
    THUMBNAIL_KEEP_DAYS,
)
from custom_components.scrypted_stamina.decider import EventRecord, EventState, Policy
from custom_components.scrypted_stamina.index import (
    ACTION_COMMIT,
    THIN_EVERY,
    Copy,
    EventIndex,
    thin_kind,
)
from custom_components.scrypted_stamina.manager import StaminaManager
from custom_components.scrypted_stamina.protocol import EventFacts, Subject
from custom_components.scrypted_stamina.relevance.features import (
    SolarClock,
    event_from_json,
    event_to_json,
    to_event,
)
from custom_components.scrypted_stamina.relevance.rates import build

from .conftest import event_id

DAY = 86400.0
CAMERA = "38"


def make(
    n: int,
    age_days: float,
    *,
    now: float,
    state: EventState = EventState.COMMITTED,
    cls: str = "person",
    unusual: bool = False,
    finalized: bool = True,
    minute: float = 0.0,
) -> EventRecord:
    start = now - age_days * DAY + minute * 60
    facts = EventFacts(
        id=event_id(n),
        camera=CAMERA,
        started=start,
        ended=start + 12,
        subjects={
            "s1": Subject(
                id="s1",
                cls=cls,
                verdict="confirmed",
                sources=("trigger", "verifier"),
                score=0.9,
                first_seen=start,
                last_seen=start + 12,
                label="a label",
            )
        },
        durations={0: 2.0, 1: 2.0},
        stamps={"plugin": start},
        segments=2,
        init=True,
        segments_final=True,
    )
    record = EventRecord(
        facts=facts,
        state=state,
        area="garden",
        folder=f"folder-{n}",
        finalized=finalized,
        reason="Person at noon." if unusual else "",
        reasons=["clock: 12:00 (+1.20)"] if unusual else [],
        keep_reason="unusual" if unusual else None,
        context_start={"binary_sensor.door": {"state": "off", "since": start - 60}},
        late=[{"entity": "binary_sensor.door", "kind": "door", "to": "on", "t_rel_s": 20}],
        latency={"ha_received": start + 1},
        notes=["a_note"],
    )
    record.unusual_final = unusual
    record.final_score = 0.8 if unusual else 0.2
    return record


def features(record: EventRecord, **context: str) -> str:
    """Features as the model stores them, with chosen context."""
    return json.dumps(
        {
            "camera": record.camera,
            "kind": next(iter(record.facts.subjects.values())).cls
            if record.facts.subjects
            else "motion",
            "started_at": record.facts.started,
            "ended_at": record.facts.ended,
            "duration": 12.0,
            "minute_of_day": 720,
            "solar_offset": None,
            "solar_phase": None,
            "is_weekend": False,
            "day_of_week": "Mon",
            "context": sorted(
                [name, value] for name, value in {"weather": "cloudy", **context}.items()
            ),
            "area": "garden",
        }
    )


async def opened(hass, tmp_path: Path) -> EventIndex:
    index = EventIndex(hass, tmp_path / "index.db")
    await index.async_open()
    return index


async def prune(index: EventIndex, now: float):
    return await index.async_prune(
        history_before=now - RELEVANCE_HISTORY_DAYS * DAY,
        fresh_before=now - FRESH_DAYS * DAY,
        thumbnails_before=now - THUMBNAIL_KEEP_DAYS * DAY,
        dropped_thumbnails_before=now - THUMBNAIL_DROPPED_DAYS * DAY,
    )


def column(index: EventIndex, sql: str, *params) -> list:
    db = sqlite3.connect(index.path)
    try:
        return [row[0] if len(row) == 1 else row for row in db.execute(sql, params)]
    finally:
        db.close()


# ---------------------------------------------------------------------- tiers


async def test_rows_are_slimmed_after_the_fresh_tier_and_still_read(hass, tmp_path) -> None:
    index = await opened(hass, tmp_path)
    now = dt_util.utcnow().timestamp()
    old = make(1, 40, now=now, unusual=True)
    fresh = make(2, 10, now=now)
    await index.async_save(old, features(old))
    await index.async_save(fresh, features(fresh))
    await index.async_save_copy(
        old.id, "dest", Copy(folder="events/x", files={"index.m3u8": 9}, committed=True)
    )

    result = await prune(index, now)
    assert result.slimmed == 1 and result.deleted == 0
    assert column(index, "SELECT id FROM events WHERE slim = 1") == [old.id]

    raw = json.loads(column(index, "SELECT record FROM events WHERE id = ?", old.id)[0])
    assert raw["slim"] is True
    for gone in ("context_start", "late", "latency", "gaps"):
        assert gone not in raw
    assert raw["facts"]["subjects"] == [{"id": "s1", "class": "person", "verdict": "confirmed"}]
    assert "durations" not in raw["facts"] and "stamps" not in raw["facts"]
    full = json.loads(column(index, "SELECT record FROM events WHERE id = ?", fresh.id)[0])
    assert full["context_start"] and full["late"]

    # Every reader still gets a record out of a slim row.
    slim = await index.async_get(old.id)
    assert slim is not None and slim.state is EventState.COMMITTED
    assert slim.unusual and slim.score == 0.8 and slim.reason == "Person at noon."
    assert slim.reasons == ["clock: 12:00 (+1.20)"] and slim.keep_reason == "unusual"
    assert slim.folder == "folder-1" and slim.notes == ["a_note"]
    assert [s.cls for s in slim.facts.subjects.values()] == ["person"]
    assert slim.late == [] and slim.context_start == {}
    assert [r.id for r in await index.async_events(camera=CAMERA)] == [old.id, fresh.id]
    rows, _ = await index.async_page(limit=10)
    assert [row.id for row in rows] == [fresh.id, old.id] and rows[1].classes == ["person"]
    browse = await index.async_browse(CAMERA, 0, now, 10)
    assert {row.id: row.offsite for row in browse} == {old.id: True, fresh.id: False}
    learning = await hass.async_add_executor_job(index.read_learning_rows, 0)
    assert [row[0] for row in learning] == [old.id, fresh.id]
    assert all(row[2] for row in learning)
    assert old.id not in {r.id for r in await index.async_unfinished()}

    # Slimming twice changes nothing.
    assert (await prune(index, now)).slimmed == 0
    await index.async_close()


async def test_unsettled_rows_are_never_slimmed(hass, tmp_path) -> None:
    index = await opened(hass, tmp_path)
    now = dt_util.utcnow().timestamp()
    not_final = make(1, 40, now=now, finalized=False)
    following = make(2, 40, now=now, state=EventState.FOLLOW_UP)
    pending_action = make(3, 40, now=now)
    uploading = make(4, 40, now=now, state=EventState.ORPHANED)
    unsettled_orphan = make(5, 40, now=now, state=EventState.ORPHANED)
    no_features = make(6, 40, now=now)
    settled_orphan = make(7, 40, now=now, state=EventState.ORPHANED)
    settled_orphan.notes.append("orphan_complete")
    for record in (not_final, following, pending_action, uploading, unsettled_orphan):
        await index.async_save(record, features(record))
    await index.async_save(no_features)
    await index.async_save(settled_orphan, features(settled_orphan))
    await index.async_add_action(pending_action.id, ACTION_COMMIT, now)
    await index.async_save_copy(uploading.id, "dest", Copy(folder="pending/x", files={"a": 1}))

    assert (await prune(index, now)).slimmed == 1
    assert column(index, "SELECT id FROM events WHERE slim = 1") == [settled_orphan.id]
    await index.async_close()


# ---------------------------------------------------------------------- ages


async def test_thumbnails_age_out_by_kind(hass, tmp_path) -> None:
    index = await opened(hass, tmp_path)
    now = dt_util.utcnow().timestamp()
    rows = {
        "kept_old": make(1, THUMBNAIL_KEEP_DAYS + 2, now=now),
        "kept_recent": make(2, THUMBNAIL_KEEP_DAYS - 2, now=now),
        "dropped_old": make(3, THUMBNAIL_DROPPED_DAYS + 2, now=now, state=EventState.DROPPED),
        "dropped_recent": make(4, THUMBNAIL_DROPPED_DAYS - 2, now=now, state=EventState.DROPPED),
        "orphan_recent": make(5, THUMBNAIL_KEEP_DAYS - 2, now=now, state=EventState.ORPHANED),
    }
    for record in rows.values():
        await index.async_save(record, features(record))
        await index.async_save_thumbnail(record.id, b"jpeg")

    assert (await prune(index, now)).thumbnails == 2
    left = {name for name, r in rows.items() if await index.async_has_thumbnail(r.id)}
    assert left == {"kept_recent", "dropped_recent", "orphan_recent"}
    # The row itself stays: only the picture went.
    assert await index.async_get(rows["dropped_old"].id) is not None
    page, _ = await index.async_page(include_dropped=True, limit=10)
    assert {row.id: row.thumbnail for row in page}[rows["kept_old"].id] is False
    await index.async_close()


async def test_dropped_rows_stay_for_the_whole_history(hass, tmp_path) -> None:
    assert RELEVANCE_HISTORY_DAYS >= 25 * 30 and RATE_HALF_LIFE_DAYS == 365
    index = await opened(hass, tmp_path)
    now = dt_util.utcnow().timestamp()
    week_old = make(1, 8, now=now, state=EventState.DROPPED)
    two_years = make(2, 24 * 30.4, now=now, state=EventState.DROPPED)
    too_old = make(3, 26 * 30.4, now=now, state=EventState.DROPPED)
    for record in (week_old, two_years, too_old):
        await index.async_save(record, features(record))

    # Through the manager's own nightly prune.
    manager = SimpleNamespace(now=lambda: now, index=index, history_size_mb=200)
    await StaminaManager._async_prune(manager, None)  # type: ignore[arg-type]
    assert await index.async_get(week_old.id) is not None
    assert await index.async_get(two_years.id) is not None
    assert await index.async_get(too_old.id) is None
    await index.async_close()


# ---------------------------------------------------------------------- thinning


def test_the_never_thin_rule() -> None:
    dropped = str(EventState.DROPPED)
    plain = json.dumps({"kind": "car", "context": [["weather", "cloudy"], ["tamper", "off"]]})
    assert thin_kind(dropped, False, plain) == "car"
    assert thin_kind(str(EventState.COMMITTED), False, plain) is None
    assert thin_kind(str(EventState.ORPHANED), False, plain) is None
    assert thin_kind(dropped, True, plain) is None
    assert thin_kind(dropped, False, None) is None
    for name, value in (
        ("tamper", "on"),
        ("weather", "snowy"),
        ("weather", "lightning-rainy"),
        ("alarm_control_panel.house", "armed_away"),
        ("alarm_control_panel.house", "triggered"),
        ("late:door", "<30s"),
    ):
        rare = json.dumps({"kind": "car", "context": [[name, value]]})
        assert thin_kind(dropped, False, rare) is None, (name, value)
    assert thin_kind(
        dropped, False, json.dumps({"kind": "car", "context": [["late:door", "none"]]})
    )


async def _history(index: EventIndex, now: float, count: int, *, pad: int = 0) -> list[str]:
    """Slim, thinnable dropped cars: `count` of them, 40 to 70 days old, around noon."""
    ids = []
    for n in range(count):
        record = make(
            1000 + n,
            40 + 30 * n / count,
            now=now,
            state=EventState.DROPPED,
            cls="car",
            minute=(n * 37) % 120 - 60,
        )
        await index.async_save(record, features(record, pad="x" * pad))
        ids.append(record.id)
    await prune(index, now)
    return ids


async def test_the_cap_is_met_by_thinning_and_protected_rows_stay(hass, tmp_path) -> None:
    index = await opened(hass, tmp_path)
    now = dt_util.utcnow().timestamp()
    protected = [
        make(1, 50, now=now, state=EventState.DROPPED, cls="car", unusual=True),
        make(2, 50, now=now, cls="car"),
        make(3, 50, now=now, state=EventState.ORPHANED, cls="car"),
        make(4, 50, now=now, state=EventState.DROPPED, cls="deer"),
    ]
    protected[2].notes.append("orphan_complete")
    for record in protected:
        await index.async_save(record, features(record))
    snow = make(5, 50, now=now, state=EventState.DROPPED, cls="car")
    await index.async_save(snow, features(snow, weather="snowy"))
    fresh = make(6, 5, now=now, state=EventState.DROPPED, cls="car")
    await index.async_save(fresh, features(fresh))
    ids = await _history(index, now, 1200, pad=1500)

    before = await index.async_size()
    cap = before - 1024 * 1024
    removed = await index.async_thin(cap)
    assert removed > 0
    assert await index.async_size() <= cap
    for record in (*protected, snow, fresh):
        assert await index.async_get(record.id) is not None, record.id
    weights = column(index, "SELECT SUM(weight) FROM events WHERE thin_kind = 'car'")
    # Every removed row is still counted by the one that stands for it.
    assert weights == [len(ids)]
    await index.async_close()


async def test_an_unreachable_cap_is_logged_once(hass, tmp_path, caplog) -> None:
    index = await opened(hass, tmp_path)
    now = dt_util.utcnow().timestamp()
    rare = [make(n, 50, now=now, state=EventState.DROPPED, cls="deer") for n in range(1, 30)]
    for record in rare:
        await index.async_save(record, features(record))
    await prune(index, now)
    with caplog.at_level(logging.WARNING):
        assert await index.async_thin(1) == 0
        assert await index.async_thin(1) == 0
    assert caplog.text.count("over its size cap") == 1
    assert column(index, "SELECT COUNT(*) FROM events") == [len(rare)]
    await index.async_close()


async def test_a_thinned_history_learns_the_same_rates(hass, tmp_path) -> None:
    index = await opened(hass, tmp_path)
    clock = SolarClock(hass)
    policy = Policy()
    noon = dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0)
    now = noon.timestamp()
    full = []
    for n in range(1600):
        at = noon - dt.timedelta(days=40 + n // 40, minutes=(n * 53) % 240 - 120)
        record = make(2000 + n, 0, now=at.timestamp(), state=EventState.DROPPED, cls="car")
        record.late = []  # late evidence is a rare context, never thinned
        event = to_event(record, clock, policy)
        full.append(event)
        await index.async_save(record, event_to_json(event))
    await prune(index, now)

    assert await index._async_thin_pass(0, THIN_EVERY) > 0
    rows = await hass.async_add_executor_job(index.read_learning_rows, 0)
    thinned = [event_from_json(row[2], row[4]) for row in rows]
    assert len(thinned) < len(full) / 3
    assert sum(event.weight for event in thinned) == len(full)

    before = build(sorted(full, key=lambda e: e.started_at), now=now)
    after = build(thinned, now=now)
    a, b = before.profile(CAMERA, "car"), after.profile(CAMERA, "car")
    assert a.events == b.events
    assert abs(a.weight - b.weight) / a.weight < 0.02
    for minute in range(600, 841, 30):
        expected, got = a.clock.probability(minute), b.clock.probability(minute)
        assert abs(expected - got) / expected < 0.15, minute
    for day, share in a.day_of_week.weights.items():
        assert abs(share - b.day_of_week.weights[day]) / share < 0.15
    await index.async_close()


# ---------------------------------------------------------------------- migration


async def test_an_old_index_gains_the_new_columns(hass, tmp_path) -> None:
    path = tmp_path / "index.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE events (
            id TEXT PRIMARY KEY, camera TEXT NOT NULL, started REAL NOT NULL, ended REAL,
            state TEXT NOT NULL, unusual INTEGER NOT NULL DEFAULT 0, score REAL,
            finalized INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT '',
            area TEXT NOT NULL DEFAULT '', duration REAL, features TEXT, record TEXT NOT NULL
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema', '3');
        """
    )
    now = dt_util.utcnow().timestamp()
    old = make(1, 40, now=now, state=EventState.DROPPED)
    db.execute(
        "INSERT INTO events (id, camera, started, state, finalized, features, record) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (
            old.id,
            CAMERA,
            old.facts.started,
            "dropped",
            features(old),
            json.dumps(index_module.record_to_dict(old)),
        ),
    )
    db.commit()
    db.close()

    for _ in range(2):  # idempotent: a second open finds everything in place
        index = EventIndex(hass, path)
        await index.async_open()
        assert await index.async_get_meta("schema") == str(index_module.SCHEMA_VERSION)
        await index.async_close()
    assert column(index, "SELECT weight, slim, thin_kind FROM events") == [(1, 0, None)]
    index = EventIndex(hass, path)
    await index.async_open()
    assert (await prune(index, now)).slimmed == 1
    assert column(index, "SELECT slim, thin_kind FROM events") == [(1, "person")]
    await index.async_close()
