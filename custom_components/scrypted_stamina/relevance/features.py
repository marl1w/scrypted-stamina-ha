"""Turning an event record into what the relevance model counts.

Reolink Stamina derived its events from binary-sensor transitions; here the plugin hands over
events already formed, so this module does the other half of that file's job — the `Event`
the model counts, and the solar clock — from the facts and the context.

What is counted, and where it goes in the model:

* **kind** — the main class: the most confident confirmed subject, else the most confident
  subject, else `motion`. Counted by the kind term, as in Reolink Stamina.
* **when** — minute of day, minutes from sunset, day of week (the clock, solar and weekend
  terms): together, hour of week.
* **duration** — the event span, bucketed.
* **what fired first** — the predecessor term, unchanged from Reolink Stamina.
* **everything else** is a *signal*: one more independent term each, added rather than
  conditioned on, so none of them divides the history. Numeric ones (dwell, wind, sun
  elevation) are banded by the model from their own history. They are the verdict mix, the
  sources, the subject count, dwell, night mode, tamper, audio, the weather, the sun, each
  linked entity's state at the start, the early evidence from the look-back window, and
  the late evidence from the follow-up window.

No explicit rules: nothing here says "a person at night is unusual". It only names what
happened, and the model learns what is normal for it.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import json
from typing import Any

from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from ..const import NO_AREA
from ..decider import EventRecord, Policy as DeciderPolicy, subject_confirmed, verdict_summary

# How far either side of sunrise or sunset still counts as twilight, for the phrase alone.
TWILIGHT_MINUTES = 30

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MINUTES_PER_DAY = 1440
_HALF_DAY_MINUTES = _MINUTES_PER_DAY // 2

# Late evidence, bucketed by how long after the event's end it came.
LATE_BUCKETS = ((30.0, "<30s"), (120.0, "<2m"), (600.0, "<10m"))

# Early and late evidence, as a person reads the bucket: "changed within 30 s before".
_TIMING = {
    "<30s": "within 30 s",
    "<2m": "within 2 min",
    "<10m": "within 10 min",
    "later": "more than 10 min",
}
# What each kind of linked entity is called when its early or late evidence is a reason.
_KIND_NAMES = {
    "door": "Door sensors",
    "motion": "Motion sensors",
    "presence": "Presence",
    "vibration": "Vibration sensors",
    "sound": "Sound sensors",
    "alarm": "Alarm",
    "light": "Lights",
    "lock": "Locks",
}

# Signal names, as the reason sentence spells them.
LABELS = {
    "verdicts": "verdicts",
    "sources": "detected by",
    "subjects": "subjects",
    "dwell": "dwell",
    "night": "night mode",
    "tamper": "tamper",
    "audio": "sound",
    "weather": "weather",
    "wind": "wind",
    "sun": "sun elevation",
}


@dataclass(slots=True, frozen=True)
class Event:
    """One event, as the model counts it."""

    camera: str
    kind: str
    started_at: float
    ended_at: float | None
    duration: float | None
    minute_of_day: int
    solar_offset: int | None
    solar_phase: str | None
    is_weekend: bool
    day_of_week: str = ""
    # Every signal as (name, raw value), sorted, so two events compare equal.
    context: tuple[tuple[str, str], ...] = ()
    # The camera device's area slug when the event happened, for the per-area scope.
    area: str = NO_AREA
    # How many events this one stands for: more than one once the index thinned its peers.
    # Not part of the stored features: it lives in the index's own column.
    weight: int = 1


def _wrap(minutes: float) -> int:
    """Fold a minute offset into ±720, so midnight is not a discontinuity."""
    value = round(minutes) % _MINUTES_PER_DAY
    if value >= _HALF_DAY_MINUTES:
        value -= _MINUTES_PER_DAY
    return value


class SolarClock:
    """Where the sun was, remembered per date (ported from Reolink Stamina).

    The *offset* from sunset is what the model counts; the *phase* is what a person reads.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Prepare a clock for this installation's location."""
        self._hass = hass
        self._events: dict[tuple[str, dt.date], dt.datetime | None] = {}

    def _event(self, name: str, day: dt.date) -> dt.datetime | None:
        key = (name, day)
        if key not in self._events:
            try:
                self._events[key] = get_astral_event_date(self._hass, name, day)
            except (ValueError, TypeError):
                self._events[key] = None
        return self._events[key]

    def offset(self, moment: dt.datetime) -> int | None:
        """Return minutes from that day's sunset, negative before it."""
        sunset = self._event(SUN_EVENT_SUNSET, moment.date())
        if sunset is None:
            return None
        return _wrap((moment - sunset).total_seconds() / 60.0)

    def phase(self, moment: dt.datetime) -> str | None:
        """Return "dawn", "day", "dusk" or "night"."""
        day = moment.date()
        sunrise = self._event(SUN_EVENT_SUNRISE, day)
        sunset = self._event(SUN_EVENT_SUNSET, day)
        if sunrise is None or sunset is None:
            return None
        edge = dt.timedelta(minutes=TWILIGHT_MINUTES)
        if abs(moment - sunrise) <= edge:
            return "dawn"
        if abs(moment - sunset) <= edge:
            return "dusk"
        return "day" if sunrise < moment < sunset else "night"


def main_kind(record: EventRecord, policy: DeciderPolicy) -> str:
    """Return the class the event is counted as."""
    subjects = list(record.facts.subjects.values())
    if not subjects:
        return "motion"
    confirmed = [s for s in subjects if subject_confirmed(s, policy)]
    pool = confirmed or subjects
    return max(pool, key=lambda subject: subject.score).cls or "motion"


def late_bucket(seconds: float) -> str:
    """Return how long after the end a change came, as a countable label."""
    for edge, label in LATE_BUCKETS:
        if seconds <= edge:
            return label
    return "later"


def late_signals(late: list[dict[str, Any]]) -> dict[str, str]:
    """Return the late-evidence signals: per entity kind, the first change's delay.

    Every kind that has a linked entity but no change reads `none` — a category, and the
    common one, which is what makes a door opening ninety seconds after a vetoed shape at
    the gate rare. An alarm that was triggered says so instead of a delay.
    """
    signals: dict[str, str] = {}
    for item in sorted(late, key=lambda change: float(change.get("t_rel_s", 0.0))):
        kind = str(item.get("kind") or "other")
        key = f"late:{kind}"
        if str(item.get("to")) == "triggered":
            signals[key] = "triggered"
            continue
        signals.setdefault(key, late_bucket(float(item.get("t_rel_s", 0.0))))
    return signals


def early_signals(early: list[dict[str, Any]]) -> dict[str, str]:
    """Return the early-evidence signals: per entity kind, the change nearest the start.

    The mirror of `late_signals`, bucketed the same way by how long before the start it
    came. Every kind with a linked entity but no change reads `none`; an alarm that was
    triggered says so instead.
    """
    found: dict[str, str] = {}
    for item in sorted(early, key=lambda change: abs(float(change.get("t_rel_s", 0.0)))):
        kind = str(item.get("kind") or "other")
        key = f"early:{kind}"
        if str(item.get("to")) == "triggered":
            found[key] = "triggered"
            continue
        found.setdefault(key, late_bucket(abs(float(item.get("t_rel_s", 0.0)))))
    return found


def evidence_name(name: str) -> str | None:
    """Return what an early or late signal is called in a reason, or None for any other."""
    side, _, kind = name.partition(":")
    if side not in ("early", "late") or not kind:
        return None
    return _KIND_NAMES.get(kind, "Linked sensors")


def evidence_reading(name: str, value: str) -> str:
    """Return an early or late signal's value as a person reads it; any other as it is.

    Display only: the model counts the raw value, and this never reaches it.
    """
    side, _, kind = name.partition(":")
    if side not in ("early", "late") or not kind:
        return value
    edge = "before" if side == "early" else "after"
    if value == "none":
        return f"no change {edge}"
    if value == "triggered":
        return f"triggered {edge}"
    if value in _TIMING:
        return f"changed {_TIMING[value]} {edge}"
    return value


# What a split camera's whole-frame AI counts as: a source of its own, so it is learned as
# weak evidence and never mistaken for the view's own camera AI.
WHOLE_FRAME = "whole-frame"


def _sources(subjects: list[Any]) -> str:
    """Return the sources of an event as one countable label."""
    found = {source for subject in subjects for source in subject.sources}
    if any(subject.ambiguous for subject in subjects):
        found.add(WHOLE_FRAME)
    return "+".join(sorted(found))[:96] or "none"


def _flag(value: bool | None) -> str:
    return "unknown" if value is None else ("on" if value else "off")


def signals(
    record: EventRecord,
    policy: DeciderPolicy,
    *,
    linked_kinds: dict[str, str] | None = None,
    include_late: bool = True,
) -> dict[str, str]:
    """Return every signal of an event, as raw strings."""
    facts = record.facts
    subjects = list(facts.subjects.values())
    found: dict[str, str] = {
        "verdicts": verdict_summary(facts, policy) if subjects else "none",
        "sources": _sources(subjects),
        "subjects": str(min(len(subjects), 3)) if len(subjects) < 3 else "3+",
        "dwell": f"{max((subject.dwell for subject in subjects), default=0.0):.1f}",
        "night": _flag(facts.signals.get("night_mode")),
        "tamper": _flag(facts.signals.get("tamper")),
        "audio": _flag(facts.signals.get("audio")),
        "trigger": facts.trigger or "unknown",
    }
    defaults = record.context_start.get("_defaults", {})
    if isinstance(defaults, dict):
        for name in ("weather", "wind", "sun"):
            if (value := defaults.get(name)) is not None:
                found[name] = str(value)
    for entity_id, item in record.context_start.items():
        if entity_id.startswith("_") or not isinstance(item, dict):
            continue
        found[entity_id] = str(item.get("state", "unknown"))

    # Known from the start, so counted by the live score too.
    early = early_signals(record.early)
    for kind in sorted(set((linked_kinds or {}).values())):
        found.setdefault(f"early:{kind}", early.get(f"early:{kind}", "none"))
    found.update(early)

    if include_late:
        late = late_signals(record.late)
        for kind in sorted(set((linked_kinds or {}).values())):
            found.setdefault(f"late:{kind}", late.get(f"late:{kind}", "none"))
        found.update(late)
    return found


def to_event(
    record: EventRecord,
    clock: SolarClock,
    policy: DeciderPolicy,
    *,
    linked_kinds: dict[str, str] | None = None,
    include_late: bool = True,
    now: float | None = None,
) -> Event:
    """Return the model's view of one event record.

    `now` closes an event still open, for the live score only: the duration so far.
    """
    facts = record.facts
    ended = facts.ended if facts.ended is not None else now
    local = dt_util.as_local(dt_util.utc_from_timestamp(facts.started))
    found = signals(record, policy, linked_kinds=linked_kinds, include_late=include_late)
    return Event(
        camera=facts.camera,
        kind=main_kind(record, policy),
        started_at=facts.started,
        ended_at=ended,
        duration=None if ended is None else round(max(0.0, ended - facts.started), 3),
        minute_of_day=local.hour * 60 + local.minute,
        solar_offset=clock.offset(local),
        solar_phase=clock.phase(local),
        is_weekend=local.weekday() >= 5,
        day_of_week=_DAYS[local.weekday()],
        context=tuple(sorted(found.items())),
        area=record.area or NO_AREA,
    )


def event_to_json(event: Event) -> str:
    """Serialise the model's view of a final event, for the index's features column."""
    return json.dumps(
        {
            "camera": event.camera,
            "kind": event.kind,
            "started_at": event.started_at,
            "ended_at": event.ended_at,
            "duration": event.duration,
            "minute_of_day": event.minute_of_day,
            "solar_offset": event.solar_offset,
            "solar_phase": event.solar_phase,
            "is_weekend": event.is_weekend,
            "day_of_week": event.day_of_week,
            "context": [list(pair) for pair in event.context],
            "area": event.area,
        },
        separators=(",", ":"),
    )


def event_from_json(raw: str, weight: int = 1) -> Event:
    """Read back what `event_to_json` wrote, with the row's weight."""
    data = json.loads(raw)
    data["context"] = tuple((str(k), str(v)) for k, v in data.get("context") or ())
    data["weight"] = max(1, int(weight))
    return Event(**data)
