"""Per-camera entities: linked to existing `scrypted_mqtt` camera devices, or standalone."""

from __future__ import annotations

from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scrypted_stamina import uploader as uploader_module
from custom_components.scrypted_stamina.const import DOMAIN

from .conftest import event_id, make_entry, subject, until

PER_CAMERA = (
    "event.front_door_stamina",
    "binary_sensor.front_door_unusual",
    "sensor.front_door_last_event_score",
)
HUB = (
    "select.stamina_uploads",
    "sensor.stamina_queue",
    "sensor.stamina_latency",
    "sensor.stamina_orphans",
)


async def test_entities_join_the_scrypted_mqtt_camera(hass, setup) -> None:
    """Matched by Scrypted device id through `hub.device_identifier`'s shape."""
    mqtt = MockConfigEntry(domain="scrypted_mqtt", title="Scrypted MQTT")
    mqtt.add_to_hass(hass)
    garden = ar.async_get(hass).async_create("Garden")
    devices = dr.async_get(hass)
    camera = devices.async_get_or_create(
        config_entry_id=mqtt.entry_id,
        identifiers={("scrypted_mqtt", "scrypted:38")},
        name="Front door",
        manufacturer="Reolink",
    )
    devices.async_update_device(camera.id, area_id=garden.id)

    entry = await setup()
    entities = er.async_get(hass)
    # Home Assistant prefixes entity ids with the device's area, as it does for the
    # camera's own entities in that area.
    for entity_id in (eid.replace("front_door", "garden_front_door") for eid in PER_CAMERA):
        assert hass.states.get(entity_id) is not None, entity_id
        device = devices.async_get(entities.async_get(entity_id).device_id)
        assert device.via_device_id == camera.id
        assert device.area_id == garden.id
        assert (DOMAIN, "camera:38") in device.identifiers
    # The scrypted_mqtt device itself is left as its integration made it.
    assert devices.async_get(camera.id).config_entry_id == mqtt.entry_id
    # Folders and suggestions follow the camera's own device.
    status = entry.runtime_data.manager.cameras["38"]
    assert status.device.shared and status.device.area_slug == "garden"
    assert status.device.device_id == camera.id
    for entity_id in HUB:
        assert hass.states.get(entity_id) is not None, entity_id
    await hass.config_entries.async_unload(entry.entry_id)


async def test_without_scrypted_mqtt_a_device_per_camera_is_created(hass, setup) -> None:
    entry = await setup()
    entities = er.async_get(hass)
    device = dr.async_get(hass).async_get(entities.async_get(PER_CAMERA[0]).device_id)
    assert device.name == "Front door"
    assert device.via_device_id is None
    state = hass.states.get("event.front_door_stamina")
    assert state.attributes["capabilities"]["video_recorder"] is True
    assert state.attributes["learning"] is True
    assert hass.states.get("binary_sensor.front_door_unusual").state == "off"
    assert hass.states.get("select.stamina_uploads").state == "classes"
    assert float(hass.states.get("sensor.webdav_nas_0_quota_used").state) == 0
    await hass.config_entries.async_unload(entry.entry_id)


async def test_devices_are_found_whatever_shape_the_registry_has(hass) -> None:
    """`registry.devices` iterated as ids (older) or as entries (2026.8+) both work."""
    from types import SimpleNamespace

    from custom_components.scrypted_stamina.devices import all_devices, scrypted_mqtt_device

    mqtt = MockConfigEntry(domain="scrypted_mqtt")
    mqtt.add_to_hass(hass)
    registry = dr.async_get(hass)
    for n in range(3):
        registry.async_get_or_create(
            config_entry_id=mqtt.entry_id,
            identifiers={("scrypted_mqtt", f"scrypted:{n}")},
            name=f"Camera {n}",
        )
    # another integration's device with a three-part identifier must not break the lookup
    other = MockConfigEntry(domain="legacy")
    other.add_to_hass(hass)
    registry.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("legacy", "a", "b")}, name="Legacy"
    )
    found = [d for d in all_devices(registry) if d.name != "Legacy"]
    assert len(found) == 3 and all(isinstance(item, dr.DeviceEntry) for item in found)
    assert scrypted_mqtt_device(hass, "2").name == "Camera 2"

    as_keys = SimpleNamespace(devices=[d.id for d in found], async_get=registry.async_get)
    assert [d.id for d in all_devices(as_keys)] == [d.id for d in found]


DESTINATION = (
    "sensor.webdav_nas_0_quota_used",
    "sensor.webdav_nas_0_quota_used_percent",
    "sensor.webdav_nas_0_pending",
    "sensor.webdav_nas_0_health",
)


async def test_hub_and_destination_devices(hass, setup) -> None:
    entry = await setup(destinations=2)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    hub = devices.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    assert hub.name == "Scrypted Stamina"
    for entity_id in HUB:
        assert entities.async_get(entity_id).device_id == hub.id, entity_id

    for n, subentry_id in enumerate(entry.subentries):
        device = devices.async_get_device_by_identifier(
            (DOMAIN, f"{entry.entry_id}_{subentry_id}"), entry.entry_id
        )
        assert device.name == f"WebDAV — NAS {n}"
        assert device.via_device_id == hub.id
        # Filed under the subentry, so Home Assistant lists them with the destination.
        assert device.config_entry_id == entry.entry_id
        assert device.config_subentry_id == subentry_id
        owned = [
            item
            for item in er.async_entries_for_device(entities, device.id)
            if item.config_subentry_id == subentry_id
        ]
        assert {item.unique_id.rsplit("_", 1)[-1] for item in owned} >= {"used", "percent"}
        assert len(owned) == 4

    assert hass.states.get(DESTINATION[1]).state == "0.0"
    assert hass.states.get(DESTINATION[2]).attributes["cap"] == 1024**3
    assert hass.states.get(DESTINATION[3]).state == "ok"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_quota_sensor_keeps_its_id_and_history(hass, stamina, setup) -> None:
    """The first version's quota sensor sat on the hub: same unique id, now on its device."""
    entry = make_entry(hass, stamina)
    subentry_id = next(iter(entry.subentries))
    entities = er.async_get(hass)
    old = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_{subentry_id}_quota_used",
        config_entry=entry,
        suggested_object_id="stamina_quota_used_webdav_nas_0",
    )
    await setup(entry=entry)
    item = entities.async_get(old.entity_id)
    assert item.entity_id == "sensor.stamina_quota_used_webdav_nas_0"
    assert item.config_subentry_id == subentry_id
    device = dr.async_get(hass).async_get(item.device_id)
    assert device.name == "WebDAV — NAS 0"
    await hass.config_entries.async_unload(entry.entry_id)


async def test_health_turns_failing_on_repeated_failures(
    hass, stamina, destinations, setup, monkeypatch
) -> None:
    monkeypatch.setattr(uploader_module, "BREAKER_SECONDS", 0.01)
    monkeypatch.setattr(uploader_module, "BREAKER_MAX_SECONDS", 0.05)
    entry = await setup()
    manager = entry.runtime_data.manager
    destinations["storage0"].fail = {".m4s"}
    eid = event_id(400)
    stamina.start(eid, subjects=[subject("s1")])
    for n in range(3):
        stamina.segment(eid, n, init=n == 0)
    target = next(iter(manager.uploader.targets.values()))
    await until(lambda: not target.healthy)
    manager._changed()
    await until(lambda: hass.states.get(DESTINATION[3]).state == "failing")
    state = hass.states.get(DESTINATION[3])
    assert "refused" in state.attributes["last_error"]
    assert state.attributes["failures_in_a_row"] >= 3

    # Any operation that goes through again makes it healthy (the breaker closing).
    target.succeeded()
    manager._changed()
    await until(lambda: hass.states.get(DESTINATION[3]).state == "ok")
    await hass.config_entries.async_unload(entry.entry_id)
