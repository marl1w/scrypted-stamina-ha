"""Each area's Uploads select: what that area's cameras upload (architecture §3.7).

One per area that holds a camera (`areas.py`), e.g. `select.stamina_garden_uploads`, so each area's
own alarm automation can switch it. Three positions. "Chosen classes": unusual events, and
ordinary events with a confirmed subject of a class chosen in the options. "Unusual only".
"Off": nothing is uploaded and no upload starts, not even to `pending/`; events are still
indexed, scored and listed, so the model keeps learning, and committed copies are left
alone.

Each event takes its camera's area's position at its start, so a change applies to new
events only; an event already under way finishes its normal decision. The positions are
kept in the index (by area id), so they survive restarts and an area's device coming and
going; an area never set takes the old integration-wide select's position.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import StaminaConfigEntry
from .const import UPLOAD_MODES
from .entity import AreaEntity, async_setup_per_area


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StaminaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one uploads select per area."""
    manager = entry.runtime_data.manager
    async_setup_per_area(
        hass,
        manager,
        async_add_entities,
        lambda key, title, hub_id: [UploadsSelect(manager, key, title, hub_id)],
        entry.async_on_unload,
    )


class UploadsSelect(AreaEntity, SelectEntity):
    """What new events of one area upload."""

    key = "uploads"
    platform_domain = "select"
    _attr_options = list(UPLOAD_MODES)  # noqa: RUF012 - read-only

    @property
    def current_option(self) -> str:
        """Return the area's position."""
        return self.manager.uploads_for(self.area)

    async def async_select_option(self, option: str) -> None:
        """Move the area to a position."""
        await self.manager.async_set_uploads(option, self.area)
        self.async_write_ha_state()
