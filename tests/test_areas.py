"""Area devices: one per area with a camera, under the destination, with Uploads and quota."""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.components.media_source import async_browse_media
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scrypted_stamina.const import (
    DOMAIN,
    INDEX_FILENAME,
    SUBENTRY_TYPE_DESTINATION,
    UPLOADS_CLASSES,
    UPLOADS_OFF,
)
from custom_components.scrypted_stamina.decider import EventState
from custom_components.scrypted_stamina.index import EventIndex
from custom_components.scrypted_stamina.relevance import RelevanceResult

from .conftest import Clock, event_id, make_entry, subject, until

GARDEN = "select.stamina_garden_uploads"
DRIVEWAY = "select.stamina_driveway_uploads"
GARDEN_QUOTA = "sensor.stamina_garden_quota_used"
DRIVEWAY_QUOTA = "sensor.stamina_driveway_quota_used"


def camera_device(hass, mqtt: MockConfigEntry, camera: str, name: str, area_id: str | None):
    """Register a `scrypted_mqtt` camera device, in an area."""
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=mqtt.entry_id,
        identifiers={("scrypted_mqtt", f"scrypted:{camera}")},
        name=name,
    )
    return devices.async_update_device(device.id, area_id=area_id)


def two_areas(hass, stamina):
    """Put the fake plugin's camera in Garden and a second one in Driveway."""
    mqtt = MockConfigEntry(domain="scrypted_mqtt", title="Scrypted MQTT")
    mqtt.add_to_hass(hass)
    areas = ar.async_get(hass)
    garden, driveway = areas.async_create("Garden"), areas.async_create("Driveway")
    stamina.cameras.append({**stamina.cameras[0], "id": "39", "name": "Gate"})
    front = camera_device(hass, mqtt, "38", "Front door", garden.id)
    gate = camera_device(hass, mqtt, "39", "Gate", driveway.id)
    return garden, driveway, front, gate


def destination_id(entry) -> str:
    return entry.get_subentries_of_type(SUBENTRY_TYPE_DESTINATION)[0].subentry_id


def area_device_names(hass, entry) -> set[str]:
    return {
        device.name
        for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        if device.model == "Stamina area"
    }


async def test_one_device_per_area_under_the_destination(hass, stamina, setup) -> None:
    two_areas(hass, stamina)
    entry = await setup()
    # The destination is the only subentry: the areas are its devices, not entries.
    assert [s.subentry_type for s in entry.subentries.values()] == [SUBENTRY_TYPE_DESTINATION]
    assert area_device_names(hass, entry) == {"Garden", "Driveway"}

    devices, entities = dr.async_get(hass), er.async_get(hass)
    hub = devices.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    for entity_id in (GARDEN, GARDEN_QUOTA):
        item = entities.async_get(entity_id)
        assert item is not None, entity_id
        assert item.config_subentry_id == destination_id(entry)
        device = devices.async_get(item.device_id)
        assert device.name == "Garden"
        assert device.config_subentry_id == destination_id(entry)
        assert device.via_device_id == hub.id
    assert hass.states.get(GARDEN).state == UPLOADS_CLASSES
    assert float(hass.states.get(GARDEN_QUOTA).state) == 0
    # Health stays on the destinations: no per-area status.
    assert hass.states.get("sensor.stamina_garden_status") is None

    # Cameras stay where they were: not filed under any subentry.
    camera = devices.async_get(entities.async_get("event.garden_front_door_stamina").device_id)
    assert camera.config_subentry_id is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_each_area_takes_its_own_position(hass, stamina, setup) -> None:
    two_areas(hass, stamina)
    entry = await setup()
    manager = entry.runtime_data.manager
    await hass.services.async_call(
        "select", "select_option", {"entity_id": GARDEN, "option": UPLOADS_OFF}, blocking=True
    )
    assert hass.states.get(GARDEN).state == UPLOADS_OFF
    assert hass.states.get(DRIVEWAY).state == UPLOADS_CLASSES

    front, gate = event_id(500), event_id(501)
    stamina.start(front, camera="38", subjects=[subject("s1")])
    stamina.start(gate, camera="39", subjects=[subject("s1")])
    await until(lambda: front in manager.records and gate in manager.records)
    assert manager.records[front].upload_mode == UPLOADS_OFF
    assert not manager.records[front].admitted
    assert manager.records[gate].upload_mode == UPLOADS_CLASSES
    assert manager.records[gate].admitted
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_global_select_migrates_to_every_area(hass, stamina, setup, reload) -> None:
    two_areas(hass, stamina)
    entry = make_entry(hass, stamina)
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()
    await index.async_set_meta({"uploads": "unusual"})
    await index.async_close()
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "select", DOMAIN, f"{entry.entry_id}_uploads_mode", config_entry=entry
    )

    await setup(entry=entry)
    assert registry.async_get(old.entity_id) is None
    # A one-time repair issue names the selects that replaced it.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"uploads_per_area_{entry.entry_id}")
    assert issue is not None and not issue.is_fixable
    assert issue.translation_placeholders == {"selects": f"{DRIVEWAY}, {GARDEN}"}
    assert hass.states.get(GARDEN).state == "unusual"
    assert hass.states.get(DRIVEWAY).state == "unusual"

    # Each area keeps its own from then on, across restarts.
    await hass.services.async_call(
        "select", "select_option", {"entity_id": GARDEN, "option": "off"}, blocking=True
    )
    ir.async_get(hass).async_ignore(DOMAIN, issue.issue_id, True)
    await reload(entry)
    assert hass.states.get(GARDEN).state == "off"
    assert hass.states.get(DRIVEWAY).state == "unusual"
    # Not raised again: the old select is gone.
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue.issue_id).dismissed_version
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_camera_moved_to_another_area_regroups(hass, stamina, setup) -> None:
    _, driveway, front, _ = two_areas(hass, stamina)
    entry = await setup()
    manager = entry.runtime_data.manager
    await hass.services.async_call(
        "select", "select_option", {"entity_id": DRIVEWAY, "option": "off"}, blocking=True
    )

    # Both cameras in Driveway: Garden's device goes, with its entities.
    dr.async_get(hass).async_update_device(front.id, area_id=driveway.id)
    await hass.async_block_till_done()
    assert manager.areas == {driveway.id: "Driveway"}
    assert area_device_names(hass, entry) == {"Driveway"}
    assert er.async_get(hass).async_get(GARDEN) is None
    assert hass.states.get(GARDEN) is None
    assert manager.cameras["38"].device.area_id == driveway.id
    eid = event_id(510)
    stamina.start(eid, camera="38", subjects=[subject("s1")])
    await until(lambda: eid in manager.records)
    assert manager.records[eid].upload_mode == "off"

    # A renamed area renames its device; the entity ids stay.
    ar.async_get(hass).async_update(driveway.id, name="Front yard")
    await hass.async_block_till_done()
    assert manager.areas == {driveway.id: "Front yard"}
    device = dr.async_get(hass).async_get(er.async_get(hass).async_get(DRIVEWAY).device_id)
    assert device.name == "Front yard"

    # None of it restarted the integration.
    assert entry.runtime_data.manager is manager
    await hass.config_entries.async_unload(entry.entry_id)


async def test_old_area_subentries_fold_into_the_destination(hass, stamina, setup) -> None:
    """An upgrade moves each area's device and entities, then deletes its subentry."""
    garden, _, _, _ = two_areas(hass, stamina)
    entry = make_entry(hass, stamina)
    old = ConfigSubentry(
        data=MappingProxyType({"area_id": garden.id}),
        subentry_type="area",
        title="Garden",
        unique_id=garden.id,
    )
    hass.config_entries.async_add_subentry(entry, old)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=old.subentry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_area_{garden.id}")},
        name="Garden",
    )
    select = entities.async_get_or_create(
        "select",
        DOMAIN,
        f"{entry.entry_id}_area_{garden.id}_uploads",
        config_entry=entry,
        config_subentry_id=old.subentry_id,
        device_id=device.id,
        suggested_object_id="stamina_garden_uploads",
    )
    entities.async_update_entity(select.entity_id, name="Garden alarm uploads")

    await setup(entry=entry)
    assert [s.subentry_type for s in entry.subentries.values()] == [SUBENTRY_TYPE_DESTINATION]
    item = entities.async_get(GARDEN)
    assert item.id == select.id and item.name == "Garden alarm uploads"
    assert item.config_subentry_id == destination_id(entry)
    assert item.device_id == device.id
    assert devices.async_get(device.id).config_subentry_id == destination_id(entry)
    assert hass.states.get(GARDEN) is not None
    assert area_device_names(hass, entry) == {"Garden", "Driveway"}
    await hass.config_entries.async_unload(entry.entry_id)


async def test_without_a_destination_the_areas_sit_on_the_entry(hass, stamina, setup) -> None:
    two_areas(hass, stamina)
    entry = await setup(destinations=0)
    item = er.async_get(hass).async_get(GARDEN)
    assert item.config_subentry_id is None
    assert dr.async_get(hass).async_get(item.device_id).config_subentry_id is None
    await hass.config_entries.async_unload(entry.entry_id)


def _commit_one(stamina, manager, eid: str, camera: str = "38") -> None:
    """Run one confirmed, rare (so kept) event on a camera."""

    def _score(record, *, now, include_late=True) -> RelevanceResult:
        return RelevanceResult(0.2, 3.0, 1.4, False, "rare", [])

    manager.analysis.score = _score
    stamina.start(eid, camera=camera, subjects=[subject("s1")])
    for n in range(2):
        stamina.segment(eid, n, init=n == 0)
    stamina.verdict(eid, subject("s1", verdict="confirmed"))
    stamina.end(eid, subjects=[subject("s1", verdict="confirmed")], next_segment=2, init=True)


async def test_the_area_quota_counts_its_events(hass, stamina, destinations, setup) -> None:
    garden, _, _, _ = two_areas(hass, stamina)
    entry = await setup()
    manager = entry.runtime_data.manager
    manager.now = Clock()
    eid = event_id(520)
    _commit_one(stamina, manager, eid)
    await until(lambda: eid in manager.records and manager.records[eid].ended)
    await until(lambda: manager.records[eid].state is EventState.COMMITTED)
    assert manager.records[eid].area_key == garden.id
    # Refreshed by the commit itself.
    await until(lambda: float(hass.states.get(GARDEN_QUOTA).state) > 0)
    state = hass.states.get(GARDEN_QUOTA)
    assert state.attributes["events"] == 1
    assert state.attributes["percent_of_quota"] is not None
    assert state.attributes["uploading"] == 0
    assert state.attributes["pending_events"] == 0
    assert state.attributes["last_error"] is None
    assert float(hass.states.get(DRIVEWAY_QUOTA).state) == 0

    # Renamed, the area keeps counting the events it had, in memory and after a restart.
    used = manager.area_holdings(garden.id)["used"]
    ar.async_get(hass).async_update(garden.id, name="Back yard")
    await hass.async_block_till_done()
    assert manager.area_holdings(garden.id)["used"] == used
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.manager.area_holdings(garden.id)["used"] == used
    assert float(hass.states.get(GARDEN_QUOTA).state) > 0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_old_area_sensors_are_removed(hass, stamina, setup) -> None:
    two_areas(hass, stamina)
    entry = await setup()
    registry = er.async_get(hass)
    garden = next(
        key for key, title in entry.runtime_data.manager.areas.items() if title == "Garden"
    )
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_area_{garden}_status",
        config_entry=entry,
        config_subentry_id=destination_id(entry),
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(GARDEN_QUOTA) is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_media_source_lists_cameras_by_area(hass, stamina, setup) -> None:
    two_areas(hass, stamina)
    assert await async_setup_component(hass, "media_source", {})
    entry = await setup()
    root = await async_browse_media(hass, "media-source://scrypted_stamina")
    assert [child.title for child in root.children] == ["Driveway · Gate", "Garden · Front door"]
    await hass.config_entries.async_unload(entry.entry_id)
