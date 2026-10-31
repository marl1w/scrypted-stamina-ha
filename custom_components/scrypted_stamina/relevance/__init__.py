"""Relevance: the learned "normal" per camera, and how far an event is from it.

Ported from Reolink Stamina's `relevance/`: `rates.py` (circular rates, categoricals, the
backoff, the predecessor term) and `score.py` (surprisal terms, calibration, the sentence)
are its code with the pooling scope changed from per recorder to per area. `features.py`
replaces the transition journal: the plugin delivers whole events, and the index holds them.

The model is rebuilt from the index rather than updated in place: at setup and nightly, in
an executor thread (Reolink Stamina 1.9.1), with the nightly trigger a `@callback` so the
rebuild is scheduled from the event loop (Reolink Stamina #9).

Nothing is flagged while a camera is learning: until it has `SCORE_MIN_DAYS` and
`SCORE_MIN_EVENTS` behind it, calibration gives it no threshold and `unusual` is False.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import datetime as dt
import json
import logging
import math
import time

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change

from ..const import (
    DEFAULT_RELEVANCE_SCOPE,
    DEFAULT_RELEVANCE_SENSITIVITY,
    RELEVANCE_HISTORY_DAYS,
    RELEVANCE_SENSITIVITY_FLOORS,
)
from ..decider import EventRecord, Policy
from ..index import record_from_dict
from .features import LABELS, Event, SolarClock, event_from_json, event_to_json, to_event
from .rates import Model, build, group_key
from .score import Score, calibrate, ready, score

__all__ = ["Analysis", "RelevanceResult"]

_LOGGER = logging.getLogger(__name__)

# Not on the hour (as in Reolink Stamina).
_REBUILD_HOUR = 3
_REBUILD_MINUTE = 17

# How far back the in-memory list of recent events reaches, for the predecessor term.
_RECENT_SECONDS = 3600.0


@dataclass(slots=True, frozen=True)
class RelevanceResult:
    """One event's score, ready for the record."""

    # 0..1, 0.5 at the camera's line: what the entities show.
    score: float
    # The sum of surprisals, in nats: what the model computed.
    surprisal: float
    threshold: float | None
    unusual: bool
    reason: str
    reasons: list[str]


def normalised(total: float, line: float) -> float:
    """Map a surprisal onto 0..1, with the line an event must clear at 0.5."""
    return round(1.0 / (1.0 + math.exp(-(total - line))), 4)


class Analysis:
    """The current model, and scoring against it."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        policy: Policy,
        read_rows: Callable[[float], list[tuple[str, bool, str | None, str, int]]],
        store_features: Callable[[list[tuple[str, str]]], Awaitable[None]] | None = None,
        cameras: Callable[[], list[str]] | None = None,
        scope: str = DEFAULT_RELEVANCE_SCOPE,
        sensitivity: str = DEFAULT_RELEVANCE_SENSITIVITY,
        linked_kinds: Callable[[str], dict[str, str]] | None = None,
        names: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        """Prepare an analysis.

        `read_rows(since)` is synchronous and runs in the executor: it returns each event's
        stored features (or, for events from before features were stored, its record), and
        how many events the row stands for after thinning.
        `cameras()` names the cameras whose linked-entity kinds are worked out up front, on
        the event loop, because that reads Home Assistant's state.
        """
        self._hass = hass
        self._policy = policy
        self._read_rows = read_rows
        self._store_features = store_features
        self._cameras = cameras or list
        self._scope = scope
        self._floor = RELEVANCE_SENSITIVITY_FLOORS.get(
            sensitivity, RELEVANCE_SENSITIVITY_FLOORS[DEFAULT_RELEVANCE_SENSITIVITY]
        )
        self._linked_kinds = linked_kinds or (lambda _camera: {})
        self._names = names or dict
        self._clock = SolarClock(hass)
        self._model = Model(scope=scope, floor=self._floor)
        self._recent: dict[str, Event] = {}
        self._cancel: CALLBACK_TYPE | None = None

    @property
    def model(self) -> Model:
        """Return the model as it was last built."""
        return self._model

    def state(self, camera: str) -> str:
        """Return `learning` or `active` for a camera."""
        profile = self._model.per_camera.get(camera)
        return "active" if profile is not None and ready(profile) else "learning"

    def coverage(self, camera: str) -> dict[str, float | int]:
        """Return how much history is behind a camera."""
        profile = self._model.per_camera.get(camera)
        if profile is None:
            return {"days": 0.0, "events": 0}
        return {"days": round(profile.days, 1), "events": profile.events}

    # ------------------------------------------------------------------ schedule

    def async_schedule(self) -> None:
        """Rebuild nightly, from a callback on the event loop."""
        self.async_cancel()

        @callback
        def _async_rebuild_now(_now: dt.datetime) -> None:
            self._hass.async_create_task(self.async_rebuild())

        self._cancel = async_track_time_change(
            self._hass, _async_rebuild_now, hour=_REBUILD_HOUR, minute=_REBUILD_MINUTE, second=0
        )

    def async_cancel(self) -> None:
        """Stop rebuilding."""
        if self._cancel is not None:
            self._cancel()
            self._cancel = None

    async def async_rebuild(self) -> Model:
        """Learn from the index again: reading, parsing and counting all off the loop."""
        since = time.time() - RELEVANCE_HISTORY_DAYS * 86400
        kinds = {camera: self._linked_kinds(camera) for camera in self._cameras()}
        events, model, backfill = await self._hass.async_add_executor_job(self._learn, since, kinds)
        self._model = model
        if backfill and self._store_features is not None:
            # Final events from before features were stored: written once, in chunks, so
            # the next rebuild reads them instead of recomputing.
            await self._store_features(backfill)
        cutoff = time.time() - _RECENT_SECONDS
        for event in events:
            if event.started_at >= cutoff:
                self._recent.setdefault(f"{event.camera}:{event.started_at}", event)
        _LOGGER.debug(
            "Relevance model rebuilt from %s events across %s cameras, scope %s",
            len(events),
            len(model.per_camera),
            self._scope,
        )
        return model

    def _learn(
        self, since: float, kinds: dict[str, dict[str, str]]
    ) -> tuple[list[Event], Model, list[tuple[str, str]]]:
        """Read events, turn them into features and learn a model (executor thread)."""
        events: list[Event] = []
        backfill: list[tuple[str, str]] = []
        for event_id, finalized, features, raw, weight in self._read_rows(since):
            try:
                if features:
                    events.append(event_from_json(features, weight))
                    continue
                record = record_from_dict(json.loads(raw))
            except (ValueError, KeyError, TypeError):
                continue
            event = to_event(
                record, self._clock, self._policy, linked_kinds=kinds.get(record.camera)
            )
            events.append(event)
            if finalized:
                backfill.append((event_id, event_to_json(event)))
        events.sort(key=lambda item: item.started_at)
        model = build(events, now=time.time(), scope=self._scope)
        calibrate(model, events, floor=self._floor)
        return events, model, backfill

    def features(self, record: EventRecord) -> str:
        """Return a final event's features, as stored with its row."""
        return event_to_json(
            to_event(
                record,
                self._clock,
                self._policy,
                linked_kinds=self._linked_kinds(record.camera),
            )
        )

    # ------------------------------------------------------------------ scoring

    def _previous(self, event: Event, event_id: str) -> Event | None:
        """Return what fired last before this event, inside its pool."""
        group = group_key(event.camera, self._scope, event.area)
        best: Event | None = None
        for other_id, other in self._recent.items():
            if other_id == event_id or other.started_at >= event.started_at:
                continue
            if group_key(other.camera, self._scope, other.area) != group:
                continue
            if best is None or other.started_at > best.started_at:
                best = other
        return best

    def remember(self, record: EventRecord, now: float) -> None:
        """Keep an event for the predecessor term of the ones after it."""
        self._recent[record.id] = to_event(
            record,
            self._clock,
            self._policy,
            linked_kinds=self._linked_kinds(record.camera),
            now=now,
        )
        cutoff = now - _RECENT_SECONDS
        self._recent = {k: v for k, v in self._recent.items() if v.started_at >= cutoff}

    def score(
        self, record: EventRecord, *, now: float, include_late: bool = True
    ) -> RelevanceResult:
        """Score one record against the current model.

        `include_late` is False for the live score of an open event; the provisional score at
        the end counts "no late evidence yet" as `none`, and the final one counts what came.
        """
        event = to_event(
            record,
            self._clock,
            self._policy,
            linked_kinds=self._linked_kinds(record.camera),
            include_late=include_late,
            now=now,
        )
        previous = self._previous(event, record.id)
        names = self._names()
        labels = dict(LABELS)
        for entity_id in dict(event.context):
            state = self._hass.states.get(entity_id)
            if state is not None:
                labels[entity_id] = state.name
        result: Score = score(event, self._model, previous=previous, names=names, labels=labels)
        line = result.threshold if result.threshold is not None else self._model.floor
        return RelevanceResult(
            score=normalised(result.total, line),
            surprisal=round(result.total, 3),
            threshold=None if result.threshold is None else round(result.threshold, 3),
            unusual=result.unusual,
            reason=result.reason,
            reasons=[
                f"{term.name}{f' {term.subject}' if term.subject else ''}: {term.label} "
                f"({term.contribution:+.2f})"
                for term in sorted(result.terms, key=lambda item: -item.contribution)
                if term.contribution > 0
            ][:6],
        )
