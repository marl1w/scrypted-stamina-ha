"""The review panel in the sidebar: static files and the `panel_custom` registration.

The frontend is a few plain ES modules, no build step (as Reolink Stamina's). They are
served under a URL named after the directory's contents, so editing any module changes
every module's URL and no browser keeps a stale one: the modules import each other by
relative path, so a query string on the entry point alone would bust nothing else.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, TITLE

PANEL_URL_PATH = "scrypted-stamina"
PANEL_COMPONENT = "scrypted-stamina-panel"
PANEL_ICON = "mdi:motion-play-outline"
STATIC_PREFIX = f"/{DOMAIN}_panel"
FRONTEND_DIR = Path(__file__).parent / "frontend"

_STATIC = f"{DOMAIN}_panel_static"


def frontend_fingerprint(path: Path = FRONTEND_DIR) -> str:
    """Return a short digest of the frontend directory: names, sizes and times."""
    digest = hashlib.sha256()
    for file in sorted(path.rglob("*")):
        if not file.is_file():
            continue
        stat = file.stat()
        digest.update(str(file.relative_to(path)).encode())
        digest.update(f"{stat.st_size}:{int(stat.st_mtime)}".encode())
    return digest.hexdigest()[:12]


async def async_register_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Serve the frontend and add the sidebar panel (again, after a reload)."""
    fingerprint = await hass.async_add_executor_job(frontend_fingerprint, FRONTEND_DIR)
    static_url = f"{STATIC_PREFIX}/{fingerprint}"
    # A static route cannot be removed, so each fingerprint is registered once per run.
    served: set[str] = hass.data.setdefault(_STATIC, set())
    if static_url not in served:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(static_url, str(FRONTEND_DIR), True)]
        )
        served.add(static_url)

    async_unregister_panel(hass)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        module_url=f"{static_url}/{PANEL_COMPONENT}.js",
        sidebar_title=TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=True,
        embed_iframe=False,
        config={"entry_id": entry.entry_id},
    )


@callback
def async_unregister_panel(hass: HomeAssistant) -> None:
    """Remove the sidebar panel, if it is there."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
