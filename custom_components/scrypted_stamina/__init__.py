"""Scrypted Stamina: flag unusual camera events, keep a cloud copy of the ones that matter.

Scrypted provides the facts and the video; Home Assistant decides and stores. This package
is the Home Assistant half (architecture §3): one config entry per plugin, destinations as
subentries, one managed subentry per area (`areas.py`), a manager that runs the link, the
decider, scoring and the uploader.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from .api import InsecureUrlError, StaminaApiError, StaminaAuthError, StaminaClient
from .areas import area_subentries, slug_of
from .const import (
    CONF_DESTINATION_ENTRY,
    CONF_FINGERPRINT,
    CONF_FOLDER,
    CONF_FOLLOW_UP_CAP_GB,
    CONF_ORPHAN_RETENTION_DAYS,
    CONF_QUOTA_GB,
    CONF_RETENTION_DAYS,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_FOLDER,
    DEFAULT_FOLLOW_UP_CAP_GB,
    DEFAULT_ORPHAN_RETENTION_DAYS,
    DEFAULT_QUOTA_GB,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    INDEX_FILENAME,
    LEGACY_UNUSUAL_OVERRIDES,
    PLATFORMS,
    SUBENTRY_TYPE_AREA,
    SUBENTRY_TYPE_DESTINATION,
)
from .destinations import DestinationError, async_create_destination
from .index import EventIndex
from .manager import StaminaManager
from .panel import async_register_panel, async_unregister_panel
from .playback import async_register_views
from .uploader import Target, Uploader
from .websocket_api import async_register_commands

_LOGGER = logging.getLogger(__name__)

_GB = 1024**3

type StaminaConfigEntry = ConfigEntry[StaminaData]


@dataclass(slots=True)
class StaminaData:
    """What one config entry runs."""

    client: StaminaClient
    index: EventIndex
    manager: StaminaManager
    # What a change must touch to need a reload (`_reload_key`).
    reload_key: Any = None


def _targets(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Target]:
    """Build one upload target per destination subentry that can be served now."""
    targets: dict[str, Target] = {}
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_DESTINATION:
            continue
        data = subentry.data
        owner = hass.config_entries.async_get_entry(str(data.get(CONF_DESTINATION_ENTRY)))
        if owner is None:
            _LOGGER.warning("Destination %s: its storage entry is gone", subentry.title)
            continue
        try:
            destination = async_create_destination(hass, owner)
        except DestinationError as err:
            _LOGGER.warning("Destination %s cannot be used: %s", subentry.title, err)
            continue
        targets[subentry.subentry_id] = Target(
            subentry_id=subentry.subentry_id,
            destination=destination,
            folder=str(data.get(CONF_FOLDER, DEFAULT_FOLDER)),
            quota=int(float(data.get(CONF_QUOTA_GB, DEFAULT_QUOTA_GB)) * _GB),
            retention_days=float(data.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)),
            follow_up_cap=int(
                float(data.get(CONF_FOLLOW_UP_CAP_GB, DEFAULT_FOLLOW_UP_CAP_GB)) * _GB
            ),
            orphan_retention_days=float(
                data.get(CONF_ORPHAN_RETENTION_DAYS, DEFAULT_ORPHAN_RETENTION_DAYS)
            ),
        )
    return targets


async def async_setup_entry(hass: HomeAssistant, entry: StaminaConfigEntry) -> bool:
    """Connect to the plugin and start deciding."""
    try:
        client = StaminaClient(
            hass,
            entry.data[CONF_URL],
            entry.data[CONF_TOKEN],
            verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            fingerprint=entry.data.get(CONF_FINGERPRINT),
        )
    except InsecureUrlError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    try:
        await client.async_health()
    except StaminaAuthError as err:
        raise ConfigEntryAuthFailed("the Stamina plugin refused the token") from err
    except StaminaApiError as err:
        raise ConfigEntryNotReady(f"the Stamina plugin is not reachable: {err}") from err

    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_open()

    def _uploader(manager: StaminaManager) -> Uploader:
        return Uploader(
            hass,
            client,
            index,
            _targets(hass, entry),
            on_change=manager._changed,
            on_uploaded=manager.on_uploaded,
            on_latency=manager.record_latency,
            on_quota=manager.on_quota,
            on_over_quota=manager.on_over_quota,
            before_release=manager.async_final_thumbnail,
        )

    manager = StaminaManager(hass, entry, client, index, _uploader)
    entry.runtime_data = StaminaData(client=client, index=index, manager=manager)
    try:
        await manager.async_start()
    except Exception:
        await manager.async_stop()
        await index.async_close()
        raise

    _async_drop_old_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, [Platform(p) for p in PLATFORMS])
    async_register_views(hass)
    async_register_commands(hass)
    await async_register_panel(hass, entry)
    entry.runtime_data.reload_key = _reload_key(entry)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


@callback
def _async_drop_old_entities(hass: HomeAssistant, entry: StaminaConfigEntry) -> None:
    """Remove what the per-area Uploads selects replaced, and what the old select did.

    The integration-wide `select.stamina_uploads`, and before it a switch and its option.
    Run after the manager has started, which reads the old option once to migrate the
    switch's state, and the old select's position as every area's default
    (`StaminaManager._async_restore_uploads`). Idempotent.
    """
    registry = er.async_get(hass)
    for domain, unique_id in (
        ("switch", f"{entry.entry_id}_uploads"),
        ("select", f"{entry.entry_id}_uploads_mode"),
    ):
        if old := registry.async_get_entity_id(domain, DOMAIN, unique_id):
            registry.async_remove(old)
            if domain == "select":
                _async_tell_uploads_moved(hass, entry)
    if LEGACY_UNUSUAL_OVERRIDES in entry.options:
        options = {k: v for k, v in entry.options.items() if k != LEGACY_UNUSUAL_OVERRIDES}
        hass.config_entries.async_update_entry(entry, options=options)


@callback
def _async_tell_uploads_moved(hass: HomeAssistant, entry: StaminaConfigEntry) -> None:
    """Raise a repair issue, once, naming the per-area selects the global one became.

    Raised when the old select is removed, which happens once; the user dismisses it.
    """
    registry = er.async_get(hass)
    selects = []
    for key, subentry in sorted(area_subentries(entry).items(), key=lambda i: i[1].title):
        unique_id = f"{entry.entry_id}_area_{key}_uploads"
        entity_id = registry.async_get_entity_id("select", DOMAIN, unique_id)
        selects.append(entity_id or f"select.stamina_{slug_of(subentry).replace('-', '_')}_uploads")
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"uploads_per_area_{entry.entry_id}",
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="uploads_per_area",
        translation_placeholders={"selects": ", ".join(selects) or "none yet"},
    )


def _reload_key(entry: ConfigEntry) -> tuple[Any, ...]:
    """Return what a reload depends on: everything but the area subentries.

    Those the integration manages itself while running; adding or renaming one must not
    restart the link.
    """
    return (
        dict(entry.data),
        dict(entry.options),
        {
            subentry_id: (subentry.subentry_type, subentry.title, dict(subentry.data))
            for subentry_id, subentry in entry.subentries.items()
            if subentry.subentry_type != SUBENTRY_TYPE_AREA
        },
    )


async def _async_reload(hass: HomeAssistant, entry: StaminaConfigEntry) -> None:
    """Reload when options or destinations change; re-sync when only areas did.

    An area subentry deleted by hand comes back at once.
    """
    data = getattr(entry, "runtime_data", None)
    if data is not None and data.reload_key == _reload_key(entry):
        data.manager.async_sync_areas()
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: StaminaConfigEntry) -> bool:
    """Stop everything, keeping the index."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform(p) for p in PLATFORMS]
    )
    data = getattr(entry, "runtime_data", None)
    if unloaded:
        async_unregister_panel(hass)
    if unloaded and data is not None:
        await data.manager.async_stop()
        await data.index.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the index: it records when a household is in and out."""
    index = EventIndex(hass, hass.config.path(INDEX_FILENAME))
    await index.async_delete()


__all__ = ["DOMAIN", "StaminaConfigEntry", "StaminaData"]
