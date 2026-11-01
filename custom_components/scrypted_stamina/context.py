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
window, changes of the linked entities are collected as late evidence; and the changes in
the look-back window before the start, kept in a small in-memory ring (`RecentChanges`), as
early evidence ("gate opened 20 s before").
"""

from __future__ import annotations

from collections import deque
from typing import Any

from homeassistant.const import ATTR_DEVICE_CLASS
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
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


# States that are not a reading: a change into or out of one is an integration starting,
# stopping or losing its device, never something that happened outdoors.
_NOT_READINGS = frozenset({"unknown", "unavailable"})
# Changes kept per entity. A sensor flapping more than this within the look-back window says
# nothing more by its twentieth change; the newest are the ones nearest the event.
_RECENT_PER_ENTITY = 16


class RecentChanges:
    """The last few changes of every linked entity, for the look-back window.

    In memory only, and that is deliberate: the window is minutes long, so a restart loses at
    most one window's worth, while asking the recorder at every event start would be a
    database query per event. Changes into or out of `unknown` and `unavailable` are not
    kept, so the states settling after a restart are not read as activity.
    """

    def __init__(self, keep_seconds: float) -> None:
        """Keep changes this long."""
        self._keep = keep_seconds
        self._changes: dict[str, deque[tuple[float, str, str]]] = {}

    def add(self, entity_id: str, old: State | None, new: State | None) -> None:
        """Remember one state change, when it is a real one."""
        if old is None or new is None or old.state == new.state:
            return
        if old.state in _NOT_READINGS or new.state in _NOT_READINGS:
            return
        at = new.last_changed.timestamp()
        changes = self._changes.setdefault(entity_id, deque(maxlen=_RECENT_PER_ENTITY))
        while changes and changes[0][0] < at - self._keep:
            changes.popleft()
        changes.append((at, old.state, new.state))

    def between(
        self, kinds: dict[str, str], *, started: float, since: float, until: float
    ) -> list[dict[str, Any]]:
        """Return the changes of these entities from `since` to `until`, oldest first.

        `kinds` maps each entity to its kind. Each item's `t_rel_s` is relative to the start:
        negative before it ("gate opened 20 s before" is -20). A change a moment after the
        start, made while the start was on its way to Home Assistant, is kept as well.
        """
        found = [
            {
                "entity": entity_id,
                "kind": kind,
                "from": old,
                "to": new,
                "t_rel_s": round(at - started, 1),
            }
            for entity_id, kind in kinds.items()
            for at, old, new in self._changes.get(entity_id, ())
            if since <= at <= until
        ]
        found.sort(key=lambda item: item["t_rel_s"])
        return found
