"""One state machine per event: keep, drop, or keep because nobody can say.

Pure: no Home Assistant, no I/O, no clock of its own. The manager feeds it facts, scores and
the health of the link, and acts on the state it returns. That is what makes every
transition testable in a line, and it is the only place the safety rules live:

* **Nothing pending is deleted without an explicit DROPPING decision taken while the link
  is healthy.** `decide_after_follow_up` and `drop_for_cap` refuse to return DROPPING
  otherwise, and DROPPING is the only state from which the uploader deletes `pending/`.
* **When in doubt, keep.** A verdict that never arrived, a camera that went offline, a link
  that was lost: each keeps the event (ORPHANED or KEEPING), never drops it.

States (architecture §3.4, §3.4a):

    PENDING ──end, chosen class or unusual──► KEEPING ──uploads done──► COMMITTED
       │                                     ▲
       ├──end, otherwise ordinary──► FOLLOW_UP ──window closes──┬─ unusual ─┘
       │                                                         │  (unusual-unconfirmed/)
       │                                                         └─ ordinary ─► DROPPING ─► DROPPED
       └──link lost > 2 min, epoch change, camera offline, TTL expired ─► ORPHANED (kept)

Every event, whatever its state, stays open for scoring until its follow-up window closes;
that only changes the fate of FOLLOW_UP events. A committed event gets its final score in
the index; its video is never touched again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .const import FOLDER_EVENTS, FOLDER_UNUSUAL, UPLOADS_CLASSES
from .protocol import (
    VERDICT_CONFIRMED,
    VERDICT_PENDING,
    VERDICT_UNVERIFIED,
    VERDICT_VETOED,
    EventFacts,
)


class EventState(StrEnum):
    """Where an event is in its life."""

    PENDING = "pending"
    KEEPING = "keeping"
    FOLLOW_UP = "follow_up"
    DROPPING = "dropping"
    DROPPED = "dropped"
    COMMITTED = "committed"
    ORPHANED = "orphaned"


# States in which the event still has, or may still get, files in `pending/`.
HOLDS_PENDING = frozenset(
    {EventState.PENDING, EventState.KEEPING, EventState.FOLLOW_UP, EventState.DROPPING}
)
# States that are final for the files (the follow-up window may still update metadata).
TERMINAL = frozenset({EventState.DROPPED, EventState.COMMITTED, EventState.ORPHANED})

# Why an event is kept, in the index and the entity attributes.
REASON_CONFIRMED = "confirmed"
REASON_UNUSUAL = "unusual"
REASON_UNDECIDED = "undecided"
REASON_ORPHANED = "orphaned"
# Kept under "Chosen classes" for a subject of this class: `classes: person`.
REASON_CLASSES = "classes"


@dataclass(slots=True, frozen=True)
class Policy:
    """The no-verifier fallback (architecture §3.4)."""

    unverified_seconds: float = 2.0
    unverified_score: float = 0.5


@dataclass(slots=True)
class EventRecord:
    """Everything the integration knows and has decided about one event."""

    facts: EventFacts
    state: EventState = EventState.PENDING
    # `events` or `unusual-unconfirmed`, once a keep decision names one.
    target: str | None = None
    # Whether ordinary uploads were armed when the event started (the switch gates
    # admission, not delivery: turning it off mid-event does not drop that event).
    admitted: bool = True
    # The uploads position when the event started (`UPLOAD_MODES`): it decides what keeps
    # an ordinary event, whatever the select says later.
    upload_mode: str = UPLOADS_CLASSES
    # Path pieces, fixed at start so a renamed area does not split an event across folders.
    area: str = ""
    # The area's key (its id, or `NO_AREA`) at start: what the area's sensors count it under,
    # whatever the area is called later. Empty on records from before it was kept.
    area_key: str = ""
    camera_name: str = ""
    folder: str = ""
    # The committed video's file name, chosen with the folder (`naming.event_file_name`), and
    # where the commit put it (`<kind>/<name>`, with the `-2` of a name another event had).
    file_name: str = ""
    video_path: str = ""
    # Scores: live while open, provisional at end, final when the follow-up window closes.
    live_score: float | None = None
    provisional_score: float | None = None
    final_score: float | None = None
    unusual_live: bool = False
    unusual_provisional: bool = False
    unusual_final: bool | None = None
    reasons: list[str] = field(default_factory=list)
    reason: str = ""
    keep_reason: str | None = None
    context_start: dict[str, Any] = field(default_factory=dict)
    context_end: dict[str, Any] = field(default_factory=dict)
    late: list[dict[str, Any]] = field(default_factory=list)
    follow_up_until: float | None = None
    finalized: bool = False
    gaps: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    latency: dict[str, float] = field(default_factory=dict)
    # When the drop was decided, and that the link was healthy then: the audit trail of the
    # one decision that deletes anything.
    dropped_at: float | None = None

    @property
    def id(self) -> str:
        """Return the event id."""
        return self.facts.id

    @property
    def camera(self) -> str:
        """Return the Scrypted camera id."""
        return self.facts.camera

    @property
    def ended(self) -> bool:
        """Return whether the plugin has closed the event."""
        return self.facts.ended is not None

    @property
    def unusual(self) -> bool:
        """Return the best current answer to "is this unusual"."""
        if self.unusual_final is not None:
            return self.unusual_final
        if self.ended:
            return self.unusual_provisional
        return self.unusual_live

    @property
    def score(self) -> float | None:
        """Return the best current score."""
        for value in (self.final_score, self.provisional_score, self.live_score):
            if value is not None:
                return value
        return None

    def note(self, text: str) -> None:
        """Record something worth saying about the event, once."""
        if text not in self.notes:
            self.notes.append(text)


def subject_confirmed(subject: Any, policy: Policy) -> bool:
    """Whether one subject counts as confirmed, the fallback policy included."""
    if subject.verdict == VERDICT_CONFIRMED:
        return True
    if subject.verdict == VERDICT_UNVERIFIED:
        return (
            subject.dwell >= policy.unverified_seconds and subject.score >= policy.unverified_score
        )
    return False


def verdict_summary(facts: EventFacts, policy: Policy) -> str:
    """Return `confirmed`, `vetoed` or `undecided` for the event as a whole.

    `vetoed` means there is nothing to keep it for: every subject was vetoed, or was
    unverified and too brief or too faint to count. A subject still waiting on its verdict
    makes the event `undecided`, which is kept — the verifier not answering is not a veto.
    An event with no subjects at all (a motion trigger nobody classified) is `vetoed`.
    """
    subjects = list(facts.subjects.values())
    if any(subject_confirmed(subject, policy) for subject in subjects):
        return REASON_CONFIRMED
    if any(subject.verdict == VERDICT_PENDING for subject in subjects):
        return REASON_UNDECIDED
    return VERDICT_VETOED


def chosen_class(record: EventRecord, policy: Policy, classes: frozenset[str] | None) -> str | None:
    """Return the chosen class that keeps an ordinary event, or None.

    A subject counts when its class is chosen and it is confirmed (the no-verifier fallback
    included: an unverified subject on a camera without a verifier is trusted), or still
    waiting on its verdict (a verifier not answering is not a veto). Only under the
    `classes` position; `classes` None means every class (records from before the filter).
    """
    if record.upload_mode != UPLOADS_CLASSES:
        return None
    found = sorted(
        subject.cls
        for subject in record.facts.subjects.values()
        if (classes is None or subject.cls in classes)
        and (subject_confirmed(subject, policy) or subject.verdict == VERDICT_PENDING)
    )
    return found[0] if found else None


def class_reason(cls: str) -> str:
    """Return the keep reason of an event kept for a chosen class."""
    return f"{REASON_CLASSES}: {cls}"


# What each keep reason says in plain words (entity attributes; the panel has its own).
_KEPT_BECAUSE = {
    REASON_CONFIRMED: "something was confirmed",
    REASON_UNUSUAL: "it was unusual",
    REASON_UNDECIDED: "the verifier never answered",
    REASON_ORPHANED: "it could not be decided in time",
}


def kept_because(reason: str | None) -> str | None:
    """Return why an event was kept, in plain words, or None when it was not."""
    if not reason:
        return None
    head, _, cls = reason.partition(": ")
    if head == REASON_CLASSES and cls:
        return f"a {cls} was confirmed (a chosen class)"
    return _KEPT_BECAUSE.get(reason, reason)


def decide_at_end(
    record: EventRecord, policy: Policy, classes: frozenset[str] | None = None
) -> EventState:
    """Decide what an event that has just ended becomes.

    Called with `record.unusual_provisional` already set. Only PENDING events are decided
    here; anything already orphaned or kept stays as it is. An ordinary event is kept only
    under the `classes` position, for a subject of a chosen class; any other ordinary event
    goes through its follow-up window and is kept only if it turns unusual there.
    """
    if record.state is not EventState.PENDING:
        return record.state
    if record.facts.camera_offline:
        record.note("camera_offline")
        record.keep_reason = REASON_ORPHANED
        return EventState.ORPHANED

    if (cls := chosen_class(record, policy, classes)) is not None:
        record.keep_reason = class_reason(cls)
        record.target = FOLDER_EVENTS
        return EventState.KEEPING
    if record.unusual_provisional:
        record.keep_reason = REASON_UNUSUAL
        record.target = FOLDER_UNUSUAL
        return EventState.KEEPING
    # Vetoed and ordinary: held, not dropped, until the follow-up window has had its say.
    return EventState.FOLLOW_UP


def decide_after_follow_up(record: EventRecord, *, link_healthy: bool, now: float) -> EventState:
    """Decide a FOLLOW_UP event once its window has closed and the final score is in.

    Unusual after all: kept, in `unusual-unconfirmed/`. Still ordinary: dropped — but only
    while the link is healthy. With the link down the decision waits; the facts are already
    complete, so nothing is lost by waiting, and the safety rule is not bent for convenience.
    """
    if record.state is not EventState.FOLLOW_UP:
        return record.state
    if record.unusual_final:
        record.keep_reason = REASON_UNUSUAL
        record.target = FOLDER_UNUSUAL
        return EventState.KEEPING
    if not link_healthy:
        return EventState.FOLLOW_UP
    record.dropped_at = now
    return EventState.DROPPING


def drop_for_cap(record: EventRecord, *, link_healthy: bool, now: float) -> EventState:
    """Drop a FOLLOW_UP event early because held pending storage is over its cap.

    Only a FOLLOW_UP event that is still vetoed and ordinary qualifies; ORPHANED and KEEPING
    events are never touched, whatever the cap says.
    """
    if record.state is not EventState.FOLLOW_UP or record.unusual or not link_healthy:
        return record.state
    record.dropped_at = now
    record.note("dropped_for_follow_up_cap")
    return EventState.DROPPING


def orphan(record: EventRecord, why: str, *, follow_up: bool = False) -> EventState:
    """Mark an event that can no longer be completed as kept-as-is.

    PENDING and KEEPING events that have not committed are orphaned: their facts or their
    segments can no longer be relied upon, so they are kept. FOLLOW_UP events are left alone
    when only the link was lost (their facts are complete; the drop waits for the link), but
    orphaned too after a gap or a plugin restart (`follow_up=True`), when messages about them
    may have been lost. An orphan keeps uploading; it is only never dropped.
    """
    allowed = {EventState.PENDING, EventState.KEEPING}
    if follow_up:
        # After a gap or a plugin restart, what happened to a held event is unknown: a
        # verdict that would have kept it may have been lost. Kept, then.
        allowed.add(EventState.FOLLOW_UP)
    if record.state not in allowed:
        return record.state
    record.note(why)
    record.keep_reason = record.keep_reason or REASON_ORPHANED
    record.target = record.target or FOLDER_EVENTS
    return EventState.ORPHANED


def follow_up_closed(record: EventRecord, now: float) -> bool:
    """Whether the follow-up window is over (and the event has ended)."""
    return (
        record.ended
        and record.follow_up_until is not None
        and now >= record.follow_up_until
        and not record.finalized
    )


def evictable(record: EventRecord) -> bool:
    """Whether quota or retention may delete this event: committed only, never orphaned."""
    return record.state is EventState.COMMITTED
