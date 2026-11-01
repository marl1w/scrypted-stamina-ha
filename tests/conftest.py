"""Shared fixtures: a fake Stamina plugin (aiohttp test server) and a fake destination.

The fake plugin speaks `docs/api.md`: bearer auth, `/public/v1/...`, an SSE stream with a
`hello` and resumable `seq`/`epoch`, 0-based segments with 404/425, ack, thumbnails, the
event state route, and playback that redirects to a session. Tests drive it by pushing
messages and watching what the integration does.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable
import json
import re
import time
from typing import Any
from unittest.mock import patch

from aiohttp import web
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scrypted_stamina.const import (
    CONF_DESTINATION_ENTRY,
    CONF_FOLDER,
    CONF_FOLLOW_UP_CAP_GB,
    CONF_QUOTA_GB,
    CONF_RETENTION_DAYS,
    CONF_TOKEN,
    CONF_URL,
    DOMAIN,
    INDEX_FILENAME,
    SUBENTRY_TYPE_DESTINATION,
)
from custom_components.scrypted_stamina.destinations import Destination, DestinationError
from custom_components.scrypted_stamina.destinations.base import gather

pytest_plugins = "pytest_homeassistant_custom_component"

TOKEN = "test-token-0123456789"
CAMERA = "38"
EPOCH = "ep_first"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make the custom integration loadable in every test."""
    return enable_custom_integrations


@pytest.fixture
def hass_config_dir(tmp_path) -> str:
    """Give every test an empty config dir of its own: concurrent runs never share an index."""
    return str(tmp_path)


@pytest.fixture(autouse=True)
def clean_index(hass: HomeAssistant):
    """Remove the index after every test, wherever its config dir is."""
    yield
    from pathlib import Path

    for suffix in ("", "-wal", "-shm"):
        Path(f"{hass.config.path(INDEX_FILENAME)}{suffix}").unlink(missing_ok=True)


def event_id(n: int) -> str:
    """Return a valid event id (`ev_` plus 22 characters)."""
    return f"ev_{n:022d}"


def subject(sid: str, cls: str = "person", verdict: str = "pending", **extra: Any) -> dict:
    """Return a subject object as the plugin sends it."""
    now = int(time.time() * 1000)
    return {
        "id": sid,
        "class": cls,
        "family": cls,
        "first": extra.pop("first", now - 3000),
        "last": extra.pop("last", now),
        "sources": extra.pop("sources", {"@marl1w/scrypted-reolink": {"role": "trigger"}}),
        "max_score": extra.pop("max_score", 0.9),
        "verdict": verdict,
        **extra,
    }


def event_object(
    eid: str,
    *,
    camera: str = CAMERA,
    start: float | None = None,
    end: float | None = None,
    subjects: list[dict] | None = None,
    next_segment: int = 0,
    init: bool = False,
    segments_state: str = "running",
    flags: dict | None = None,
    context: dict | None = None,
) -> dict:
    """Return an `event` object as the plugin sends it."""
    start = start or time.time()
    return {
        "id": eid,
        "camera": {"id": camera, "name": "Front door"},
        "state": "closed" if end else "open",
        "start": int(start * 1000),
        "end": int(end * 1000) if end else None,
        "trigger": {"kind": "object", "class": "person"},
        "subjects": subjects or [],
        "flags": {"camera_offline": False, **(flags or {})},
        "latency": {"camera_event_time": int(start * 1000)},
        "context": context or {"at_start": {"night_mode": False}, "current": {}},
        "segments": {"state": segments_state, "init": init, "next": next_segment},
    }


class FakeStamina:
    """An in-memory Stamina plugin."""

    def __init__(self) -> None:
        self.epoch = EPOCH
        self.messages: list[dict] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.not_ready: set[tuple[str, str]] = set()
        self.acks: list[tuple[str, int]] = []
        self.thumbnails: dict[str, bytes] = {}
        self.event_state: dict[str, dict] = {}
        self.stream_requests: list[dict[str, str]] = []
        self.last_event_ids: list[str] = []
        self.health_status = 200
        self.stream_status = 200
        self.playback_status = 302
        self.sessions = 0
        self.stopped_sessions: set[str] = set()
        # Held closed to keep a reconnecting client from finishing its replay.
        self.replay_gate: asyncio.Event | None = None
        self.event_status = 200
        self.forgotten = False
        self.recoverable: list[dict] = []
        self.fetches: list[tuple[str, str]] = []
        self.redirect_to = ""
        self.gap_next = False
        self.cameras = [
            {
                "id": CAMERA,
                "name": "Front door",
                "online": True,
                "capabilities": {"video_recorder": True, "prebuffer": True},
                "sources": [{"source": "@marl1w/scrypted-reolink", "role": "trigger"}],
            }
        ]
        self._wake = asyncio.Event()
        self._generation = 0
        self.url = ""

    # ------------------------------------------------------------- driving

    def push(self, message_type: str, **fields: Any) -> int:
        """Publish one message; return its seq."""
        seq = (self.messages[-1]["seq"] + 1) if self.messages else 1
        message = {"v": 1, "seq": seq, "epoch": self.epoch, "type": message_type, **fields}
        self.messages.append(message)
        self._wake.set()
        return seq

    def start(self, eid: str, **kwargs: Any) -> int:
        return self.push("event.start", event=event_object(eid, **kwargs), observations=[])

    def update(self, eid: str, **kwargs: Any) -> int:
        return self.push("event.update", event=event_object(eid, **kwargs), observations=[])

    def end(self, eid: str, **kwargs: Any) -> int:
        kwargs.setdefault("end", time.time())
        kwargs.setdefault("segments_state", "ended")
        return self.push("event.end", event=event_object(eid, **kwargs))

    def verdict(self, eid: str, sub: dict) -> int:
        return self.push("subject.verdict", event_id=eid, camera_id=CAMERA, subject=sub)

    def cut(self, eid: str, n: int, *, init: bool = False) -> None:
        """Cut one segment without announcing it (the plugin's side only)."""
        self.files[(eid, f"{n}.m4s")] = f"seg{n}".encode() * 10
        if init:
            self.files[(eid, "init.mp4")] = b"init-" + eid.encode()

    def segment(
        self,
        eid: str,
        n: int,
        data: bytes | None = None,
        *,
        init: bool = False,
        last: bool = False,
    ) -> int:
        """Cut one segment (and init with it when asked), and announce it."""
        self.cut(eid, n, init=init)
        if data is not None:
            self.files[(eid, f"{n}.m4s")] = data
        segment = {"n": n, "init": 0, "discontinuity": False, "duration_s": 2.0, "bytes": 50}
        seq = self.push(
            "event.segment",
            event_id=eid,
            camera_id=CAMERA,
            kind="cut",
            last=False,
            segment=segment,
        )
        if last:
            seq = self.last(eid, n + 1)
        return seq

    def last(self, eid: str, following: int) -> int:
        """Announce that no more segments will come (docs/api.md, `kind: last`)."""
        return self.push(
            "event.segment",
            event_id=eid,
            camera_id=CAMERA,
            kind="last",
            last=True,
            segment=None,
            next=following,
            state="ended",
            reason=None,
        )

    def restart_segmenter(self, eid: str, n: int, k: int) -> int:
        """Cut segment n after an ffmpeg restart: it needs init-<k>.mp4."""
        self.files[(eid, f"{n}.m4s")] = f"seg{n}".encode() * 10
        self.files[(eid, f"init-{k}.mp4")] = f"init{k}".encode()
        segment = {"n": n, "init": k, "discontinuity": True, "duration_s": 2.0, "bytes": 50}
        return self.push(
            "event.segment", event_id=eid, camera_id=CAMERA, kind="cut", last=False, segment=segment
        )

    def evict(self, eid: str, numbers: list[int]) -> int:
        """Drop segments for memory before they were fetched."""
        for n in numbers:
            self.files.pop((eid, f"{n}.m4s"), None)
        return self.push(
            "event.segment",
            event_id=eid,
            camera_id=CAMERA,
            kind="evicted",
            last=False,
            segment=None,
            evicted=numbers,
        )

    def restart(self, epoch: str) -> None:
        """Simulate a plugin restart: a new epoch, an empty ring."""
        self.epoch = epoch
        self.messages = []
        self.files = {}
        self.forgotten = True  # a restarted plugin knows none of the old events
        self.drop_streams()

    def drop_streams(self) -> None:
        """Close every open SSE stream."""
        self._generation += 1
        self._wake.set()

    # ------------------------------------------------------------- routes

    def _authorised(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def health(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.health_status != 200:
            return web.json_response({"error": "internal_error"}, status=self.health_status)
        return web.json_response({"ok": True, "api": 1, "epoch": self.epoch})

    async def list_cameras(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"cameras": self.cameras})

    async def stream(self, request: web.Request) -> web.StreamResponse:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.stream_status != 200:
            return web.json_response({"error": "internal_error"}, status=self.stream_status)
        self.stream_requests.append(dict(request.query))
        since = request.query.get("since")
        epoch = request.query.get("epoch")
        if last := request.headers.get("Last-Event-ID"):
            # The standard header wins over the query (docs/api.md).
            epoch, _, since = last.rpartition(":")
            self.last_event_ids.append(last)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        reset = bool(epoch) and epoch != self.epoch
        gap, self.gap_next = self.gap_next, False
        last_seq = self.messages[-1]["seq"] if self.messages else 0
        hello = {
            "v": 1,
            "type": "hello",
            "epoch": self.epoch,
            "last_seq": last_seq,
            "reset": reset,
            "gap": gap,
        }
        await response.write(f"event: hello\ndata: {json.dumps(hello)}\n\n".encode())
        if self.replay_gate is not None:
            await self.replay_gate.wait()
        if since is None and epoch is None:
            sent = self.messages[-1]["seq"] if self.messages else 0
        elif reset:
            sent = 0
        else:
            sent = int(since or 0)
        generation = self._generation
        while generation == self._generation:
            for message in [m for m in self.messages if m["seq"] > sent]:
                payload = json.dumps(message)
                await response.write(
                    f"id: {self.epoch}:{message['seq']}\nevent: {message['type']}\n"
                    f"data: {payload}\n\n".encode()
                )
                sent = message["seq"]
            self._wake.clear()
            try:
                async with asyncio.timeout(0.5):
                    await self._wake.wait()
            except TimeoutError:
                await response.write(b": keepalive\n\n")
        return response

    async def segment_file(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        key = (request.match_info["eid"], request.match_info["name"])
        self.fetches.append(key)
        if key in self.not_ready:
            return web.json_response({"error": "not_yet_available"}, status=425)
        if key not in self.files:
            return web.json_response({"error": "not_found"}, status=404)
        return web.Response(body=self.files[key], content_type="video/iso.segment")

    async def event(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.event_status != 200:
            return web.json_response({"error": "internal_error"}, status=self.event_status)
        eid = request.match_info["eid"]
        if self.forgotten and eid not in self.event_state:
            return web.json_response({"error": "not_found"}, status=404)
        state = self.event_state.get(eid, {"segments": {"live": False}})
        return web.json_response({"id": eid, **state})

    async def events_since(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"events": self.recoverable})

    async def redirect(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        raise web.HTTPFound(location=self.redirect_to)

    async def ack(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        body = await request.json()
        self.acks.append((request.match_info["eid"], int(body["upTo"])))
        return web.json_response({"ok": True})

    async def thumbnail(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        data = self.thumbnails.get(request.match_info["eid"])
        if data is None:
            return web.json_response({"error": "not_found"}, status=404)
        return web.Response(body=data, content_type="image/jpeg")

    async def playback(self, request: web.Request) -> web.Response:
        if not self._authorised(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        if self.playback_status != 302:
            return web.json_response({"error": "playback_failed"}, status=self.playback_status)
        self.sessions += 1
        raise web.HTTPFound(location=f"playback/pb_{self.sessions:022d}/index.m3u8")

    async def session_file(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if request.match_info["sid"] in self.stopped_sessions:
            return web.json_response({"error": "not_found"}, status=404)
        if name == "index.m3u8":
            text = (
                '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2.0,\n0.m4s\n'
                "#EXTINF:2.0,\nhttp://192.0.2.9/evil.m4s\n#EXT-X-ENDLIST\n"
            )
            return web.Response(text=text, content_type="application/vnd.apple.mpegurl")
        if name == "cut.m4s":
            # Declares more than it sends, then drops the connection.
            response = web.StreamResponse(headers={"Content-Type": "video/iso.segment"})
            response.content_length = 100_000
            await response.prepare(request)
            await response.write(b"x" * 1000)
            request.transport.close()
            return response
        if name.endswith(".html"):
            return web.Response(text="<script>alert(1)</script>", content_type="text/html")
        return web.Response(body=b"media-" + name.encode(), content_type="video/mp4")

    def app(self) -> web.Application:
        app = web.Application()
        base = "/endpoint/@marl1w/scrypted-stamina/public/v1"
        app.router.add_get(f"{base}/health", self.health)
        app.router.add_get(f"{base}/cameras", self.list_cameras)
        app.router.add_get(f"{base}/events/stream", self.stream)
        app.router.add_get(f"{base}/events", self.events_since)
        app.router.add_get(f"{base}/cameras/{{cam}}/redirect.m3u8", self.redirect)
        app.router.add_get(f"{base}/events/{{eid}}", self.event)
        app.router.add_get(f"{base}/events/{{eid}}/thumbnail.jpg", self.thumbnail)
        app.router.add_post(f"{base}/events/{{eid}}/ack", self.ack)
        app.router.add_get(f"{base}/events/{{eid}}/{{name}}", self.segment_file)
        app.router.add_get(f"{base}/cameras/{{cam}}/playback.m3u8", self.playback)
        app.router.add_get(f"{base}/cameras/{{cam}}/playback/{{sid}}/{{name}}", self.session_file)
        return app


@pytest.fixture
async def stamina(socket_enabled, aiohttp_server) -> FakeStamina:
    """Start a fake Stamina plugin on 127.0.0.1."""
    fake = FakeStamina()
    server = await aiohttp_server(fake.app())
    fake.url = str(server.make_url("/endpoint/@marl1w/scrypted-stamina"))
    yield fake
    fake.drop_streams()


class FakeDestination(Destination):
    """An in-memory destination, recording what it was asked to do."""

    def __init__(self, *, name: str = "Fake") -> None:
        self.files: dict[str, bytes] = {}
        self.ops: list[tuple[str, str]] = []
        self.fail: set[str] = set()
        self._name = name

    @property
    def label(self) -> str:
        return self._name

    async def async_store(self, path: str, data: bytes) -> None:
        if any(part in path for part in self.fail):
            raise DestinationError(f"refused {path}")
        self.ops.append(("store", path))
        self.files[path] = data

    async def async_store_stream(self, path: str, size: int, chunks: AsyncIterable[bytes]) -> None:
        await self.async_store(path, await gather(chunks, size))

    async def async_delete(self, path: str) -> None:
        self.ops.append(("delete", path))
        self.files.pop(path, None)

    async def async_list(self, folder: str) -> dict[str, int]:
        prefix = f"{folder}/"
        return {
            path: len(data)
            for path, data in self.files.items()
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        }

    async def async_read(self, path: str) -> bytes:
        self.ops.append(("read", path))
        if path not in self.files:
            raise DestinationError(f"no {path}")
        return self.files[path]

    async def async_read_range(self, path: str, start: int, end: int) -> bytes:
        self.ops.append(("read_range", path))
        if path not in self.files:
            raise DestinationError(f"no {path}")
        return self.files[path][start : end + 1]

    async def async_delete_folder(self, folder: str) -> None:
        self.ops.append(("delete_folder", folder))
        for path in [p for p in self.files if p.startswith(f"{folder}/")]:
            del self.files[path]

    def under(self, prefix: str) -> list[str]:
        return sorted(path for path in self.files if path.startswith(prefix))

    def videos(self, prefix: str = "scrypted-stamina/events/") -> list[str]:
        """Return the committed videos under `prefix`."""
        return [
            path for path in self.under(prefix) if path.endswith(".mp4") and "/pending/" not in path
        ]


def joined(fake: FakeDestination, prefix: str = "scrypted-stamina/events/") -> set[str]:
    """Return the files joined into the videos under `prefix`, told apart by their content.

    `FakeStamina` cuts segment n as `seg<n>` repeated, init as `init-<event id>` and a later
    init k as `init<k>`.
    """
    found: set[str] = set()
    for path in fake.videos(prefix):
        data = fake.files[path]
        if b"init-ev_" in data:
            found.add("init.mp4")
        found |= {f"init-{int(k)}.mp4" for k in re.findall(rb"init(\d+)", data)}
        found |= {f"{int(n):05d}.m4s" for n in re.findall(rb"seg(\d+)", data)}
    return found


async def until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait (in real time) for a condition the integration reaches asynchronously."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def make_entry(
    hass: HomeAssistant,
    stamina: FakeStamina,
    *,
    options: dict | None = None,
    destinations: int = 1,
    quota_gb: float = 15,
    follow_up_cap_gb: float = 1.0,
) -> MockConfigEntry:
    """Add the config entry (and storage entries for its destinations) to hass."""
    subentries = []
    for n in range(destinations):
        owner = MockConfigEntry(domain="webdav", entry_id=f"storage{n}", title=f"NAS {n}")
        owner.add_to_hass(hass)
        subentries.append(
            ConfigSubentryData(
                data={
                    CONF_DESTINATION_ENTRY: f"storage{n}",
                    CONF_FOLDER: "scrypted-stamina",
                    CONF_QUOTA_GB: quota_gb,
                    CONF_RETENTION_DAYS: 0,
                    CONF_FOLLOW_UP_CAP_GB: follow_up_cap_gb,
                },
                subentry_type=SUBENTRY_TYPE_DESTINATION,
                title=f"WebDAV — NAS {n}",
                unique_id=f"storage{n}",
            )
        )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Scrypted Stamina",
        unique_id=DOMAIN,
        data={CONF_URL: stamina.url, CONF_TOKEN: TOKEN},
        options=options or {},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def destinations() -> dict[str, FakeDestination]:
    """The fake destinations, by storage entry id."""
    return {"storage0": FakeDestination(name="NAS 0"), "storage1": FakeDestination(name="NAS 1")}


@pytest.fixture
def setup(
    hass: HomeAssistant, stamina: FakeStamina, destinations: dict[str, FakeDestination]
) -> Callable[..., Awaitable[MockConfigEntry]]:
    """Return a coroutine that sets the integration up against the fake plugin."""

    async def _setup(entry: MockConfigEntry | None = None, **kwargs: Any) -> MockConfigEntry:
        entry = entry or make_entry(hass, stamina, **kwargs)
        with patch(
            "custom_components.scrypted_stamina.async_create_destination",
            side_effect=lambda _hass, owner: destinations[owner.entry_id],
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        manager = entry.runtime_data.manager
        await until(lambda: manager.link_healthy)
        return entry

    return _setup


@pytest.fixture
def expected_lingering_timers() -> bool:
    """The link's reconnect and keepalive timers may outlive a test by a moment."""
    return True


@pytest.fixture
def reload(
    hass: HomeAssistant, destinations: dict[str, FakeDestination]
) -> Callable[[MockConfigEntry], Awaitable[None]]:
    """Return a coroutine that restarts the integration (an HA restart, for its state)."""

    async def _reload(entry: MockConfigEntry) -> None:
        with patch(
            "custom_components.scrypted_stamina.async_create_destination",
            side_effect=lambda _hass, owner: destinations[owner.entry_id],
        ):
            assert await hass.config_entries.async_reload(entry.entry_id)
            await hass.async_block_till_done()
        await until(lambda: entry.runtime_data.manager.link_healthy)

    return _reload


class Clock:
    """A clock the manager reads, moved forward by tests together with HA's timers."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    async def advance(self, hass: HomeAssistant, seconds: float) -> None:
        from datetime import timedelta

        from homeassistant.util import dt as dt_util
        from pytest_homeassistant_custom_component.common import async_fire_time_changed

        self.offset += seconds
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=self.offset))
        await hass.async_block_till_done()
