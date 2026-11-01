"""Shared entity plumbing: devices, unique ids, and listening to the manager.

Per-camera entities sit on a Stamina device per camera, named like the camera and linked to
its `scrypted_mqtt` device when there is one (see `devices.py`). Integration-wide entities
sit on one "Scrypted Stamina" service device; each destination's on a device of its own,
named after the destination and filed under its subentry; and each area's (its Uploads
select and quota) on a device named after the area, filed under the first destination's
subentry (`areas.py`).
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .areas import area_home, area_identifier, slug_of
from .const import DOMAIN, signal_areas, signal_camera, signal_cameras, signal_update
from .devices import async_ensure_device
from .manager import CameraStatus, StaminaManager

HUB_NAME = "Scrypted Stamina"


def hub_device(entry: ConfigEntry) -> DeviceInfo:
    """Return the integration's own device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=HUB_NAME,
        manufacturer="Scrypted",
        model="Stamina",
        entry_type=DeviceEntryType.SERVICE,
    )


@callback
def async_hub_device_id(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """Return the hub device's id, creating the device if need be (for `via_device_id`)."""
    return (
        dr.async_get(hass)
        .async_get_or_create(config_entry_id=entry.entry_id, **hub_device(entry))
        .id
    )


def destination_device(entry: ConfigEntry, subentry_id: str, title: str, hub_id: str) -> DeviceInfo:
    """Return one destination's device, under the hub."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{subentry_id}")},
        name=title,
        manufacturer="Scrypted",
        model="Stamina destination",
        entry_type=DeviceEntryType.SERVICE,
        via_device_id=hub_id,
    )


def area_device(entry: ConfigEntry, key: str, title: str, hub_id: str) -> DeviceInfo:
    """Return one area's device, under the hub."""
    return DeviceInfo(
        identifiers={area_identifier(entry.entry_id, key)},
        name=title,
        manufacturer="Scrypted",
        model="Stamina area",
        entry_type=DeviceEntryType.SERVICE,
        via_device_id=hub_id,
    )


def hub_entity_id(domain: str, key: str) -> str:
    """Return a hub entity's id, `<domain>.stamina_<key>`, whatever the device is named.

    Only a suggestion: an entity already in the registry keeps its own id.
    """
    return f"{domain}.stamina_{key}"


class StaminaEntity(Entity):
    """An entity refreshed whenever the manager says something changed."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: StaminaManager) -> None:
        """Bind to the manager."""
        self.manager = manager

    def _signal(self) -> str:
        """Return the dispatcher signal this entity refreshes on."""
        return signal_update(self.manager.entry.entry_id)

    async def async_added_to_hass(self) -> None:
        """Listen for updates (coalesced by the manager to at most one a second)."""
        self.async_on_remove(async_dispatcher_connect(self.hass, self._signal(), self._refresh))

    @callback
    def _refresh(self) -> None:
        self.async_write_ha_state()


class CameraEntity(StaminaEntity):
    """An entity about one camera, on that camera's device."""

    key: str = ""

    def __init__(self, manager: StaminaManager, camera: str) -> None:
        """Bind to a camera."""
        super().__init__(manager)
        self.camera = camera
        status = manager.cameras[camera]
        self._attr_unique_id = f"{manager.entry.entry_id}_{camera}_{self.key}"
        self._attr_device_info = status.device.device_info
        self._attr_translation_key = self.key

    def _signal(self) -> str:
        """Refresh on this camera's own signal only."""
        return signal_camera(self.manager.entry.entry_id, self.camera)

    @property
    def status(self) -> CameraStatus:
        """Return the camera's status."""
        return self.manager.cameras[self.camera]


class AreaEntity(StaminaEntity):
    """An entity about one area, on that area's device."""

    key: str = ""
    platform_domain: str = ""

    def __init__(self, manager: StaminaManager, key: str, title: str, hub_id: str) -> None:
        """Bind to an area."""
        super().__init__(manager)
        self.area = key
        self._attr_unique_id = f"{manager.entry.entry_id}_area_{key}_{self.key}"
        self._attr_device_info = area_device(manager.entry, key, title, hub_id)
        self._attr_translation_key = self.key
        # A suggestion, `<domain>.stamina_<area>_<key>`: the id stays if the area is renamed.
        slug = slug_of(key, title).replace("-", "_")
        self.entity_id = f"{self.platform_domain}.stamina_{slug}_{self.key}"


@callback
def async_setup_per_area(
    hass: HomeAssistant,
    manager: StaminaManager,
    add: AddConfigEntryEntitiesCallback,
    build: Callable[[str, str, str], list[Entity]],
    on_remove: Callable[[Callable[[], None]], None],
) -> None:
    """Add entities for every area holding a camera now, and follow the areas after.

    An area that comes is added, filed under `area_home`; one renamed renames its device; one
    no camera is in any more goes, its device and entities with it.
    """
    entry = manager.entry
    known: dict[str, list[Entity]] = {}

    async def _async_sync() -> None:
        current = dict(manager.areas)
        devices = dr.async_get(hass)
        for key in [k for k in known if k not in current]:
            for entity in known.pop(key):
                await entity.async_remove(force_remove=True)
            identifier = area_identifier(entry.entry_id, key)
            if device := devices.async_get_device_by_identifier(identifier, entry.entry_id):
                devices.async_remove_device(device.id)
        hub_id = async_hub_device_id(hass, entry)
        for key, title in current.items():
            if key not in known:
                known[key] = build(key, title, hub_id)
                add(known[key], config_subentry_id=area_home(entry))
                continue
            identifier = area_identifier(entry.entry_id, key)
            device = devices.async_get_device_by_identifier(identifier, entry.entry_id)
            if device is not None and device.name != title:
                devices.async_update_device(device.id, name=title)

    @callback
    def _schedule() -> None:
        entry.async_create_task(hass, _async_sync(), "stamina area entities")

    _schedule()
    on_remove(async_dispatcher_connect(hass, signal_areas(entry.entry_id), _schedule))


@callback
def async_setup_per_camera(
    hass: HomeAssistant,
    manager: StaminaManager,
    add: Callable[[list[Entity]], None],
    build: Callable[[str], list[Entity]],
    on_remove: Callable[[Callable[[], None]], None],
) -> None:
    """Add entities for every camera now, and for cameras that appear later."""
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [camera for camera in manager.cameras if camera not in known]
        known.update(new)
        for camera in new:
            async_ensure_device(hass, manager.entry.entry_id, manager.cameras[camera].device)
        entities = [entity for camera in new for entity in build(camera)]
        if entities:
            add(entities)

    _add_new()
    on_remove(async_dispatcher_connect(hass, signal_cameras(manager.entry.entry_id), _add_new))
