"""`select.stamina_uploads`: what is uploaded (architecture §3.7).

Three positions. "Chosen classes": unusual events, and ordinary events with a confirmed
subject of a class chosen in the options. "Unusual only". "Off": nothing is uploaded and no
upload starts, not even to `pending/`; events are still indexed, scored and listed, so the
model keeps learning, and committed copies are left alone.

Each event takes the position at its start, so a change applies to new events only; an
event already under way finishes its normal decision.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import StaminaConfigEntry
from .const import UPLOAD_MODES
from .entity import StaminaEntity, hub_device, hub_entity_id


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StaminaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the uploads select."""
    async_add_entities([UploadsSelect(entry.runtime_data.manager)])


class UploadsSelect(StaminaEntity, SelectEntity):
    """What new events upload."""

    _attr_translation_key = "uploads"

    def __init__(self, manager) -> None:
        """Bind to the manager."""
        super().__init__(manager)
        self._attr_options = list(UPLOAD_MODES)
        # Not the old switch's id: that entity is removed on upgrade (`__init__.py`).
        self._attr_unique_id = f"{manager.entry.entry_id}_uploads_mode"
        self._attr_device_info = hub_device(manager.entry)
        self.entity_id = hub_entity_id("select", "uploads")

    @property
    def current_option(self) -> str:
        """Return the current position."""
        return self.manager.uploads

    async def async_select_option(self, option: str) -> None:
        """Move to a position."""
        await self.manager.async_set_uploads(option)
