"""Sensors: the last score per camera, and the uploader's queue, quota and latency.

On each camera's device:

* `sensor.<cam>_last_event_score` — 0 to 1, the best score of the camera's last event.

On the "Scrypted Stamina" hub device:

* `sensor.stamina_queue` — segment files waiting to be uploaded, across destinations.
* `sensor.stamina_latency` — p50 of time from detection to first segment uploaded, with
  p50 and p95 as attributes (architecture §4).
* `sensor.stamina_orphans` — the most orphans any destination holds.

On each destination's device (filed under its subentry):

* quota used (bytes, shown in GB) and the same as a percentage of the quota;
* pending: what the follow-up window holds in `pending/`, against its cap;
* health: `ok`, or `failing` after `HEALTH_FAILURES` failed operations in a row (uploads,
  commits, evictions), with the last error as an attribute.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import StaminaConfigEntry
from .const import signal_quota, signal_update
from .entity import (
    CameraEntity,
    StaminaEntity,
    async_setup_per_camera,
    destination_device,
    hub_device,
    hub_entity_id,
)
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
    # One device per destination, filed under its subentry; a removed destination's
    # entities are removed with it.
    live: set[str] = set()
    for subentry_id, target in manager.uploader.targets.items():
        title = entry.subentries[subentry_id].title
        entities = [cls(manager, target, title) for cls in DESTINATION_SENSORS]
        live.update(str(entity.unique_id) for entity in entities)
        async_add_entities(entities, config_subentry_id=subentry_id)
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.domain != "sensor" or item.unique_id in live:
            continue
        if item.unique_id.endswith(tuple(f"_{cls.key}" for cls in DESTINATION_SENSORS)):
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
        self.entity_id = hub_entity_id("sensor", key)


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


class _DestinationSensor(StaminaEntity, SensorEntity):
    """An entity about one destination, on that destination's device."""

    key: str = ""

    def __init__(self, manager: StaminaManager, target: Target, title: str) -> None:
        """Bind to one destination."""
        super().__init__(manager)
        self.target = target
        # `<entry>_<subentry>_quota_used` is the id the first version had: kept, so history
        # survives the move onto the destination's device.
        self._attr_unique_id = f"{manager.entry.entry_id}_{target.subentry_id}_{self.key}"
        self._attr_device_info = destination_device(manager.entry, target.subentry_id, title)
        self._attr_translation_key = self.key

    def _signal(self) -> str:
        """Refresh when the destination's holdings change (commit, evict, drop)."""
        return signal_quota(self.manager.entry.entry_id)


class QuotaSensor(_DestinationSensor):
    """Bytes held on one destination."""

    key = "quota_used"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset(
        {"events", "orphans", "orphan_bytes", "uploaded", "last_error"}
    )

    @property
    def native_value(self) -> int:
        """Return the bytes held."""
        return self.target.usage.used

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the quota and what the holdings are made of."""
        return {
            "destination": self.target.label,
            "quota": self.target.quota,
            "events": len(self.target.usage),
            "orphans": self.target.usage.orphans()[0],
            "orphan_bytes": self.target.usage.orphans()[1],
            "uploaded": self.target.uploaded,
        }


class QuotaPercentSensor(_DestinationSensor):
    """How much of its quota a destination holds."""

    key = "quota_percent"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        """Return the share of the quota in use."""
        quota = self.target.quota
        return None if quota <= 0 else round(100 * self.target.usage.used / quota, 2)


class PendingSensor(_DestinationSensor):
    """What the follow-up window holds in `pending/`, against its cap."""

    key = "pending"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = UnitOfInformation.BYTES
    _attr_suggested_unit_of_measurement = UnitOfInformation.MEGABYTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def _signal(self) -> str:
        """Pending footage changes with every segment: the coalesced update signal."""
        return signal_update(self.manager.entry.entry_id)

    @property
    def native_value(self) -> int:
        """Return the bytes held for the follow-up window."""
        return self.target.usage.follow_up_bytes()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the cap and the share of it in use."""
        cap = self.target.follow_up_cap
        held = self.target.usage.follow_up_bytes()
        return {
            "cap": cap,
            "percent_of_cap": None if cap <= 0 else round(100 * held / cap, 1),
        }


class HealthSensor(_DestinationSensor):
    """Whether a destination is working: `ok`, or `failing` with the last error."""

    key = "health"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "failing"]  # noqa: RUF012 - read-only
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def _signal(self) -> str:
        """Failures happen between holdings changes: the coalesced update signal."""
        return signal_update(self.manager.entry.entry_id)

    @property
    def native_value(self) -> str:
        """Return `ok` or `failing`."""
        return "ok" if self.target.healthy else "failing"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the last error and how many operations failed in a row."""
        return {
            "last_error": self.target.last_error,
            "failures_in_a_row": self.target.errors,
            "over_quota": self.target.over_quota,
        }


DESTINATION_SENSORS: tuple[type[_DestinationSensor], ...] = (
    QuotaSensor,
    QuotaPercentSensor,
    PendingSensor,
    HealthSensor,
)


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
