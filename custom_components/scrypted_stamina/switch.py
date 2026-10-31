"""`switch.stamina_uploads`: arms ordinary uploads (architecture §3.7).

The switch gates admission, not delivery (as in Reolink Stamina): an event already being
uploaded when it is turned off still completes. Unusual events still upload while it is off
when the "unusual events upload anyway" option is on.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import StaminaConfigEntry
from .entity import StaminaEntity, hub_device


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StaminaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the uploads switch."""
    async_add_entities([UploadsSwitch(entry.runtime_data.manager)])


class UploadsSwitch(StaminaEntity, SwitchEntity):
    """Whether new events are uploaded."""

    _attr_translation_key = "uploads"

    def __init__(self, manager) -> None:
        """Bind to the manager."""
        super().__init__(manager)
        self._attr_unique_id = f"{manager.entry.entry_id}_uploads"
        self._attr_device_info = hub_device(manager.entry)

    @property
    def is_on(self) -> bool:
        """Return whether uploads are armed."""
        return self.manager.uploads_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Arm uploads."""
        await self.manager.async_set_uploads(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disarm ordinary uploads."""
        await self.manager.async_set_uploads(False)
