"""Which Home Assistant device a Scrypted camera's Stamina entities live on.

When `scrypted_mqtt` is installed it already has one device per camera, identified as
`("scrypted_mqtt", "<topic root>:<scrypted device id>")` (its `hub.device_identifier`).

Since the 2026.8 device registry, a device belongs to exactly one config entry: an entity
of another integration that names the same identifiers gets a device of its own rather than
joining the existing one (`DeviceRegistry.async_get_or_create` matches identifiers within
the calling config entry only). So Stamina cannot put its entities *on* the `scrypted_mqtt`
device any more. It does the closest thing the registry supports: one Stamina device per
camera, with the camera's name, linked to the `scrypted_mqtt` device by `via_device_id`
(the camera's page lists it under "connected devices", and Stamina's page names the
camera), and placed in the camera's area. The camera's device is matched by Scrypted id.

Without `scrypted_mqtt`, the same Stamina device simply has no via link.

The folder layout and the reason sentences read the name and the area from the
`scrypted_mqtt` device when there is one — it is the one the user names and places — and
from Stamina's own device otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, SCRYPTED_MQTT_DOMAIN
from .context import device_area_name
from .naming import area_slug, camera_slug


@dataclass(slots=True, frozen=True)
class CameraDevice:
    """A camera as Home Assistant knows it."""

    camera: str
    device_info: DeviceInfo
    # The device whose area suggests linked entities: the scrypted_mqtt one if any.
    device_id: str | None
    name: str
    area: str | None
    shared: bool

    @property
    def area_slug(self) -> str:
        """Return the area folder name."""
        return area_slug(self.area)

    @property
    def slug(self) -> str:
        """Return the camera's folder-name slug."""
        return camera_slug(self.name, fallback=self.camera)


def own_identifier(camera: str) -> tuple[str, str]:
    """Return the identifier of Stamina's device for a camera."""
    return (DOMAIN, f"camera:{camera}")


def all_devices(registry: dr.DeviceRegistry) -> list[dr.DeviceEntry]:
    """Return every device entry, whichever shape `registry.devices` has.

    It has been a mapping (iterating gives ids) and, since 2026.8, a view whose iteration
    gives entries and whose mapping use is deprecated. Both are handled, without touching
    the deprecated mapping API where the view exists.
    """
    items = list(registry.devices)
    if items and isinstance(items[0], str):
        return [device for key in items if (device := registry.async_get(key)) is not None]
    return items


@callback
def scrypted_mqtt_device(hass: HomeAssistant, camera: str) -> dr.DeviceEntry | None:
    """Return the `scrypted_mqtt` device of a Scrypted device id, whatever its topic root."""
    for device in all_devices(dr.async_get(hass)):
        for domain, identifier in device.identifiers:
            if domain != SCRYPTED_MQTT_DOMAIN:
                continue
            _, sep, device_id = identifier.rpartition(":")
            if sep and device_id == camera:
                return device
    return None


@callback
def own_device(hass: HomeAssistant, entry_id: str, camera: str) -> dr.DeviceEntry | None:
    """Return Stamina's device for a camera, once it exists."""
    return dr.async_get(hass).async_get_device_by_identifier(own_identifier(camera), entry_id)


@callback
def resolve_camera(
    hass: HomeAssistant, camera: str, fallback_name: str, entry_id: str | None = None
) -> CameraDevice:
    """Return the device a camera's entities belong to, and its name and area."""
    shared = scrypted_mqtt_device(hass, camera)
    own = own_device(hass, entry_id, camera) if entry_id else None
    if shared is not None:
        name = shared.name_by_user or shared.name or fallback_name
        return CameraDevice(
            camera=camera,
            device_info=DeviceInfo(
                identifiers={own_identifier(camera)},
                name=name,
                manufacturer="Scrypted",
                model="Stamina",
                via_device_id=shared.id,
            ),
            device_id=shared.id,
            name=name,
            area=device_area_name(hass, shared.id),
            shared=True,
        )
    name = (own.name_by_user or own.name or fallback_name) if own else fallback_name
    return CameraDevice(
        camera=camera,
        device_info=DeviceInfo(
            identifiers={own_identifier(camera)},
            name=fallback_name,
            manufacturer="Scrypted",
            model="Stamina",
        ),
        device_id=own.id if own else None,
        name=name,
        area=device_area_name(hass, own.id) if own else None,
        shared=False,
    )


@callback
def async_sync_area(hass: HomeAssistant, entry_id: str, camera: str) -> None:
    """Place Stamina's device in the camera device's area, unless the user chose one."""
    shared = scrypted_mqtt_device(hass, camera)
    own = own_device(hass, entry_id, camera)
    if shared is None or own is None or shared.area_id is None or own.area_id is not None:
        return
    dr.async_get(hass).async_update_device(own.id, area_id=shared.area_id)


@callback
def async_ensure_device(hass: HomeAssistant, entry_id: str, device: CameraDevice) -> None:
    """Create a camera's Stamina device and place it before any entity is added.

    Done up front so every entity of the camera is named against the same device, area
    included (Home Assistant prefixes entity ids with the area when there is one).
    """
    dr.async_get(hass).async_get_or_create(config_entry_id=entry_id, **device.device_info)
    async_sync_area(hass, entry_id, device.camera)


@callback
def async_relink(hass: HomeAssistant, entry_id: str, device: CameraDevice) -> None:
    """Point a camera's Stamina device at its `scrypted_mqtt` device, and follow its area.

    For when `scrypted_mqtt` loads after Stamina (or its device appears later): the Stamina
    device was created without a via link and gets one now.
    """
    own = own_device(hass, entry_id, device.camera)
    if own is None or not device.shared or device.device_id is None:
        return
    if own.via_device_id != device.device_id:
        dr.async_get(hass).async_update_device(own.id, via_device_id=device.device_id)
    async_sync_area(hass, entry_id, device.camera)
