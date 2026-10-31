"""Scrypted Stamina: flag unusual camera events, keep a cloud copy of the ones that matter.

Scrypted provides the facts and the video; Home Assistant decides and stores. This package
is the Home Assistant half (architecture §3): one config entry per plugin, destinations as
subentries, a manager that runs the link, the decider, scoring and the uploader.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import InsecureUrlError, StaminaApiError, StaminaAuthError, StaminaClient
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
    PLATFORMS,
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

    await hass.config_entries.async_forward_entry_setups(entry, [Platform(p) for p in PLATFORMS])
    async_register_views(hass)
    async_register_commands(hass)
    await async_register_panel(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: StaminaConfigEntry) -> None:
    """Reload when options or subentries change."""
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
