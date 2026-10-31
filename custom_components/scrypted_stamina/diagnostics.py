"""Diagnostics: enough to explain a missing upload, nothing that authenticates.

The token is redacted by key and, as a literal, from every string (errors can quote it).
The plugin URL is reduced to its scheme and path, because the host is an address on the
user's network. Linked entity ids are kept — they are what a bug report needs — but no
states or history.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import StaminaConfigEntry
from .const import CONF_TOKEN, CONF_URL
from .redact import scrub_credentials

TO_REDACT = {CONF_TOKEN, "access_token", "refresh_token", "password", "Authorization"}


def _url_shape(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://<host>{parts.path}"


def _scrub(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return scrub_credentials(value, secrets=secrets)
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, secrets) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: StaminaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the config entry."""
    data = entry.runtime_data
    manager = data.manager
    link = manager.link
    secrets = (str(entry.data.get(CONF_TOKEN, "")),)
    report: dict[str, Any] = {
        "entry": {
            **async_redact_data(dict(entry.data), TO_REDACT),
            CONF_URL: _url_shape(str(entry.data.get(CONF_URL, ""))),
        },
        "options": dict(entry.options),
        "destinations": [
            {
                "title": subentry.title,
                **async_redact_data(dict(subentry.data), TO_REDACT),
            }
            for subentry in entry.subentries.values()
        ],
        "link": {
            "connected": manager.link_healthy,
            "seq": link.seq if link else None,
            "epoch": link.epoch if link else None,
            "connects": link.connects if link else 0,
            "last_error": link.last_error if link else None,
            "lost_since": link.lost_since if link else None,
        },
        "uploads": manager.uploads,
        "uploader": {
            "queue": manager.uploader.queue,
            "active": manager.uploader.active,
            "capacity": manager.uploader.capacity,
            "targets": {
                target.label: {
                    "used": target.usage.used,
                    "quota": target.quota,
                    "events": len(target.usage),
                    "follow_up_held": target.usage.follow_up_bytes(),
                    "last_error": target.last_error,
                }
                for target in manager.uploader.targets.values()
            },
        },
        "latency": manager.latency_stats(),
        "cameras": {
            camera: {
                "shared_device": status.device.shared,
                "area": status.device.area_slug,
                "capabilities": status.info.capabilities,
                "relevance": manager.analysis.state(camera),
                "coverage": manager.analysis.coverage(camera),
                "linked": manager.linked(camera),
            }
            for camera, status in manager.cameras.items()
        },
        "open_events": [
            {
                "id": record.id,
                "camera": record.camera,
                "state": str(record.state),
                "segments": record.facts.segments,
                "gaps": record.gaps,
                "notes": record.notes,
            }
            for record in manager.records.values()
        ],
        "indexed_events": await data.index.async_count(),
    }
    return _scrub(report, secrets)
