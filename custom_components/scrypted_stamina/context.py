"""Context from Home Assistant: what else was going on (architecture §3.2, §6).

Two sources, both read here and nowhere else:

* **Linked entities**, chosen per camera in the options (pre-filled from the camera
  device's area). Each one's *kind* — door, alarm, presence, motion, vibration, light, lock,
  other — comes from its domain and device class, and tells scoring how to group its late
  changes.
* **Default context**, needing no linking: `sun.sun` (elevation) and the first `weather.*`
  entity (condition and wind speed).

A snapshot is taken at `event.start` and `event.end`: each linked entity's state and when
it last changed *relative to the event* ("door opened 5 s before"). During the follow-up
window, changes of the linked entities are collected as late evidence.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import ATTR_DEVICE_CLASS
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

# Entity domains a camera's area offers as suggestions, in the order they are listed.
SUGGESTED_DOMAINS = (
    "binary_sensor",
    "alarm_control_panel",
    "lock",
    "cover",
    "person",
    "device_tracker",
    "light",
)
# Binary sensor device classes worth suggesting; anything else is noise (battery, problem…).
_SUGGESTED_BINARY_CLASSES = frozenset(
    {
        "door",
        "garage_door",
        "window",
        "opening",
        "motion",
        "occupancy",
        "presence",
        "vibration",
        "moving",
        "sound",
        "tamper",
        "safety",
    }
)
_KINDS_BY_CLASS = {
    "door": "door",
    "garage_door": "door",
    "window": "door",
    "opening": "door",
    "motion": "motion",
    "moving": "motion",
    "occupancy": "presence",
    "presence": "presence",
    "vibration": "vibration",
    "tamper": "vibration",
    "sound": "sound",
    "garage": "door",
    "gate": "door",
    "curtain": "door",
    "shutter": "door",
}
_KINDS_BY_DOMAIN = {
    "alarm_control_panel": "alarm",
    "person": "presence",
    "device_tracker": "presence",
    "light": "light",
    "lock": "lock",
    "cover": "door",
}


def entity_kind(hass: HomeAssistant, entity_id: str) -> str:
    """Return how scoring reads an entity, from its domain and device class."""
    domain = entity_id.partition(".")[0]
    state = hass.states.get(entity_id)
    device_class = state.attributes.get(ATTR_DEVICE_CLASS) if state is not None else None
    if device_class is None:
        entry = er.async_get(hass).async_get(entity_id)
        if entry is not None:
            device_class = entry.device_class or entry.original_device_class
    if device_class and (kind := _KINDS_BY_CLASS.get(str(device_class))):
        return kind
    return _KINDS_BY_DOMAIN.get(domain, "other")


def device_area_name(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Return the name of a device's area, if it has one."""
    if device_id is None:
        return None
    device = dr.async_get(hass).async_get(device_id)
    if device is None or device.area_id is None:
        return None
    area = ar.async_get(hass).async_get_area(device.area_id)
    return area.name if area is not None else None


def suggestions(hass: HomeAssistant, device_id: str | None) -> list[str]:
    """Return entities in the camera device's area worth linking (architecture §3.2)."""
    if device_id is None:
        return []
    device = dr.async_get(hass).async_get(device_id)
    if device is None or device.area_id is None:
        return []
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    found: list[str] = []
    for entry in entities.entities.values():
        if entry.disabled_by is not None or entry.hidden_by is not None:
            continue
        if entry.domain not in SUGGESTED_DOMAINS or entry.device_id == device_id:
            continue
        area_id = entry.area_id
        if area_id is None and entry.device_id is not None:
            owner = devices.async_get(entry.device_id)
            area_id = owner.area_id if owner is not None else None
        if area_id != device.area_id:
            continue
        if entry.domain == "binary_sensor":
            device_class = entry.device_class or entry.original_device_class
            if device_class not in _SUGGESTED_BINARY_CLASSES:
                continue
        found.append(entry.entity_id)
    order = {domain: index for index, domain in enumerate(SUGGESTED_DOMAINS)}
    return sorted(found, key=lambda item: (order.get(item.partition(".")[0], 99), item))


def default_weather(hass: HomeAssistant) -> str | None:
    """Return the default weather entity: the first one found, by entity id."""
    found = sorted(state.entity_id for state in hass.states.async_all("weather"))
    return found[0] if found else None


def _number(value: Any) -> float | None:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _entity(state: State | None, kind: str, at: float) -> dict[str, Any]:
    """Return one entity's snapshot relative to a moment."""
    if state is None:
        return {"state": "unknown", "kind": kind, "changed_rel_s": None}
    return {
        "state": state.state,
        "kind": kind,
        # Negative: changed before the moment. "door opened 5 s before" is -5.
        "changed_rel_s": round(state.last_changed.timestamp() - at, 1),
    }


def snapshot(hass: HomeAssistant, linked: list[str], at: float) -> dict[str, Any]:
    """Return the context at one moment: linked entities and the default context."""
    found: dict[str, Any] = {}
    for entity_id in linked:
        found[entity_id] = _entity(hass.states.get(entity_id), entity_kind(hass, entity_id), at)

    defaults: dict[str, Any] = {}
    if (sun := hass.states.get("sun.sun")) is not None:
        defaults["sun"] = _number(sun.attributes.get("elevation"))
        defaults["sun_state"] = sun.state
    if (weather_id := default_weather(hass)) is not None and (
        weather := hass.states.get(weather_id)
    ) is not None:
        defaults["weather"] = weather.state
        defaults["wind"] = _number(weather.attributes.get("wind_speed"))
        defaults["weather_entity"] = weather_id
    found["_defaults"] = {key: value for key, value in defaults.items() if value is not None}
    return found


def late_change(
    entity_id: str, kind: str, old: State | None, new: State | None, *, started: float, ended: float
) -> dict[str, Any] | None:
    """Return one late-evidence item, or None when nothing really changed."""
    if new is None or (old is not None and old.state == new.state):
        return None
    at = new.last_changed.timestamp()
    return {
        "entity": entity_id,
        "kind": kind,
        "from": old.state if old is not None else None,
        "to": new.state,
        # After the end, which is what the window is about; and from the start, for the reader.
        "t_rel_s": round(at - ended, 1),
        "t_from_start_s": round(at - started, 1),
    }
