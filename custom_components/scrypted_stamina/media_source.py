"""Media source "Scrypted events": camera → day → event (architecture §3.6).

An event plays on its own (`auto`: the camera's recording through the plugin, falling back
to a committed cloud copy when the recorder fails or no longer has it), and it also expands
into its two sources, so either can be chosen explicitly: "Recording" and "Cloud copy".
Only what is still there is offered: the recording while the recorder keeps that day, the
cloud copy while one is committed and not evicted. An event with neither is listed (it
happened) but cannot be played; one whose thumbnail has aged out shows none.
The token never leaves Home Assistant: everything plays through the signed views in
`playback.py`.
"""

from __future__ import annotations

import datetime as dt
import time

from homeassistant.components.media_player import BrowseError, MediaClass
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, TITLE
from .index import BrowseRow
from .playback import HLS, play_url, thumbnail_url
from .websocket_api import has_recorder

MAX_EVENTS_PER_DAY = 500
MODES = {"recorder": "Recording", "cloud": "Cloud copy"}


async def async_get_media_source(hass: HomeAssistant) -> MediaSource:
    """Return the Scrypted events media source."""
    return StaminaMediaSource(hass)


class StaminaMediaSource(MediaSource):
    """Events by camera and day."""

    name = "Scrypted events"

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind to Home Assistant."""
        super().__init__(DOMAIN)
        self.hass = hass

    def _entry(self):
        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise BrowseError("Scrypted Stamina is not loaded")
        return entries[0]

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Return a playable URL for one event (and, optionally, one source)."""
        entry = self._entry()
        parts = (item.identifier or "").split("/")
        if len(parts) not in (3, 4):
            raise Unresolvable("not an event")
        record = await entry.runtime_data.index.async_get(parts[2])
        if record is None or record.camera != parts[0]:
            raise Unresolvable("no such event")
        mode = parts[3] if len(parts) == 4 else "auto"
        if mode not in ("auto", *MODES):
            raise Unresolvable("no such source")
        return PlayMedia(play_url(self.hass, entry.entry_id, record.id, mode), HLS)

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse cameras, days, events, sources."""
        entry = self._entry()
        data = entry.runtime_data
        parts = [part for part in (item.identifier or "").split("/") if part]
        if not parts:
            return await self._cameras(data)
        if len(parts) == 1:
            return await self._days(data, parts[0])
        if len(parts) == 2:
            return await self._events(entry.entry_id, data, parts[0], parts[1])
        if len(parts) == 3:
            row = await data.index.async_browse_one(parts[2])
            if row is None:
                raise BrowseError("no such event")
            return self._sources(entry.entry_id, data, parts[0], "/".join(parts), row)
        raise BrowseError("unknown item")

    def _name(self, data, camera: str) -> str:
        status = data.manager.cameras.get(camera)
        return status.device.name if status else f"Camera {camera}"

    @staticmethod
    def _folder(identifier: str | None, title: str, children, child_class=MediaClass.DIRECTORY):
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=child_class,
        )

    async def _cameras(self, data) -> BrowseMediaSource:
        cameras = set(await data.index.async_cameras()) | set(data.manager.cameras)
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=camera,
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=self._name(data, camera),
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.DIRECTORY,
            )
            for camera in sorted(cameras, key=lambda item: self._name(data, item))
        ]
        return self._folder(None, TITLE, children)

    async def _days(self, data, camera: str) -> BrowseMediaSource:
        days: dict[str, int] = {}
        for started in await data.index.async_days(camera):
            day = dt_util.as_local(dt_util.utc_from_timestamp(started)).date().isoformat()
            days[day] = days.get(day, 0) + 1
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{camera}/{day}",
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=f"{day} ({count})",
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.VIDEO,
            )
            for day, count in days.items()
        ]
        return self._folder(camera, self._name(data, camera), children)

    async def _events(self, entry_id: str, data, camera: str, day: str) -> BrowseMediaSource:
        try:
            date = dt.date.fromisoformat(day)
        except ValueError as err:
            raise BrowseError("bad day") from err
        start = dt_util.start_of_local_day(date)
        rows = await data.index.async_browse(
            camera,
            start.timestamp(),
            (start + dt.timedelta(days=1)).timestamp(),
            MAX_EVENTS_PER_DAY,
        )
        children = []
        for row in rows:
            when = dt_util.as_local(dt_util.utc_from_timestamp(row.started))
            mark = " ⚠" if row.unusual else ""
            playable = bool(self._available(data, camera, row))
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"{camera}/{day}/{row.id}",
                    media_class=MediaClass.VIDEO,
                    media_content_type=HLS,
                    title=f"{when:%H:%M:%S} {row.summary}{mark}",
                    can_play=playable,
                    can_expand=playable,
                    thumbnail=self._thumbnail(entry_id, row),
                )
            )
        return self._folder(f"{camera}/{day}", day, children, MediaClass.VIDEO)

    def _available(self, data, camera: str, row: BrowseRow) -> list[str]:
        """Return the sources that still have this event: the recorder, the cloud, both."""
        status = data.manager.cameras.get(camera)
        age_days = (time.time() - row.started) / 86400
        modes = []
        if (
            status is not None
            and has_recorder(status.info.capabilities)
            and age_days <= data.manager.recorder_retention_days
        ):
            modes.append("recorder")
        if row.offsite:
            modes.append("cloud")
        return modes

    def _thumbnail(self, entry_id: str, row: BrowseRow) -> str | None:
        return thumbnail_url(self.hass, entry_id, row.id) if row.thumbnail else None

    def _sources(
        self, entry_id: str, data, camera: str, identifier: str, row: BrowseRow
    ) -> BrowseMediaSource:
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"{identifier}/{mode}",
                media_class=MediaClass.VIDEO,
                media_content_type=HLS,
                title=MODES[mode],
                can_play=True,
                can_expand=False,
                thumbnail=self._thumbnail(entry_id, row),
            )
            for mode in self._available(data, camera, row)
        ]
        return self._folder(identifier, row.id, children, MediaClass.VIDEO)
