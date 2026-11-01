"""Every transition of the per-event state machine (architecture §3.4, §3.4a)."""

from __future__ import annotations

import time

import pytest

from custom_components.scrypted_stamina.const import FOLDER_EVENTS, FOLDER_UNUSUAL
from custom_components.scrypted_stamina.decider import (
    EventRecord,
    EventState,
    Policy,
    decide_after_follow_up,
    decide_at_end,
    drop_for_cap,
    evictable,
    follow_up_closed,
    orphan,
    subject_confirmed,
)
from custom_components.scrypted_stamina.protocol import EventFacts, Subject

POLICY = Policy(unverified_seconds=2.0, unverified_score=0.5)


def record(*verdicts: str, ended: bool = True, offline: bool = False, **subject_kw) -> EventRecord:
    now = time.time()
    subjects = {
        f"s{n}": Subject(
            id=f"s{n}",
            cls="person",
            verdict=verdict,
            score=subject_kw.get("score", 0.9),
            first_seen=now - subject_kw.get("dwell", 5.0),
            last_seen=now,
        )
        for n, verdict in enumerate(verdicts)
    }
    facts = EventFacts(
        id="ev_1",
        camera="38",
        started=now - 30,
        ended=now if ended else None,
        subjects=subjects,
        camera_offline=offline,
    )
    return EventRecord(facts=facts)


@pytest.mark.parametrize(
    ("verdicts", "unusual", "state", "target", "reason"),
    [
        (("confirmed",), False, EventState.KEEPING, FOLDER_EVENTS, "classes: person"),
        (("vetoed", "confirmed"), False, EventState.KEEPING, FOLDER_EVENTS, "classes: person"),
        (("pending",), False, EventState.KEEPING, FOLDER_EVENTS, "classes: person"),
        (("vetoed",), True, EventState.KEEPING, FOLDER_UNUSUAL, "unusual"),
        (("vetoed",), False, EventState.FOLLOW_UP, None, None),
        ((), False, EventState.FOLLOW_UP, None, None),
    ],
)
def test_decision_at_end(verdicts, unusual, state, target, reason) -> None:
    item = record(*verdicts)
    item.unusual_provisional = unusual
    assert decide_at_end(item, POLICY) is state
    assert item.target == target
    assert item.keep_reason == reason


def test_unverified_subjects_follow_the_fallback_policy() -> None:
    """No verifier: long enough and confident enough counts as confirmed, else as vetoed."""
    assert decide_at_end(record("unverified", dwell=3.0), POLICY) is EventState.KEEPING
    assert decide_at_end(record("unverified", dwell=1.0), POLICY) is EventState.FOLLOW_UP
    assert decide_at_end(record("unverified", score=0.2), POLICY) is EventState.FOLLOW_UP
    weak = record("unverified", dwell=1.0).facts.subjects["s0"]
    assert not subject_confirmed(weak, POLICY)


def test_camera_offline_mid_event_is_kept() -> None:
    item = record("vetoed", offline=True)
    assert decide_at_end(item, POLICY) is EventState.ORPHANED
    assert "camera_offline" in item.notes


def test_only_pending_events_are_decided_at_end() -> None:
    item = record("vetoed")
    item.state = EventState.ORPHANED
    assert decide_at_end(item, POLICY) is EventState.ORPHANED


def test_follow_up_unusual_is_kept_apart() -> None:
    item = record("vetoed")
    item.state = EventState.FOLLOW_UP
    item.unusual_final = True
    assert decide_after_follow_up(item, link_healthy=False, now=1.0) is EventState.KEEPING
    assert item.target == FOLDER_UNUSUAL


def test_follow_up_ordinary_drops_only_with_a_healthy_link() -> None:
    """The safety rule: no DROPPING without a healthy link."""
    item = record("vetoed")
    item.state = EventState.FOLLOW_UP
    item.unusual_final = False
    assert decide_after_follow_up(item, link_healthy=False, now=1.0) is EventState.FOLLOW_UP
    assert item.dropped_at is None
    assert decide_after_follow_up(item, link_healthy=True, now=5.0) is EventState.DROPPING
    assert item.dropped_at == 5.0


def test_follow_up_decision_leaves_other_states_alone() -> None:
    for state in (EventState.KEEPING, EventState.COMMITTED, EventState.ORPHANED):
        item = record("confirmed")
        item.state = state
        item.unusual_final = False
        assert decide_after_follow_up(item, link_healthy=True, now=1.0) is state


def test_cap_drops_only_ordinary_follow_up_events() -> None:
    item = record("vetoed")
    item.state = EventState.FOLLOW_UP
    assert drop_for_cap(item, link_healthy=False, now=1.0) is EventState.FOLLOW_UP
    assert drop_for_cap(item, link_healthy=True, now=1.0) is EventState.DROPPING
    for state in (EventState.KEEPING, EventState.ORPHANED, EventState.PENDING):
        other = record("vetoed")
        other.state = state
        assert drop_for_cap(other, link_healthy=True, now=1.0) is state


def test_orphaning_keeps_pending_and_keeping_only() -> None:
    for state in (EventState.PENDING, EventState.KEEPING):
        item = record("vetoed", ended=False)
        item.state = state
        assert orphan(item, "link_lost") is EventState.ORPHANED
        assert "link_lost" in item.notes
        assert item.target == FOLDER_EVENTS
    for state in (EventState.FOLLOW_UP, EventState.DROPPING, EventState.COMMITTED):
        item = record("vetoed")
        item.state = state
        assert orphan(item, "link_lost") is state


def test_follow_up_window_closes_once() -> None:
    item = record("vetoed")
    item.follow_up_until = 100.0
    assert not follow_up_closed(item, 99.0)
    assert follow_up_closed(item, 100.0)
    item.finalized = True
    assert not follow_up_closed(item, 200.0)


def test_only_committed_events_are_evictable() -> None:
    item = record("confirmed")
    for state in EventState:
        item.state = state
        assert evictable(item) is (state is EventState.COMMITTED)


def test_best_current_score() -> None:
    item = record("vetoed", ended=False)
    item.live_score, item.unusual_live = 0.4, True
    assert item.unusual and item.score == 0.4
    item.facts.ended = time.time()
    item.provisional_score, item.unusual_provisional = 0.3, False
    assert not item.unusual and item.score == 0.3
    item.final_score, item.unusual_final = 0.9, True
    assert item.unusual and item.score == 0.9


def test_preroll_is_read_kept_and_restored() -> None:
    raw = {"id": "ev1", "camera": "11", "start": 1_730_370_000_000}
    first = EventFacts.parse({**raw, "segments": {"next": 0, "preroll_ms": 0}})
    first.merge(EventFacts.parse({**raw, "segments": {"next": 5, "preroll_ms": 9500}}))
    first.merge(EventFacts.parse(raw))
    assert first.preroll == 9.5
    assert EventFacts.restore(first.as_dict()).preroll == 9.5
    assert EventFacts.parse({**raw, "segments": {"preroll_ms": "x"}}).preroll is None
