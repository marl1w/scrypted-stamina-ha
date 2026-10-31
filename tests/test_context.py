"""Context snapshots: linked entities relative to the event, sun and weather by default."""

from __future__ import annotations

import time

from homeassistant.core import State

from custom_components.scrypted_stamina.context import (
    default_weather,
    entity_kind,
    late_change,
    snapshot,
)

from .conftest import CAMERA, event_id, subject, until
from .test_events import OPTIONS


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
