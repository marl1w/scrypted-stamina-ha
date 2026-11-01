"""Each area's Uploads select: the three positions, the class filter, and the migrations."""

from __future__ import annotations

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.scrypted_stamina.const import (
    CONF_UPLOAD_CLASSES,
    DOMAIN,
    INDEX_FILENAME,
    LEGACY_UNUSUAL_OVERRIDES,
    NO_AREA,
)
from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.index import EventIndex
from custom_components.scrypted_stamina.relevance import RelevanceResult

from .conftest import Clock, event_id, make_entry, subject, until

# The test camera has no area: it is in the "No area" subentry.
UPLOADS = "select.stamina_no_area_uploads"


def model(unusual: bool):
    def _score(record, *, now, include_late=True) -> RelevanceResult:
        return RelevanceResult(0.9 if unusual else 0.2, 3.0, 1.4, unusual, "rare", [])

    return _score


def run(stamina, eid: str, cls: str, verdict: str = "confirmed") -> None:
    stamina.start(eid, subjects=[subject("s1", cls)])
    for n in range(2):
        stamina.segment(eid, n, init=n == 0)
    stamina.verdict(eid, subject("s1", cls, verdict=verdict))
    stamina.end(eid, subjects=[subject("s1", cls, verdict=verdict)], next_segment=2, init=True)


async def settle(hass, manager, clock: Clock, eid: str):
    """Let the event finish, past its follow-up window, and return its stored record."""
    await until(lambda: eid in manager.records and manager.records[eid].ended)
    await until(lambda: manager.records[eid].state is not EventState.PENDING)
    await clock.advance(hass, 11 * 60)
    await until(lambda: eid not in manager.records)
    return await manager.index.async_get(eid)


def uploaded(destinations, folder: str) -> list[str]:
    return destinations["storage0"].under(f"scrypted-stamina/{folder}/")


# position, unusual, class -> kept, keep reason, folder with footage (None: nothing uploaded)
MATRIX = [
    ("classes", False, "person", True, "classes: person", "events"),
    ("classes", True, "person", True, "classes: person", "events"),
    ("classes", False, "animal", False, None, None),
    ("classes", True, "animal", True, "unusual", "unusual-unconfirmed"),
    ("unusual", False, "person", False, None, None),
    ("unusual", True, "person", True, "unusual", "unusual-unconfirmed"),
    ("unusual", False, "animal", False, None, None),
    ("unusual", True, "animal", True, "unusual", "unusual-unconfirmed"),
    ("off", False, "person", False, None, None),
    ("off", True, "person", True, "unusual", None),
    ("off", False, "animal", False, None, None),
    ("off", True, "animal", True, "unusual", None),
]


@pytest.mark.parametrize(("mode", "unusual", "cls", "kept", "reason", "folder"), MATRIX)
async def test_every_position(
    hass, stamina, destinations, setup, mode, unusual, cls, kept, reason, folder
) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    clock = Clock()
    manager.now = clock
    manager.analysis.score = model(unusual)
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": UPLOADS, "option": mode},
        blocking=True,
    )
    assert manager.uploads_for(NO_AREA) == mode
    eid = event_id(300)
    run(stamina, eid, cls)
    stored = await settle(hass, manager, clock, eid)

    assert stored.state is (EventState.COMMITTED if kept else EventState.DROPPED)
    assert stored.keep_reason == reason
    assert stored.upload_mode == mode
    for where in ("events", "unusual-unconfirmed", "pending"):
        assert bool(uploaded(destinations, where)) == (where == folder), where
    if mode == "off":
        # Nothing ever started uploading, yet the event was indexed and scored.
        assert not destinations["storage0"].files
        assert stored.score is not None and stored.reason == "rare"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_unverified_camera_keeps_its_chosen_class(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    clock = Clock()
    manager.now = clock
    manager.analysis.score = model(False)
    eid = event_id(310)
    run(stamina, eid, "person", verdict="unverified")
    stored = await settle(hass, manager, clock, eid)
    assert stored.state is EventState.COMMITTED and stored.keep_reason == "classes: person"
    assert uploaded(destinations, "events")
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_change_mid_event_applies_to_new_events_only(
    hass, stamina, destinations, setup
) -> None:
    entry = await setup()
    manager = entry.runtime_data.manager
    clock = Clock()
    manager.now = clock
    manager.analysis.score = model(False)

    first = event_id(320)
    stamina.start(first, subjects=[subject("s1")])
    await until(lambda: first in manager.records)
    await manager.async_set_uploads("off", NO_AREA)
    stamina.segment(first, 0, init=True)
    stamina.end(first, subjects=[subject("s1", verdict="confirmed")], next_segment=1, init=True)
    assert (await settle(hass, manager, clock, first)).state is EventState.COMMITTED
    assert uploaded(destinations, "events")

    # The manager's clock is now 11 minutes ahead: the second event is stamped on that clock,
    # or its follow-up window has already closed when it ends and it is gone before `settle`.
    second = event_id(321)
    began = clock()
    stamina.start(second, start=began, subjects=[subject("s1")])
    await until(lambda: second in manager.records)
    await manager.async_set_uploads("classes", NO_AREA)
    stamina.segment(second, 0, init=True)
    stamina.end(
        second,
        start=began,
        end=clock(),
        subjects=[subject("s1", verdict="confirmed")],
        next_segment=1,
        init=True,
    )
    stored = await settle(hass, manager, clock, second)
    assert stored.state is EventState.DROPPED and stored.upload_mode == "off"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_position_survives_a_restart(hass, stamina, destinations, setup, reload) -> None:
    entry = await setup()
    await entry.runtime_data.manager.async_set_uploads("unusual", NO_AREA)
    await reload(entry)
    assert entry.runtime_data.manager.uploads_for(NO_AREA) == "unusual"
    assert hass.states.get(UPLOADS).state == "unusual"
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("enabled", "overrides", "expected"),
    [
        ("1", False, "classes"),
        ("0", True, "unusual"),
        ("0", None, "unusual"),  # the old option's default was on
        ("0", False, "off"),
        (None, None, "classes"),
    ],
)
async def test_the_switch_migrates_to_the_select(
    hass, stamina, destinations, setup, reload, enabled, overrides, expected
) -> None:
    options = {} if overrides is None else {LEGACY_UNUSUAL_OVERRIDES: overrides}
    entry = make_entry(hass, stamina, options=options)
    if enabled is not None:
        index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
        await index.async_open()
        await index.async_set_meta({"uploads_enabled": enabled})
        await index.async_close()
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", DOMAIN, f"{entry.entry_id}_uploads", config_entry=entry)

    await setup(entry=entry)
    assert entry.runtime_data.manager.uploads_for(NO_AREA) == expected
    assert LEGACY_UNUSUAL_OVERRIDES not in entry.options
    assert registry.async_get_entity_id("switch", DOMAIN, f"{entry.entry_id}_uploads") is None
    # Idempotent: a second start keeps what was migrated.
    await reload(entry)
    assert entry.runtime_data.manager.uploads_for(NO_AREA) == expected
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_class_option_defaults_to_people_and_vehicles(hass, stamina, setup) -> None:
    entry = await setup(options={LEGACY_UNUSUAL_OVERRIDES: True})
    assert entry.runtime_data.manager.upload_classes == {"person", "vehicle"}
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = {
        str(key): key.default() for key in result["data_schema"].schema if callable(key.default)
    }
    assert defaults[CONF_UPLOAD_CLASSES] == ["person", "vehicle"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_UPLOAD_CLASSES: ["person", "animal"]}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_UPLOAD_CLASSES] == ["person", "animal"]
    assert LEGACY_UNUSUAL_OVERRIDES not in entry.options
    await hass.async_block_till_done()
    assert entry.runtime_data.manager.upload_classes == {"person", "animal"}
    await hass.config_entries.async_unload(entry.entry_id)
