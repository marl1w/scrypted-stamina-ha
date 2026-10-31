"""`binary_sensor.<cam>_unusual`: on while an unusual event is open (architecture §3.7).

Also pulses on for a short while when the follow-up window turns an ended event unusual.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import StaminaConfigEntry
from .entity import CameraEntity, async_setup_per_camera


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StaminaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one unusual sensor per camera."""
    manager = entry.runtime_data.manager
    async_setup_per_camera(
        hass,
        manager,
        async_add_entities,
        lambda camera: [UnusualSensor(manager, camera)],
        entry.async_on_unload,
    )


class UnusualSensor(CameraEntity, BinarySensorEntity):
    """On while the camera has an unusual event open."""

    key = "unusual"

    @property
    def is_on(self) -> bool:
        """Return whether an unusual event is open (or just turned unusual late)."""
        return self.status.unusual_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return which events are open and unusual."""
        return {"events": sorted(self.status.open_unusual)}
