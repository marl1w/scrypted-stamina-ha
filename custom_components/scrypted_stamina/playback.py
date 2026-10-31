"""Playback through Home Assistant, with the plugin's token kept server-side.

Views, all answering only URLs this module signed:

* **Play** — `/api/scrypted_stamina/play/<entry>/<event>.m3u8?mode=auto|recorder|cloud` is
  what the media source hands out. `auto` tries the camera's recording through the plugin
  and falls back to a committed cloud copy when the recorder fails, has no recording that
  old, or the camera has none.
* **Recording proxy** — `/api/scrypted_stamina/proxy/<entry>/<plugin path>` forwards a GET
  to the plugin with the bearer token added here. Redirects (playback answers with one to a
  session) are followed by the API client, same origin only. Playlists are rewritten so
  every URI points back here with a fresh signature; media is streamed through.
* **Cloud copy** — the play view answers with a playlist of byte ranges of the committed
  video (`hls.py`); `/api/scrypted_stamina/cloud/<entry>/<destination>/<event>/video.mp4`
  serves those ranges, each read from the destination as that range only (in chunks of
  `VIDEO_CHUNK`), so neither a long video nor several players are ever held in memory.
* **Thumbnail** — the event's 320 px JPEG from the index.

Why signatures rather than Home Assistant auth: a media player fetches segments itself and
cannot add a bearer header. The signature is an HMAC over the path and an expiry, with a key
that lives only in memory, so a URL is useless outside its window and after a restart.

Hardening: the proxy forwards only plugin paths under `/v1/cameras/<id>/` and
`/v1/events/<id>/`; a playlist URI that cannot be rewritten to one of those is dropped, not
passed through; content types are an allowlist (anything else is `octet-stream`); every
response says `nosniff` and `sandbox`; playlists and cloud reads have size limits.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
import hashlib
import hmac
import posixpath
import re
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlsplit

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from .api import StaminaApiError, async_read_limited, playback_path
from .const import DOMAIN, MAX_SEGMENT_BYTES
from .destinations import DestinationError
from .uploader import video_playlist

if TYPE_CHECKING:
    from . import StaminaData
    from .decider import EventRecord
    from .destinations import Destination
    from .index import Copy

PLAY_PREFIX = f"/api/{DOMAIN}/play"
PROXY_PREFIX = f"/api/{DOMAIN}/proxy"
CLOUD_PREFIX = f"/api/{DOMAIN}/cloud"
THUMBNAIL_PREFIX = f"/api/{DOMAIN}/thumbnail"
URL_LIFETIME = 6 * 3600
# A play URL starts recorder sessions: it is kept short-lived.
PLAY_URL_LIFETIME = 3600
# A signed thumbnail URL is itself the capability, so the picture may be cached privately.
THUMBNAIL_CACHE = "private, max-age=3600"
MAX_PLAYLIST_BYTES = 1024 * 1024
# A committed video (about 70 MB an hour) is never read whole: each range a player asks for
# is read from the destination as that range, at most this much at a time.
VIDEO_NAME = "video.mp4"
VIDEO_CHUNK = 4 * 1024 * 1024
# Seconds of recording either side of an event, for in-place playback.
PLAYBACK_LEAD = 5.0
PLAYBACK_TAIL = 5.0

HLS = "application/vnd.apple.mpegurl"
ALLOWED_TYPES = frozenset(
    {"video/mp4", "video/iso.segment", "video/mp2t", "audio/mp4", HLS, "image/jpeg"}
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
    "Cache-Control": "no-store",
}

_KEY = f"{DOMAIN}_signing_key"
_VIEWS = f"{DOMAIN}_views"
# Recording playback (`playback.m3u8`), its session files, and event segments.
_ALLOWED = re.compile(
    r"^v1/(cameras/[A-Za-z0-9_-]{1,32}(/playback/pb_[A-Za-z0-9_-]{22})?"
    r"|events/ev_[A-Za-z0-9_-]{22})/[A-Za-z0-9_.-]{1,64}$"
)
_URI_ATTR = re.compile(r'URI="([^"]+)"')
# A playback session's playlist path, and which event it plays (to start a new session when
# the plugin stopped this one: it does after 2 minutes paused, or when memory runs short).
_SESSION = re.compile(
    r"^v1/cameras/[A-Za-z0-9_-]{1,32}/playback/(pb_[A-Za-z0-9_-]{22})/index\.m3u8$"
)
_SESSIONS = f"{DOMAIN}_sessions"
_MAX_SESSIONS = 64


def _key(hass: HomeAssistant) -> bytes:
    key = hass.data.get(_KEY)
    if key is None:
        key = hass.data[_KEY] = secrets.token_bytes(32)
    return key


def _signature(hass: HomeAssistant, path: str, expires: int) -> str:
    return hmac.new(_key(hass), f"{path}|{expires}".encode(), hashlib.sha256).hexdigest()


def sign(
    hass: HomeAssistant,
    path: str,
    query: str = "",
    lifetime: int = URL_LIFETIME,
    cap: int | None = None,
) -> str:
    """Return `path` (absolute, under /api) with an expiry and signature appended.

    `cap` is the expiry of the URL this one was derived from (a segment URL inside a signed
    playlist): a derived URL never outlives its parent, so nothing is re-minted after it.
    """
    expires = int(time.time()) + lifetime
    if cap is not None:
        expires = min(expires, cap)
    target = f"{path}?{query}" if query else path
    joiner = "&" if query else "?"
    return f"{target}{joiner}exp={expires}&sig={_signature(hass, target, expires)}"


def request_expiry(request: web.Request) -> int | None:
    """Return the expiry a (verified) request was signed with."""
    try:
        return int(request.query.get("exp", ""))
    except ValueError:
        return None


def verify(hass: HomeAssistant, request: web.Request) -> bool:
    """Return whether a request carries a valid, unexpired signature for its own URL."""
    params = list(parse_qsl(request.query_string, keep_blank_values=True))
    rest = [(k, v) for k, v in params if k not in ("exp", "sig")]
    found = dict(params)
    try:
        expires = int(found.get("exp", "0"))
    except ValueError:
        return False
    if expires < time.time():
        return False
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in rest)
    target = f"{request.path}?{query}" if query else request.path
    return hmac.compare_digest(_signature(hass, target, expires), found.get("sig", ""))


def proxy_url(
    hass: HomeAssistant, entry_id: str, plugin_path: str, cap: int | None = None
) -> str | None:
    """Return a signed proxy URL for a plugin path (with its query), or None if not allowed."""
    parts = urlsplit(plugin_path)
    path = parts.path.lstrip("/")
    if not _ALLOWED.match(path) or ".." in path:
        return None
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in parse_qsl(parts.query))
    return sign(hass, f"{PROXY_PREFIX}/{entry_id}/{path}", query, cap=cap)


def cloud_url(
    hass: HomeAssistant,
    entry_id: str,
    subentry: str,
    event_id: str,
    name: str,
    cap: int | None = None,
) -> str:
    """Return a signed URL for one file of a committed event."""
    return sign(hass, f"{CLOUD_PREFIX}/{entry_id}/{subentry}/{event_id}/{name}", cap=cap)


def play_url(
    hass: HomeAssistant, entry_id: str, event_id: str, mode: str = "auto", cap: int | None = None
) -> str:
    """Return a signed URL that plays an event (`auto`, `recorder` or `cloud`)."""
    return sign(
        hass,
        f"{PLAY_PREFIX}/{entry_id}/{event_id}.m3u8",
        f"mode={mode}",
        PLAY_URL_LIFETIME,
        cap=cap,
    )


def thumbnail_url(hass: HomeAssistant, entry_id: str, event_id: str) -> str:
    """Return a signed URL for an event's stored thumbnail."""
    return sign(hass, f"{THUMBNAIL_PREFIX}/{entry_id}/{event_id}.jpg")


def rewrite_playlist(text: str, resolve: Callable[[str], str | None]) -> str:
    """Rewrite every URI in a playlist; drop the lines whose URI cannot be rewritten."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            unresolved = False

            def _attr(match: re.Match[str]) -> str:
                nonlocal unresolved
                url = resolve(match.group(1))
                if url is None:
                    unresolved = True
                    return match.group(0)
                return f'URI="{url}"'

            line = _URI_ATTR.sub(_attr, line)
            if unresolved:
                continue
        elif stripped:
            url = resolve(stripped)
            if url is None:
                continue
            line = url
        lines.append(line)
    return "\n".join(lines) + "\n"


def safe_type(content_type: str | None) -> str:
    """Return a content type from the allowlist, or `application/octet-stream`."""
    value = (content_type or "").split(";")[0].strip().lower()
    if value in ("application/x-mpegurl", "audio/mpegurl"):
        return HLS
    return value if value in ALLOWED_TYPES else "application/octet-stream"


def _response(status: int = 200, **kwargs: Any) -> web.Response:
    return web.Response(status=status, headers=SECURITY_HEADERS, **kwargs)


def _data(hass: HomeAssistant, entry_id: str) -> StaminaData | None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None
    return getattr(entry, "runtime_data", None)


def _playlist_type(path: str, content_type: str) -> bool:
    return path.endswith(".m3u8") or "mpegurl" in content_type.lower()


def _remember_session(hass: HomeAssistant, final: str, event_id: str) -> None:
    match = _SESSION.match(final.lstrip("/"))
    if match is None:
        return
    sessions: OrderedDict[str, str] = hass.data.setdefault(_SESSIONS, OrderedDict())
    sessions[match.group(1)] = event_id
    while len(sessions) > _MAX_SESSIONS:
        sessions.popitem(last=False)


def _session_event(hass: HomeAssistant, path: str) -> str | None:
    match = _SESSION.match(path)
    if match is None:
        return None
    return hass.data.get(_SESSIONS, {}).get(match.group(1))


async def _async_open_session(
    hass: HomeAssistant,
    entry_id: str,
    data: StaminaData,
    path: str,
    params: dict[str, str],
    event_id: str,
    cap: int | None = None,
) -> str | None:
    """Start a recording session on the plugin; return its proxied playlist URL, or None.

    The player is sent to the session's own playlist (through the proxy) rather than given
    its content here: a live playlist is reloaded every few seconds, and reloading this view
    would start a new session each time.
    """
    try:
        response, final = await data.client.async_open_raw(path, params)
    except StaminaApiError:
        return None
    try:
        if response.status != 200 or _SESSION.match(final.lstrip("/")) is None:
            return None
    finally:
        response.release()
    _remember_session(hass, final, event_id)
    return proxy_url(hass, entry_id, final, cap)


def _rewritten_plugin_playlist(
    hass: HomeAssistant,
    entry_id: str,
    data: StaminaData,
    body: bytes,
    final: str,
    cap: int | None = None,
) -> web.Response:
    base = posixpath.dirname(final)
    api_path = urlsplit(data.client.base).path
    origin = urlsplit(data.client.base).netloc

    def resolve(uri: str) -> str | None:
        parts = urlsplit(uri)
        if parts.scheme or parts.netloc:
            if parts.netloc != origin or not parts.path.startswith(api_path + "/"):
                return None
            target = parts.path[len(api_path) :]
        elif parts.path.startswith("/"):
            if not parts.path.startswith(api_path + "/"):
                return None
            target = parts.path[len(api_path) :]
        else:
            target = posixpath.normpath(posixpath.join(base, parts.path))
        if parts.query:
            target = f"{target}?{parts.query}"
        return proxy_url(hass, entry_id, target, cap)

    text = rewrite_playlist(body.decode("utf-8", errors="replace"), resolve)
    return _response(text=text, content_type=HLS)


class _Truncated(Exception):
    """The upstream body ended early or ran past its limit."""


class _Lookups:
    """A small cache of event copies, so a playing segment does not hit SQLite each time."""

    def __init__(self, size: int = 64, ttl: float = 30.0) -> None:
        self._items: OrderedDict[str, tuple[float, dict[str, Copy]]] = OrderedDict()
        self._size, self._ttl = size, ttl

    async def get(self, data: StaminaData, event_id: str) -> dict[str, Copy]:
        now = time.monotonic()
        hit = self._items.get(event_id)
        if hit is not None and now - hit[0] < self._ttl:
            self._items.move_to_end(event_id)
            return hit[1]
        copies = await data.index.async_copies(event_id)
        if any(not (copy.committed or copy.deleted) for copy in copies.values()):
            return copies  # still changing: not worth remembering
        self._items[event_id] = (now, copies)
        while len(self._items) > self._size:
            self._items.popitem(last=False)
        return copies


_LOOKUPS = _Lookups()


async def _async_cloud_playlist(
    hass: HomeAssistant,
    entry_id: str,
    data: StaminaData,
    record: EventRecord,
    cap: int | None = None,
) -> web.Response | None:
    """Return a committed video's playlist, or None when there is none."""
    for subentry, copy in (await _LOOKUPS.get(data, record.id)).items():
        target = data.manager.uploader.targets.get(subentry)
        if target is None or not copy.playable or not copy.ranges:
            continue
        uri = cloud_url(hass, entry_id, subentry, record.id, VIDEO_NAME, cap)
        return _response(text=video_playlist(record, copy, uri), content_type=HLS)
    return None


def _byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return `(start, end)` (inclusive) of a single `bytes=` range, or None to send it all.

    A range that is not one this understands is ignored, as HTTP allows; one that lies past
    the end comes back as `(size, size - 1)`, which the caller answers with 416.
    """
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    first, _, last = header[6:].strip().partition("-")
    try:
        if not first:
            length = int(last)
            return (max(0, size - length), size - 1) if length > 0 else None
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    except ValueError:
        return None
    if start >= size:
        return (size, size - 1)
    return (start, end) if 0 <= start <= end else None


def _has_recorder(capabilities: dict) -> bool:
    """Whether the plugin can play this camera's recordings in place."""
    value = capabilities.get("video_recorder", capabilities.get("videoRecorder"))
    if isinstance(value, dict):
        return bool(value.get("available", True))
    return bool(value)


class PlayView(HomeAssistantView):
    """Plays an event: from the recorder, from the cloud, or the first that works."""

    url = PLAY_PREFIX + "/{entry_id}/{event_id}.m3u8"
    name = f"api:{DOMAIN}:play"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind to Home Assistant."""
        self._hass = hass

    async def get(self, request: web.Request, entry_id: str, event_id: str) -> web.Response:
        """Serve the event's playlist."""
        hass = self._hass
        if not verify(hass, request):
            return _response(403)
        data = _data(hass, entry_id)
        if data is None:
            return _response(404)
        mode = request.query.get("mode", "auto")
        # The live record first: the index row of an event still in progress can be a step
        # behind (an end not saved yet would cut the playback short).
        record = data.manager.records.get(event_id) or await data.index.async_get(event_id)
        if record is None:
            return _response(404)
        if mode in ("auto", "recorder"):
            status = data.manager.cameras.get(record.camera)
            age_days = (time.time() - record.facts.started) / 86400
            recent = age_days <= data.manager.recorder_retention_days
            if status is not None and _has_recorder(status.info.capabilities) and recent:
                start = record.facts.started - PLAYBACK_LEAD
                span = (record.facts.duration or 30.0) + PLAYBACK_LEAD + PLAYBACK_TAIL
                path = playback_path(record.camera, start, span)
                if path is not None:
                    route, _, query = path.partition("?")
                    session = await _async_open_session(
                        hass,
                        entry_id,
                        data,
                        route,
                        dict(parse_qsl(query)),
                        event_id,
                        request_expiry(request),
                    )
                    if session is not None:
                        raise web.HTTPFound(session, headers=SECURITY_HEADERS)
        if mode in ("auto", "cloud"):
            played = await _async_cloud_playlist(
                hass, entry_id, data, record, request_expiry(request)
            )
            if played is not None:
                return played
        return _response(404)


class ProxyView(HomeAssistantView):
    """Forwards signed requests to the plugin, adding the token."""

    url = PROXY_PREFIX + "/{entry_id}/{path:.+}"
    name = f"api:{DOMAIN}:proxy"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind to Home Assistant."""
        self._hass = hass

    async def get(self, request: web.Request, entry_id: str, path: str) -> web.StreamResponse:
        """Serve one plugin file: a playlist rewritten, anything else streamed."""
        hass = self._hass
        if not verify(hass, request) or not _ALLOWED.match(path) or ".." in path:
            return _response(403)
        data = _data(hass, entry_id)
        if data is None:
            return _response(404)
        params = {k: v for k, v in request.query.items() if k not in ("exp", "sig")}
        try:
            response, final = await data.client.async_open_raw(f"/{path}", params)
        except StaminaApiError:
            return _response(502)
        try:
            if response.status == 404 and (event_id := _session_event(hass, path)) is not None:
                # The plugin stopped this session: start a new one for the same event.
                raise web.HTTPFound(
                    play_url(hass, entry_id, event_id, "recorder", request_expiry(request)),
                    headers=SECURITY_HEADERS,
                )
            if response.status != 200:
                return _response(response.status if response.status in (404, 425) else 502)
            if _playlist_type(path, response.content_type):
                try:
                    body = await async_read_limited(response, MAX_PLAYLIST_BYTES, path)
                except StaminaApiError:
                    return _response(502)
                return _rewritten_plugin_playlist(
                    hass, entry_id, data, body, final, request_expiry(request)
                )
            if (response.content_length or 0) > MAX_SEGMENT_BYTES:
                return _response(502)
            out = web.StreamResponse(
                headers={**SECURITY_HEADERS, "Content-Type": safe_type(response.content_type)}
            )
            await out.prepare(request)
            sent = 0
            try:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    sent += len(chunk)
                    if sent > MAX_SEGMENT_BYTES:
                        raise _Truncated
                    await out.write(chunk)
                if response.content_length is not None and sent != response.content_length:
                    raise _Truncated
                await out.write_eof()
            except (_Truncated, aiohttp.ClientError, TimeoutError):
                # Headers are out already: a cut body must not end as a complete 200. The
                # connection is aborted, so the player sees a failed fetch and retries.
                if request.transport is not None:
                    request.transport.abort()
            return out
        except (aiohttp.ClientError, TimeoutError):
            return _response(502)
        finally:
            response.release()


class CloudView(HomeAssistantView):
    """Serves a committed event's video from a destination, by byte range."""

    url = CLOUD_PREFIX + "/{entry_id}/{subentry}/{event_id}/{name}"
    name = f"api:{DOMAIN}:cloud"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind to Home Assistant."""
        self._hass = hass

    async def get(
        self, request: web.Request, entry_id: str, subentry: str, event_id: str, name: str
    ) -> web.StreamResponse:
        """Serve the video, or the range of it asked for."""
        hass = self._hass
        if not verify(hass, request) or name != VIDEO_NAME:
            return _response(403)
        data = _data(hass, entry_id)
        if data is None:
            return _response(404)
        target = data.manager.uploader.targets.get(subentry)
        copy = (await _LOOKUPS.get(data, event_id)).get(subentry)
        if target is None or copy is None or not copy.playable:
            return _response(404)
        size = copy.video_size
        headers = {**SECURITY_HEADERS, "Accept-Ranges": "bytes", "Content-Type": "video/mp4"}
        span = _byte_range(request.headers.get("Range"), size)
        if span is None:
            start, end, status = 0, size - 1, 200
        else:
            start, end = span
            if start >= size:
                return web.Response(
                    status=416, headers={**headers, "Content-Range": f"bytes */{size}"}
                )
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        destination, path = target.destination, str(copy.video)  # playable: not None
        # The first chunk is read before anything is sent, so a destination that fails
        # outright is a clean 502 rather than a cut response.
        try:
            first = await _async_chunk(destination, path, start, end)
        except DestinationError:
            return _response(502)
        out = web.StreamResponse(status=status, headers=headers)
        out.content_length = end - start + 1
        await out.prepare(request)
        try:
            await out.write(first)
            offset = start + len(first)
            while offset <= end:
                chunk = await _async_chunk(destination, path, offset, end)
                await out.write(chunk)
                offset += len(chunk)
            await out.write_eof()
        except (DestinationError, ConnectionResetError):
            # Headers are out already: a cut body must not end as a complete response.
            if request.transport is not None:
                request.transport.abort()
        return out


async def _async_chunk(destination: Destination, path: str, start: int, end: int) -> memoryview:
    """Read the next `VIDEO_CHUNK` at most of `start..end` (inclusive), exactly as asked."""
    last = min(end, start + VIDEO_CHUNK - 1)
    body = memoryview(await destination.async_read_range(path, start, last))
    if len(body) != last - start + 1:
        raise DestinationError(f"{path}: {len(body)} bytes for {start}-{last}")
    return body


class ThumbnailView(HomeAssistantView):
    """Serves an event's thumbnail from the index."""

    url = THUMBNAIL_PREFIX + "/{entry_id}/{event_id}.jpg"
    name = f"api:{DOMAIN}:thumbnail"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind to Home Assistant."""
        self._hass = hass

    async def get(self, request: web.Request, entry_id: str, event_id: str) -> web.Response:
        """Serve one thumbnail."""
        if not verify(self._hass, request):
            return _response(403)
        data = _data(self._hass, entry_id)
        if data is None:
            return _response(404)
        body = await data.index.async_thumbnail(event_id)
        if body is None:
            return _response(404)
        return web.Response(
            body=body,
            content_type="image/jpeg",
            headers={**SECURITY_HEADERS, "Cache-Control": THUMBNAIL_CACHE},
        )


@callback
def async_register_views(hass: HomeAssistant) -> None:
    """Register the views once per Home Assistant run."""
    if hass.data.get(_VIEWS):
        return
    hass.data[_VIEWS] = True
    for view in (PlayView, ProxyView, CloudView, ThumbnailView):
        hass.http.register_view(view(hass))
