"""Websocket commands for the review panel (architecture §3.6).

Thin reads over what the integration already holds — the index, the manager's live records
and camera list, the relevance analysis, the signed playback URLs — shaped for the panel:

* `scrypted_stamina/events` — newest first, filtered (unusual, class, camera or area,
  not-kept included or not), paged with an opaque cursor.
* `scrypted_stamina/event` — one event: why it scored as it did, what was decided, where
  the copy is, and signed URLs that play it, with `order`, which plays first.
* `scrypted_stamina/cameras` — each camera's status, capabilities and learning progress,
  and `playback_order` (`cloud_first`, the default, or `recording_first`).
* `scrypted_stamina/subscribe` — `{"changed": true}` when something the panel shows has
  changed, at most once per `NUDGE_SECONDS` (the manager's signals are coalesced here).

All four are admin-only: the history says when a household is in and out.

Nothing here writes. The token never reaches the browser: video and thumbnails go through
the signed views in `playback.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import re
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components import websocket_api
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
import voluptuous as vol

from .const import (
    DOMAIN,
    SCORE_MIN_DAYS,
    SCORE_MIN_EVENTS,
    signal_cameras,
    signal_event,
    signal_update,
)
from .context import collapse
from .decider import EventRecord, EventState
from .playback import play_url, playback_order, thumbnail_url

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from . import StaminaData

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
# The fallback scan (an index without `async_page`): rows read per query, and per call. A
# filter that matches little returns a short page and a cursor rather than reading the whole
# history at once.
BATCH = 200
SCAN_CAP = 2000
# At most one `changed` message per subscription in this many seconds.
NUDGE_SECONDS = 1.0

ERR_NOT_LOADED = "not_loaded"
ERR_NOT_FOUND = "not_found"

_REGISTERED = f"{DOMAIN}_websocket_registered"
# "predecessor: 90s after Gate (+0.83)", as `relevance.Analysis.score` writes them.
_REASON = re.compile(r"^(?P<head>[^:]+): (?P<label>.*) \((?P<weight>[+-]?\d+(?:\.\d+)?)\)$")
_CURSOR = re.compile(r"^(?P<started>\d+(?:\.\d+)?(?:e[+-]?\d+)?):(?P<id>[A-Za-z0-9_.-]{1,96})$")
_DROPPED = (str(EventState.DROPPED), str(EventState.DROPPING))


def _strict_int(value: Any) -> int:
    """Accept an int, not a bool (which Python counts as one)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise vol.Invalid("expected an integer")
    return value


# ---------------------------------------------------------------------- shapes


@dataclass(slots=True, frozen=True)
class EventQuery:
    """What the panel asked for."""

    unusual: bool = False
    cls: str | None = None
    cameras: frozenset[str] | None = None
    include_dropped: bool = False
    limit: int = DEFAULT_LIMIT
    before: tuple[float, str] | None = None

    def matches(self, record: EventRecord) -> bool:
        """Whether one event belongs on the page."""
        if not self.include_dropped and record.state is EventState.DROPPED:
            return False
        if self.unusual and not record.unusual:
            return False
        if self.cameras is not None and record.camera not in self.cameras:
            return False
        return self.cls is None or self.cls in classes(record)


def classes(record: EventRecord) -> list[str]:
    """Return the classes seen in an event; `motion` when nothing was classified."""
    return sorted({subject.cls for subject in record.facts.subjects.values()}) or ["motion"]


def encode_cursor(started: float, event_id: str) -> str:
    """Return the cursor that continues after an event."""
    return f"{started!r}:{event_id}"


def decode_cursor(value: str) -> tuple[float, str] | None:
    """Read a cursor back, or None when it is malformed."""
    match = _CURSOR.match(value)
    if match is None:
        return None
    return float(match["started"]), match["id"]


def parse_reason(text: str) -> dict[str, Any]:
    """Split one relevance reason into its term, subject, label and weight."""
    match = _REASON.match(text)
    if match is None:
        return {"term": None, "subject": None, "label": text, "weight": None}
    term, _, subject = match["head"].partition(" ")
    return {
        "term": term,
        "subject": subject or None,
        "label": match["label"],
        "weight": float(match["weight"]),
    }


def has_recorder(capabilities: dict[str, Any]) -> bool:
    """Whether the plugin can play this camera's recordings in place (as `playback.py`)."""
    value = capabilities.get("video_recorder", capabilities.get("videoRecorder"))
    if isinstance(value, dict):
        return bool(value.get("available", True))
    return bool(value)


# ---------------------------------------------------------------------- lookups


def _entry(hass: HomeAssistant) -> ConfigEntry | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0] if entries else None


def _camera_name(data: StaminaData, camera: str, fallback: str | None = None) -> str:
    status = data.manager.cameras.get(camera)
    if status is not None:
        return status.device.name
    return fallback or f"Camera {camera}"


def _camera_area(data: StaminaData, camera: str) -> str | None:
    status = data.manager.cameras.get(camera)
    return status.device.area if status is not None else None


def record_row(hass: HomeAssistant, entry_id: str, data: StaminaData, record: EventRecord) -> dict:
    """Return what the list shows for one event, from a full record."""
    return {
        "id": record.id,
        "camera": record.camera,
        "camera_name": _camera_name(
            data, record.camera, record.camera_name or record.facts.camera_name
        ),
        "area": _camera_area(data, record.camera),
        "started": record.facts.started,
        "ended": record.facts.ended,
        "duration": record.facts.duration,
        "classes": classes(record),
        "unusual": record.unusual,
        "score": record.score,
        "state": str(record.state),
        "reason": record.reason,
        "late": len(record.late),
        "thumbnail": thumbnail_url(hass, entry_id, record.id),
    }


async def _sql_rows(
    hass: HomeAssistant, entry_id: str, data: StaminaData, query: EventQuery
) -> tuple[list[dict], str | None]:
    """One page through `EventIndex.async_page`: every filter in SQL."""
    rows, following = await data.index.async_page(
        unusual=True if query.unusual else None,
        include_dropped=query.include_dropped,
        cameras=None if query.cameras is None else sorted(query.cameras),
        cls=query.cls,
        before=query.before,
        limit=query.limit,
    )
    out = []
    for row in rows:
        live = data.manager.records.get(row.id)
        if live is not None:
            out.append(record_row(hass, entry_id, data, live))
            continue
        reason = ""
        if row.unusual:
            # Only unusual rows show their sentence; there are few of them.
            full = await data.index.async_get(row.id)
            reason = full.reason if full is not None else ""
        out.append(
            {
                "id": row.id,
                "camera": row.camera,
                "camera_name": _camera_name(data, row.camera),
                "area": _camera_area(data, row.camera),
                "started": row.started,
                "ended": None if row.duration is None else row.started + row.duration,
                "duration": row.duration,
                "classes": row.classes or ["motion"],
                "unusual": row.unusual,
                "score": row.score,
                "state": row.state,
                "reason": reason,
                "late": 0,
                "thumbnail": thumbnail_url(hass, entry_id, row.id) if row.thumbnail else None,
            }
        )
    return out, None if following is None else encode_cursor(*following)


async def _batch(
    data: StaminaData, camera: str | None, before: tuple[float, str] | None
) -> tuple[list[EventRecord], bool]:
    """Fallback: the next rows after a cursor, newest first, and whether the history ended.

    Ordered by `(started, id)`, both descending, so events that started in the same
    millisecond on two cameras are neither skipped nor repeated across pages.
    """
    index = data.index
    rows: list[EventRecord] = []
    until = None
    if before is not None:
        started, last_id = before
        ties = await index.async_events(
            camera=camera, since=started, until=math.nextafter(started, math.inf)
        )
        rows = sorted((r for r in ties if r.id < last_id), key=lambda r: r.id, reverse=True)
        until = started
    rest = await index.async_events(camera=camera, until=until, limit=BATCH, newest_first=True)
    ended = len(rest) < BATCH
    rest.sort(key=lambda r: (r.facts.started, r.id), reverse=True)
    if not ended:
        # The limit may have cut through events sharing the oldest start: leave them all for
        # the next batch, which reads them whole.
        oldest = rest[-1].facts.started
        rest = [r for r in rest if r.facts.started != oldest] or rest
    return rows + rest, ended


async def scan_page(data: StaminaData, query: EventQuery) -> tuple[list[EventRecord], str | None]:
    """Fallback page for an index without `async_page`: filtered here, bounded per call."""
    if query.cameras is not None and not query.cameras:
        return [], None
    single = next(iter(query.cameras)) if query.cameras and len(query.cameras) == 1 else None
    found: list[EventRecord] = []
    before = query.before
    scanned = 0
    while True:
        rows, ended = await _batch(data, single, before)
        for row in rows:
            record = data.manager.records.get(row.id, row)
            scanned += 1
            before = (row.facts.started, row.id)
            if query.matches(record):
                found.append(record)
                if len(found) >= query.limit:
                    return found, encode_cursor(*before)
        if ended or not rows:
            return found, None
        if scanned >= SCAN_CAP:
            return found, encode_cursor(*before)


async def async_rows(
    hass: HomeAssistant, entry_id: str, data: StaminaData, query: EventQuery
) -> tuple[list[dict], str | None]:
    """Return one page of list rows and the next cursor (None at the end)."""
    if query.cameras is not None and not query.cameras:
        return [], None
    if hasattr(data.index, "async_page"):
        return await _sql_rows(hass, entry_id, data, query)
    records, cursor = await scan_page(data, query)
    return [record_row(hass, entry_id, data, record) for record in records], cursor


def _verifiers(capabilities: dict[str, Any]) -> set[str]:
    return {str(name) for name in capabilities.get("verifiers") or ()} | {
        str(item.get("source"))
        for item in capabilities.get("sources") or ()
        if isinstance(item, dict) and item.get("role") == "verifier"
    }


async def event_detail(
    hass: HomeAssistant, entry: ConfigEntry, data: StaminaData, record: EventRecord
) -> dict:
    """Return everything the event screen shows, with signed playback URLs."""
    manager = data.manager
    status = manager.cameras.get(record.camera)
    capabilities = status.info.capabilities if status is not None else {}
    verifiers = _verifiers(capabilities)
    copies = await data.index.async_copies(record.id)
    cloud = any(copy.playable for copy in copies.values())
    age_days = (time.time() - record.facts.started) / 86400
    recorder = has_recorder(capabilities) and age_days <= manager.recorder_retention_days
    linked_kinds = getattr(manager, "linked_kinds", None)
    watching = sorted(set((linked_kinds(record.camera) if linked_kinds else {}).values()))
    subentries = entry.subentries
    row = record_row(hass, entry.entry_id, data, record)
    if not await data.index.async_has_thumbnail(record.id):
        # Thumbnails age out of the index: the panel shows its placeholder instead.
        row["thumbnail"] = None
    return {
        **row,
        "subjects": [
            {
                "class": subject.cls,
                "label": subject.label,
                "verdict": subject.verdict,
                "verified_by": [s for s in subject.sources if s in verifiers],
                "seen_by": [s for s in subject.sources if s not in verifiers],
                "score": round(subject.score, 3),
                "dwell": round(subject.dwell, 1),
            }
            for subject in record.facts.subjects.values()
        ],
        "reasons": [parse_reason(text) for text in record.reasons],
        "scores": {
            "live": record.live_score,
            "provisional": record.provisional_score,
            "final": record.final_score,
        },
        "finalized": record.finalized,
        "follow_up_until": record.follow_up_until,
        "watching": watching,
        # One item per entity: the change nearest the event, and `count`, how many it made.
        "late_evidence": [_late(hass, item) for item in collapse(record.late)],
        # The same shape, from the look-back window: `t_rel_s` from the start, negative.
        "early_evidence": [_late(hass, item) for item in collapse(record.early)],
        "learning": manager.analysis.state(record.camera) == "learning",
        "keep_reason": record.keep_reason,
        "target": record.target,
        "cloud_path": manager.cloud_path(record),
        "copies": [
            {
                "destination": (
                    subentries[sid].title if sid in subentries else "Removed destination"
                ),
                "committed": copy.committed,
                "deleted": copy.deleted,
                "files": len(copy.files),
            }
            for sid, copy in copies.items()
        ],
        "notes": list(record.notes),
        "gaps": len(record.gaps),
        "play": {
            "url": play_url(hass, entry.entry_id, record.id) if recorder or cloud else None,
            # Each source on its own, for the panel to fall back from one to the other:
            # `url` (auto) plays the cloud copy first by default, so it is not the recording.
            "recorder": play_url(hass, entry.entry_id, record.id, "recorder") if recorder else None,
            "cloud": play_url(hass, entry.entry_id, record.id, "cloud") if cloud else None,
            "order": playback_order(entry.options),
        },
    }


def _late(hass: HomeAssistant, item: dict[str, Any]) -> dict[str, Any]:
    entity_id = str(item.get("entity", ""))
    state = hass.states.get(entity_id)
    return {
        "entity": entity_id,
        "name": state.name if state is not None else entity_id,
        "kind": item.get("kind"),
        "from": item.get("from"),
        "to": item.get("to"),
        "t_rel_s": item.get("t_rel_s"),
        "count": item.get("count", 1),
    }


def _context_feature(capabilities: dict[str, Any], last: EventRecord | None, name: str) -> Any:
    """Whether the camera has a night-mode or tamper sensor; None when nobody has said.

    The plugin lists them as `context_sensors`; failing that, an event's signals name the
    features the camera reported.
    """
    if name in capabilities:
        return bool(capabilities[name])
    sensors = capabilities.get("context_sensors")
    if isinstance(sensors, list):
        return any(isinstance(s, dict) and s.get("feature") == name for s in sensors)
    if last is not None and name in last.facts.signals:
        return True
    return None


def camera_rows(data: StaminaData) -> list[dict[str, Any]]:
    """Return each camera's status, capabilities and learning progress."""
    manager = data.manager
    rows = []
    for camera, status in manager.cameras.items():
        caps = status.info.capabilities or {}
        prebuffer_ms = caps.get("prebuffer_ms")
        coverage = manager.analysis.coverage(camera)
        last = status.last
        rows.append(
            {
                "id": camera,
                "name": status.device.name,
                "area": status.device.area,
                "online": caps.get("online"),
                "verifier_live": caps.get("verifier_live"),
                "verifier_present": caps.get("verifier_present"),
                "verifiers": sorted(_verifiers(caps)),
                "prebuffer": caps.get("prebuffer"),
                "prebuffer_s": None if prebuffer_ms is None else round(prebuffer_ms / 1000),
                "recorder": has_recorder(caps),
                "audio": caps.get("audio"),
                "night_mode": _context_feature(caps, last, "night_mode"),
                "tamper": _context_feature(caps, last, "tamper"),
                "learning": {
                    "active": manager.analysis.state(camera) == "active",
                    "days": coverage.get("days", 0),
                    "events": coverage.get("events", 0),
                },
                "last_event": None
                if last is None
                else {"id": last.id, "started": last.facts.started, "unusual": last.unusual},
            }
        )
    rows.sort(key=lambda row: ((row["area"] or "\uffff").casefold(), row["name"].casefold()))
    return rows


def _cameras_in_area(data: StaminaData, area: str) -> frozenset[str]:
    return frozenset(
        camera
        for camera, status in data.manager.cameras.items()
        if (status.device.area or "") == area
    )


# ---------------------------------------------------------------------- commands


def _loaded(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg_id: int
) -> ConfigEntry | None:
    entry = _entry(hass)
    if entry is None:
        connection.send_error(msg_id, ERR_NOT_LOADED, "Scrypted Stamina is not loaded")
    return entry


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/events",
        vol.Optional("unusual", default=False): bool,
        vol.Optional("cls"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Exclusive("camera", "where"): vol.All(str, vol.Length(min=1, max=96)),
        vol.Exclusive("area", "where"): vol.All(str, vol.Length(max=128)),
        vol.Optional("include_dropped", default=False): bool,
        vol.Optional("cursor"): vol.Any(None, vol.All(str, vol.Length(max=200))),
        vol.Optional("limit", default=DEFAULT_LIMIT): vol.All(
            _strict_int, vol.Range(min=1, max=MAX_LIMIT)
        ),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_events(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List events, newest first."""
    before = None
    if msg.get("cursor"):
        before = decode_cursor(msg["cursor"])
        if before is None:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Bad cursor")
            return
    entry = _loaded(hass, connection, msg["id"])
    if entry is None:
        return
    data: StaminaData = entry.runtime_data
    cameras: frozenset[str] | None = None
    if "camera" in msg:
        cameras = frozenset({msg["camera"]})
    elif "area" in msg:
        cameras = _cameras_in_area(data, msg["area"])
    query = EventQuery(
        unusual=msg["unusual"],
        cls=msg.get("cls"),
        cameras=cameras,
        include_dropped=msg["include_dropped"],
        limit=msg["limit"],
        before=before,
    )
    rows, cursor = await async_rows(hass, entry.entry_id, data, query)
    connection.send_result(
        msg["id"],
        {"events": rows, "cursor": cursor, "link_healthy": data.manager.link_healthy},
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/event",
        vol.Required("event_id"): vol.All(str, vol.Length(min=1, max=96)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_event(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return one event in full, with signed playback URLs."""
    entry = _loaded(hass, connection, msg["id"])
    if entry is None:
        return
    data: StaminaData = entry.runtime_data
    record = data.manager.records.get(msg["event_id"]) or await data.index.async_get(
        msg["event_id"]
    )
    if record is None:
        connection.send_error(msg["id"], ERR_NOT_FOUND, "No such event")
        return
    connection.send_result(msg["id"], {"event": await event_detail(hass, entry, data, record)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/cameras"})
@websocket_api.require_admin
@callback
def ws_cameras(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return each camera's status, capabilities and learning progress."""
    entry = _loaded(hass, connection, msg["id"])
    if entry is None:
        return
    data: StaminaData = entry.runtime_data
    connection.send_result(
        msg["id"],
        {
            "cameras": camera_rows(data),
            # Which source plays first: `cloud_first` (the default) or `recording_first`.
            "playback_order": playback_order(entry.options),
            "link_healthy": data.manager.link_healthy,
            "learning_needs": {"days": SCORE_MIN_DAYS, "events": SCORE_MIN_EVENTS},
        },
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@websocket_api.require_admin
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send `{"changed": true}` when events or cameras change, at most once a second.

    The manager fires `signal_update` (already coalesced per second), and `signal_event`
    and `signal_cameras` as they happen; a burst of any of them becomes one message.
    """
    entry = _loaded(hass, connection, msg["id"])
    if entry is None:
        return
    msg_id = msg["id"]
    pending: CALLBACK_TYPE | None = None

    @callback
    def _send(_now: Any) -> None:
        nonlocal pending
        pending = None
        connection.send_message(websocket_api.event_message(msg_id, {"changed": True}))

    @callback
    def _forward(*_args: Any) -> None:
        nonlocal pending
        if pending is None:
            pending = async_call_later(hass, NUDGE_SECONDS, _send)

    unsubs: list[Callable[[], None]] = [
        async_dispatcher_connect(hass, signal(entry.entry_id), _forward)
        for signal in (signal_update, signal_event, signal_cameras)
    ]

    @callback
    def _unsubscribe() -> None:
        if pending is not None:
            pending()
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg_id] = _unsubscribe
    connection.send_result(msg_id)


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    """Register the panel's commands once per Home Assistant run."""
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    for command in (ws_events, ws_event, ws_cameras, ws_subscribe):
        websocket_api.async_register_command(hass, command)
