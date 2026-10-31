"""The plugin's messages, parsed into facts the rest of the integration can trust.

Everything that arrives from the plugin is validated here and nowhere else. Ids end up in
folder names and URLs, so they are held to a strict alphabet; scores are clamped; strings
are bounded. A message that does not parse is dropped with a debug line rather than raising
into the link, because one bad message must not cost the stream.

The shapes are `docs/api.md` (API v1): times in epoch milliseconds (converted to seconds
here), segments numbered from 0, the camera as `{id, name}`, subjects with `first`/`last`,
`max_score` and `sources` keyed by source plugin, camera context under
`context.at_start`/`current`, and hop timestamps under `latency`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Ids from the plugin: `ev_…` events, Scrypted device ids for cameras, short subject ids.
# Used in paths, so strict, and never containing `..`.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_MAX_TEXT = 128
_MAX_SUBJECTS = 64
_MAX_SOURCES = 16
_MAX_SEGMENTS = 10_000_000
# Per-segment metadata kept for at most this many segments (a 10 min event at 1 s is 600).
_MAX_DURATIONS = 4096

VERDICT_CONFIRMED = "confirmed"
VERDICT_VETOED = "vetoed"
VERDICT_UNVERIFIED = "unverified"
VERDICT_PENDING = "pending"
VERDICTS = frozenset({VERDICT_CONFIRMED, VERDICT_VETOED, VERDICT_UNVERIFIED, VERDICT_PENDING})

MSG_HELLO = "hello"
MSG_START = "event.start"
MSG_UPDATE = "event.update"
MSG_END = "event.end"
MSG_VERDICT = "subject.verdict"
MSG_SEGMENT = "event.segment"
EVENT_MESSAGES = frozenset({MSG_START, MSG_UPDATE, MSG_END, MSG_VERDICT, MSG_SEGMENT})

# `segments.state` values after which no more segments will be cut.
# `segments.state` values that are final once the event is closed. While it is open none of
# them is: a `released` (or idle) segmenter can resume, with a discontinuity and a new init.
SEGMENTS_FINAL = frozenset({"ended", "failed", "none", "released"})
# Later inits (after an ffmpeg restart) are numbered; this many at most are followed.
_MAX_INITS = 64

# Camera context features (`context.at_start` / `current`) kept as event signals.
CONTEXT_FEATURES = ("night_mode", "tamper", "audio", "motion", "occupied", "online")


class ProtocolError(ValueError):
    """Raised when a message cannot be read."""


def valid_id(value: object) -> str | None:
    """Return the id if it is safe to use in a path, else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and _ID.match(value) and ".." not in value:
        return value
    return None


def _text(value: object, default: str = "") -> str:
    """Return a bounded string."""
    if value is None:
        return default
    return str(value)[:_MAX_TEXT]


def _clamp(value: object, low: float = 0.0, high: float = 1.0) -> float | None:
    """Return a number clamped to a range, or None if it is not a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value:  # NaN
        return None
    return max(low, min(high, float(value)))


def _seconds(value: object) -> float | None:
    """Return a plugin timestamp (epoch milliseconds) as epoch seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    # Anything below 1e11 is already seconds; the plugin sends milliseconds.
    return float(value) / 1000.0 if value > 1e11 else float(value)


def _count(value: object, high: int = _MAX_SEGMENTS) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return min(value, high)


def _pick(data: Mapping[str, Any], *names: str) -> Any:
    """Return the first present field among a few aliases."""
    for name in names:
        if name in data:
            return data[name]
    return None


@dataclass(slots=True)
class Subject:
    """One physical thing seen in an event, and what the verifiers made of it."""

    id: str
    cls: str
    verdict: str = VERDICT_PENDING
    sources: tuple[str, ...] = ()
    score: float = 0.0
    first_seen: float | None = None
    last_seen: float | None = None
    label: str | None = None
    # Sources flagged `ambiguous` (a split camera's whole-frame AI, docs/api.md item 24):
    # weak evidence of their own, never confirming and never the view's own camera AI.
    ambiguous: tuple[str, ...] = ()

    @property
    def dwell(self) -> float:
        """Return how long it was seen, in seconds."""
        if self.first_seen is None or self.last_seen is None:
            return 0.0
        return max(0.0, self.last_seen - self.first_seen)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the index, `event.json` and entity attributes."""
        return {
            "id": self.id,
            "class": self.cls,
            "verdict": self.verdict,
            "sources": list(self.sources),
            "score": round(self.score, 3),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "label": self.label,
            "ambiguous": list(self.ambiguous),
        }

    @classmethod
    def parse(cls, data: object) -> Subject | None:
        """Read one subject object, or None when it is unusable."""
        if not isinstance(data, Mapping):
            return None
        subject_id = valid_id(data.get("id"))
        if subject_id is None:
            return None
        verdict = _text(data.get("verdict"), VERDICT_PENDING)
        raw_sources = data.get("sources") or ()
        names = list(raw_sources) if isinstance(raw_sources, (Mapping, list, tuple)) else []
        ambiguous: list[str] = []
        if isinstance(raw_sources, Mapping):
            for name, detail in raw_sources.items():
                if isinstance(detail, Mapping) and detail.get("ambiguous") is True:
                    ambiguous.append(name)
            names = [name for name in names if name not in ambiguous]
        label = None
        for item in data.get("enrichments") or ():
            if isinstance(item, Mapping) and item.get("label"):
                label = _text(item["label"])
                break
        return cls(
            id=subject_id,
            cls=_text(_pick(data, "class", "cls"), "unknown") or "unknown",
            verdict=verdict if verdict in VERDICTS else VERDICT_PENDING,
            sources=tuple(sorted({_text(name) for name in names if name}))[:_MAX_SOURCES],
            score=_clamp(_pick(data, "max_score", "score")) or 0.0,
            first_seen=_seconds(_pick(data, "first", "firstSeen")),
            last_seen=_seconds(_pick(data, "last", "lastSeen")),
            label=label,
            ambiguous=tuple(sorted({_text(name) for name in ambiguous if name}))[:_MAX_SOURCES],
        )

    @classmethod
    def restore(cls, data: Mapping[str, Any]) -> Subject:
        """Read back what `as_dict` wrote."""
        return cls(
            id=str(data["id"]),
            cls=str(data.get("class", "unknown")),
            verdict=str(data.get("verdict", VERDICT_PENDING)),
            sources=tuple(data.get("sources") or ()),
            score=float(data.get("score") or 0.0),
            first_seen=data.get("first_seen"),
            last_seen=data.get("last_seen"),
            label=data.get("label"),
            ambiguous=tuple(data.get("ambiguous") or ()),
        )


@dataclass(slots=True)
class EventFacts:
    """What the plugin says about one event. Facts only: no decisions."""

    id: str
    camera: str
    started: float
    ended: float | None = None
    camera_name: str | None = None
    subjects: dict[str, Subject] = field(default_factory=dict)
    # Segments cut so far: numbers 0 .. segments-1. `init` once init.mp4 exists.
    segments: int = 0
    init: bool = False
    # True once the segmenter has stopped: no more segments will be cut.
    segments_final: bool = False
    durations: dict[int, float] = field(default_factory=dict)
    # Segments that start afresh (the plugin's ffmpeg restarted mid-event).
    discontinuities: list[int] = field(default_factory=list)
    # The init each segment needs, where it is not init.mp4 (k >= 1: init-<k>.mp4).
    init_of: dict[int, int] = field(default_factory=dict)
    # Camera context features, true if they were on at any point of the event.
    signals: dict[str, bool] = field(default_factory=dict)
    # Hop timestamps (`latency`), epoch seconds.
    stamps: dict[str, float] = field(default_factory=dict)
    camera_offline: bool = False
    end_reason: str | None = None
    continues: str | None = None
    trigger: str | None = None

    @property
    def duration(self) -> float | None:
        """Return the event's length in seconds, None while it is open."""
        return None if self.ended is None else max(0.0, self.ended - self.started)

    def segment_seen(
        self,
        number: int,
        *,
        init: int = 0,
        duration: float | None,
        last: bool = False,
        discontinuity: bool = False,
    ) -> None:
        """Record one cut segment (its init exists before it, docs/api.md)."""
        self.segments = max(self.segments, min(number + 1, _MAX_SEGMENTS))
        self.init = True
        if init and len(self.init_of) < _MAX_DURATIONS:
            self.init_of[number] = init
        if duration is not None and len(self.durations) < _MAX_DURATIONS:
            self.durations[number] = duration
        if (
            discontinuity
            and number not in self.discontinuities
            and len(self.discontinuities) < _MAX_DURATIONS
        ):
            self.discontinuities.append(number)
        if last:
            self.segments_final = True

    def merge(self, other: EventFacts) -> None:
        """Fold a later message's facts into these, never losing what was known."""
        if other.ended is not None:
            self.ended = other.ended
        self.camera_name = other.camera_name or self.camera_name
        self.subjects.update(other.subjects)
        self.segments = max(self.segments, other.segments)
        self.init = self.init or other.init
        self.segments_final = self.segments_final or other.segments_final
        for number, value in other.durations.items():
            if number in self.durations or len(self.durations) < _MAX_DURATIONS:
                self.durations[number] = value
        for number, init in other.init_of.items():
            if len(self.init_of) < _MAX_DURATIONS:
                self.init_of[number] = init
        for number in other.discontinuities:
            if number not in self.discontinuities:
                self.discontinuities.append(number)
        # A signal that was on at any point during the event stays on: tamper for one
        # second is a tampered event.
        for key, value in other.signals.items():
            self.signals[key] = self.signals.get(key, False) or value
        for key, value in other.stamps.items():
            self.stamps.setdefault(key, value)
        self.camera_offline = self.camera_offline or other.camera_offline
        self.end_reason = other.end_reason or self.end_reason
        self.continues = other.continues or self.continues
        self.trigger = self.trigger or other.trigger

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the index and `event.json`."""
        return {
            "id": self.id,
            "camera": self.camera,
            "camera_name": self.camera_name,
            "started": self.started,
            "ended": self.ended,
            "subjects": [subject.as_dict() for subject in self.subjects.values()],
            "segments": self.segments,
            "init": self.init,
            "segments_final": self.segments_final,
            "durations": {str(k): v for k, v in self.durations.items()},
            "discontinuities": list(self.discontinuities),
            "init_of": {str(k): v for k, v in self.init_of.items()},
            "signals": dict(self.signals),
            "stamps": dict(self.stamps),
            "camera_offline": self.camera_offline,
            "end_reason": self.end_reason,
            "continues": self.continues,
            "trigger": self.trigger,
        }

    @classmethod
    def restore(cls, data: Mapping[str, Any]) -> EventFacts:
        """Read back what `as_dict` wrote."""
        subjects = [Subject.restore(item) for item in data.get("subjects") or ()]
        return cls(
            id=str(data["id"]),
            camera=str(data["camera"]),
            camera_name=data.get("camera_name"),
            started=float(data["started"]),
            ended=data.get("ended"),
            subjects={subject.id: subject for subject in subjects},
            segments=int(data.get("segments") or 0),
            init=bool(data.get("init")),
            segments_final=bool(data.get("segments_final")),
            durations={int(k): float(v) for k, v in (data.get("durations") or {}).items()},
            discontinuities=[int(n) for n in data.get("discontinuities") or ()],
            init_of={int(k): int(v) for k, v in (data.get("init_of") or {}).items()},
            signals={str(k): bool(v) for k, v in (data.get("signals") or {}).items()},
            stamps={str(k): float(v) for k, v in (data.get("stamps") or {}).items()},
            camera_offline=bool(data.get("camera_offline")),
            end_reason=data.get("end_reason"),
            continues=data.get("continues"),
            trigger=data.get("trigger"),
        )

    @classmethod
    def parse(cls, data: object) -> EventFacts:
        """Read one `event` object."""
        if not isinstance(data, Mapping):
            raise ProtocolError("event is not an object")
        camera_raw = data.get("camera")
        camera_name = None
        if isinstance(camera_raw, Mapping):
            camera_name = _text(camera_raw.get("name")) or None
            camera_raw = camera_raw.get("id")
        event_id = valid_id(data.get("id"))
        camera = valid_id(camera_raw)
        started = _seconds(_pick(data, "start", "started"))
        if event_id is None or camera is None or started is None:
            raise ProtocolError("event lacks a usable id, camera or start")

        subjects: dict[str, Subject] = {}
        raw_subjects = data.get("subjects") or ()
        if isinstance(raw_subjects, list):
            for raw in raw_subjects[:_MAX_SUBJECTS]:
                if (subject := Subject.parse(raw)) is not None:
                    subjects[subject.id] = subject

        count, init, final = 0, False, False
        segments = data.get("segments")
        if isinstance(segments, Mapping):
            count = _count(segments.get("next")) or 0
            init = bool(segments.get("init"))
            final = str(segments.get("state")) in SEGMENTS_FINAL

        signals: dict[str, bool] = {}
        context = data.get("context")
        if isinstance(context, Mapping):
            for part in ("at_start", "current"):
                values = context.get(part)
                if not isinstance(values, Mapping):
                    continue
                for name in CONTEXT_FEATURES:
                    value = values.get(name)
                    if isinstance(value, bool):
                        signals[name] = signals.get(name, False) or value
        flags = data.get("flags") if isinstance(data.get("flags"), Mapping) else {}
        latency = data.get("latency") if isinstance(data.get("latency"), Mapping) else {}
        trigger = data.get("trigger") if isinstance(data.get("trigger"), Mapping) else {}
        state = _text(data.get("state"))
        ended = _seconds(data.get("end"))
        if ended is None and state == "closed":
            ended = _seconds(data.get("last_observation")) or started
        # Provisional until the event is closed (docs/api.md, changes 14-22).
        final = final and ended is not None
        return cls(
            id=event_id,
            camera=camera,
            camera_name=camera_name,
            started=started,
            ended=ended,
            subjects=subjects,
            segments=count,
            init=init,
            segments_final=final,
            signals=signals,
            stamps={
                _text(key): stamp
                for key, value in latency.items()
                if (stamp := _seconds(value)) is not None
            },
            camera_offline=bool(flags.get("camera_offline")),
            end_reason=_text(data.get("end_reason")) or None,
            continues=valid_id(data.get("continues")),
            trigger=_text(trigger.get("kind")) or None,
        )


@dataclass(slots=True)
class Message:
    """One message from the stream."""

    seq: int | None
    epoch: str
    type: str
    event: EventFacts | None = None
    event_id: str | None = None
    # `event.segment`: the segment number, whether init exists, and its duration.
    segment: int | None = None
    # `cut`, `evicted` or `last` (docs/api.md, changes 1).
    segment_kind: str = "cut"
    # The init a cut segment needs: 0 is init.mp4, k is init-<k>.mp4.
    segment_init: int = 0
    segment_evicted: list[int] = field(default_factory=list)
    segment_next: int | None = None
    segment_duration: float | None = None
    segment_last: bool = False
    segment_discontinuity: bool = False
    # `subject.verdict`: the subject.
    subject: Subject | None = None
    # `hello`: the stream's own report of what resuming found.
    gap: bool = False
    reset: bool = False
    # `hello`: the newest seq the plugin has, which the link must reach to be caught up.
    last_seq: int | None = None


def _parse_segment(message: Message, data: Mapping[str, Any]) -> None:
    """Read an `event.segment` message of any kind."""
    kind = _text(data.get("kind"), "cut") or "cut"
    message.segment_kind = kind
    message.segment_last = data.get("last") is True or kind == "last"
    if kind == "evicted":
        raw = data.get("evicted") or ()
        message.segment_evicted = [
            n for item in list(raw)[:_MAX_DURATIONS] if (n := _count(item)) is not None
        ]
        return
    if kind == "last":
        message.segment_next = _count(data.get("next"))
        return
    segment = data.get("segment")
    if not isinstance(segment, Mapping):
        raise ProtocolError("event.segment carries no segment")
    number = _count(segment.get("n"))
    if number is None:
        raise ProtocolError("event.segment has no number")
    message.segment = number
    init = segment.get("init", 0)
    # Booleans from the first draft meant "init.mp4 exists": init 0.
    message.segment_init = 0 if isinstance(init, bool) else (_count(init, high=_MAX_INITS) or 0)
    message.segment_duration = _clamp(segment.get("duration_s"), 0.0, 60.0)
    message.segment_discontinuity = segment.get("discontinuity") is True


def parse_message(data: object) -> Message:
    """Turn one SSE `data:` object into a message, or raise `ProtocolError`."""
    if not isinstance(data, Mapping):
        raise ProtocolError("message is not an object")
    kind = _text(data.get("type"))
    epoch = _text(data.get("epoch"))
    if kind == MSG_HELLO:
        # A greeting has no seq and must never be stored as a resume point.
        return Message(
            seq=None,
            epoch=epoch,
            type=kind,
            gap=bool(data.get("gap")),
            reset=bool(data.get("reset")),
            last_seq=_count(data.get("last_seq"), high=2**62),
        )
    if kind not in EVENT_MESSAGES:
        raise ProtocolError(f"unknown message type {kind!r}")
    seq = data.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ProtocolError("message has no seq")
    message = Message(seq=seq, epoch=epoch, type=kind)

    if isinstance(data.get("event"), Mapping):
        message.event = EventFacts.parse(data["event"])
        message.event_id = message.event.id
    else:
        message.event_id = valid_id(_pick(data, "event_id", "eventId"))
    if message.event_id is None:
        raise ProtocolError(f"{kind} names no event")

    if kind == MSG_SEGMENT:
        _parse_segment(message, data)
    if kind == MSG_VERDICT:
        message.subject = Subject.parse(data.get("subject"))
        if message.subject is None:
            raise ProtocolError("subject.verdict names no subject")
    if kind == MSG_START and message.event is None:
        raise ProtocolError("event.start carries no event")
    return message


@dataclass(slots=True, frozen=True)
class CameraInfo:
    """One camera with the Stamina mixin, and what it can do."""

    id: str
    name: str
    capabilities: dict[str, Any]

    @classmethod
    def parse(cls, data: object) -> CameraInfo | None:
        """Read one camera from `GET /v1/cameras`."""
        if not isinstance(data, Mapping):
            return None
        camera_id = valid_id(data.get("id"))
        if camera_id is None:
            return None
        capabilities = data.get("capabilities")
        capabilities = dict(capabilities) if isinstance(capabilities, Mapping) else {}
        sources = data.get("sources")
        if isinstance(sources, list):
            capabilities["sources"] = [
                {"source": item.get("source"), "role": item.get("role")}
                for item in sources[:_MAX_SOURCES]
                if isinstance(item, Mapping)
            ]
        if "online" in data:
            capabilities["online"] = bool(data.get("online"))
        return cls(
            id=camera_id,
            name=_text(data.get("name"), camera_id) or camera_id,
            capabilities=_bounded(capabilities),
        )


def _bounded(value: Any, depth: int = 0) -> Any:
    """Return a JSON-ish value with strings and nesting bounded, for attributes."""
    if depth > 4:
        return None
    if isinstance(value, Mapping):
        return {_text(k): _bounded(v, depth + 1) for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in list(value)[:64]]
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _text(value)
