"""The review panel: its websocket commands and its registration.

The commands run against a real index (SQLite in a temporary directory) and a fake
manager holding only what the commands read: cameras, live records, the relevance
analysis, the cloud path and the link's health.

The list runs twice: through `EventIndex.async_page` (filters in SQL) and through the
fallback scan for an index without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.scrypted_stamina import panel, websocket_api as ws
from custom_components.scrypted_stamina.const import (
    DOMAIN,
    signal_cameras,
    signal_event,
    signal_update,
)
from custom_components.scrypted_stamina.decider import EventRecord, EventState
from custom_components.scrypted_stamina.index import Copy, EventIndex
from custom_components.scrypted_stamina.manager import CameraStatus
from custom_components.scrypted_stamina.protocol import CameraInfo, EventFacts, Subject

from .conftest import event_id

T0 = 1_760_000_000.0
VERIFIER = "scrypted-coral-guard"
TRIGGER = "@example/scrypted-camera"


@dataclass
class FakeAnalysis:
    active: set[str] = field(default_factory=set)

    def state(self, camera: str) -> str:
        return "active" if camera in self.active else "learning"

    def coverage(self, camera: str) -> dict[str, float | int]:
        if camera in self.active:
            return {"days": 9.0, "events": 420}
        return {"days": 2.5, "events": 31}


@dataclass
class FakeManager:
    cameras: dict[str, CameraStatus]
    records: dict[str, EventRecord] = field(default_factory=dict)
    analysis: FakeAnalysis = field(default_factory=FakeAnalysis)
    recorder_retention_days: float = 7.0
    link_healthy: bool = True

    def cloud_path(self, record: EventRecord) -> str | None:
        if record.state is EventState.DROPPED:
            return None
        return f"events/{record.area}/{record.id}"

    def linked_kinds(self, camera: str) -> dict[str, str]:
        return {"binary_sensor.side_gate": "door", "binary_sensor.hall": "presence"}


class ScanOnlyIndex:
    """An index from before `async_page`: everything but that method."""

    def __init__(self, index: EventIndex) -> None:
        self._index = index

    def __getattr__(self, name: str) -> Any:
        if name == "async_page":
            raise AttributeError(name)
        return getattr(self._index, name)


def camera(cam: str, name: str, area: str | None, **caps: Any) -> CameraStatus:
    return CameraStatus(
        info=CameraInfo(id=cam, name=name, capabilities=caps),
        device=SimpleNamespace(name=name, area=area),  # type: ignore[arg-type]
    )


def record(
    n: int,
    cam: str = "11",
    *,
    started: float | None = None,
    cls: tuple[str, ...] = ("person",),
    state: EventState = EventState.COMMITTED,
    unusual: bool = False,
    **extra: Any,
) -> EventRecord:
    start = T0 + n * 60 if started is None else started
    facts = EventFacts(
        id=event_id(n),
        camera=cam,
        started=start,
        ended=start + 20,
        subjects={
            f"s{i}": Subject(
                id=f"s{i}",
                cls=c,
                verdict="confirmed",
                sources=(TRIGGER, VERIFIER),
                score=0.8,
                first_seen=start,
                last_seen=start + 6,
            )
            for i, c in enumerate(cls)
        },
    )
    rec = EventRecord(facts=facts, state=state, area="garden", finalized=True, **extra)
    rec.unusual_final = unusual
    rec.final_score = 0.9 if unusual else 0.2
    return rec


@pytest.fixture(params=["sql", "scan"])
async def stamina_entry(hass: HomeAssistant, tmp_path: Path, request: pytest.FixtureRequest):
    """A loaded entry whose runtime data is a real index and a fake manager."""
    index = EventIndex(hass, tmp_path / "index.db")
    await index.async_open()
    manager = FakeManager(
        cameras={
            "11": camera(
                "11",
                "Front door",
                "Entrance",
                online=True,
                verifier_live=True,
                verifier_present=True,
                verifiers=[VERIFIER],
                prebuffer=True,
                prebuffer_ms=10000,
                video_recorder=True,
                audio=True,
            ),
            "12": camera("12", "Driveway", "Outside", online=False, video_recorder=False),
            "13": camera("13", "Garden", "Outside"),
        },
        analysis=FakeAnalysis(active={"11"}),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Scrypted Stamina",
        state=ConfigEntryState.LOADED,
        subentries_data=[
            ConfigSubentryData(
                data={},
                subentry_type="destination",
                subentry_id="dest1",
                title="WebDAV: NAS",
                unique_id="dest1",
            )
        ],
    )
    entry.add_to_hass(hass)
    served = index if request.param == "sql" else ScanOnlyIndex(index)
    entry.runtime_data = SimpleNamespace(index=served, manager=manager)
    ws.async_register_commands(hass)
    yield SimpleNamespace(entry=entry, index=index, manager=manager, mode=request.param)
    await index.async_close()


async def call(client, **msg: Any) -> dict:
    await client.send_json_auto_id(msg)
    reply = await client.receive_json()
    assert reply["success"], reply
    return reply["result"]


async def failure(client, **msg: Any) -> dict:
    await client.send_json_auto_id(msg)
    reply = await client.receive_json()
    assert reply["success"] is False, reply
    return reply["error"]


async def ids(client, **msg: Any) -> list[str]:
    result = await call(client, type=f"{DOMAIN}/events", **msg)
    return [row["id"] for row in result["events"]]


async def all_ids(client, **msg: Any) -> list[str]:
    """Follow the cursor to the end."""
    seen: list[str] = []
    cursor = None
    while True:
        page = {**msg, "cursor": cursor} if cursor else msg
        result = await call(client, type=f"{DOMAIN}/events", **page)
        seen += [row["id"] for row in result["events"]]
        cursor = result["cursor"]
        if cursor is None:
            return seen


# ---------------------------------------------------------------------- events


async def test_events_newest_first_without_dropped(hass, hass_ws_client, stamina_entry) -> None:
    index = stamina_entry.index
    await index.async_save(record(1))
    await index.async_save(record(2, state=EventState.DROPPED))
    await index.async_save(record(3, unusual=True, reason="Person at 03:12."))
    await index.async_save_thumbnail(event_id(3), b"jpeg")
    client = await hass_ws_client(hass)

    result = await call(client, type=f"{DOMAIN}/events")
    assert [row["id"] for row in result["events"]] == [event_id(3), event_id(1)]
    assert result["cursor"] is None and result["link_healthy"] is True
    row = result["events"][0]
    assert row["camera_name"] == "Front door" and row["area"] == "Entrance"
    assert row["unusual"] is True and row["classes"] == ["person"]
    assert row["reason"] == "Person at 03:12."
    assert row["ended"] == row["started"] + 20
    assert row["thumbnail"].startswith(f"/api/{DOMAIN}/thumbnail/") and "sig=" in row["thumbnail"]
    if stamina_entry.mode == "sql":
        # The index knows which events have a thumbnail: no request for one that has none.
        assert result["events"][1]["thumbnail"] is None

    assert await ids(client, include_dropped=True) == [event_id(n) for n in (3, 2, 1)]


async def test_events_filters(hass, hass_ws_client, stamina_entry) -> None:
    index = stamina_entry.index
    await index.async_save(record(1, "11", cls=("person",)))
    await index.async_save(record(2, "12", cls=("vehicle",), unusual=True))
    await index.async_save(record(3, "13", cls=()))
    await index.async_save(record(4, "11", cls=("animal", "person"), unusual=True))
    client = await hass_ws_client(hass)

    assert await ids(client, unusual=True) == [event_id(4), event_id(2)]
    assert await ids(client, cls="person") == [event_id(4), event_id(1)]
    assert await ids(client, cls="motion") == [event_id(3)]
    assert await ids(client, camera="12") == [event_id(2)]
    assert await ids(client, area="Outside") == [event_id(3), event_id(2)]
    assert await ids(client, area="Nowhere") == []
    assert await ids(client, area="Outside", unusual=True) == [event_id(2)]


async def test_events_prefer_the_live_record(hass, hass_ws_client, stamina_entry) -> None:
    saved = record(1, unusual=True)
    await stamina_entry.index.async_save(saved)
    live = record(1, unusual=True, reason="Still open.")
    live.facts.ended = None
    stamina_entry.manager.records[live.id] = live
    client = await hass_ws_client(hass)
    row = (await call(client, type=f"{DOMAIN}/events"))["events"][0]
    assert row["ended"] is None and row["reason"] == "Still open."


async def test_events_page_through_ties_without_gaps(hass, hass_ws_client, stamina_entry) -> None:
    index = stamina_entry.index
    # Three events in the same millisecond on different cameras, among others.
    for n in range(1, 8):
        started = T0 + 500 if n in (3, 4, 5) else None
        await index.async_save(record(n, ("11", "12", "13")[n % 3], started=started))
    client = await hass_ws_client(hass)
    expected = await ids(client, limit=200)
    assert len(expected) == 7
    with patch.object(ws, "BATCH", 2):
        assert await all_ids(client, limit=2) == expected


async def test_fallback_scan_is_bounded(hass, hass_ws_client, stamina_entry) -> None:
    if stamina_entry.mode != "scan":
        pytest.skip("the SQL page needs no scan bound")
    index = stamina_entry.index
    for n in range(1, 31):
        await index.async_save(record(n, unusual=n == 2))
    client = await hass_ws_client(hass)
    with patch.object(ws, "BATCH", 5), patch.object(ws, "SCAN_CAP", 10):
        first = await call(client, type=f"{DOMAIN}/events", unusual=True)
        assert first["events"] == [] and first["cursor"]
        assert await all_ids(client, unusual=True) == [event_id(2)]


async def test_events_reject_bad_input(hass, hass_ws_client, stamina_entry) -> None:
    client = await hass_ws_client(hass)
    for bad in ({"limit": 5000}, {"limit": True}, {"camera": "11", "area": "x"}):
        error = await failure(client, type=f"{DOMAIN}/events", **bad)
        assert error["code"] == "invalid_format"
    for cursor in ("garbage", "1:", ":ev", "1:../x"):
        error = await failure(client, type=f"{DOMAIN}/events", cursor=cursor)
        assert error["code"] == "invalid_format"


async def test_commands_are_admin_only(
    hass, hass_ws_client, hass_read_only_access_token, stamina_entry
) -> None:
    client = await hass_ws_client(hass, hass_read_only_access_token)
    for command in ("events", "cameras", "subscribe"):
        error = await failure(client, type=f"{DOMAIN}/{command}")
        assert error["code"] == "unauthorized"
    error = await failure(client, type=f"{DOMAIN}/event", event_id=event_id(1))
    assert error["code"] == "unauthorized"


# ---------------------------------------------------------------------- one event


async def test_event_detail(hass, hass_ws_client, stamina_entry) -> None:
    hass.states.async_set("binary_sensor.side_gate", "on", {"friendly_name": "Side gate"})
    now = time.time()
    rec = record(
        1,
        started=now - 120,
        unusual=True,
        reasons=["clock: 03:12 (+1.84)", "predecessor: 90s after Driveway (+1.12)", "oddly put"],
        reason="Person at 03:12 on Front door: first time in 2 weeks.",
        keep_reason="unusual",
        late=[
            {
                "entity": "binary_sensor.side_gate",
                "kind": "door",
                "from": "off",
                "to": "on",
                "t_rel_s": 40,
            }
        ],
        early=[
            {
                "entity": "binary_sensor.side_gate",
                "kind": "door",
                "from": "off",
                "to": "on",
                "t_rel_s": -20,
            }
        ],
        notes=["camera_offline"],
    )
    await stamina_entry.index.async_save(rec)
    await stamina_entry.index.async_save_copy(
        rec.id,
        "dest1",
        Copy(
            folder="pending/garden/x",
            files={"init.mp4": 9},
            committed=True,
            video="events/v.mp4",
        ),
    )
    client = await hass_ws_client(hass)
    event = (await call(client, type=f"{DOMAIN}/event", event_id=rec.id))["event"]

    assert event["reasons"][0] == {
        "term": "clock",
        "subject": None,
        "label": "03:12",
        "weight": 1.84,
    }
    assert event["reasons"][1]["term"] == "predecessor"
    assert event["reasons"][1]["label"] == "90s after Driveway"
    assert event["reasons"][2] == {
        "term": None,
        "subject": None,
        "label": "oddly put",
        "weight": None,
    }
    assert event["late_evidence"][0]["name"] == "Side gate"
    # The gate opened 20 s before the start, linked to the event as well.
    assert event["early_evidence"] == [
        {
            "entity": "binary_sensor.side_gate",
            "name": "Side gate",
            "kind": "door",
            "from": "off",
            "to": "on",
            "t_rel_s": -20,
        }
    ]
    subject = event["subjects"][0]
    assert subject["verified_by"] == [VERIFIER] and subject["seen_by"] == [TRIGGER]
    assert subject["dwell"] == 6.0
    assert event["watching"] == ["door", "presence"]
    assert event["cloud_path"] == f"events/garden/{rec.id}"
    assert event["copies"] == [
        {"destination": "WebDAV: NAS", "committed": True, "deleted": False, "files": 1}
    ]
    assert event["learning"] is False and event["notes"] == ["camera_offline"]
    play = event["play"]
    assert play["recorder"] is True
    assert "mode=auto" in play["url"] and "sig=" in play["url"]
    assert "mode=cloud" in play["cloud"]


async def test_event_without_any_video(hass, hass_ws_client, stamina_entry) -> None:
    # A camera with no recorder, and an event that was not kept: nothing can play it.
    rec = record(1, "12", state=EventState.DROPPED)
    await stamina_entry.index.async_save(rec)
    client = await hass_ws_client(hass)
    event = (await call(client, type=f"{DOMAIN}/event", event_id=rec.id))["event"]
    assert event["play"] == {"url": None, "recorder": False, "cloud": None}
    assert event["cloud_path"] is None and event["learning"] is True
    assert event["subjects"][0]["verified_by"] == []


async def test_event_too_old_for_the_recorder(hass, hass_ws_client, stamina_entry) -> None:
    rec = record(1, "11", started=time.time() - 30 * 86400)
    await stamina_entry.index.async_save(rec)
    client = await hass_ws_client(hass)
    event = (await call(client, type=f"{DOMAIN}/event", event_id=rec.id))["event"]
    assert event["play"]["recorder"] is False and event["play"]["url"] is None


async def test_event_not_found(hass, hass_ws_client, stamina_entry) -> None:
    client = await hass_ws_client(hass)
    error = await failure(client, type=f"{DOMAIN}/event", event_id=event_id(99))
    assert error["code"] == ws.ERR_NOT_FOUND


# ---------------------------------------------------------------------- cameras


async def test_cameras(hass, hass_ws_client, stamina_entry) -> None:
    last = record(5, "11", unusual=True)
    last.facts.signals["night_mode"] = False
    stamina_entry.manager.cameras["11"].last = last
    client = await hass_ws_client(hass)
    result = await call(client, type=f"{DOMAIN}/cameras")

    assert [cam["name"] for cam in result["cameras"]] == ["Front door", "Driveway", "Garden"]
    front = result["cameras"][0]
    assert front["online"] is True and front["verifier_live"] is True
    assert front["verifiers"] == [VERIFIER]
    assert front["prebuffer"] is True and front["prebuffer_s"] == 10
    assert front["recorder"] is True and front["audio"] is True
    assert front["night_mode"] is True and front["tamper"] is None
    assert front["learning"] == {"active": True, "days": 9.0, "events": 420}
    assert front["last_event"] == {"id": last.id, "started": last.facts.started, "unusual": True}
    driveway = result["cameras"][1]
    assert driveway["online"] is False and driveway["recorder"] is False
    assert driveway["learning"]["active"] is False and driveway["last_event"] is None
    assert result["learning_needs"] == {"days": 7.0, "events": 200}


async def test_cameras_come_grouped_by_area(hass, hass_ws_client, stamina_entry) -> None:
    cameras = stamina_entry.manager.cameras
    cameras["14"] = camera("14", "Side path", None)
    cameras["15"] = camera("15", "Back yard", "Outside")
    cameras["16"] = camera("16", "Attic", "Attic")
    client = await hass_ws_client(hass)
    result = await call(client, type=f"{DOMAIN}/cameras")
    # Areas by name, cameras by name inside each, no area last; every row says its area.
    assert [(cam["area"], cam["name"]) for cam in result["cameras"]] == [
        ("Attic", "Attic"),
        ("Entrance", "Front door"),
        ("Outside", "Back yard"),
        ("Outside", "Driveway"),
        ("Outside", "Garden"),
        (None, "Side path"),
    ]
    source = (panel.FRONTEND_DIR / "scrypted-stamina-panel.js").read_text()
    assert "areaGroups" in source and '"No area"' in source


# ---------------------------------------------------------------------- subscribe


async def test_subscribe_coalesces_changes(hass, hass_ws_client, stamina_entry) -> None:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": f"{DOMAIN}/subscribe"})
    assert (await client.receive_json())["success"]
    entry_id = stamina_entry.entry.entry_id
    # A burst of every kind of signal becomes one message.
    async_dispatcher_send(hass, signal_update(entry_id))
    async_dispatcher_send(hass, signal_event(entry_id), "11", "normal", {})
    async_dispatcher_send(hass, signal_cameras(entry_id))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=ws.NUDGE_SECONDS + 1))
    await hass.async_block_till_done()
    message = await client.receive_json()
    assert message["type"] == "event" and message["event"] == {"changed": True}

    # Nothing else was queued: the next message is the reply to a new command.
    await client.send_json_auto_id({"type": f"{DOMAIN}/cameras"})
    assert (await client.receive_json())["type"] == "result"


async def test_commands_when_not_loaded(hass, hass_ws_client) -> None:
    ws.async_register_commands(hass)
    client = await hass_ws_client(hass)
    for command in ("events", "cameras", "subscribe"):
        error = await failure(client, type=f"{DOMAIN}/{command}")
        assert error["code"] == ws.ERR_NOT_LOADED


# ---------------------------------------------------------------------- helpers


def test_cursor_round_trip() -> None:
    assert ws.decode_cursor(ws.encode_cursor(T0 + 0.123, event_id(1))) == (T0 + 0.123, event_id(1))
    for bad in ("", "abc", "1:", ":ev", "1:../x"):
        assert ws.decode_cursor(bad) is None


# ---------------------------------------------------------------------- panel


async def test_panel_registers_and_reregisters(hass) -> None:
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN)
    await panel.async_register_panel(hass, entry)
    panels = hass.data["frontend_panels"]
    registered = panels[panel.PANEL_URL_PATH]
    assert registered.require_admin is True
    custom = registered.config["_panel_custom"]
    assert custom["name"] == panel.PANEL_COMPONENT
    assert custom["module_url"].startswith(f"{panel.STATIC_PREFIX}/")
    assert custom["module_url"].endswith("/scrypted-stamina-panel.js")
    assert registered.config["entry_id"] == entry.entry_id

    # A reload registers it again without complaint.
    await panel.async_register_panel(hass, entry)
    panel.async_unregister_panel(hass)
    assert panel.PANEL_URL_PATH not in panels
    panel.async_unregister_panel(hass)


def test_the_panel_is_one_page() -> None:
    """No view to navigate to: the event opens beside the list, or under its row."""
    source = (panel.FRONTEND_DIR / "scrypted-stamina-panel.js").read_text()
    assert "pushState" not in source  # the URL is only ever replaced: Back leaves the panel
    assert '"/cameras"' not in source and "_back(" not in source
    for needed in ("WIDE_PX", "_preselect", "ArrowDown", "cam-chip", "aria-expanded"):
        assert needed in source, needed


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_frontend_parses() -> None:
    for path in panel.FRONTEND_DIR.glob("*.js"):
        result = subprocess.run(
            ["node", "--check", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, (path.name, result.stderr)


def test_frontend_is_shipped_without_the_preview() -> None:
    names = {path.name for path in panel.FRONTEND_DIR.iterdir()}
    assert {"scrypted-stamina-panel.js", "dom.js", "format.js", "player.js", "styles.js"} <= names
    assert not any(name.endswith(".html") for name in names)
    assert len(panel.frontend_fingerprint()) == 12


async def test_a_slim_row_without_thumbnail_or_footage(hass, hass_ws_client, stamina_entry) -> None:
    from homeassistant.components.media_source import MediaSourceItem

    from custom_components.scrypted_stamina.media_source import StaminaMediaSource

    index = stamina_entry.index
    now = time.time()
    # Older than the fresh tier and the recorder; one with a committed cloud copy.
    gone = record(1, "11", started=now - 40 * 86400, unusual=True, reason="Person at 03:12.")
    kept = record(2, "11", started=now - 40 * 86400 + 60)
    for rec in (gone, kept):
        await index.async_save(rec, features='{"kind": "person", "context": []}')
    await index.async_save_copy(
        kept.id,
        "dest1",
        Copy(
            folder="pending/x",
            files={"init.mp4": 9},
            committed=True,
            video="events/v.mp4",
        ),
    )
    pruned = await index.async_prune(
        history_before=0,
        fresh_before=now - 30 * 86400,
        thumbnails_before=now - 30 * 86400,
        dropped_thumbnails_before=now - 7 * 86400,
    )
    assert pruned.slimmed == 2
    client = await hass_ws_client(hass)

    event = (await call(client, type=f"{DOMAIN}/event", event_id=gone.id))["event"]
    assert event["thumbnail"] is None and event["play"]["url"] is None
    assert event["reason"] == "Person at 03:12." and event["classes"] == ["person"]
    assert event["late_evidence"] == [] and event["subjects"][0]["class"] == "person"
    event = (await call(client, type=f"{DOMAIN}/event", event_id=kept.id))["event"]
    assert event["play"]["cloud"] is not None and event["play"]["recorder"] is False

    source = StaminaMediaSource(hass)
    day = dt_util.as_local(dt_util.utc_from_timestamp(gone.facts.started)).date().isoformat()
    events = await source.async_browse_media(MediaSourceItem(hass, DOMAIN, f"11/{day}", None))
    items = {item.identifier.rsplit("/", 1)[1]: item for item in events.children}
    assert items[gone.id].can_play is False and items[gone.id].thumbnail is None
    assert items[kept.id].can_play is True
    sources = await source.async_browse_media(
        MediaSourceItem(hass, DOMAIN, f"11/{day}/{kept.id}", None)
    )
    assert [child.title for child in sources.children] == ["Cloud copy"]
