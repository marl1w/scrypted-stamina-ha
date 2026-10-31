"""Sensors: the last score per camera, and the uploader's queue, quota and latency.

* `sensor.<cam>_last_event_score` — 0 to 1, the best score of the camera's last event.
* `sensor.stamina_queue` — segment files waiting to be uploaded, across destinations.
* `sensor.stamina_quota_used_<destination>` — bytes held on each destination.
* `sensor.stamina_latency` — p50 of time from detection to first segment uploaded, with
  p50 and p95 as attributes (architecture §4).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import StaminaConfigEntry
from .const import signal_quota
from .entity import CameraEntity, StaminaEntity, async_setup_per_camera, hub_device
from .manager import StaminaManager
from .uploader import Target


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StaminaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors."""
    manager = entry.runtime_data.manager
    async_add_entities([QueueSensor(manager), LatencySensor(manager), OrphanSensor(manager)])
    # On the hub device rather than one per destination, so they read
    # `sensor.stamina_quota_used_<destination>`; a removed destination's sensor is removed.
    async_add_entities(
        [
            QuotaSensor(manager, target, entry.subentries[subentry_id].title)
            for subentry_id, target in manager.uploader.targets.items()
        ]
    )
    registry = er.async_get(hass)
    live = {f"{entry.entry_id}_{sid}_quota_used" for sid in manager.uploader.targets}
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.unique_id.endswith("_quota_used") and item.unique_id not in live:
            registry.async_remove(item.entity_id)
    async_setup_per_camera(
        hass,
        manager,
        async_add_entities,
        lambda camera: [LastScoreSensor(manager, camera)],
        entry.async_on_unload,
    )


class LastScoreSensor(CameraEntity, SensorEntity):
    """The camera's last event score."""

    key = "last_event_score"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        """Return the last score, 0 to 1."""
        last = self.status.last
        return None if last is None else last.score

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return which event the score belongs to."""
        last = self.status.last
        if last is None:
            return {}
        return {
            "event_id": last.id,
            "unusual": last.unusual,
            "final": last.final_score is not None,
            "reason": last.reason,
        }


class _HubSensor(StaminaEntity, SensorEntity):
    def __init__(self, manager: StaminaManager, key: str) -> None:
        super().__init__(manager)
        self._attr_translation_key = key
        self._attr_unique_id = f"{manager.entry.entry_id}_{key}"
        self._attr_device_info = hub_device(manager.entry)


class QueueSensor(_HubSensor):
    """Segment files waiting to be uploaded."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    # They change with every segment; the recorder keeps the state, not these.
    _unrecorded_attributes = frozenset(
        {"events_uploading", "uploads_at_once", "link", "link_last_error"}
    )

    def __init__(self, manager: StaminaManager) -> None:
        """Bind to the manager."""
        super().__init__(manager, "queue")

    @property
    def native_value(self) -> int:
        """Return the number of files waiting."""
        return self.manager.uploader.queue

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the uploader's state."""
        link = self.manager.link
        return {
            "events_uploading": self.manager.uploader.active,
            "uploads_at_once": self.manager.uploader.capacity,
            "link": "connected" if self.manager.link_healthy else "disconnected",
            "link_last_error": link.last_error if link else None,
        }


class LatencySensor(_HubSensor):
    """Time from detection to the first segment uploaded."""

    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, manager: StaminaManager) -> None:
        """Bind to the manager."""
        super().__init__(manager, "latency")

    @property
    def native_value(self) -> float | None:
        """Return the median."""
        return self.manager.latency_stats()["p50"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return p50, p95 and the sample count."""
        return {**self.manager.latency_stats(), "samples": len(self.manager.latencies)}


class QuotaSensor(StaminaEntity, SensorEntity):
    """Bytes held on one destination."""

    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "quota_used"
    _unrecorded_attributes = frozenset(
        {"events", "follow_up_held", "orphans", "orphan_bytes", "uploaded", "last_error"}
    )

    def _signal(self) -> str:
        """Refresh only when the destination's holdings change (commit, evict, drop)."""
        return signal_quota(self.manager.entry.entry_id)

    def __init__(self, manager: StaminaManager, target: Target, title: str) -> None:
        """Bind to one destination."""
        super().__init__(manager)
        self.target = target
        self._attr_unique_id = f"{manager.entry.entry_id}_{target.subentry_id}_quota_used"
        self._attr_device_info = hub_device(manager.entry)
        self._attr_translation_placeholders = {"destination": title}

    @property
    def native_value(self) -> int:
        """Return the bytes held."""
        return self.target.usage.used

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the quota, the follow-up hold and the last error."""
        return {
            "destination": self.target.label,
            "quota": self.target.quota,
            "events": len(self.target.usage),
            "follow_up_held": self.target.usage.follow_up_bytes(),
            "follow_up_cap": self.target.follow_up_cap,
            "orphans": self.target.usage.orphans()[0],
            "orphan_bytes": self.target.usage.orphans()[1],
            "uploaded": self.target.uploaded,
            "last_error": self.target.last_error,
        }


class OrphanSensor(_HubSensor):
    """Events kept because nobody could decide: how many, and what they hold."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, manager: StaminaManager) -> None:
        """Bind to the manager."""
        super().__init__(manager, "orphans")

    def _signal(self) -> str:
        return signal_quota(self.manager.entry.entry_id)

    @property
    def native_value(self) -> int:
        """Return the most orphans any destination holds."""
        counts = [t.usage.orphans()[0] for t in self.manager.uploader.targets.values()]
        return max(counts, default=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the bytes orphans hold, per destination."""
        return {
            target.label: target.usage.orphans()[1]
            for target in self.manager.uploader.targets.values()
        }
