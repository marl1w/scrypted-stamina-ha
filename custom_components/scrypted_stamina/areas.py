"""One device per area that holds a camera, with the area's Uploads select and quota.

The devices are filed under the first destination's subentry, so the integration page lists
them with it rather than as entries of their own (directly on the entry when there is no
destination). Cameras without an area share a "No area" device. The device and entity
unique ids carry the area id (or `NO_AREA`), so an area keeps its Uploads position across
renames.

Earlier versions gave every area a subentry of its own; `async_file_area_devices` moves
their devices and entities onto the destination and deletes those subentries.
"""

from __future__ import annotations

from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN, NO_AREA, SUBENTRY_TYPE_DESTINATION
from .devices import CameraDevice
from .naming import area_slug

NO_AREA_TITLE: Final = "No area"
# The subentries earlier versions kept per area; only read to remove them.
LEGACY_SUBENTRY_TYPE_AREA: Final = "area"


def area_key(device: CameraDevice) -> str:
    """Return the key of a camera's area: its id, or `NO_AREA`."""
    return device.area_id or NO_AREA


def area_identifier(entry_id: str, key: str) -> tuple[str, str]:
    """Return the identifier of an area's device."""
    return (DOMAIN, f"{entry_id}_area_{key}")


def wanted_areas(devices: list[CameraDevice]) -> dict[str, str]:
    """Return the areas the cameras are in, by key, with their titles."""
    return {area_key(device): device.area or NO_AREA_TITLE for device in devices}


def slug_of(key: str, title: str) -> str:
    """Return the area folder name events of this area are filed under."""
    return NO_AREA if key == NO_AREA else area_slug(title)


def area_home(entry: ConfigEntry) -> str | None:
    """Return the subentry the area devices are filed under: the first destination's."""
    return next(
        (
            subentry.subentry_id
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_DESTINATION
        ),
        None,
    )


@callback
def async_file_area_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """File every area device, and its entities, under `area_home`; drop old area subentries.

    Moved before the subentries go, so removing them takes no device or entity with it:
    entity ids, names and history stay. Idempotent; run at every setup, which also follows a
    first destination added or removed.
    """
    home = area_home(entry)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    prefix = f"{entry.entry_id}_area_"
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if not any(d == DOMAIN and i.startswith(prefix) for d, i in device.identifiers):
            continue
        if device.config_subentry_id != home:
            devices.async_update_device(device.id, new_config_subentry_id=home)
        for item in er.async_entries_for_device(
            entities, device.id, include_disabled_entities=True
        ):
            if item.config_subentry_id != home:
                entities.async_update_entity(item.entity_id, config_subentry_id=home)
    for subentry in list(entry.subentries.values()):
        if subentry.subentry_type == LEGACY_SUBENTRY_TYPE_AREA:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
