"""What a destination holds, and what has to go to make room.

Ported from Reolink Stamina's `cloud/index.py` (`ClipIndex.plan_eviction`): the quota is
enforced against this record rather than by asking the destination how full it is, because
a listing costs a round trip per upload and the folder may hold files this integration did
not put there. Eviction is oldest first, no further than needed, and refuses outright when
the incoming file could not fit even if everything evictable went.

What may be evicted here:

* committed events (`events/` and `unusual-unconfirmed/`) — by quota or retention, oldest
  first;
* orphaned events — by their own retention only (default 30 days): kept because nobody
  could decide, so never pushed out by ordinary traffic, but not kept for ever either;
* pending events (uploading, or held in the follow-up window) — never.

The follow-up window has a cap of its own: pending storage held by FOLLOW_UP events. Over
it, the oldest FOLLOW_UP events are named — the manager decides which of them may go.

Totals are kept running and the evictable events in start order, so a check per uploaded
segment costs nothing however many events a destination holds.
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass

from .decider import EventState


@dataclass(slots=True)
class Held:
    """One event folder on one destination."""

    event_id: str
    started: float
    state: EventState
    size: int


class DestinationUsage:
    """The event folders one destination holds."""

    def __init__(self) -> None:
        """Start empty."""
        self._held: dict[str, Held] = {}
        self._used = 0
        self._follow_up = 0
        self._orphans = 0
        self._orphan_bytes = 0
        # (started, id) of committed and of follow-up events, oldest first.
        self._committed: list[tuple[float, str]] = []
        self._follow_ups: list[tuple[float, str]] = []

    def __len__(self) -> int:
        """Return how many event folders are held."""
        return len(self._held)

    @property
    def used(self) -> int:
        """Return the bytes occupied."""
        return self._used

    def get(self, event_id: str) -> Held | None:
        """Return one event's entry."""
        return self._held.get(event_id)

    def _index(self, held: Held, add: bool) -> None:
        key = (held.started, held.event_id)
        for state, order in (
            (EventState.COMMITTED, self._committed),
            (EventState.FOLLOW_UP, self._follow_ups),
        ):
            if held.state is not state:
                continue
            if add:
                insort(order, key)
            elif key in order:
                order.remove(key)
        if held.state is EventState.FOLLOW_UP:
            self._follow_up += held.size if add else -held.size
        if held.state is EventState.ORPHANED:
            self._orphans += 1 if add else -1
            self._orphan_bytes += held.size if add else -held.size

    def set(self, event_id: str, started: float, state: EventState, size: int) -> None:
        """Record (or replace) one event folder."""
        self.remove(event_id)
        held = Held(event_id, started, state, size)
        self._held[event_id] = held
        self._used += size
        self._index(held, add=True)

    def grow(self, event_id: str, started: float, state: EventState, extra: int) -> None:
        """Add bytes to one event folder."""
        held = self._held.get(event_id)
        if held is None:
            self.set(event_id, started, state, extra)
            return
        held.size += extra
        self._used += extra
        if held.state is EventState.FOLLOW_UP:
            self._follow_up += extra
        if held.state is EventState.ORPHANED:
            self._orphan_bytes += extra

    def state(self, event_id: str, state: EventState) -> None:
        """Update one event's state, which decides whether it may be evicted."""
        held = self._held.get(event_id)
        if held is None or held.state is state:
            return
        self._index(held, add=False)
        held.state = state
        self._index(held, add=True)

    def remove(self, event_id: str) -> None:
        """Forget one event folder."""
        held = self._held.pop(event_id, None)
        if held is None:
            return
        self._used -= held.size
        self._index(held, add=False)

    def plan_eviction(self, incoming: int, quota: int) -> list[Held]:
        """Which committed events must go for `incoming` bytes to fit inside `quota`.

        Returns nothing when even evicting every committed event would not make room: that
        would empty the archive and still fail, so the caller sees it does not fit instead.
        """
        if incoming > quota:
            return []
        used = self._used
        if used + incoming <= quota:
            return []
        doomed: list[Held] = []
        for _, event_id in self._committed:
            if used + incoming <= quota:
                break
            held = self._held[event_id]
            doomed.append(held)
            used -= held.size
        return doomed if used + incoming <= quota else []

    def expired(self, before: float, orphans_before: float | None = None) -> list[Held]:
        """Return committed events older than `before`, and orphans older than theirs."""
        found = [self._held[i] for started, i in self._committed if started < before]
        if orphans_before is not None:
            found += [
                held
                for held in self._held.values()
                if held.state is EventState.ORPHANED and held.started < orphans_before
            ]
        return found

    def follow_up_bytes(self) -> int:
        """Return the pending bytes held by FOLLOW_UP events."""
        return self._follow_up

    def follow_up_oldest(self) -> list[str]:
        """Return the FOLLOW_UP events, oldest first."""
        return [event_id for _, event_id in self._follow_ups]

    def orphans(self) -> tuple[int, int]:
        """Return how many orphaned events are held, and their bytes (kept running)."""
        return self._orphans, self._orphan_bytes
