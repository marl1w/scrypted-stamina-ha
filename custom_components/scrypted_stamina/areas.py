"""One subentry per area that holds a camera, managed by the integration itself.

Home Assistant's integration page groups devices by subentry, so each area gets one, titled
with the area's name, holding one device with the area's Uploads select and quota.
Cameras without an area share a "No area" one. The unique id is the area id (or `NO_AREA`):
an area keeps its Uploads position across renames, and across its subentry being removed
and added again.

The integration adds, renames and removes these as cameras move between areas; there is no
flow to add one by hand (`config_flow.py` does not list the type). One deleted by hand comes
back on the next sync, and with it the area's stored position.
"""

from __future__ import annotations

from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import CONF_AREA_ID, DOMAIN, NO_AREA, NO_AREA_TITLE, SUBENTRY_TYPE_AREA
from .devices import CameraDevice
from .naming import area_slug


def area_key(device: CameraDevice) -> str:
    """Return the key of a camera's area: its id, or `NO_AREA`."""
    return device.area_id or NO_AREA


def area_identifier(entry_id: str, key: str) -> tuple[str, str]:
    """Return the identifier of an area's device."""
    return (DOMAIN, f"{entry_id}_area_{key}")


def area_subentries(entry: ConfigEntry) -> dict[str, ConfigSubentry]:
    """Return the area subentries, by area key."""
    return {
        str(subentry.unique_id): subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_AREA
    }


def wanted_areas(devices: list[CameraDevice]) -> dict[str, str]:
    """Return the areas the cameras are in, by key, with their titles."""
    return {area_key(device): device.area or NO_AREA_TITLE for device in devices}


def slug_of(subentry: ConfigSubentry) -> str:
    """Return the area folder name events of this area are filed under."""
    if subentry.unique_id == NO_AREA:
        return NO_AREA
    return area_slug(subentry.title)


@callback
def async_sync_area_subentries(
    hass: HomeAssistant, entry: ConfigEntry, wanted: dict[str, str]
) -> bool:
    """Add, rename and remove area subentries to match `wanted`; return whether any changed.

    With no camera known at all (the plugin unreachable at start), nothing is removed.
    """
    if not wanted:
        return False
    existing = area_subentries(entry)
    changed = False
    devices = dr.async_get(hass)
    for key, title in wanted.items():
        subentry = existing.pop(key, None)
        if subentry is None:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType({CONF_AREA_ID: key}),
                    subentry_type=SUBENTRY_TYPE_AREA,
                    title=title,
                    unique_id=key,
                ),
            )
            changed = True
        elif subentry.title != title:
            hass.config_entries.async_update_subentry(entry, subentry, title=title)
            device = devices.async_get_device_by_identifier(
                area_identifier(entry.entry_id, key), entry.entry_id
            )
            if device is not None:
                devices.async_update_device(device.id, name=title)
            changed = True
    for subentry in existing.values():
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        changed = True
    return changed
