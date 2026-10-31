"""Scoring smoke tests for the ported relevance model, with Scrypted features."""

from __future__ import annotations

import datetime as dt

from homeassistant.util import dt as dt_util

from custom_components.scrypted_stamina.const import (
    RELEVANCE_SCOPE_AREA,
    RELEVANCE_SCOPE_CAMERA,
    RELEVANCE_SCOPE_TOGETHER,
)
from custom_components.scrypted_stamina.decider import EventRecord, Policy
from custom_components.scrypted_stamina.protocol import EventFacts, Subject
from custom_components.scrypted_stamina.relevance import normalised
from custom_components.scrypted_stamina.relevance.features import (
    SolarClock,
    late_signals,
    signals,
    to_event,
)
from custom_components.scrypted_stamina.relevance.rates import build, group_key, preceded
from custom_components.scrypted_stamina.relevance.score import calibrate, score

POLICY = Policy()


def make(
    at: dt.datetime,
    *,
    camera: str = "38",
    area: str = "garden",
    cls: str = "person",
    verdict: str = "confirmed",
    duration: float = 12.0,
    late: list | None = None,
) -> EventRecord:
    start = at.timestamp()
    facts = EventFacts(
        id=f"ev_{camera}_{int(start)}",
        camera=camera,
        started=start,
        ended=start + duration,
        subjects={
            "s1": Subject(
                id="s1",
                cls=cls,
                verdict=verdict,
                sources=("@marl1w/scrypted-reolink", "scrypted-coral-guard"),
                score=0.9,
                first_seen=start,
                last_seen=start + duration,
            )
        },
        signals={"night_mode": at.hour < 6},
        trigger="object",
    )
    return EventRecord(facts=facts, area=area, late=late or [])


def history(hass, *, days: int = 14, per_day: int = 25, camera: str = "38", area: str = "garden"):
    """A camera that sees people around lunchtime, every day."""
    start = dt_util.now().replace(hour=12, minute=0, second=0, microsecond=0)
    start -= dt.timedelta(days=days)
    records = []
    for day in range(days):
        for n in range(per_day):
            at = start + dt.timedelta(days=day, minutes=5 * n)
            records.append(make(at, camera=camera, area=area))
    return records


def learn(hass, records, scope=RELEVANCE_SCOPE_TOGETHER):
    clock = SolarClock(hass)
    events = [to_event(r, clock, POLICY) for r in sorted(records, key=lambda r: r.facts.started)]
    model = build(events, now=dt_util.now().timestamp(), scope=scope)
    calibrate(model, events)
    return clock, model


async def test_nothing_is_flagged_while_learning(hass) -> None:
    clock, model = learn(hass, history(hass, days=3, per_day=20))
    night = make(dt_util.now().replace(hour=3, minute=0))
    result = score(to_event(night, clock, POLICY), model, previous=None)
    assert result.threshold is None
    assert not result.unusual


async def test_a_rare_event_scores_high_once_learned(hass) -> None:
    clock, model = learn(hass, history(hass))
    usual = score(
        to_event(make(dt_util.now().replace(hour=12, minute=30)), clock, POLICY),
        model,
        previous=None,
    )
    rare = score(
        to_event(make(dt_util.now().replace(hour=3, minute=10), cls="vehicle"), clock, POLICY),
        model,
        previous=None,
    )
    assert rare.threshold is not None
    assert rare.total > usual.total
    assert rare.unusual and not usual.unusual
    assert "Vehicle" in rare.reason
    assert 0.5 < normalised(rare.total, rare.threshold) <= 1.0


async def test_predecessor_says_what_fired_first(hass) -> None:
    """The cross-camera sequence term, kept exactly from Reolink Stamina."""
    clock, model = learn(hass, history(hass))
    at = dt_util.now().replace(hour=12, minute=30)
    first = to_event(make(at, camera="40", area="garden"), clock, POLICY)
    second = to_event(make(at + dt.timedelta(seconds=17), camera="38"), clock, POLICY)
    result = score(second, model, previous=first, names={"40": "Garage"})
    term = next(term for term in result.terms if term.name == "predecessor")
    assert term.label == "5s after Garage"


async def test_area_scope_pools_by_area(hass) -> None:
    clock = SolarClock(hass)
    at = dt_util.now().replace(hour=12, minute=0)
    a = to_event(make(at, camera="1", area="garden"), clock, POLICY)
    b = to_event(make(at + dt.timedelta(seconds=20), camera="2", area="drive"), clock, POLICY)
    c = to_event(make(at + dt.timedelta(seconds=40), camera="3", area="garden"), clock, POLICY)
    d = to_event(make(at + dt.timedelta(seconds=60), camera="4", area="no-area"), clock, POLICY)

    assert group_key("1", RELEVANCE_SCOPE_AREA, "garden") == group_key(
        "3", RELEVANCE_SCOPE_AREA, "garden"
    )
    assert group_key("1", RELEVANCE_SCOPE_AREA, "garden") != group_key(
        "2", RELEVANCE_SCOPE_AREA, "drive"
    )
    assert group_key("1", RELEVANCE_SCOPE_CAMERA, "garden") == "1"

    by_area = dict(
        (event.camera, previous)
        for event, previous in preceded([a, b, c, d], scope=RELEVANCE_SCOPE_AREA)
    )
    assert by_area["3"] is a  # the garden camera before, not the drive one in between
    assert by_area["2"] is None
    assert by_area["4"] is None  # cameras without an area pool on their own

    together = dict(
        (event.camera, previous)
        for event, previous in preceded([a, b, c, d], scope=RELEVANCE_SCOPE_TOGETHER)
    )
    assert together["3"] is b


async def test_features_cover_facts_context_and_late_evidence(hass) -> None:
    item = make(
        dt_util.now().replace(hour=2),
        verdict="vetoed",
        late=[
            {
                "entity": "binary_sensor.gate",
                "kind": "door",
                "from": "off",
                "to": "on",
                "t_rel_s": 75.0,
            },
            {
                "entity": "alarm_control_panel.home",
                "kind": "alarm",
                "from": "armed_away",
                "to": "triggered",
                "t_rel_s": 200.0,
            },
        ],
    )
    item.context_start = {
        "_defaults": {"weather": "rainy", "wind": 31.5, "sun": -20.1},
        "binary_sensor.gate": {"state": "off", "kind": "door", "changed_rel_s": -600},
    }
    found = signals(
        item, POLICY, linked_kinds={"binary_sensor.gate": "door", "light.porch": "light"}
    )
    assert found["verdicts"] == "vetoed"
    assert found["sources"] == "@marl1w/scrypted-reolink+scrypted-coral-guard"
    assert found["night"] == "on"
    assert found["weather"] == "rainy" and found["wind"] == "31.5" and found["sun"] == "-20.1"
    assert found["binary_sensor.gate"] == "off"
    assert found["late:door"] == "<2m"
    assert found["late:alarm"] == "triggered"
    assert found["late:light"] == "none"
    assert late_signals([]) == {}
    provisional = signals(item, POLICY, include_late=False)
    assert not any(key.startswith("late:") for key in provisional)


async def test_whole_frame_ai_is_its_own_weak_source(hass) -> None:
    """A split camera's whole-frame AI (`ambiguous`) is not the view's own camera AI."""
    from custom_components.scrypted_stamina.protocol import Subject

    parsed = Subject.parse(
        {
            "id": "s1",
            "class": "person",
            "verdict": "unverified",
            "sources": {
                "@marl1w/scrypted-reolink": {"role": "trigger", "ambiguous": True},
                "scrypted-coral-guard": {"role": "verifier"},
            },
        }
    )
    assert parsed.sources == ("scrypted-coral-guard",)
    assert parsed.ambiguous == ("@marl1w/scrypted-reolink",)
    assert Subject.restore(parsed.as_dict()).ambiguous == parsed.ambiguous
    assert Subject.parse({"id": "s2", "class": "car", "sources": {"x": {}}}).ambiguous == ()

    item = make(dt_util.now().replace(hour=12))
    item.facts.subjects["s1"] = parsed
    assert signals(item, POLICY)["sources"] == "scrypted-coral-guard+whole-frame"
