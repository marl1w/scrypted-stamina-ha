"""Shared entity plumbing: devices, unique ids, and listening to the manager.

Per-camera entities sit on a Stamina device per camera, named like the camera and linked to
its `scrypted_mqtt` device when there is one (see `devices.py`). Integration-wide entities
sit on one "Stamina" service device.
"""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, signal_camera, signal_cameras, signal_update
from .devices import async_ensure_device
from .manager import CameraStatus, StaminaManager

HUB_NAME = "Stamina"


def hub_device(entry: ConfigEntry) -> DeviceInfo:
    """Return the integration's own device."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        # "Stamina", so the integration-wide entities read `switch.stamina_uploads`.
        name=HUB_NAME,
        manufacturer="Scrypted",
        model="Stamina",
        entry_type=DeviceEntryType.SERVICE,
    )


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
