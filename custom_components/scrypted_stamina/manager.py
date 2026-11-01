"""The integration's runtime: link messages in, decisions, uploads and entity updates out.

This is the glue. The rules live elsewhere — `decider.py` decides, `uploader.py` moves
files, `relevance/` scores, `context.py` reads Home Assistant — and this module calls them
in the right order and writes every outcome to the index before acting on it:

* `event.start`: a record is created with the camera's area, name and folder fixed; the
  context is snapshotted; the event is registered for the predecessor term (so overlapping
  events on several cameras count); uploading begins if the camera's area uploads.
* `event.update`, `subject.verdict`, `event.segment`: facts are merged; the uploader is
  poked; the live score (for `binary_sensor.<cam>_unusual`) is refreshed.
* `event.end`: the end context is snapshotted, the provisional score computed, and the
  decider says KEEPING, FOLLOW_UP or ORPHANED. The follow-up window opens.
* The follow-up window collects late evidence from the camera's linked entities; when it
  closes the final score is computed, and a FOLLOW_UP event is kept or dropped.

Footage-safety rules enforced here, on top of the decider's:

* A drop is attempted only while the link is healthy *and caught up* (the replay after a
  reconnect fully read), and every attempt first re-reads the event from the plugin and
  re-scores it: a confirmed subject or an unusual score sends it back to KEEPING.
* A gap or a reset in the stream orphans every event that is not final, FOLLOW_UP included,
  and what was missed is recovered from the plugin's event list.
* Orphans keep uploading until the plugin has nothing more; then they commit and stay.
* Commits, drops and orphan work run as tasks, never inline in the stream's dispatch, and
  one per event at a time.

Restart: every unfinished event is read back from the index and picked up where it was —
uploads resume, an ended event still PENDING is decided, follow-up windows resume (with the
linked entities' changes made while Home Assistant was down added as late evidence), a
decided drop or commit is replayed, and an event that can no longer be completed is
orphaned and kept.

Writes are coalesced: entity updates and record saves within a second are batched, and the
stream position is written every few seconds; state transitions are saved at once.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
import datetime as dt
import json
import logging
import statistics
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .api import StaminaApiError, StaminaClient
from .areas import area_key, slug_of, wanted_areas
from .const import (
    CONF_CAMERAS,
    CONF_FOLLOW_UP_MINUTES,
    CONF_HISTORY_SIZE_MB,
    CONF_LINKED,
    CONF_LOOK_BACK_MINUTES,
    CONF_RECORDER_RETENTION_DAYS,
    CONF_RELEVANCE_SCOPE,
    CONF_RELEVANCE_SENSITIVITY,
    CONF_UNVERIFIED_SCORE,
    CONF_UNVERIFIED_SECONDS,
    CONF_UPLOAD_CLASSES,
    DEFAULT_FOLLOW_UP_MINUTES,
    DEFAULT_HISTORY_SIZE_MB,
    DEFAULT_LOOK_BACK_MINUTES,
    DEFAULT_RECORDER_RETENTION_DAYS,
    DEFAULT_RELEVANCE_SCOPE,
    DEFAULT_RELEVANCE_SENSITIVITY,
    DEFAULT_UNVERIFIED_SCORE,
    DEFAULT_UNVERIFIED_SECONDS,
    DEFAULT_UPLOAD_CLASSES,
    DOMAIN,
    FOLDER_EVENTS,
    FOLDER_UNUSUAL,
    FRESH_DAYS,
    LATENCY_SAMPLES,
    LEGACY_UNUSUAL_OVERRIDES,
    LOOK_BACK_MAX_MINUTES,
    META_AREA_UPLOADS,
    META_UPLOADS,
    POSITION_SAVE_SECONDS,
    RELEVANCE_HISTORY_DAYS,
    SAVE_DEBOUNCE_SECONDS,
    SCRYPTED_MQTT_DOMAIN,
    SEGMENT_TTL_SECONDS,
    THUMBNAIL_DROPPED_DAYS,
    THUMBNAIL_KEEP_DAYS,
    UNUSUAL_PULSE_SECONDS,
    UPDATE_DEBOUNCE_SECONDS,
    UPLOAD_MODES,
    UPLOADS_CLASSES,
    UPLOADS_OFF,
    UPLOADS_UNUSUAL,
    signal_areas,
    signal_camera,
    signal_cameras,
    signal_event,
    signal_quota,
    signal_update,
)
from .context import RecentChanges, collapse, entity_kind, late_change, snapshot
from .decider import (
    REASON_UNUSUAL,
    EventRecord,
    EventState,
    Policy,
    chosen_class,
    class_reason,
    decide_after_follow_up,
    decide_at_end,
    drop_for_cap,
    follow_up_closed,
    kept_because,
    orphan,
    subject_confirmed,
)
from .devices import CameraDevice, async_relink, own_identifier, resolve_camera
from .index import ACTION_COMMIT, ACTION_DROP, EventIndex
from .link import StaminaLink
from .naming import event_file_name, event_folder_name
from .protocol import (
    MSG_END,
    MSG_SEGMENT,
    MSG_START,
    MSG_VERDICT,
    CameraInfo,
    EventFacts,
    Message,
    ProtocolError,
)
from .relevance import Analysis
from .uploader import Uploader, video_file_name

_LOGGER = logging.getLogger(__name__)

# How long after the start a thumbnail is asked for when nothing has been confirmed yet.
THUMBNAIL_DELAY = 3.0
RETENTION_INTERVAL = dt.timedelta(hours=1)
PRUNE_FIRST_SECONDS = 300.0
# Early evidence runs up to a start's arrival, at most this long after the start itself.
START_GRACE_SECONDS = 30.0
PRUNE_HOUR = 3
PRUNE_MINUTE = 41
# How long a failed commit or drop waits before it is tried again.
RETRY_SECONDS = 60.0
# How far before the oldest open event a recovery after a gap looks.
RECOVERY_MARGIN = 15 * 60.0

ORPHAN_COMPLETE = "orphan_complete"


@dataclass(slots=True)
class CameraStatus:
    """What the per-camera entities show."""

    info: CameraInfo
    device: CameraDevice
    last: EventRecord | None = None
    pulse_until: float = 0.0
    open_unusual: set[str] = field(default_factory=set)

    @property
    def unusual_on(self) -> bool:
        """Whether `binary_sensor.<cam>_unusual` is on."""
        return bool(self.open_unusual) or time.time() < self.pulse_until


class StaminaManager:
    """Everything one config entry runs."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: StaminaClient,
        index: EventIndex,
        uploader_factory: Callable[[StaminaManager], Uploader],
    ) -> None:
        """Wire the parts together; nothing runs until `async_start`."""
        self.hass = hass
        self.entry = entry
        self.client = client
        self.index = index
        options = entry.options
        self.policy = Policy(
            unverified_seconds=float(
                options.get(CONF_UNVERIFIED_SECONDS, DEFAULT_UNVERIFIED_SECONDS)
            ),
            unverified_score=float(options.get(CONF_UNVERIFIED_SCORE, DEFAULT_UNVERIFIED_SCORE)),
        )
        self.upload_classes = frozenset(
            str(cls) for cls in options.get(CONF_UPLOAD_CLASSES, DEFAULT_UPLOAD_CLASSES)
        )
        self.recorder_retention_days = float(
            options.get(CONF_RECORDER_RETENTION_DAYS, DEFAULT_RECORDER_RETENTION_DAYS)
        )
        self.history_size_mb = float(options.get(CONF_HISTORY_SIZE_MB, DEFAULT_HISTORY_SIZE_MB))
        # The clock, replaceable in tests: every decision about time reads it.
        self.now: Callable[[], float] = time.time
        self.records: dict[str, EventRecord] = {}
        self.cameras: dict[str, CameraStatus] = {}
        # The areas that hold a camera, by key, with their titles: one device each (`areas.py`).
        self.areas: dict[str, str] = {}
        # Each area's Uploads position by area key, persisted in the index's meta; an
        # area without one takes the default, the old global select's position.
        self.default_uploads = UPLOADS_CLASSES
        self.area_uploads: dict[str, str] = {}
        # The area key of every event with a copy somewhere, and what each area's events
        # hold, recounted when holdings change (`_recount_areas`), for the area sensors.
        self._event_areas: dict[str, str] = {}
        self._area_totals: dict[str, dict[str, Any]] = {}
        self.latencies: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self.analysis = Analysis(
            hass,
            policy=self.policy,
            read_rows=index.read_learning_rows,
            store_features=index.async_set_features,
            cameras=lambda: list(self.cameras),
            scope=options.get(CONF_RELEVANCE_SCOPE, DEFAULT_RELEVANCE_SCOPE),
            sensitivity=options.get(CONF_RELEVANCE_SENSITIVITY, DEFAULT_RELEVANCE_SENSITIVITY),
            linked_kinds=self.linked_kinds,
            names=self.camera_names,
        )
        self.uploader = uploader_factory(self)
        self.link: StaminaLink | None = None
        self._timers: dict[str, CALLBACK_TYPE] = {}
        # Per camera: its newest event, where the next one's look-back window stops; and the
        # one event collecting late evidence, if any (`_linked_changed`). A change belongs to
        # one event per camera, never two.
        self._newest: dict[str, EventRecord] = {}
        self._collecting: dict[str, EventRecord] = {}
        # Recent changes of every linked entity, for the look-back window at each start.
        self.recent = RecentChanges(LOOK_BACK_MAX_MINUTES * 60.0)
        self._unsub: list[CALLBACK_TYPE] = []
        self._thumbnails: set[str] = set()
        self._final_thumbnails: set[str] = set()
        self._one_shots: dict[str, CALLBACK_TYPE] = {}
        # Events with a commit or a drop under way.
        self._busy: set[str] = set()
        # Commits asked for while the event was busy, to be looked at once it is not.
        self._recheck: set[str] = set()
        self._verdict_again: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._capping = False
        # Coalescing: entity updates, record saves, the stream position.
        self._dirty_hub = False
        self._dirty_cameras: set[str] = set()
        self._dirty_quota = False
        self._dirty_records: dict[str, EventRecord] = {}
        self._position_saved = 0.0
        self._stopping = False

    # ------------------------------------------------------------------ options

    def camera_options(self, camera: str) -> dict[str, Any]:
        """Return one camera's options."""
        return dict((self.entry.options.get(CONF_CAMERAS) or {}).get(camera) or {})

    def linked(self, camera: str) -> list[str]:
        """Return a camera's linked entities."""
        return list(self.camera_options(camera).get(CONF_LINKED) or [])

    def linked_kinds(self, camera: str) -> dict[str, str]:
        """Return a camera's linked entities and how each is read."""
        return {entity_id: entity_kind(self.hass, entity_id) for entity_id in self.linked(camera)}

    def follow_up_seconds(self, camera: str) -> float:
        """Return a camera's follow-up window."""
        minutes = self.camera_options(camera).get(CONF_FOLLOW_UP_MINUTES, DEFAULT_FOLLOW_UP_MINUTES)
        return max(0.0, float(minutes)) * 60.0

    def look_back_seconds(self, camera: str) -> float:
        """Return a camera's look-back window, at most what the ring keeps."""
        minutes = self.camera_options(camera).get(CONF_LOOK_BACK_MINUTES, DEFAULT_LOOK_BACK_MINUTES)
        return min(max(0.0, float(minutes)), float(LOOK_BACK_MAX_MINUTES)) * 60.0

    def _all_linked(self) -> list[str]:
        """Return every camera's linked entities, once each."""
        cameras = self.entry.options.get(CONF_CAMERAS) or {}
        return sorted({entity_id for camera in cameras for entity_id in self.linked(camera)})

    def camera_names(self) -> dict[str, str]:
        """Return each camera's name, for the reason sentence."""
        return {camera: status.device.name for camera, status in self.cameras.items()}

    # ------------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Resume from the index, connect, and start the timers."""
        self.default_uploads = await self._async_restore_uploads()
        stored = await self.index.async_get_meta(META_AREA_UPLOADS)
        self.area_uploads = {
            str(key): mode
            for key, mode in (json.loads(stored) if stored else {}).items()
            if mode in UPLOAD_MODES
        }
        await self.async_refresh_cameras()
        if linked := self._all_linked():
            # Before anything can start an event: the look-back window reads this ring.
            self._unsub.append(
                async_track_state_change_event(self.hass, linked, self._linked_changed)
            )
        await self.uploader.async_start()
        slugs = {slug_of(key, title): key for key, title in self.areas.items()}
        self._event_areas = {
            event_id: key or slugs.get(slug, "")
            for event_id, (key, slug) in (await self.index.async_copy_areas()).items()
        }
        # Learned in the background: setup does not wait on months of history. Until it is
        # done every camera reads as learning, which is what an empty model says anyway.
        self._spawn(self.analysis.async_rebuild(), "relevance rebuild")
        self.analysis.async_schedule()
        await self._async_resume()
        for record in self.records.values():
            if not record.area_key:
                record.area_key = slugs.get(record.area, "")
        self._recount_areas()

        seq = await self.index.async_get_meta("seq")
        epoch = await self.index.async_get_meta("epoch")
        self.link = StaminaLink(
            self.hass,
            self.client,
            seq=int(seq) if seq else None,
            epoch=epoch or None,
            on_message=self.async_handle,
            on_epoch_change=self._async_epoch_changed,
            on_reset=self._async_reset,
            on_lost=self._async_link_lost,
            on_state=self._link_state_changed,
            on_auth_failed=self._auth_failed,
            on_skipped=self._async_message_skipped,
        )
        self.link.async_start()
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_retention, RETENTION_INTERVAL)
        )
        # Pruning: a few minutes after start, then nightly at a fixed time.
        self._one_shots["prune"] = async_call_later(self.hass, PRUNE_FIRST_SECONDS, self._prune_now)
        self._unsub.append(
            async_track_time_change(
                self.hass, self._prune_now, hour=PRUNE_HOUR, minute=PRUNE_MINUTE, second=0
            )
        )
        self._unsub.append(
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._device_updated)
        )
        self._unsub.append(
            self.hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, self._area_updated)
        )

    async def async_stop(self) -> None:
        """Stop everything, keeping what is in the index."""
        self._stopping = True
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        for cancel in [
            *self._timers.values(),
            *self._one_shots.values(),
        ]:
            cancel()
        self._timers.clear()
        self._collecting.clear()
        self._one_shots.clear()
        self.analysis.async_cancel()
        if self.link is not None:
            await self.link.async_stop()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.uploader.async_stop()
        await self._async_flush_saves()
        await self._async_save_position(force=True)

    @property
    def link_healthy(self) -> bool:
        """Whether the link to the plugin is up and caught up."""
        return self.link is not None and self.link.healthy

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        """Run work as a task the manager owns (cancelled on stop)."""
        if self._stopping:
            coro.close()
            return
        task = self.hass.async_create_background_task(coro, f"scrypted_stamina {name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @callback
    def _auth_failed(self) -> None:
        self.entry.async_start_reauth(self.hass)

    @callback
    def _link_state_changed(self) -> None:
        """React to the link becoming healthy (connected and caught up) or not."""
        if self.link_healthy:
            for record in list(self.records.values()):
                if record.state is EventState.FOLLOW_UP and record.finalized:
                    self._spawn(self._async_after_follow_up(record), f"decide {record.id}")
                elif record.state is EventState.DROPPING:
                    self._spawn(self._async_drop(record), f"drop {record.id}")
        self._changed()

    # ------------------------------------------------------------------ cameras

    async def async_refresh_cameras(self) -> None:
        """Read the camera list from the plugin and resolve each camera's device."""
        try:
            cameras = await self.client.async_cameras()
        except StaminaApiError as err:
            _LOGGER.debug("Camera list unavailable: %s", err)
            return
        added = False
        for info in cameras:
            device = resolve_camera(self.hass, info.id, info.name, self.entry.entry_id)
            if info.id in self.cameras:
                self.cameras[info.id].info = info
                self.cameras[info.id].device = device
            else:
                self.cameras[info.id] = CameraStatus(info=info, device=device)
                added = True
        if added:
            async_dispatcher_send(self.hass, signal_cameras(self.entry.entry_id))
        self.async_sync_areas()

    def _camera(self, camera: str, name: str | None = None) -> CameraStatus:
        """Return a camera's status, creating a placeholder for one not listed yet."""
        status = self.cameras.get(camera)
        if status is None:
            info = CameraInfo(id=camera, name=name or f"Camera {camera}", capabilities={})
            status = CameraStatus(
                info=info,
                device=resolve_camera(self.hass, camera, info.name, self.entry.entry_id),
            )
            self.cameras[camera] = status
            async_dispatcher_send(self.hass, signal_cameras(self.entry.entry_id))
            self._spawn(self.async_refresh_cameras(), "cameras")
        return status

    @callback
    def _device_updated(self, event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
        """Follow camera devices that appear or change after setup: links and areas.

        A `scrypted_mqtt` device that appears is linked to; a camera moved to another area
        (on either device) moves to that area's device and Uploads position.
        """
        if event.data["action"] not in ("create", "update"):
            return
        device = dr.async_get(self.hass).async_get(event.data["device_id"])
        if device is None:
            return
        found = False
        for camera, status in self.cameras.items():
            if own_identifier(camera) not in device.identifiers and not any(
                domain == SCRYPTED_MQTT_DOMAIN and identifier.rpartition(":")[2] == camera
                for domain, identifier, *_ in device.identifiers
            ):
                continue
            status.device = resolve_camera(self.hass, camera, status.info.name, self.entry.entry_id)
            async_relink(self.hass, self.entry.entry_id, status.device)
            self._changed(camera)
            found = True
        if found:
            self.async_sync_areas()

    @callback
    def _area_updated(self, event: Event[ar.EventAreaRegistryUpdatedData]) -> None:
        """Follow an area renamed or removed: titles, folders and area devices."""
        if event.data["action"] == "reorder":
            return
        for camera, status in self.cameras.items():
            status.device = resolve_camera(self.hass, camera, status.info.name, self.entry.entry_id)
            self._changed(camera)
        self.async_sync_areas()

    @callback
    def async_sync_areas(self) -> None:
        """Keep `areas` to the areas that hold a camera, and tell their entities.

        With no camera known at all (the plugin unreachable at start), nothing is removed.
        """
        if self._stopping:
            return
        wanted = wanted_areas([status.device for status in self.cameras.values()])
        if wanted and wanted != self.areas:
            self.areas = wanted
            async_dispatcher_send(self.hass, signal_areas(self.entry.entry_id))

    # ------------------------------------------------------------------ messages

    async def async_handle(self, message: Message) -> None:
        """Apply one message from the plugin (quickly: heavy work is spawned)."""
        record = self.records.get(message.event_id or "")
        if (
            message.type == MSG_SEGMENT
            and message.event_id
            and (
                record is None
                or (record.state is EventState.ORPHANED and ORPHAN_COMPLETE in record.notes)
            )
        ):
            # An orphan committed already, in memory or not: more of it means reopening it.
            record = await self._async_reopen_orphan(message.event_id, record) or record
        if record is None and message.type != MSG_START and message.event is not None:
            # Joined mid-event (e.g. after a long outage): treat the first message as a start.
            record = await self._async_started(message.event)
        elif message.type == MSG_START and message.event is not None:
            record = await self._async_started(message.event, existing=record)
        if record is None:
            self._save_position_soon()
            return

        if message.event is not None and message.type != MSG_START:
            record.facts.merge(message.event)
        if message.type == MSG_VERDICT and message.subject is not None:
            record.facts.subjects[message.subject.id] = message.subject
        if message.type == MSG_SEGMENT:
            self._apply_segment(record, message)
        self.uploader.heard(record.id)

        if record.facts.camera_offline and record.state in (
            EventState.PENDING,
            EventState.KEEPING,
        ):
            record.state = orphan(record, "camera_offline")
            await self._async_finish_orphan(record)
        elif message.type == MSG_END or (record.ended and record.state is EventState.PENDING):
            await self._async_ended(record)
        elif message.type == MSG_VERDICT and record.state in (
            EventState.FOLLOW_UP,
            EventState.DROPPING,
        ):
            # A late upgrade (`vetoed` -> `confirmed`, docs/api.md) after the end: kept.
            await self._async_late_verdict(record)
        elif message.type == MSG_SEGMENT:
            # Segments change nothing the score reads: no rescoring per segment.
            self._save(record)
            self.uploader.poke(record.id)
        else:
            self._score_live(record)
            if any(subject_confirmed(s, self.policy) for s in record.facts.subjects.values()):
                self._thumbnail_soon(record, 0.0, "confirmed")
            self._save(record)
            self.uploader.poke(record.id)
        self._save_position_soon()
        self._changed(record.camera)

    async def _async_reopen_orphan(
        self, event_id: str, record: EventRecord | None = None
    ) -> EventRecord | None:
        """Take back an orphan that was committed, because more of it has arrived.

        Its upload resumes into its pending folder, and it is committed again once the plugin
        says there is nothing more: what arrived since goes into a file of its own
        (`-part2.mp4`, …), so the video already committed is never rewritten.
        """
        if record is None:
            record = await self.index.async_get(event_id)
        if record is None or record.state is not EventState.ORPHANED:
            return None
        if ORPHAN_COMPLETE in record.notes:
            record.notes.remove(ORPHAN_COMPLETE)
        record.facts.segments_final = False
        record.note("orphan_reopened")
        self.records[event_id] = record
        if record.admitted:
            await self.uploader.async_track(record)
        await self._async_save_now(record)
        self._spawn(self._async_commit_if_ready(record), f"commit {record.id}")
        return record

    async def _async_message_skipped(self, message: Message) -> None:
        """Keep the event of a message that could not be handled even after retries."""
        record = self.records.get(message.event_id or "") if message.event_id else None
        if record is None:
            return
        state = orphan(record, "message_skipped", follow_up=True)
        if state is not record.state:
            record.state = state
            await self._async_finish_orphan(record)

    def _apply_segment(self, record: EventRecord, message: Message) -> None:
        """Apply an `event.segment` of any kind: a cut, an eviction, or the last word."""
        facts = record.facts
        if message.segment_kind == "evicted":
            self.uploader.evicted(record, message.segment_evicted)
        elif message.segment is not None:
            facts.segment_seen(
                message.segment,
                init=message.segment_init,
                duration=message.segment_duration,
                discontinuity=message.segment_discontinuity,
            )
        if message.segment_next is not None:
            facts.segments = max(facts.segments, message.segment_next)
        if message.segment_last:
            facts.segments_final = True

    async def _async_started(
        self, facts: EventFacts, existing: EventRecord | None = None, note: str | None = None
    ) -> EventRecord:
        """Create the record for a new event."""
        if existing is not None:
            existing.facts.merge(facts)
            return existing
        stored = await self.index.async_get(facts.id)
        if stored is not None:
            stored.facts.merge(facts)
            self.records[facts.id] = stored
            return stored

        status = self._camera(facts.camera, facts.camera_name)
        device = status.device
        mode = self.uploads_for(area_key(device))
        local = dt_util.as_local(dt_util.utc_from_timestamp(facts.started))
        record = EventRecord(
            facts=facts,
            admitted=mode == UPLOADS_CLASSES,
            upload_mode=mode,
            area=device.area_slug,
            area_key=area_key(device),
            camera_name=device.name,
            folder=event_folder_name(local, device.name, facts.id),
            file_name=event_file_name(local, device.area_slug, device.name),
        )
        if note:
            record.note(note)
        record.latency["ha_received"] = self.now()
        record.context_start = snapshot(self.hass, self.linked(facts.camera), facts.started)
        record.early = collapse(
            self.recent.between(
                self.linked_kinds(facts.camera),
                started=facts.started,
                since=self._look_back_since(facts),
                # Up to its arrival, which is the moment late evidence could not yet be watched
                # for; bounded, so an event recovered long after it began takes nothing later.
                until=facts.started
                + min(max(0.0, self.now() - facts.started), START_GRACE_SECONDS),
            )
        )
        self._note_newest(record)
        self.records[facts.id] = record
        self._event_areas[record.id] = record.area_key
        # Registered at once, so an event on another camera that overlaps this one sees it
        # as what fired first.
        self.analysis.remember(record, self.now())
        self._score_live(record)
        await self._async_save_now(record)
        if record.admitted:
            await self.uploader.async_track(record)
        else:
            record.note(f"uploads_{mode}_at_start")
        self._thumbnail_soon(record, THUMBNAIL_DELAY, "start")
        return record

    async def _async_late_verdict(self, record: EventRecord) -> None:
        """Keep a held (or dropping) event whose subject was confirmed after it ended."""
        if record.id in self._busy:
            # A drop under way re-checks the verdicts before deleting; if it is past that
            # point, or gave up for now, this verdict is looked at again as soon as it ends.
            self._verdict_again.add(record.id)
            self._save(record)
            return
        cls = self._confirmed_class(record)
        if cls is None:
            self._save(record)
            return
        await self._async_keep(record, class_reason(cls), FOLDER_EVENTS, "confirmed_after_end")

    def _confirmed_class(self, record: EventRecord) -> str | None:
        """Return the chosen class a late verdict confirmed, keeping a held event."""
        cls = chosen_class(record, self.policy, self.upload_classes)
        confirmed = cls is not None and any(
            subject.cls == cls and subject_confirmed(subject, self.policy)
            for subject in record.facts.subjects.values()
        )
        return cls if confirmed else None

    async def _async_keep(self, record: EventRecord, reason: str, target: str, note: str) -> None:
        """Move a held or dropping event to KEEPING, and commit it once it is uploaded."""
        record.keep_reason, record.target = reason, target
        record.dropped_at = None
        record.note(note)
        await self._async_set_state(record, EventState.KEEPING)
        await self._async_keep_upload(record)
        self._spawn(self._async_commit_if_ready(record), f"commit {record.id}")

    async def _async_keep_upload(self, record: EventRecord) -> None:
        """Start uploading a kept unusual event that started under "Unusual only"."""
        if record.admitted:
            return
        if record.keep_reason == REASON_UNUSUAL and record.upload_mode == UPLOADS_UNUSUAL:
            record.admitted = True
            record.note("uploaded_because_unusual")
            await self.uploader.async_track(record)
        else:
            record.note("uploads_off")

    def _score_live(self, record: EventRecord) -> None:
        """Refresh the live score of an open event, for the unusual binary sensor."""
        result = self.analysis.score(record, now=self.now(), include_late=False)
        record.live_score = result.score
        record.unusual_live = result.unusual
        status = self._camera(record.camera)
        was_on = status.unusual_on
        if result.unusual and not record.ended:
            status.open_unusual.add(record.id)
        else:
            status.open_unusual.discard(record.id)
        if status.unusual_on != was_on:
            # Not held back by the debounce: an unusual event opening is news at once.
            async_dispatcher_send(self.hass, signal_camera(self.entry.entry_id, record.camera))

    async def _async_ended(self, record: EventRecord) -> None:
        """Decide an event that has just ended, and open its follow-up window."""
        if record.follow_up_until is not None:
            self._save(record)
            return  # a repeated end
        status = self._camera(record.camera)
        status.open_unusual.discard(record.id)
        record.latency["end_received"] = self.now()
        ended = record.facts.ended or self.now()
        record.context_end = snapshot(self.hass, self.linked(record.camera), ended)
        result = self.analysis.score(record, now=self.now())
        record.provisional_score = result.score
        record.unusual_provisional = result.unusual
        record.reason, record.reasons = result.reason, result.reasons
        self.analysis.remember(record, self.now())

        state = decide_at_end(record, self.policy, self.upload_classes)
        record.follow_up_until = ended + self.follow_up_seconds(record.camera)
        await self._async_set_state(record, state)
        self._open_follow_up(record)
        status.last = record
        self._fire(record, late=False)

        if state is EventState.KEEPING:
            await self._async_keep_upload(record)
            self._spawn(self._async_commit_if_ready(record), f"commit {record.id}")
        elif state is EventState.ORPHANED:
            await self._async_finish_orphan(record)
        elif (
            state is EventState.FOLLOW_UP
            and not record.admitted
            and record.upload_mode == UPLOADS_UNUSUAL
        ):
            # Unusual only: a late unusual would still be uploaded — and the plugin
            # keeps segments only for its TTL, which the window may outlast. So the event is
            # held in `pending/` like any other; ordinary at the end, it is dropped as usual.
            record.admitted = True
            record.note("held_while_off")
            await self.uploader.async_track(record)
        self._spawn(self._async_enforce_follow_up_cap(), "cap")

    # ------------------------------------------------------------------ states

    async def _async_set_state(self, record: EventRecord, state: EventState) -> None:
        """Change an event's state and write it down at once."""
        record.state = state
        await self._async_save_now(record)
        self.uploader.set_state(record)
        self._changed(record.camera)

    def on_uploaded(self, record: EventRecord) -> None:
        """Act on the uploader having every segment it will ever get of an event."""
        self._spawn(self._async_commit_if_ready(record), f"commit {record.id}")
        self._spawn(self._async_enforce_follow_up_cap(), "cap")

    def on_quota(self) -> None:
        """Tell the quota sensors a destination's holdings changed (through the debounce)."""
        self._dirty_quota = True
        self._changed()

    def on_over_quota(self, target: Any, over: bool) -> None:
        """Raise (or clear) a repair issue: a destination went over quota with pending footage."""
        issue_id = f"over_quota_{target.subentry_id}"
        if over:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="over_quota",
                translation_placeholders={"destination": target.label},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _ready_to_commit(self, record: EventRecord) -> bool:
        if record.state is EventState.KEEPING:
            return record.ended and (
                not self.uploader.tracking(record.id) or self.uploader.complete(record)
            )
        if record.state is EventState.ORPHANED and ORPHAN_COMPLETE not in record.notes:
            return not self.uploader.tracking(record.id) or self.uploader.complete(record)
        return False

    async def _async_commit_if_ready(self, record: EventRecord) -> None:
        """Commit a KEEPING (or orphaned) event once everything it will get is uploaded.

        Several paths can ask at once (the uploader finishing, a late verdict, a restart),
        so a commit already under way is not started twice. A failed commit is retried
        after `RETRY_SECONDS`; the files stay where they are meanwhile, never deleted.
        """
        if record.id in self._busy:
            self._recheck.add(record.id)  # looked at again when the other work finishes
            return
        if not self._ready_to_commit(record):
            return
        self._busy.add(record.id)
        try:
            await self.index.async_add_action(record.id, ACTION_COMMIT, self.now())
            try:
                await self.uploader.async_commit(record)
            except Exception as err:  # any failure is retried later
                _LOGGER.warning("Commit of %s failed, will retry: %s", record.id, err)
                self._changed()  # the destination's health may have turned
                self._retry_later(record, self._async_commit_if_ready)
                return
            if record.state is EventState.KEEPING:
                record.state = EventState.COMMITTED
            else:
                record.note(ORPHAN_COMPLETE)
                if not record.ended:
                    record.finalized = True  # no end, no window: nothing more to learn
            await self._async_save_now(
                record, features=self.analysis.features(record) if record.finalized else None
            )
            self.uploader.set_state(record)
            await self.index.async_done_action(record.id, ACTION_COMMIT)
        finally:
            self._unbusy(record)
        self._changed(record.camera)
        await self._async_maybe_forget(record)

    def _unbusy(self, record: EventRecord) -> None:
        """End an event's commit or drop, and run a commit that was asked for meanwhile."""
        self._busy.discard(record.id)
        if record.id in self._verdict_again:
            self._verdict_again.discard(record.id)
            if record.state in (EventState.FOLLOW_UP, EventState.DROPPING):
                self._spawn(self._async_late_verdict(record), f"verdict {record.id}")
        if record.id in self._recheck:
            self._recheck.discard(record.id)
            self._spawn(self._async_commit_if_ready(record), f"commit {record.id}")

    def _retry_later(self, record: EventRecord, action: Callable[[EventRecord], Awaitable]) -> None:
        """Try a failed commit or drop again in a while."""

        @callback
        def _again(_now: Any) -> None:
            self._one_shots.pop(f"{record.id}:retry", None)
            self._spawn(action(record), f"retry {record.id}")

        key = f"{record.id}:retry"
        if key not in self._one_shots and not self._stopping:
            self._one_shots[key] = async_call_later(self.hass, RETRY_SECONDS, _again)

    async def _async_finish_orphan(self, record: EventRecord) -> None:
        """Keep an orphaned event: it keeps uploading, then commits where it is."""
        await self._async_save_now(record)
        self.uploader.set_state(record)
        self._camera(record.camera).open_unusual.discard(record.id)
        if record.admitted and not self.uploader.tracking(record.id):
            await self.uploader.async_track(record)
        self._spawn(self._async_commit_if_ready(record), f"commit {record.id}")

    async def _async_still_droppable(self, record: EventRecord) -> bool | None:
        """Re-check a drop against the plugin and the model, right before deleting.

        True: drop. False: kept after all (moved back to KEEPING). None: cannot tell now
        (the plugin is unreachable); the drop waits.
        """
        try:
            state = await self.client.async_event(record.id)
        except StaminaApiError:
            return None
        if state is not None and isinstance(state.get("event"), dict):
            with suppress(ProtocolError):
                record.facts.merge(EventFacts.parse(state["event"]))
        if (cls := self._confirmed_class(record)) is not None:
            await self._async_keep(
                record, class_reason(cls), FOLDER_EVENTS, "confirmed_before_drop"
            )
            return False
        result = self.analysis.score(record, now=self.now())
        if result.unusual:
            record.unusual_final = True
            record.final_score = result.score
            await self._async_keep(record, REASON_UNUSUAL, FOLDER_UNUSUAL, "unusual_before_drop")
            return False
        return True

    async def _async_drop(self, record: EventRecord) -> None:
        """Carry out a DROPPING decision: re-check, write it down, then delete."""
        if record.state is not EventState.DROPPING or record.id in self._busy:
            return
        if not self.link_healthy:
            return  # retried when the link is healthy again (`_link_state_changed`)
        self._busy.add(record.id)
        keep = False
        try:
            droppable = await self._async_still_droppable(record)
            if droppable is None:
                self._retry_later(record, self._async_drop)
                return
            if not droppable:
                keep = True
                return
            await self.index.async_add_action(record.id, ACTION_DROP, self.now())
            try:
                await self.uploader.async_drop(record)
            except Exception as err:  # any failure is retried later
                _LOGGER.warning("Dropping %s failed, will retry: %s", record.id, err)
                self._retry_later(record, self._async_drop)
                return
            await self._async_set_state(record, EventState.DROPPED)
            await self.index.async_done_action(record.id, ACTION_DROP)
        finally:
            self._unbusy(record)
        if keep:
            await self._async_commit_if_ready(record)
            return
        await self._async_maybe_forget(record)

    async def _async_maybe_forget(self, record: EventRecord) -> None:
        """Let go of a record whose files and scores are both final."""
        files_final = record.state in (EventState.COMMITTED, EventState.DROPPED) or (
            record.state is EventState.ORPHANED and ORPHAN_COMPLETE in record.notes
        )
        if not files_final:
            return
        await self.uploader.async_forget(record.id)
        if record.finalized:
            self.records.pop(record.id, None)
            self._thumbnails -= {f"{record.id}:start", f"{record.id}:confirmed"}
            self._final_thumbnails.discard(record.id)

    async def _async_enforce_follow_up_cap(self) -> None:
        """Drop the oldest held events while a destination holds more than its cap.

        Only events still vetoed and ordinary *with the late evidence so far*, and never one
        that has any late evidence at all: that is exactly what the window is waiting for.
        """
        if self._capping:
            return
        self._capping = True
        try:
            for target, oldest in self.uploader.follow_up_over_cap():
                for event_id in oldest:
                    if target.usage.follow_up_bytes() <= target.follow_up_cap:
                        break
                    record = self.records.get(event_id)
                    if record is None or record.late:
                        continue
                    if self.analysis.score(record, now=self.now()).unusual:
                        continue
                    state = drop_for_cap(record, link_healthy=self.link_healthy, now=self.now())
                    if state is EventState.DROPPING:
                        await self._async_set_state(record, state)
                        await self._async_drop(record)
        finally:
            self._capping = False

    # ------------------------------------------------------------------ follow-up

    @callback
    def _linked_changed(self, event: Event[EventStateChangedData]) -> None:
        """Remember a linked entity's change, and add it to the events collecting it.

        Remembered for the look-back window of what comes next; late evidence of the one
        event collecting on each camera the entity is linked to.
        """
        entity_id = event.data["entity_id"]
        old, new = event.data["old_state"], event.data["new_state"]
        self.recent.add(entity_id, old, new)
        for record in self._collecting.values():
            if record.finalized or entity_id not in self.linked(record.camera):
                continue
            item = late_change(
                entity_id,
                entity_kind(self.hass, entity_id),
                old,
                new,
                started=record.facts.started,
                ended=record.facts.ended or record.facts.started,
            )
            if item is not None:
                record.late.append(item)
                self._save(record)

    def _note_newest(self, record: EventRecord) -> None:
        """Remember an event as its camera's newest, unless a later one is known."""
        newest = self._newest.get(record.camera)
        if newest is None or newest.facts.started <= record.facts.started:
            self._newest[record.camera] = record

    def _newer_start(self, record: EventRecord) -> float | None:
        """Return when the next event on the record's camera started, if one has."""
        newest = self._newest.get(record.camera)
        if newest is None or newest.facts.started <= record.facts.started:
            return None
        return newest.facts.started

    def _look_back_since(self, facts: EventFacts) -> float:
        """Return where a new event's look-back window starts, and take it from the last event.

        Never before the previous event on the camera ended: what happened during it is its
        own. One still in its follow-up stops collecting here, and gives up the changes the
        new window covers, which are nearer this start than its end; one whose window has
        closed keeps everything up to that close.
        """
        since = facts.started - self.look_back_seconds(facts.camera)
        status = self.cameras.get(facts.camera)
        previous = self._newest.get(facts.camera) or (status.last if status else None)
        if previous is None or previous.facts.started >= facts.started:
            return since
        if self._collecting.get(facts.camera) is previous:
            del self._collecting[facts.camera]
        ended = previous.facts.ended
        if ended is None:
            return max(since, facts.started)
        if previous.finalized:
            return max(since, ended, previous.follow_up_until or ended)
        since = max(since, ended)
        cut = round(since - ended, 1)
        kept = [item for item in previous.late if float(item.get("t_rel_s", 0.0)) < cut]
        if len(kept) != len(previous.late):
            previous.late = kept
            self._save(previous)
        return since

    @callback
    def _open_follow_up(self, record: EventRecord) -> None:
        """Start collecting late evidence, unless a newer event has, and arm the window's end."""
        if record.follow_up_until is None or record.finalized:
            return
        self._cancel_follow_up(record)
        if self._newer_start(record) is None:
            self._collecting[record.camera] = record

        @callback
        def _close(_now: Any) -> None:
            self._timers.pop(record.id, None)
            self._spawn(self._async_close_follow_up(record), f"close {record.id}")

        delay = max(0.0, record.follow_up_until - self.now())
        self._timers[record.id] = async_call_later(self.hass, delay, _close)

    @callback
    def _cancel_follow_up(self, record: EventRecord) -> None:
        if (cancel := self._timers.pop(record.id, None)) is not None:
            cancel()
        if self._collecting.get(record.camera) is record:
            del self._collecting[record.camera]

    def _catch_up_late(self, record: EventRecord) -> None:
        """Add linked-entity changes made while Home Assistant was not listening.

        After a restart, the state machine only reports what changes from now on; anything
        that changed during the window before the restart is only visible as `last_changed`.
        """
        started = record.facts.started
        ended = record.facts.ended or started
        until = min(self.now(), record.follow_up_until or self.now())
        if (newer := self._newer_start(record)) is not None:
            until = min(until, newer)  # from there on, the newer event's
        seen = {(item.get("entity"), item.get("to")) for item in record.late}
        for entity_id in self.linked(record.camera):
            state = self.hass.states.get(entity_id)
            if state is None or (entity_id, state.state) in seen:
                continue
            if not ended <= state.last_changed.timestamp() <= until:
                continue
            item = late_change(
                entity_id,
                entity_kind(self.hass, entity_id),
                None,
                state,
                started=started,
                ended=ended,
            )
            if item is not None:
                item["while_restarting"] = True
                record.late.append(item)

    async def _async_close_follow_up(self, record: EventRecord) -> None:
        """Compute the final score once the window has closed, then act on it."""
        if not follow_up_closed(record, self.now()):
            return
        self._cancel_follow_up(record)
        result = self.analysis.score(record, now=self.now())
        # Complete now, so stored as one item per entity; what scoring reads is unchanged.
        record.late = collapse(record.late)
        was_unusual = record.unusual_provisional
        record.final_score = result.score
        record.unusual_final = result.unusual
        record.reason, record.reasons = result.reason, result.reasons
        record.finalized = True
        await self._async_save_now(record, features=self.analysis.features(record))
        if result.unusual and not was_unusual:
            status = self._camera(record.camera)
            status.pulse_until = time.time() + UNUSUAL_PULSE_SECONDS
            status.last = record
            self._fire(record, late=True)

            @callback
            def _pulse_off(_now: Any) -> None:
                self._one_shots.pop(f"{record.id}:pulse", None)
                self._changed(record.camera)

            self._one_shots[f"{record.id}:pulse"] = async_call_later(
                self.hass, UNUSUAL_PULSE_SECONDS + 0.1, _pulse_off
            )
        await self._async_after_follow_up(record)
        self._changed(record.camera)

    async def _async_after_follow_up(self, record: EventRecord) -> None:
        """Act on the final score: keep or drop a held event, update a kept one."""
        if record.state is EventState.FOLLOW_UP:
            state = decide_after_follow_up(record, link_healthy=self.link_healthy, now=self.now())
            if state is EventState.KEEPING:
                await self._async_set_state(record, state)
                await self._async_keep_upload(record)
                self._spawn(self._async_commit_if_ready(record), f"commit {record.id}")
            elif state is EventState.DROPPING:
                await self._async_set_state(record, state)
                await self._async_drop(record)
        if not record.admitted and record.state is not EventState.FOLLOW_UP:
            # Uploads were off and the window did not change that: let the plugin go.
            await self.uploader.async_release(record)
        await self._async_maybe_forget(record)

    # ------------------------------------------------------------------ link events

    async def _async_orphan_open(self, why: str, *, follow_up: bool) -> None:
        """Orphan every event that is not final (FOLLOW_UP too after a gap or reset)."""
        for record in list(self.records.values()):
            state = orphan(record, why, follow_up=follow_up)
            if state is record.state:
                continue
            record.state = state
            await self._async_finish_orphan(record)
        self._changed()

    async def _async_link_lost(self) -> None:
        await self._async_orphan_open("link_lost", follow_up=False)

    async def _async_epoch_changed(self, _old: str | None, _new: str) -> None:
        await self._async_orphan_open("plugin_restarted", follow_up=True)

    async def _async_reset(self) -> None:
        """Messages were lost: keep what is open, then recover what was missed."""
        open_since = [r.facts.started for r in self.records.values() if not r.finalized]
        await self._async_orphan_open("stream_gap", follow_up=True)
        since = min([*open_since, self.now()]) - RECOVERY_MARGIN
        self._spawn(self._async_recover(since), "recover")

    async def _async_recover(self, since: float) -> None:
        """Read the events the plugin still knows, and fold in what the gap hid."""
        try:
            events = await self.client.async_events_since(since)
        except StaminaApiError as err:
            _LOGGER.debug("Recovery after a gap failed: %s", err)
            return
        for raw in events:
            try:
                facts = EventFacts.parse(raw.get("event", raw))
            except ProtocolError:
                continue
            segments = raw.get("segments", {}) if "event" in raw else {}
            if segments is None:
                facts.segments_final = True  # released: nothing more will come
            elif isinstance(segments, dict):
                following = segments.get("next")
                if isinstance(following, int) and not isinstance(following, bool):
                    facts.segments = max(facts.segments, following)
                if segments.get("live") is False:
                    facts.segments_final = True
            record = self.records.get(facts.id)
            if record is not None:
                record.facts.merge(facts)
                self._save(record)
                continue
            if await self.index.async_get(facts.id) is not None:
                continue
            record = await self._async_started(facts, note="recovered_after_gap")
            if record.ended:
                await self._async_ended(record)
        self._changed()

    def _save_position_soon(self) -> None:
        """Write the stream position at most every few seconds."""
        if self.now() - self._position_saved >= POSITION_SAVE_SECONDS:
            self._spawn(self._async_save_position(), "position")
            return
        if "position" not in self._one_shots and not self._stopping:

            @callback
            def _later(_now: Any) -> None:
                self._one_shots.pop("position", None)
                self._spawn(self._async_save_position(), "position")

            self._one_shots["position"] = async_call_later(self.hass, POSITION_SAVE_SECONDS, _later)

    async def _async_save_position(self, *, force: bool = False) -> None:
        """Remember where the stream is, together with every record it changed.

        One transaction: a restart never resumes past a message whose effect on a record
        was not saved.
        """
        if self.link is None or self.link.seq is None:
            await self._async_flush_saves()
            return
        self._position_saved = self.now()
        dirty, self._dirty_records = self._dirty_records, {}
        await self.index.async_save_with_position(
            [(record, None) for record in dirty.values()],
            {"seq": str(self.link.seq), "epoch": self.link.epoch or ""},
        )

    # ------------------------------------------------------------------ saving

    def _save(self, record: EventRecord) -> None:
        """Save a record soon, coalescing the saves of a busy second."""
        self._dirty_records[record.id] = record
        if "save" in self._one_shots or self._stopping:
            return

        @callback
        def _flush(_now: Any) -> None:
            self._one_shots.pop("save", None)
            self._spawn(self._async_flush_saves(), "save")

        self._one_shots["save"] = async_call_later(self.hass, SAVE_DEBOUNCE_SECONDS, _flush)

    async def _async_save_now(self, record: EventRecord, features: str | None = None) -> None:
        """Save a record at once (state transitions, decisions)."""
        self._dirty_records.pop(record.id, None)
        await self.index.async_save(record, features)

    async def _async_flush_saves(self) -> None:
        dirty, self._dirty_records = self._dirty_records, {}
        if dirty:
            await self.index.async_save_many([(record, None) for record in dirty.values()])

    # ------------------------------------------------------------------ restart

    async def _async_resume(self) -> None:
        """Pick up every unfinished event after a restart."""
        now = self.now()
        for record in await self.index.async_unfinished():
            self.records[record.id] = record
            self._note_newest(record)
        actions = await self.index.async_actions()
        # The spool holds segments for commits to come: only these events can still have one.
        await self.uploader.async_clean_spool({*self.records, *(eid for eid, _ in actions)})

        for record in list(self.records.values()):
            try:
                await self._async_resume_record(record, now)
            except Exception:
                # One record must not stop the others: keep it, as an orphan.
                _LOGGER.exception("Resuming event %s failed; it is kept as orphaned", record.id)
                record.state = EventState.ORPHANED
                record.note("resume_failed")
                await self._async_save_now(record)
        await self._async_resume_actions(actions, now)

    async def _async_resume_record(self, record: EventRecord, now: float) -> None:
        """Pick up one unfinished event."""
        if record.state in (EventState.PENDING, EventState.KEEPING) and (
            not record.ended and now - record.facts.started > SEGMENT_TTL_SECONDS
        ):
            record.state = orphan(record, "expired_during_restart")
            await self._async_finish_orphan(record)
            return
        if (
            record.admitted
            and record.state
            in (
                EventState.PENDING,
                EventState.KEEPING,
                EventState.FOLLOW_UP,
                EventState.ORPHANED,
            )
            and ORPHAN_COMPLETE not in record.notes
        ):
            await self.uploader.async_track(record)
        if record.state is EventState.PENDING and record.ended:
            # Its end was handled before the restart, but not its decision.
            record.follow_up_until = None
            await self._async_ended(record)
            return
        if record.ended and not record.finalized:
            self._catch_up_late(record)
            self._open_follow_up(record)

    async def _async_resume_actions(self, actions: list[tuple[str, str]], now: float) -> None:
        """Replay decided actions, and restart what each kept event was waiting for."""
        for event_id, action in actions:
            record = self.records.get(event_id) or await self.index.async_get(event_id)
            if record is None:
                await self.index.async_done_action(event_id, action)
                continue
            self.records[event_id] = record
            stale_commit = action == ACTION_COMMIT and record.state not in (
                EventState.KEEPING,
                EventState.ORPHANED,
            )
            stale_drop = action == ACTION_DROP and record.state is not EventState.DROPPING
            if stale_commit or stale_drop:
                await self.index.async_done_action(event_id, action)

        for record in list(self.records.values()):
            if record.state is EventState.DROPPING:
                pass  # carried out once the link is healthy (`_link_state_changed`)
            elif record.state in (EventState.KEEPING, EventState.ORPHANED):
                self._spawn(self._async_commit_if_ready(record), f"commit {record.id}")
            if follow_up_closed(record, now):
                self._spawn(self._async_close_follow_up(record), f"close {record.id}")
            await self._async_maybe_forget(record)
            status = self.cameras.get(record.camera)
            if status is not None and record.ended:
                status.last = record

        # The last event of each camera, for the entities, even when nothing is unfinished.
        for camera, status in self.cameras.items():
            if status.last is None:
                latest = await self.index.async_events(camera=camera, limit=1, newest_first=True)
                status.last = latest[0] if latest else None

    # ------------------------------------------------------------------ misc

    async def _async_retention(self, _now: Any) -> None:
        async def settled(event_id: str) -> bool:
            record = self.records.get(event_id) or await self.index.async_get(event_id)
            return record is not None and ORPHAN_COMPLETE in record.notes

        await self.uploader.async_retention(self.now(), settled)
        self._changed()

    @callback
    def _prune_now(self, _now: Any) -> None:
        self._one_shots.pop("prune", None)
        self._spawn(self._async_prune(_now), "prune")

    async def _async_prune(self, _now: Any) -> None:
        """Age the index: history horizon, slim tier, thumbnails, then the size cap."""
        now = self.now()
        pruned = await self.index.async_prune(
            history_before=now - RELEVANCE_HISTORY_DAYS * 86400,
            fresh_before=now - FRESH_DAYS * 86400,
            thumbnails_before=now - THUMBNAIL_KEEP_DAYS * 86400,
            dropped_thumbnails_before=now - THUMBNAIL_DROPPED_DAYS * 86400,
        )
        thinned = await self.index.async_thin(int(self.history_size_mb * 1024 * 1024))
        if pruned.deleted or pruned.slimmed or pruned.thumbnails or thinned:
            _LOGGER.debug(
                "Index pruned: %s rows deleted, %s slimmed, %s thumbnails removed, %s thinned",
                pruned.deleted,
                pruned.slimmed,
                pruned.thumbnails,
                thinned,
            )

    def uploads_for(self, key: str) -> str:
        """Return an area's Uploads position, by area key."""
        return self.area_uploads.get(key, self.default_uploads)

    async def async_set_uploads(self, mode: str, key: str) -> None:
        """Set an area's Uploads position. It applies to events that start from now on."""
        if mode not in UPLOAD_MODES:
            raise ValueError(f"unknown uploads position {mode}")
        self.area_uploads[key] = mode
        await self.index.async_set_meta(
            {META_AREA_UPLOADS: json.dumps(self.area_uploads, sort_keys=True)}
        )
        self._changed()

    def _recount_areas(self) -> None:
        """Recount what each area's events hold, on the destination holding most of them.

        Run when the destinations' holdings change (commit, evict, drop, start), not on every
        read. Every kept event goes to every destination, so they agree but for failures.
        """
        targets = list(self.uploader.targets.values())
        self._event_areas = {
            event_id: key
            for event_id, key in self._event_areas.items()
            if event_id in self.records or any(t.usage.get(event_id) for t in targets)
        }
        totals: dict[str, dict[str, Any]] = {}
        for target in targets:
            mine: dict[str, list[int]] = {}
            for event_id, key in self._event_areas.items():
                if held := target.usage.get(event_id):
                    counted = mine.setdefault(key, [0, 0])
                    counted[0] += held.size
                    counted[1] += 1
            for key, (used, events) in mine.items():
                if key not in totals or used >= totals[key]["used"]:
                    totals[key] = {"used": used, "events": events, "target": target.subentry_id}
        self._area_totals = totals

    def area_holdings(self, key: str) -> dict[str, Any]:
        """Return what one area's events hold, and what it has uploading and pending.

        The holdings are the last recount's; the rest is read from the events in memory.
        """
        targets = list(self.uploader.targets.values())
        held = dict(self._area_totals.get(key) or {"used": 0, "events": 0})
        # The quota is read live, of the destination the recount chose (else the one it
        # would choose when nothing is held: the last), so an option change shows at once.
        target = self.uploader.targets.get(str(held.pop("target", ""))) or (
            targets[-1] if targets else None
        )
        held["quota"] = target.quota if target is not None else 0
        mine = [record for record in self.records.values() if record.area_key == key]
        pending = [record for record in mine if record.state is EventState.FOLLOW_UP]
        failing = [target for target in targets if not target.healthy]
        return {
            **held,
            "uploading": sum(
                1
                for record in mine
                if record.admitted and record.state in (EventState.PENDING, EventState.KEEPING)
            ),
            "pending_events": len(pending),
            "pending_bytes": sum(
                max((t.usage.get(r.id).size for t in targets if t.usage.get(r.id)), default=0)
                for r in pending
            ),
            "last_error": failing[0].last_error if failing else None,
        }

    async def _async_restore_uploads(self) -> str:
        """Return the stored position, migrating the old switch's state once.

        The switch on becomes "Chosen classes"; off becomes "Unusual only" when unusual
        events used to upload anyway (the old option, on by default), else "Off".
        """
        stored = await self.index.async_get_meta(META_UPLOADS)
        if stored in UPLOAD_MODES:
            return stored
        enabled = await self.index.async_get_meta("uploads_enabled")
        if enabled != "0":
            mode = UPLOADS_CLASSES
        elif self.entry.options.get(LEGACY_UNUSUAL_OVERRIDES, True):
            mode = UPLOADS_UNUSUAL
        else:
            mode = UPLOADS_OFF
        await self.index.async_set_meta({META_UPLOADS: mode})
        return mode

    def record_latency(self, seconds: float) -> None:
        """Keep a time-to-first-segment-uploaded sample."""
        if 0 <= seconds < 3600:
            self.latencies.append(round(seconds, 3))
            self._changed()

    def latency_stats(self) -> dict[str, float | None]:
        """Return p50 and p95 of recent samples."""
        samples = sorted(self.latencies)
        if not samples:
            return {"p50": None, "p95": None}
        if len(samples) == 1:
            return {"p50": samples[0], "p95": samples[0]}
        cuts = statistics.quantiles(samples, n=20, method="inclusive")
        return {"p50": round(statistics.median(samples), 3), "p95": round(cuts[18], 3)}

    async def async_final_thumbnail(self, record: EventRecord) -> None:
        """Fetch the thumbnail once more before the final ack releases it.

        The plugin replaces the start picture with one taken at the first confirmation, and
        forgets both at the final ack (`docs/api.md`), so this is the last chance.
        """
        if record.id in self._final_thumbnails:
            return
        self._final_thumbnails.add(record.id)
        try:
            data = await self.client.async_thumbnail(record.id)
        except StaminaApiError as err:
            _LOGGER.debug("No thumbnail for %s: %s", record.id, err)
            return
        if data:
            await self.index.async_save_thumbnail(record.id, data)

    def _thumbnail_soon(self, record: EventRecord, delay: float, kind: str) -> None:
        """Fetch the event's thumbnail: shortly after the start, and again at confirmation.

        Each `kind` is fetched once; a `425` (not ready) leaves it to be tried at the next
        chance, and the end of the event always tries again (`async_final_thumbnail`).
        """
        key = f"{record.id}:{kind}"
        if key in self._thumbnails:
            return
        self._thumbnails.add(key)

        async def _fetch() -> None:
            try:
                data = await self.client.async_thumbnail(record.id)
            except StaminaApiError as err:
                _LOGGER.debug("No thumbnail for %s: %s", record.id, err)
                data = None
            if data:
                await self.index.async_save_thumbnail(record.id, data)

        if delay <= 0:
            self._spawn(_fetch(), f"thumbnail {record.id}")
            return

        @callback
        def _later(_now: Any) -> None:
            self._one_shots.pop(key, None)
            self._spawn(_fetch(), f"thumbnail {record.id}")

        if not self._stopping:
            self._one_shots[key] = async_call_later(self.hass, delay, _later)

    def cloud_path(self, record: EventRecord) -> str | None:
        """Return the event's path inside each destination's folder, for attributes.

        A settled event is its video (as committed, `-2` and all); one still uploading is its
        pending folder.
        """
        if record.state is EventState.DROPPED or not record.folder:
            return None
        if record.state is EventState.COMMITTED or ORPHAN_COMPLETE in record.notes:
            kind = record.target or FOLDER_EVENTS
            return record.video_path or f"{kind}/{video_file_name(record)}"
        return f"pending/{record.area}/{record.folder}"

    def attributes(self, record: EventRecord) -> dict[str, Any]:
        """Return an event's entity attributes (architecture §3.7)."""
        status = self.cameras.get(record.camera)
        return {
            "event_id": record.id,
            "score": record.score,
            "provisional_score": record.provisional_score,
            "final_score": record.final_score,
            "reason": record.reason,
            "reasons": record.reasons,
            "subjects": [
                {
                    "class": s.cls,
                    "sources": [
                        *s.sources,
                        *(["camera AI (whole frame)"] if s.ambiguous else []),
                    ],
                    "verdict": s.verdict,
                }
                for s in record.facts.subjects.values()
            ],
            "decision": str(record.state),
            "keep_reason": record.keep_reason,
            "kept_because": kept_because(record.keep_reason),
            "cloud_path": self.cloud_path(record),
            "started": dt_util.utc_from_timestamp(record.facts.started).isoformat(),
            "duration": record.facts.duration,
            "late_evidence": len(record.late),
            "learning": self.analysis.state(record.camera) == "learning",
            "capabilities": status.info.capabilities if status else {},
        }

    def _fire(self, record: EventRecord, *, late: bool) -> None:
        """Fire `event.<cam>_stamina`."""
        async_dispatcher_send(
            self.hass,
            signal_event(self.entry.entry_id),
            record.camera,
            "unusual" if record.unusual else "normal",
            {**self.attributes(record), "late": late},
        )

    @callback
    def _changed(self, camera: str | None = None) -> None:
        """Tell entities something they show has changed.

        At once when idle, then at most once a second (leading and trailing edge), so a
        binary sensor turning on is not held back by the debounce.
        """
        self._dirty_hub = True
        if camera is not None:
            self._dirty_cameras.add(camera)
        if self._stopping:
            return
        if "changed" in self._one_shots:
            return  # inside a window: the trailing flush will carry it
        self._flush_changes()

        @callback
        def _trailing(_now: Any) -> None:
            self._one_shots.pop("changed", None)
            if self._dirty_hub or self._dirty_cameras or self._dirty_quota:
                self._changed()

        self._one_shots["changed"] = async_call_later(self.hass, UPDATE_DEBOUNCE_SECONDS, _trailing)

    @callback
    def _flush_changes(self) -> None:
        cameras, self._dirty_cameras = self._dirty_cameras, set()
        if self._dirty_quota:
            # Before any signal: the area sensors refresh on the hub's.
            self._recount_areas()
        if self._dirty_hub:
            self._dirty_hub = False
            async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))
        if self._dirty_quota:
            self._dirty_quota = False
            async_dispatcher_send(self.hass, signal_quota(self.entry.entry_id))
        for item in cameras:
            async_dispatcher_send(self.hass, signal_camera(self.entry.entry_id, item))
