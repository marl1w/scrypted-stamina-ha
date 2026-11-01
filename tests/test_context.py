"""Context snapshots: linked entities relative to the event, sun and weather by default."""

from __future__ import annotations

import time

from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.scrypted_stamina.context import (
    RecentChanges,
    default_weather,
    entity_kind,
    late_change,
    snapshot,
)

from .conftest import CAMERA, event_id, subject, until
from .test_events import OPTIONS


def changed(entity_id: str, old: str, new: str, at: float) -> tuple[State, State]:
    """A state change at a given moment."""
    stamp = dt_util.utc_from_timestamp(at)
    return State(entity_id, old), State(entity_id, new, last_changed=stamp)


async def test_snapshot_states_and_changes_relative_to_the_event(hass) -> None:
    hass.states.async_set("binary_sensor.gate", "on", {"device_class": "door"})
    hass.states.async_set("alarm_control_panel.home", "armed_away")
    hass.states.async_set("sun.sun", "below_horizon", {"elevation": -12.34})
    hass.states.async_set("weather.zz_other", "sunny", {"wind_speed": 1})
    hass.states.async_set("weather.home", "rainy", {"wind_speed": 31.46})
    changed = hass.states.get("binary_sensor.gate").last_changed.timestamp()

    found = snapshot(
        hass, ["binary_sensor.gate", "alarm_control_panel.home", "lock.gone"], changed + 5
    )
    assert found["binary_sensor.gate"] == {"state": "on", "kind": "door", "changed_rel_s": -5.0}
    assert found["alarm_control_panel.home"]["kind"] == "alarm"
    assert found["lock.gone"] == {"state": "unknown", "kind": "lock", "changed_rel_s": None}
    assert found["_defaults"]["sun"] == -12.3
    assert found["_defaults"]["weather"] == "rainy"  # the first weather entity, by id
    assert found["_defaults"]["wind"] == 31.5
    assert default_weather(hass) == "weather.home"


async def test_snapshot_needs_no_linking(hass) -> None:
    hass.states.async_set("sun.sun", "above_horizon", {"elevation": 40})
    found = snapshot(hass, [], time.time())
    assert set(found) == {"_defaults"}
    assert found["_defaults"]["sun"] == 40.0


async def test_kinds_come_from_domain_and_device_class(hass) -> None:
    hass.states.async_set("binary_sensor.shock", "off", {"device_class": "vibration"})
    hass.states.async_set("binary_sensor.curtain", "off", {"device_class": "motion"})
    hass.states.async_set("person.someone", "home")
    assert entity_kind(hass, "binary_sensor.shock") == "vibration"
    assert entity_kind(hass, "binary_sensor.curtain") == "motion"
    assert entity_kind(hass, "person.someone") == "presence"
    assert entity_kind(hass, "light.porch") == "light"
    assert entity_kind(hass, "switch.anything") == "other"


def test_late_change_records_delay_from_end_and_start() -> None:
    old = State("binary_sensor.gate", "off")
    new = State("binary_sensor.gate", "on")
    at = new.last_changed.timestamp()
    item = late_change("binary_sensor.gate", "door", old, new, started=at - 100, ended=at - 40)
    assert item == {
        "entity": "binary_sensor.gate",
        "kind": "door",
        "from": "off",
        "to": "on",
        "t_rel_s": 40.0,
        "t_from_start_s": 100.0,
    }
    assert late_change("x.y", "other", new, State("x.y", "on"), started=0, ended=0) is None


async def test_context_is_taken_at_start_and_end(hass, stamina, setup) -> None:
    hass.states.async_set("binary_sensor.gate", "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS)
    manager = entry.runtime_data.manager
    eid = event_id(70)
    stamina.start(eid, subjects=[subject("s1")])
    await until(lambda: eid in manager.records)
    assert manager.records[eid].context_start["binary_sensor.gate"]["state"] == "off"
    hass.states.async_set("binary_sensor.gate", "on", {"device_class": "door"})
    stamina.end(eid, subjects=[subject("s1", verdict="confirmed")])
    await until(lambda: bool(manager.records[eid].context_end))
    assert manager.records[eid].context_end["binary_sensor.gate"]["state"] == "on"
    assert CAMERA in manager.cameras
    await hass.config_entries.async_unload(entry.entry_id)


def test_recent_changes_cover_the_look_back_window() -> None:
    """The ring keeps real changes only, and answers relative to the start."""
    start = 1_000_000.0
    recent = RecentChanges(600.0)
    recent.add("binary_sensor.gate", *changed("binary_sensor.gate", "off", "on", start - 20))
    recent.add("binary_sensor.gate", *changed("binary_sensor.gate", "on", "off", start - 5))
    recent.add("light.porch", *changed("light.porch", "off", "on", start - 300))
    # Not activity: an integration coming back, or nothing changing at all.
    recent.add("binary_sensor.shed", *changed("binary_sensor.shed", "unavailable", "on", start))
    recent.add("binary_sensor.shed", *changed("binary_sensor.shed", "on", "on", start))

    kinds = {"binary_sensor.gate": "door", "light.porch": "light", "binary_sensor.shed": "door"}
    found = recent.between(kinds, started=start, since=start - 120, until=start + 3)
    gate = {"entity": "binary_sensor.gate", "kind": "door"}
    assert found == [
        {**gate, "from": "off", "to": "on", "t_rel_s": -20.0},
        {**gate, "from": "on", "to": "off", "t_rel_s": -5.0},
    ]
    # A wider window reaches the light; an entity not asked for is never returned.
    wider = recent.between({"light.porch": "light"}, started=start, since=start - 600, until=start)
    assert [item["t_rel_s"] for item in wider] == [-300.0]


def test_recent_changes_forget_what_is_older_than_they_keep() -> None:
    start = 1_000_000.0
    recent = RecentChanges(60.0)
    recent.add("binary_sensor.gate", *changed("binary_sensor.gate", "off", "on", start - 500))
    recent.add("binary_sensor.gate", *changed("binary_sensor.gate", "on", "off", start))
    found = recent.between({"binary_sensor.gate": "door"}, started=start, since=0, until=start)
    assert [item["to"] for item in found] == ["off"]


async def test_changes_before_the_start_are_early_evidence(hass, stamina, setup) -> None:
    """A gate opened just before the camera saw someone is linked to the event."""
    hass.states.async_set("binary_sensor.gate", "off", {"device_class": "door"})
    entry = await setup(options=OPTIONS)
    manager = entry.runtime_data.manager
    hass.states.async_set("binary_sensor.gate", "on", {"device_class": "door"})
    await hass.async_block_till_done()

    eid = event_id(71)
    stamina.start(eid, subjects=[subject("s1")])
    await until(lambda: eid in manager.records)
    early = manager.records[eid].early
    assert [(item["entity"], item["kind"], item["from"], item["to"]) for item in early] == [
        ("binary_sensor.gate", "door", "off", "on")
    ]
    assert -120.0 <= early[0]["t_rel_s"] <= 30.0
    assert manager.look_back_seconds(CAMERA) == 120.0
    await hass.config_entries.async_unload(entry.entry_id)
