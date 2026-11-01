"""One subentry per camera, managed by the integration: its settings, device and entities.

Titled with the camera's name and keyed by its Scrypted id, each holds the camera's own
settings (linked entities, look-back and follow-up windows), which its reconfigure step
changes; the camera's Stamina device and entities are filed under it. The integration adds,
renames and removes them as cameras appear in and leave the plugin's list; the subentry
flow refuses to add one by hand.

Earlier versions kept these settings in the options, under `cameras`;
`async_migrate_camera_options` moves them into the subentries once.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback

from .const import CONF_CAMERAS, SUBENTRY_TYPE_CAMERA
from .devices import own_device


def camera_subentries(entry: ConfigEntry) -> dict[str, ConfigSubentry]:
    """Return the camera subentries, by Scrypted id."""
    return {
        str(subentry.unique_id): subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_CAMERA
    }


def camera_settings(entry: ConfigEntry, camera: str) -> Mapping[str, Any]:
    """Return one camera's settings: its subentry's data, empty when it has none."""
    subentry = camera_subentries(entry).get(camera)
    return subentry.data if subentry is not None else {}


def _add(hass: HomeAssistant, entry: ConfigEntry, camera: str, title: str, data: Any) -> None:
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(dict(data)),
            subentry_type=SUBENTRY_TYPE_CAMERA,
            title=title,
            unique_id=camera,
        ),
    )


@callback
def async_migrate_camera_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move the per-camera options into camera subentries, then drop them from the options.

    Titled with the camera's device name for now; the first sync renames them.
    """
    legacy = entry.options.get(CONF_CAMERAS)
    if legacy is None:
        return
    existing = camera_subentries(entry)
    for camera, settings in legacy.items():
        if camera in existing:
            continue
        device = own_device(hass, entry.entry_id, camera)
        name = (device.name_by_user or device.name) if device else None
        _add(hass, entry, camera, name or f"Camera {camera}", settings or {})
    options = {key: value for key, value in entry.options.items() if key != CONF_CAMERAS}
    hass.config_entries.async_update_entry(entry, options=options)


@callback
def async_sync_camera_subentries(
    hass: HomeAssistant, entry: ConfigEntry, names: dict[str, str], listed: set[str]
) -> None:
    """Add a subentry per camera in `names`, retitle the renamed, remove the unlisted.

    `listed` is the plugin's last camera list: a subentry goes only when its camera is not
    in it, and never when the list is empty (the plugin unreachable).
    """
    existing = camera_subentries(entry)
    for camera, name in names.items():
        subentry = existing.get(camera)
        if subentry is None:
            _add(hass, entry, camera, name, {})
        elif subentry.title != name:
            hass.config_entries.async_update_subentry(entry, subentry, title=name)
    if not listed:
        return
    for camera, subentry in existing.items():
        if camera not in listed:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
