"""Segments from the plugin to every destination, then commit or drop the folder.

Per event, a **pump**: one fetcher and one **lane** per destination.

* The fetcher reads each segment from the plugin once, in order (init first), into a small
  shared ring, staying up to `PREFETCH` segments ahead of the slowest lane that can store
  now — so segment n+1 is on its way while n is being stored. The ring keeps a segment for
  every lane within its lag budget, degraded ones included.
* Each lane writes segments to its destination in order, with its own cursor. A lane more
  than `LAG_BUDGET` segments behind (its destination slow or down) no longer holds the
  fetcher back: it reads what it needs from the plugin itself. **The plugin is the buffer**:
  nothing is acknowledged until every destination has a segment, so a lagging lane can
  always fetch it again — a ring miss is a re-fetch, never a failure. A segment is given up
  for one destination only when the plugin answers 404 for it (or, unreachable, after the
  segment TTL), or after `SEGMENT_ATTEMPTS` counted refusals by the destination itself.
* Each destination has its own upload slots, and a circuit breaker: a failure (counted only
  when the breaker was closed, or by its single half-open probe) opens it for a while,
  doubling; one probe goes through when it half-opens; success closes it. Destination
  operations run with one attempt each, so no backoff ever sleeps while holding a slot: the
  lane retries, with the slot released.
* **The quota is soft for pending footage.** When committed events cannot make enough room,
  the upload goes ahead over quota and a repair issue says so; a quota shortfall is never a
  breaker failure or a segment attempt.

The pump runs while the event is PENDING, KEEPING, FOLLOW_UP — or ORPHANED: an orphan is
"never drop", not "stop uploading". Its segments end only when the plugin says so (`last`,
`live: false` or a failure on a closed event, or the event forgotten). For an event known to
be closed and a plugin unreachable for the segment TTL, nothing more can be had and it ends;
for an event not known closed, it never ends on silence alone.

Commit writes `index.m3u8` (only segments that destination holds, with their init) and
`event.json`, then moves the folder: one server-side move where the provider has one, else
copy then delete — listing the *source* folder, so a file the index does not know about is
not deleted uncopied. It is idempotent: a folder already at its target with nothing left at
the source is a commit that already happened, and the copy skips files already there. Drop
deletes the pending folder; the uploader never deletes `pending/` on its own initiative,
only for an event the manager hands it in DROPPING.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
import json
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant

from .api import (
    SegmentExpiredError,
    SegmentNotReadyError,
    StaminaApiError,
    StaminaClient,
)
from .capacity import available_bytes, describe, expected_bytes, slots
from .const import (
    ASSUMED_SEGMENT_BYTES,
    FOLDER_EVENTS,
    FOLDER_PENDING,
    SEGMENT_ATTEMPTS,
    SEGMENT_NOT_READY_DELAY,
    SEGMENT_TTL_SECONDS,
)
from .decider import EventRecord, EventState
from .destinations import Destination, DestinationError, MoveUnsupportedError
from .destinations.base import attempts_limited
from .hls import playlist
from .index import Copy, EventIndex
from .naming import event_path, segment_name
from .quota import DestinationUsage
from .redact import scrub_credentials

_LOGGER = logging.getLogger(__name__)

INIT = "init.mp4"
MANIFEST = "index.m3u8"
EVENT_JSON = "event.json"
# The init segment's key wherever segments are numbered (segments count from 0).
INIT_KEY = -1

# How far the fetcher may run ahead of the slowest lane that keeps pace.
PREFETCH = 2
# A lane further behind than this fetches for itself instead of holding the fetcher back.
LAG_BUDGET = 8
# A destination that fails is skipped for this long, doubling up to the maximum.
BREAKER_SECONDS = 15.0
BREAKER_MAX_SECONDS = 300.0
# How often an ended event's state is asked while waiting for its last segment, and how
# often once the plugin has been silent for a whole TTL about an event not known closed.
_TAIL_POLL = 2.0
_SILENT_POLL = 30.0
_FETCH_BACKOFF = (1.0, 2.0, 4.0, 8.0)

UPLOADING = frozenset(
    {EventState.PENDING, EventState.KEEPING, EventState.FOLLOW_UP, EventState.ORPHANED}
)

# Breaker admissions.
_CLOSED = "closed"
_PROBE = "probe"


@dataclass(slots=True)
class Target:
    """One destination subentry, as the uploader uses it."""

    subentry_id: str
    destination: Destination
    folder: str
    quota: int
    retention_days: float
    follow_up_cap: int
    orphan_retention_days: float = 30.0
    usage: DestinationUsage = field(default_factory=DestinationUsage)
    last_error: str | None = None
    uploaded: int = 0
    degraded_until: float = 0.0
    failures: int = 0
    probing: bool = False
    over_quota: bool = False
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))
    evicting: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def label(self) -> str:
        """Return the destination's label."""
        return self.destination.label

    @property
    def degraded(self) -> bool:
        """Return whether the circuit breaker is not closed."""
        return self.failures > 0

    def admit(self, now: float) -> str | None:
        """Return how an attempt may go ahead now (`closed`, `probe`), or None to wait."""
        if self.failures == 0:
            return _CLOSED
        if now >= self.degraded_until and not self.probing:
            self.probing = True
            return _PROBE
        return None

    def release(self, admission: str | None) -> None:
        """Give back a probe that was not used."""
        if admission == _PROBE:
            self.probing = False

    def failed(self, err: Exception, admission: str | None, now: float) -> bool:
        """Record a failure; return whether it counted (breaker closed, or the probe)."""
        self.last_error = scrub_credentials(str(err))
        if admission not in (_CLOSED, _PROBE):
            return False
        self.probing = False
        self.failures += 1
        delay = min(BREAKER_SECONDS * 2 ** (self.failures - 1), BREAKER_MAX_SECONDS)
        self.degraded_until = now + delay
        return True

    def succeeded(self) -> None:
        """Close the breaker."""
        self.failures = 0
        self.degraded_until = 0.0
        self.probing = False
        self.last_error = None


@dataclass(slots=True)
class Lane:
    """One destination's progress on one event."""

    subentry_id: str
    copy: Copy
    cursor: int = 0
    task: asyncio.Task | None = None
    attempts: dict[int, int] = field(default_factory=dict)
    finished: bool = False
    # How many later inits the cursor was computed with: a new one restarts the scan.
    inits_seen: int = 0


@dataclass(slots=True)
class Pump:
    """One event's upload, as it is right now."""

    record: EventRecord
    lanes: dict[str, Lane]
    ring: dict[int, bytes] = field(default_factory=dict)
    fetched: set[int] = field(default_factory=set)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    fetcher: asyncio.Task | None = None
    fetch_cursor: int = 0
    acked: int = -1
    # When the plugin last answered anything about this event (HA's monotonic clock).
    last_heard: float = field(default_factory=time.monotonic)
    fetch_done: bool = False
    reported: bool = False

    @property
    def wanted(self) -> bool:
        """Whether segments of this event should be uploading."""
        return self.record.state in UPLOADING and self.record.admitted

    def segment_keys(self) -> list[int]:
        """Return every key in the order needed: init, segments, later inits before use."""
        facts = self.record.facts
        keys = [INIT_KEY] if facts.init or facts.segments else []
        if not facts.init_of:
            return [*keys, *range(facts.segments)]
        seen: set[int] = set()
        for number in range(facts.segments):
            needed = facts.init_of.get(number, 0)
            if needed and needed not in seen:
                seen.add(needed)
                keys.append(init_key(needed))
            keys.append(number)
        return keys

    def poke(self) -> None:
        """Wake the fetcher and the lanes."""
        self.wake.set()


def init_key(k: int) -> int:
    """Return the key of init k (0 is init.mp4, k >= 1 is init-<k>.mp4)."""
    return -1 - k


def _name(key: int) -> str:
    if key == INIT_KEY:
        return INIT
    if key < 0:
        return f"init-{-1 - key}.mp4"
    return segment_name(key)


def plugin_name(key: int) -> str:
    """Return a file's name on the plugin (`<n>.m4s` unpadded, docs/api.md)."""
    return f"{key}.m4s" if key >= 0 else _name(key)


def satisfied(record: EventRecord, copy: Copy, key: int) -> bool:
    """Whether a destination needs nothing more for a segment (a ring miss is not this)."""
    if copy.deleted or key in copy.failed or key in record.gaps:
        return True
    return _name(key) in copy.files


def event_json(record: EventRecord) -> dict[str, Any]:
    """Return what `event.json` holds: facts, context, scores, decision, latency.

    Never a URL, a token or anything else that authenticates (architecture §7).
    """
    return {
        "version": 1,
        "event": record.facts.as_dict(),
        "camera_name": record.camera_name,
        "area": record.area,
        "state": str(record.state),
        "keep_reason": record.keep_reason,
        "unusual": record.unusual,
        "score": {
            "live": record.live_score,
            "provisional": record.provisional_score,
            "final": record.final_score,
            "unusual_provisional": record.unusual_provisional,
            "unusual_final": record.unusual_final,
        },
        "reason": record.reason,
        "reasons": record.reasons,
        "context": {"start": record.context_start, "end": record.context_end},
        "late": record.late,
        "follow_up_until": record.follow_up_until,
        "gaps": sorted(record.gaps),
        "notes": record.notes,
        "latency": {**record.facts.stamps, **record.latency},
    }


def manifest_for(record: EventRecord, copy: Copy) -> bytes:
    """Return the playlist of the segments one destination holds *with their init*."""
    init_of = record.facts.init_of
    held = sorted(
        number
        for name in copy.files
        if name.endswith(".m4s") and (number := int(name.split(".")[0])) >= 0
        if _name(init_key(init_of.get(number, 0))) in copy.files
    )
    return playlist(
        held,
        durations=record.facts.durations,
        discontinuities=record.facts.discontinuities,
        init_of=init_of,
    ).encode()


class Uploader:
    """Moves every kept event's segments to every destination."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: StaminaClient,
        index: EventIndex,
        targets: dict[str, Target],
        *,
        on_change: Callable[[], None],
        on_uploaded: Callable[[EventRecord], None],
        on_latency: Callable[[float], None],
        on_quota: Callable[[], None] | None = None,
        on_over_quota: Callable[[Target, bool], None] | None = None,
        before_release: Callable[[EventRecord], Awaitable[None]] | None = None,
    ) -> None:
        """Configure the uploader; nothing runs until events are handed over."""
        self._hass = hass
        self._client = client
        self._index = index
        self.targets = targets
        self._on_change = on_change
        self._on_uploaded = on_uploaded
        self._on_latency = on_latency
        self._on_quota = on_quota or (lambda: None)
        self._on_over_quota = on_over_quota or (lambda target, over: None)
        self._before_release_hook = before_release
        self._pumps: dict[str, Pump] = {}
        self._sizes: deque[int] = deque(maxlen=50)
        self.capacity = "not yet measured"
        self.enabled = True
        # Monotonic clock, replaceable in tests (the TTL and the breaker read it).
        self.monotonic: Callable[[], float] = time.monotonic

    # ------------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Load what every destination holds, and size the parallelism."""
        for target in self.targets.values():
            for event_id, started, state, size in await self._index.async_destination_usage(
                target.subentry_id
            ):
                target.usage.set(event_id, started, EventState(state), size)
        await self._async_size()

    async def _async_size(self) -> None:
        """Work out how many uploads each destination may run at once."""
        expected = expected_bytes(list(self._sizes), ASSUMED_SEGMENT_BYTES)
        available = await self._hass.async_add_executor_job(available_bytes)
        per_destination = max(1, slots(available, expected) // max(1, len(self.targets)))
        self.capacity = describe(available, expected)
        for target in self.targets.values():
            target.slots = asyncio.Semaphore(per_destination)

    async def async_stop(self) -> None:
        """Stop every pump and close the destinations."""
        for event_id in list(self._pumps):
            await self.async_halt(event_id)
        self._pumps.clear()
        for target in self.targets.values():
            await target.destination.async_close()

    # ------------------------------------------------------------------ status

    @property
    def queue(self) -> int:
        """Return how many segment files are waiting to be uploaded, across destinations."""
        waiting = 0
        for pump in self._pumps.values():
            if not pump.wanted:
                continue
            keys = pump.segment_keys()
            for lane in pump.lanes.values():
                waiting += sum(
                    1 for key in keys[lane.cursor :] if not satisfied(pump.record, lane.copy, key)
                )
        return waiting

    @property
    def active(self) -> int:
        """Return how many events are being uploaded."""
        return sum(1 for pump in self._pumps.values() if pump.wanted)

    # ------------------------------------------------------------------ events

    async def async_track(self, record: EventRecord) -> None:
        """Start (or resume) uploading one event."""
        pump = self._pumps.get(record.id)
        if pump is None:
            copies = await self._index.async_copies(record.id)
            lanes = {}
            for subentry_id, target in self.targets.items():
                copy = copies.get(subentry_id) or Copy(
                    folder=event_path(target.folder, FOLDER_PENDING, record.area, record.folder)
                )
                lanes[subentry_id] = Lane(subentry_id=subentry_id, copy=copy)
            pump = Pump(record=record, lanes=lanes, last_heard=self.monotonic())
            self._pumps[record.id] = pump
        pump.record = record
        self.poke(record.id)

    def heard(self, event_id: str) -> None:
        """Note that the plugin just said something about an event (resets its TTL)."""
        if (pump := self._pumps.get(event_id)) is not None:
            pump.last_heard = self.monotonic()

    def poke(self, event_id: str) -> None:
        """Tell a pump something changed: a new segment, a new state."""
        pump = self._pumps.get(event_id)
        if pump is None:
            return
        pump.poke()
        if not pump.wanted or not self.enabled:
            return
        pump.reported = False
        if pump.fetcher is None or pump.fetcher.done():
            pump.fetch_done = False
            pump.fetcher = self._hass.async_create_background_task(
                self._async_fetch_loop(pump), f"scrypted_stamina fetch {event_id}"
            )
        for lane in pump.lanes.values():
            if lane.task is None or lane.task.done():
                lane.finished = False
                lane.task = self._hass.async_create_background_task(
                    self._async_lane(pump, lane), f"scrypted_stamina upload {event_id}"
                )

    async def async_halt(self, event_id: str) -> None:
        """Stop an event's fetcher and lanes, and wait until they have."""
        pump = self._pumps.get(event_id)
        if pump is None:
            return
        tasks = [pump.fetcher, *(lane.task for lane in pump.lanes.values())]
        running = [task for task in tasks if task is not None and not task.done()]
        for task in running:
            task.cancel()
        for task in running:
            with suppress(asyncio.CancelledError, Exception):
                await task
        for lane in pump.lanes.values():
            if lane.task in running:
                # A cancelled lane may have held the half-open probe: give it back.
                self.targets[lane.subentry_id].probing = False

    async def async_forget(self, event_id: str) -> None:
        """Stop following an event whose files are final."""
        await self.async_halt(event_id)
        self._pumps.pop(event_id, None)

    def set_state(self, record: EventRecord) -> None:
        """Mirror an event's state into the usage tables (it decides evictability)."""
        for target in self.targets.values():
            target.usage.state(record.id, record.state)
        self.poke(record.id)

    def tracking(self, event_id: str) -> bool:
        """Return whether an event has a pump."""
        return event_id in self._pumps

    def evicted(self, record: EventRecord, numbers: list[int]) -> None:
        """Record segments the plugin dropped for memory before they were fetched: gaps."""
        pump = self._pumps.get(record.id)
        fetched = pump.fetched if pump is not None else set()
        for number in numbers:
            if number not in fetched and number not in record.gaps:
                record.gaps.append(number)
        self.poke(record.id)

    def complete(self, record: EventRecord) -> bool:
        """Whether every segment is uploaded, failed or a gap, and no more will come."""
        pump = self._pumps.get(record.id)
        if pump is None:
            return record.ended or record.state is EventState.ORPHANED
        if not pump.fetch_done:
            return False
        keys = pump.segment_keys()
        return all(
            satisfied(record, lane.copy, key) for lane in pump.lanes.values() for key in keys
        )

    # ------------------------------------------------------------------ fetching

    def _lag(self, pump: Pump, lane: Lane, keys: list[int]) -> int:
        """Return how many keys lie between a lane's cursor and the fetcher's position."""
        return max(0, min(pump.fetch_cursor, len(keys)) - lane.cursor)

    def _pacing(self, pump: Pump, lane: Lane, keys: list[int]) -> bool:
        """Whether a lane keeps pace with the fetcher (degraded or not, within its budget)."""
        return not lane.copy.deleted and self._lag(pump, lane, keys) <= LAG_BUDGET

    def _needed_by(self, pump: Pump, key: int) -> list[Lane]:
        return [lane for lane in pump.lanes.values() if not satisfied(pump.record, lane.copy, key)]

    async def _async_fetch_loop(self, pump: Pump) -> None:
        """Fetch each needed segment once, in order, a little ahead of the lanes."""
        record = pump.record
        try:
            while pump.wanted and self.enabled:
                keys = pump.segment_keys()
                while pump.fetch_cursor < len(keys) and (
                    keys[pump.fetch_cursor] in pump.fetched
                    or not self._needed_by(pump, keys[pump.fetch_cursor])
                ):
                    pump.fetch_cursor += 1
                if pump.fetch_cursor >= len(keys):
                    if record.facts.segments_final:
                        pump.fetch_done = True
                        pump.poke()
                        await self._async_maybe_done(pump)
                        return
                    await self._async_check_final(pump)
                    continue
                key = keys[pump.fetch_cursor]
                needing = self._needed_by(pump, key)
                healthy = [lane for lane in needing if not self.targets[lane.subentry_id].degraded]
                # The window is measured against the lanes that can store now. A degraded
                # lane keeps the ring's segments while within its lag budget, and past it
                # reads from the plugin itself: it never stalls the others.
                leading = [
                    lane
                    for lane in pump.lanes.values()
                    if self._pacing(pump, lane, keys)
                    and not self.targets[lane.subentry_id].degraded
                ]
                ahead = sum(
                    1
                    for held in pump.ring
                    if any(not satisfied(record, lane.copy, held) for lane in leading)
                )
                if not healthy or ahead >= PREFETCH:
                    # Nobody who can store it now needs it, or the lanes keeping pace have
                    # enough in hand: wait. Lanes far behind fetch for themselves.
                    await self._wait(pump, _TAIL_POLL)
                    continue
                data = await self._async_fetch(pump, key)
                pump.fetched.add(key)
                if data is not None:
                    pump.ring[key] = data
                    self._sizes.append(len(data))
                self._trim(pump)
                pump.poke()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Fetching event %s failed", record.id)

    async def _wait(self, pump: Pump, timeout: float) -> None:  # noqa: ASYNC109
        pump.wake.clear()
        with suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await pump.wake.wait()

    def _expired(self, pump: Pump) -> bool:
        """Whether the plugin has been silent about an event for longer than its TTL."""
        return self.monotonic() - pump.last_heard > SEGMENT_TTL_SECONDS

    async def _async_check_final(self, pump: Pump) -> None:
        """Wait for the plugin to say whether more segments are coming.

        Only an answer from the plugin ends the event's segments: `live: false` or a failure
        on a closed event, the segments released after it closed, or the event forgotten
        (404). An error is not an answer. For an event known to be closed, a plugin silent
        for a whole TTL can no longer hold anything, and that ends it too; for one not known
        closed, silence never does — it is asked again, less often.
        """
        facts = pump.record.facts
        if facts.ended is None and pump.record.state is not EventState.ORPHANED:
            await self._wait(pump, 30.0)  # open: segments are announced as they come
            return
        try:
            state = await self._client.async_event(pump.record.id)
        except StaminaApiError:
            if self._expired(pump):
                if facts.ended is not None:
                    facts.segments_final = True
                    pump.record.note("segments_unconfirmed_ttl")
                    return
                await self._wait(pump, _SILENT_POLL)
                return
            await self._wait(pump, _TAIL_POLL)
            return
        pump.last_heard = self.monotonic()
        if state is None:
            facts.segments_final = True  # forgotten: nothing more can be had
            return
        event = state.get("event")
        closed = facts.ended is not None or (
            isinstance(event, dict) and (event.get("state") == "closed" or bool(event.get("end")))
        )
        segments = state.get("segments")
        if isinstance(segments, dict):
            following = segments.get("next")
            if isinstance(following, int) and not isinstance(following, bool):
                facts.segments = max(facts.segments, following)
            facts.init = facts.init or bool(segments.get("init"))
        if not closed:
            # An open event's segmenter may be released and resume: nothing is final yet.
            await self._wait(pump, _TAIL_POLL)
            return
        if segments is None and "segments" in state:
            facts.segments_final = True  # released: everything was acked
            return
        if isinstance(segments, dict) and (
            segments.get("live") is False or segments.get("failure")
        ):
            facts.segments_final = True
            return
        await self._wait(pump, _TAIL_POLL)

    async def _async_fetch(self, pump: Pump, key: int) -> bytes | None:
        """Fetch one segment, waiting out 425 and recording 404 as a gap."""
        record = pump.record
        attempt = 0
        while pump.wanted:
            try:
                data = await self._client.async_event_file(record.id, plugin_name(key))
            except SegmentNotReadyError:
                pump.last_heard = self.monotonic()
                await asyncio.sleep(SEGMENT_NOT_READY_DELAY)
                continue
            except SegmentExpiredError:
                pump.last_heard = self.monotonic()
                self._gap(record, key)
                return None
            except StaminaApiError as err:
                if self._expired(pump):
                    self._gap(record, key)
                    return None
                delay = _FETCH_BACKOFF[min(attempt, len(_FETCH_BACKOFF) - 1)]
                attempt += 1
                _LOGGER.debug("Fetching %s of %s failed (%s)", _name(key), record.id, err)
                await asyncio.sleep(delay)
                continue
            pump.last_heard = self.monotonic()
            if key >= 0 and "first_fetch" not in record.latency:
                record.latency["first_fetch"] = time.time()
            return data
        return None

    async def _async_refetch(self, pump: Pump, lane: Lane, key: int) -> bytes | None:
        """Fetch a segment again for one lane that missed the ring.

        Nothing is acknowledged while a lane still needs it, so the plugin still holds it:
        only its 404 (or a TTL of silence) gives the segment up, for this destination alone.
        """
        try:
            data = await self._client.async_event_file(pump.record.id, plugin_name(key))
        except SegmentNotReadyError:
            await asyncio.sleep(SEGMENT_NOT_READY_DELAY)
            return None
        except SegmentExpiredError:
            await self._async_lane_failed(pump, lane, key)
            return None
        except StaminaApiError:
            if self._expired(pump):
                await self._async_lane_failed(pump, lane, key)
            else:
                await asyncio.sleep(_FETCH_BACKOFF[0])
            return None
        pump.last_heard = self.monotonic()
        return data

    async def _async_lane_failed(self, pump: Pump, lane: Lane, key: int) -> None:
        if key not in lane.copy.failed:
            lane.copy.failed.append(key)
            await self._index.async_save_copy_header(pump.record.id, lane.subentry_id, lane.copy)

    def _gap(self, record: EventRecord, key: int) -> None:
        """Record a segment that can no longer be had."""
        if key < 0:
            # Without its init a segment does not play, but it is still evidence.
            record.note("init_missing")
        if key not in record.gaps:
            record.gaps.append(key)

    def _trim(self, pump: Pump) -> None:
        """Let go of ring entries no pacing lane needs (lanes behind re-fetch for themselves)."""
        keys = pump.segment_keys()
        pacing = [lane for lane in pump.lanes.values() if self._pacing(pump, lane, keys)]
        for key in list(pump.ring):
            if not any(not satisfied(pump.record, lane.copy, key) for lane in pacing):
                del pump.ring[key]

    # ------------------------------------------------------------------ lanes

    def _advance(self, pump: Pump, lane: Lane) -> int | None:
        """Move a lane's cursor past what it has, and return the key it needs next."""
        keys = pump.segment_keys()
        inits = len(pump.record.facts.init_of)
        if inits != lane.inits_seen:
            lane.inits_seen, lane.cursor = inits, 0
        while lane.cursor < len(keys) and satisfied(pump.record, lane.copy, keys[lane.cursor]):
            lane.cursor += 1
        return keys[lane.cursor] if lane.cursor < len(keys) else None

    async def _async_lane(self, pump: Pump, lane: Lane) -> None:
        """Store one event's segments on one destination, in order."""
        target = self.targets[lane.subentry_id]
        record = pump.record
        try:
            while pump.wanted and self.enabled and not lane.copy.deleted:
                key = self._advance(pump, lane)
                if key is None:
                    if pump.fetch_done:
                        break
                    await self._wait(pump, 30.0)
                    continue
                admission = target.admit(self.monotonic())
                if admission is None:
                    await asyncio.sleep(
                        max(0.05, min(5.0, target.degraded_until - self.monotonic()))
                    )
                    continue
                data = pump.ring.get(key)
                if data is None:
                    keys = pump.segment_keys()
                    if key not in pump.fetched and self._pacing(pump, lane, keys):
                        target.release(admission)
                        await self._wait(pump, 30.0)  # the fetcher brings it
                        continue
                    data = await self._async_refetch(pump, lane, key)
                    if data is None:
                        target.release(admission)
                        continue
                await self._async_store(pump, lane, target, key, data, admission)
                self._trim(pump)
                await self._async_ack_contiguous(pump)
                pump.poke()
                self._on_change()
            lane.finished = True
            await self._async_maybe_done(pump)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Uploading event %s to %s failed", record.id, target.label)

    async def _async_store(
        self, pump: Pump, lane: Lane, target: Target, key: int, data: bytes, admission: str
    ) -> None:
        """Write one segment to one destination."""
        record, copy, name = pump.record, lane.copy, _name(key)
        await self._async_make_room(target, len(data))
        async with target.slots:
            if admission == _CLOSED and target.degraded:
                return  # it opened while this waited for a slot; try again later
            if admission == _PROBE and not target.probing:
                return  # the probe was given back (a halt) while this waited
            try:
                with attempts_limited(1):
                    await target.destination.async_store(f"{copy.folder}/{name}", data)
            except DestinationError as err:
                counted = target.failed(err, admission, self.monotonic())
                _LOGGER.warning(
                    "%s refused %s of %s: %s", target.label, name, record.id, target.last_error
                )
                if counted:
                    lane.attempts[key] = lane.attempts.get(key, 0) + 1
                    if lane.attempts[key] >= SEGMENT_ATTEMPTS:
                        await self._async_lane_failed(pump, lane, key)
                return
        target.succeeded()
        copy.files[name] = len(data)
        target.uploaded += 1
        target.usage.grow(record.id, record.facts.started, record.state, len(data))
        await self._index.async_add_file(record.id, lane.subentry_id, name, len(data))
        if len(copy.files) == 1:
            await self._index.async_save_copy_header(record.id, lane.subentry_id, copy)
        if key >= 0 and "first_upload" not in record.latency:
            record.latency["first_upload"] = time.time()
            detected = record.facts.stamps.get("camera_event_time", record.facts.started)
            self._on_latency(record.latency["first_upload"] - detected)

    async def _async_maybe_done(self, pump: Pump) -> None:
        """Report an event whose every lane has finished, once."""
        if pump.reported or not pump.fetch_done:
            return
        if not all(lane.finished or lane.copy.deleted for lane in pump.lanes.values()):
            return
        if not self.complete(pump.record):
            return
        pump.reported = True
        await self._before_release(pump.record)
        await self._async_ack(pump, pump.record.facts.segments - 1)
        self._on_uploaded(pump.record)

    # ------------------------------------------------------------------ quota

    async def _async_make_room(self, target: Target, incoming: int) -> None:
        """Evict the oldest committed events until `incoming` fits — or go over, and say so.

        Pending footage is never refused for want of room: losing it is worse than a full
        destination, so the quota is soft here and a repair issue tells the user.
        """
        if target.usage.used + incoming <= target.quota:
            if target.over_quota:
                target.over_quota = False
                self._on_over_quota(target, False)
            return
        async with target.evicting:
            if target.usage.used + incoming <= target.quota:
                return
            doomed = target.usage.plan_eviction(incoming, target.quota)
            if not doomed:
                if not target.over_quota:
                    target.over_quota = True
                    _LOGGER.warning("%s is over its quota with pending footage", target.label)
                    self._on_over_quota(target, True)
                return
            for held in doomed:
                try:
                    await self._async_evict(target, held.event_id)
                except DestinationError as err:
                    target.last_error = scrub_credentials(str(err))
                    return

    async def _async_evict(self, target: Target, event_id: str) -> None:
        """Delete one event folder from one destination."""
        copies = await self._index.async_copies(event_id)
        copy = copies.get(target.subentry_id)
        if copy is not None and not copy.deleted:
            await target.destination.async_delete_folder(copy.folder)
            copy.deleted = True
            await self._index.async_save_copy(event_id, target.subentry_id, copy)
        target.usage.remove(event_id)
        self._on_quota()
        _LOGGER.debug("Evicted %s from %s", event_id, target.label)

    async def async_retention(
        self, now: float, settled_orphan: Callable[[str], Awaitable[bool]]
    ) -> None:
        """Evict committed events, and settled orphans, older than each retention.

        An orphan goes only once its upload is complete and committed where it is:
        `settled_orphan(event_id)` says so. A folder in `pending/` is never removed here.
        """
        for target in self.targets.values():
            committed_before = (
                now - target.retention_days * 86400 if target.retention_days > 0 else 0.0
            )
            orphans_before = (
                now - target.orphan_retention_days * 86400
                if target.orphan_retention_days > 0
                else None
            )
            for held in target.usage.expired(committed_before, orphans_before):
                if held.state is EventState.ORPHANED:
                    copy = (await self._index.async_copies(held.event_id)).get(target.subentry_id)
                    if copy is None or not copy.committed:
                        continue
                    if not await settled_orphan(held.event_id):
                        continue
                try:
                    await self._async_evict(target, held.event_id)
                except DestinationError as err:
                    target.last_error = scrub_credentials(str(err))

    # ------------------------------------------------------------------ acks

    async def _before_release(self, record: EventRecord) -> None:
        """Run the hook that must happen before the final ack (the thumbnail fetch)."""
        if self._before_release_hook is not None:
            await self._before_release_hook(record)

    async def _async_ack_contiguous(self, pump: Pump) -> None:
        """Release the segments every destination holds, up to the first one missing."""
        up_to = pump.acked
        for key in range(pump.acked + 1, pump.record.facts.segments):
            if any(not satisfied(pump.record, lane.copy, key) for lane in pump.lanes.values()):
                break
            up_to = key
        if pump.record.facts.ended is not None and up_to == pump.record.facts.segments - 1:
            return  # the final ack waits for the thumbnail (see `_async_maybe_done`)
        await self._async_ack(pump, up_to)

    async def _async_ack(self, pump: Pump, up_to: int) -> None:
        """Acknowledge segments up to `up_to`, once each."""
        if up_to <= pump.acked:
            return
        try:
            await self._client.async_ack(pump.record.id, up_to)
            pump.acked = up_to
        except StaminaApiError as err:
            _LOGGER.debug("Ack of %s failed: %s", pump.record.id, err)

    async def async_release(self, record: EventRecord) -> None:
        """Acknowledge everything of an event that will not be uploaded (best effort)."""
        if record.facts.segments <= 0:
            return
        await self._before_release(record)
        try:
            await self._client.async_ack(record.id, record.facts.segments - 1)
        except StaminaApiError as err:
            _LOGGER.debug("Release of %s failed: %s", record.id, err)

    # ------------------------------------------------------------------ outcomes

    async def _async_copies(self, record: EventRecord) -> dict[str, Copy]:
        pump = self._pumps.get(record.id)
        if pump is not None:
            return {sid: lane.copy for sid, lane in pump.lanes.items()}
        return await self._index.async_copies(record.id)

    def follow_up_over_cap(self) -> list[tuple[Target, list[str]]]:
        """Return each destination over its follow-up cap, with its FOLLOW_UP events."""
        return [
            (target, target.usage.follow_up_oldest())
            for target in self.targets.values()
            if target.usage.follow_up_bytes() > target.follow_up_cap
        ]

    async def async_commit(self, record: EventRecord) -> None:
        """Write the manifest and `event.json`, and move the folder to its final place.

        A copy already committed (an orphan that received more segments after its first
        commit) gets its `index.m3u8` and `event.json` rewritten where it is.
        """
        await self.async_halt(record.id)
        copies = await self._async_copies(record)
        kind = record.target or FOLDER_EVENTS
        details = json.dumps(event_json(record), indent=2, default=str).encode()
        for subentry_id, copy in copies.items():
            target = self.targets.get(subentry_id)
            if target is None or copy.deleted:
                continue
            if copy.committed:
                if copy.files and any(name.endswith(".m4s") for name in copy.files):
                    manifest = manifest_for(record, copy)
                    for name, data in ((MANIFEST, manifest), (EVENT_JSON, details)):
                        await target.destination.async_store(f"{copy.folder}/{name}", data)
                        copy.files[name] = len(data)
                    await self._index.async_save_copy(record.id, subentry_id, copy)
                continue
            final = event_path(target.folder, kind, record.area, record.folder)
            if not copy.files:
                # Nothing reached this destination: no folder to move, none worth making.
                copy.committed, copy.folder = True, final
                await self._index.async_save_copy_header(record.id, subentry_id, copy)
                continue
            try:
                await self._async_commit_copy(target.destination, record, copy, final, details)
            except DestinationError as err:
                target.last_error = scrub_credentials(str(err))
                _LOGGER.warning(
                    "%s could not commit %s: %s", target.label, record.id, target.last_error
                )
                raise
            copy.folder, copy.committed = final, True
            target.usage.set(record.id, record.facts.started, record.state, copy.size)
            await self._index.async_save_copy(record.id, subentry_id, copy)
        record.latency["committed"] = time.time()
        self._on_quota()

    async def _async_commit_copy(
        self, destination: Destination, record: EventRecord, copy: Copy, final: str, details: bytes
    ) -> None:
        """Commit one copy, safely repeatable after a crash at any point."""
        manifest = manifest_for(record, copy)
        if copy.folder != final:
            source = await destination.async_list(copy.folder)
            if not source:
                at_target = await destination.async_list(final)
                if at_target:
                    # Moved already (a restart after the move, before it was recorded).
                    if f"{final}/{MANIFEST}" not in at_target:
                        await destination.async_store(f"{final}/{MANIFEST}", manifest)
                    await destination.async_store(f"{final}/{EVENT_JSON}", details)
                    copy.files[MANIFEST], copy.files[EVENT_JSON] = len(manifest), len(details)
                    return
        for name, data in ((MANIFEST, manifest), (EVENT_JSON, details)):
            await destination.async_store(f"{copy.folder}/{name}", data)
            copy.files[name] = len(data)
        if copy.folder == final:
            return
        if destination.can_move:
            try:
                await destination.async_move(copy.folder, final)
                return
            except MoveUnsupportedError as err:
                _LOGGER.debug("%s cannot move (%s); copying instead", destination.label, err)
        # Copy whatever the source folder holds — not only what the index knows — before
        # deleting it, so nothing there is lost uncopied.
        source = await destination.async_list(copy.folder)
        present = await destination.async_list(final)
        prefix = f"{copy.folder}/"
        for path in sorted(source):
            name = path[len(prefix) :] if path.startswith(prefix) else path.rsplit("/", 1)[-1]
            if f"{final}/{name}" in present:
                continue
            data = await destination.async_read(path)
            await destination.async_store(f"{final}/{name}", data)
            copy.files.setdefault(name, len(data))
        await destination.async_delete_folder(copy.folder)

    async def async_update_details(self, record: EventRecord) -> None:
        """Rewrite `event.json` of an already-committed event (after the follow-up window)."""
        copies = await self._async_copies(record)
        details = json.dumps(event_json(record), indent=2, default=str).encode()
        for subentry_id, copy in copies.items():
            target = self.targets.get(subentry_id)
            if target is None or copy.deleted or not copy.committed or not copy.files:
                continue
            try:
                await target.destination.async_store(f"{copy.folder}/{EVENT_JSON}", details)
            except DestinationError as err:
                target.last_error = scrub_credentials(str(err))
                _LOGGER.warning("%s could not update %s: %s", target.label, record.id, err)

    async def async_drop(self, record: EventRecord) -> None:
        """Delete an event's pending folder everywhere. Only for an event in DROPPING."""
        if record.state is not EventState.DROPPING:
            raise RuntimeError(f"refusing to delete {record.id} in state {record.state}")
        await self.async_halt(record.id)
        copies = await self._async_copies(record)
        for subentry_id, copy in copies.items():
            target = self.targets.get(subentry_id)
            if target is None or copy.deleted or copy.committed:
                continue
            await target.destination.async_delete_folder(copy.folder)
            copy.deleted = True
            target.usage.remove(record.id)
            await self._index.async_save_copy(record.id, subentry_id, copy)
        self._on_quota()
        await self.async_release(record)
