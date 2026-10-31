"""`event.<cam>_stamina`: fires `normal` or `unusual` when an event ends (architecture §3.7).

It fires a second time, `unusual` with `late: true`, when the follow-up window turns an
ordinary event into an unusual one (§3.4a). The camera's capabilities are attributes too.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import StaminaConfigEntry
from .const import signal_event
from .entity import CameraEntity, async_setup_per_camera

EVENT_TYPES = ["normal", "unusual"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StaminaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one event entity per camera."""
    manager = entry.runtime_data.manager
    async_setup_per_camera(
        hass,
        manager,
        async_add_entities,
        lambda camera: [StaminaEventEntity(manager, camera)],
        entry.async_on_unload,
    )


class StaminaEventEntity(CameraEntity, EventEntity):
    """Fires once per event, and again if it turns out unusual late."""

    key = "stamina"
    _attr_event_types = EVENT_TYPES

    async def async_added_to_hass(self) -> None:
        """Listen for decided events as well as updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_event(self.manager.entry.entry_id), self._fired
            )
        )

    @callback
    def _fired(self, camera: str, event_type: str, attributes: dict[str, Any]) -> None:
        if camera != self.camera:
            return
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the camera's capabilities and learning state alongside the event data."""
        coverage = self.manager.analysis.coverage(self.camera)
        return {
            "capabilities": self.status.info.capabilities,
            "learning": self.manager.analysis.state(self.camera) == "learning",
            "learned_days": coverage["days"],
            "learned_events": coverage["events"],
        }
