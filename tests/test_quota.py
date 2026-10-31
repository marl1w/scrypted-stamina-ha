"""Quota eviction (ported from Reolink Stamina) and the follow-up cap."""

from __future__ import annotations

from custom_components.scrypted_stamina.capacity import expected_bytes, slots
from custom_components.scrypted_stamina.const import MAX_PARALLEL_UPLOADS
from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.quota import DestinationUsage


def usage(*items: tuple[str, float, EventState, int]) -> DestinationUsage:
    held = DestinationUsage()
    for event_id, started, state, size in items:
        held.set(event_id, started, state, size)
    return held


def test_eviction_takes_the_oldest_committed_first_and_no_further() -> None:
    held = usage(
        ("old", 1, EventState.COMMITTED, 40),
        ("mid", 2, EventState.COMMITTED, 40),
        ("new", 3, EventState.COMMITTED, 40),
    )
    assert [item.event_id for item in held.plan_eviction(30, 120)] == ["old"]
    assert [item.event_id for item in held.plan_eviction(70, 120)] == ["old", "mid"]


def test_pending_and_orphaned_events_are_never_evicted() -> None:
    held = usage(
        ("orphan", 1, EventState.ORPHANED, 50),
        ("pending", 2, EventState.PENDING, 50),
        ("hold", 3, EventState.FOLLOW_UP, 50),
        ("kept", 4, EventState.COMMITTED, 50),
    )
    assert [item.event_id for item in held.plan_eviction(40, 220)] == ["kept"]
    # Even evicting everything committed would not make room: nothing is evicted.
    assert held.plan_eviction(150, 220) == []
    assert held.plan_eviction(500, 220) == []


def test_retention_names_old_committed_events_only() -> None:
    held = usage(
        ("a", 1, EventState.COMMITTED, 1),
        ("b", 2, EventState.ORPHANED, 1),
        ("c", 50, EventState.COMMITTED, 1),
    )
    assert [item.event_id for item in held.expired(10)] == ["a"]


def test_follow_up_cap_drops_oldest_follow_up_events_first() -> None:
    """Over the cap, the oldest vetoed and ordinary events go; KEEPING/ORPHANED never."""
    held = usage(
        ("keep", 0, EventState.KEEPING, 500),
        ("orphan", 1, EventState.ORPHANED, 500),
        ("f1", 2, EventState.FOLLOW_UP, 300),
        ("f2", 3, EventState.FOLLOW_UP, 300),
        ("f3", 4, EventState.FOLLOW_UP, 300),
    )
    assert held.follow_up_bytes() == 900
    assert held.follow_up_oldest() == ["f1", "f2", "f3"]
    held.state("f1", EventState.DROPPING)
    assert held.follow_up_bytes() == 600
    held.grow("f2", 3, EventState.FOLLOW_UP, 50)
    assert held.follow_up_bytes() == 650 and held.used == 1950
    held.remove("f3")
    assert held.follow_up_oldest() == ["f2"] and held.used == 1650


def test_orphans_go_only_by_their_own_retention() -> None:
    held = usage(
        ("orphan", 1, EventState.ORPHANED, 50),
        ("kept", 2, EventState.COMMITTED, 50),
    )
    assert held.plan_eviction(60, 100) == []  # orphans never make room
    assert [h.event_id for h in held.expired(0, orphans_before=10)] == ["orphan"]
    assert held.orphans() == (1, 50)


def test_slots_follow_memory_and_never_reach_zero() -> None:
    assert slots(None, 1000) == 1
    assert slots(10_000, 0) == 1
    assert slots(1 << 30, 256 * 1024) == MAX_PARALLEL_UPLOADS
    assert slots(4 * 256 * 1024 * 10, 256 * 1024) == 4
    assert expected_bytes([], 99) == 99
    assert expected_bytes([3, 7, 5], 99) == 7
