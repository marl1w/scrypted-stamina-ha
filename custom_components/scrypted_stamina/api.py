"""The plugin's HTTP API, as plain requests (architecture §2.5, `docs/api.md`).

One client per config entry, holding the base URL and the token. The token goes in the
`Authorization` header and nowhere else: never in a URL, never in a log line. Errors are
mapped onto a handful of exceptions the callers act on — an expired segment is a gap, a
segment not cut yet is a short wait, a refused token (or a changed certificate) is a reauth.

Transport rules:

* **TLS is pinned on first use.** Scrypted serves a self-signed certificate, so ordinary
  verification cannot work; instead the config flow records the certificate's SHA-256 and
  every request checks it (`aiohttp.Fingerprint`). A different certificate is refused and
  starts a reauth, where the new one is shown and accepted explicitly.
* **Plain `http://` only to loopback.** The token travels in a header; on the network it
  must be encrypted.
* **Bodies are read whole, within a limit.** `StreamReader.read(n)` returns whatever has
  arrived, not `n` bytes; a segment read that way can be silently short. Every read here
  loops to the end of the body and fails if the limit is passed.
* **Redirects are followed by hand**: at most two hops, and only to the same scheme, host
  and port, under the API base. Nothing the plugin answers can send the token elsewhere.

The SSE stream itself lives in `link.py`; this module only opens the response for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
import hashlib
import ipaddress
import json
import logging
import ssl
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import get_default_no_verify_context
from yarl import URL

from .const import (
    API_CAMERAS,
    API_HEALTH,
    API_STREAM,
    MAX_SEGMENT_BYTES,
    MAX_THUMBNAIL_BYTES,
    REQUEST_TIMEOUT,
)
from .protocol import CameraInfo, valid_id

_LOGGER = logging.getLogger(__name__)

MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_SMALL_BYTES = 64 * 1024
MAX_REDIRECTS = 2
_CHUNK = 64 * 1024


class StaminaApiError(Exception):
    """The plugin could not be reached or did not answer usefully."""


class StaminaAuthError(StaminaApiError):
    """The plugin refused the token."""


class CertificateChangedError(StaminaAuthError):
    """The plugin's certificate is not the one pinned at setup."""


class InsecureUrlError(StaminaApiError):
    """A plain-HTTP URL to a host that is not this machine."""


class SegmentExpiredError(StaminaApiError):
    """The plugin no longer holds this segment (404): it is a gap."""


class SegmentNotReadyError(StaminaApiError):
    """The plugin has not cut this segment yet (425): ask again shortly."""


def normalise_url(url: str) -> str:
    """Return the plugin URL without a trailing slash."""
    return url.strip().rstrip("/")


def api_base(url: str) -> str:
    """Return the base every route hangs off: the plugin endpoint's `/public` path.

    Scrypted's non-public endpoint path wants a Scrypted login before the request reaches
    the plugin; the plugin checks its own token on `/public/` instead (`docs/api.md`). The
    endpoint URL is accepted with or without it.
    """
    url = normalise_url(url)
    return url if url.endswith("/public") else f"{url}/public"


def is_loopback(host: str | None) -> bool:
    """Return whether a host is this machine."""
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_url(url: str) -> None:
    """Refuse a URL the token must not travel over."""
    parts = urlsplit(normalise_url(url))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise InsecureUrlError("the URL must be http(s)://host/…")
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        raise InsecureUrlError("plain http is only allowed to this machine; use https")


async def async_read_limited(response: aiohttp.ClientResponse, limit: int, what: str) -> bytes:
    """Read a whole body, failing once it passes `limit` (never returning a short one)."""
    if (response.content_length or 0) > limit:
        raise StaminaApiError(f"{what} is larger than {limit} bytes")
    body = bytearray()
    async for chunk in response.content.iter_chunked(_CHUNK):
        body.extend(chunk)
        if len(body) > limit:
            raise StaminaApiError(f"{what} is larger than {limit} bytes")
    return bytes(body)


async def async_probe_fingerprint(hass: HomeAssistant, url: str) -> str | None:
    """Return the SHA-256 of the certificate an https URL presents (None for http).

    A bare TLS handshake, no request: nothing (least of all the token) is sent to a server
    whose certificate has not been accepted yet.
    """
    parts = urlsplit(api_base(url))
    if parts.scheme != "https" or not parts.hostname:
        return None
    host, port = parts.hostname, parts.port or 443
    try:
        ipaddress.ip_address(host)
        server_hostname = None
    except ValueError:
        server_hostname = host
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            _, writer = await asyncio.open_connection(
                host,
                port,
                ssl=get_default_no_verify_context(),
                server_hostname=server_hostname,
            )
    except (TimeoutError, OSError) as err:
        raise StaminaApiError(f"{type(err).__name__} reading the certificate") from err
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    finally:
        writer.close()
        with suppress(OSError, ssl.SSLError):
            await writer.wait_closed()
    if not der:
        raise StaminaApiError("the plugin presented no certificate")
    return hashlib.sha256(der).hexdigest()


class StaminaClient:
    """Requests against one Stamina plugin."""

    def __init__(
        self,
        hass: HomeAssistant,
        url: str,
        token: str,
        *,
        verify_ssl: bool = False,
        fingerprint: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Bind to a plugin URL with a token (and the certificate pinned at setup)."""
        check_url(url)
        self.url = normalise_url(url)
        self.base = api_base(url)
        self._origin = urlsplit(self.base)
        self._token = token
        self._session = session or async_get_clientsession(hass, verify_ssl=verify_ssl)
        self._ssl: Any
        if self._origin.scheme != "https":
            self._ssl = None
        elif fingerprint:
            self._ssl = aiohttp.Fingerprint(bytes.fromhex(fingerprint))
        elif verify_ssl:
            self._ssl = None  # ordinary CA verification
        else:
            # Never a silent unverified connection: https without verification needs a pin.
            raise InsecureUrlError("https without a pinned certificate; re-authenticate to pin it")

    @property
    def token(self) -> str:
        """Return the token, for redaction only."""
        return self._token

    @property
    def headers(self) -> dict[str, str]:
        """Return the headers every request carries."""
        return {"Authorization": f"Bearer {self._token}"}

    def route(self, path: str) -> str:
        """Return the absolute URL of a route under the plugin endpoint."""
        return f"{self.base}/{path.lstrip('/')}"

    def relative(self, url: URL) -> str | None:
        """Return a URL's path below the API base, or None if it is anywhere else."""
        if (url.scheme, url.host, url.port) != (
            self._origin.scheme,
            self._origin.hostname,
            URL(self.base).port,
        ):
            return None
        # The raw (still percent-encoded) path is what the server routes on: an encoded
        # slash or backslash could step out of the API base after decoding.
        raw = url.raw_path
        lowered = raw.lower()
        if "%2f" in lowered or "%5c" in lowered or "\\" in raw:
            return None
        base = URL(self.base).raw_path
        if not raw.startswith(base + "/") or "/../" in raw + "/":
            return None
        return url.path[len(URL(self.base).path) :]

    def _kwargs(self) -> dict[str, Any]:
        return {} if self._ssl is None else {"ssl": self._ssl}

    async def _open(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
        timeout: float = REQUEST_TIMEOUT,  # noqa: ASYNC109 - a per-request bound
        headers: dict[str, str] | None = None,
    ) -> tuple[aiohttp.ClientResponse, str]:
        """Open a response, following same-origin redirects by hand; caller releases it."""
        url = URL(self.route(path))
        if params:
            url = url.update_query(dict(params))
        for _ in range(MAX_REDIRECTS + 1):
            try:
                response = await self._session.request(
                    method,
                    url,
                    headers={**self.headers, **(headers or {})},
                    json=json_body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    **self._kwargs(),
                )
            except aiohttp.ServerFingerprintMismatch as err:
                raise CertificateChangedError("the plugin's certificate has changed") from err
            except (TimeoutError, aiohttp.ClientError) as err:
                raise StaminaApiError(f"{type(err).__name__} on {method} {path}") from err
            if response.status in (401, 403):
                response.release()
                raise StaminaAuthError("the plugin refused the token")
            if response.status not in (301, 302, 303, 307, 308):
                return response, self.relative(response.url) or path
            location = response.headers.get("Location", "")
            response.release()
            lowered = location.lower()
            if "\\" in location or "%2f" in lowered or "%5c" in lowered:
                raise StaminaApiError("the plugin redirected outside its API")
            try:
                target = url.join(URL(location))
            except ValueError as err:
                raise StaminaApiError("the plugin sent an unreadable redirect") from err
            if target.user is not None or target.password is not None:
                raise StaminaApiError("the plugin redirected outside its API")
            if self.relative(target) is None:
                raise StaminaApiError("the plugin redirected outside its API")
            url, method, json_body = target, "GET", None
        raise StaminaApiError(f"too many redirects on {path}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
        limit: int = MAX_JSON_BYTES,
    ) -> tuple[int, bytes, str]:
        """Make one request and return its status, whole body and content type."""
        response, _ = await self._open(method, path, json_body=json_body, params=params)
        try:
            body = await async_read_limited(response, limit, path)
            return response.status, body, response.content_type
        except (TimeoutError, aiohttp.ClientError) as err:
            raise StaminaApiError(f"{type(err).__name__} reading {path}") from err
        finally:
            response.release()

    async def _json(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        """GET a route and return its JSON."""
        status, body, _ = await self._request("GET", path, params=params)
        if status != 200:
            raise StaminaApiError(f"GET {path} answered HTTP {status}")
        try:
            return json.loads(body)
        except ValueError as err:
            raise StaminaApiError(f"GET {path} did not return JSON") from err

    async def async_health(self) -> dict[str, Any]:
        """Return `/v1/health`: proves the URL, the certificate and the token in one call."""
        data = await self._json(API_HEALTH)
        if not isinstance(data, dict):
            raise StaminaApiError("health is not an object")
        return data

    async def async_cameras(self) -> list[CameraInfo]:
        """Return the cameras with the Stamina mixin."""
        data = await self._json(API_CAMERAS)
        items = data.get("cameras", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise StaminaApiError("cameras is not a list")
        return [camera for item in items if (camera := CameraInfo.parse(item)) is not None]

    async def async_segment(self, event_id: str, number: int | None) -> bytes:
        """Return one segment, or `init.mp4` when `number` is None."""
        return await self.async_event_file(
            event_id, "init.mp4" if number is None else f"{number}.m4s"
        )

    async def async_event_file(self, event_id: str, name: str) -> bytes:
        """Return one media file of an event: `init.mp4`, `init-<k>.mp4` or `<n>.m4s`."""
        path = f"/v1/events/{quote(event_id, safe='')}/{quote(name, safe='')}"
        status, body, _ = await self._request("GET", path, limit=MAX_SEGMENT_BYTES)
        if status == 404:
            raise SegmentExpiredError(f"{name} of {event_id} is gone")
        if status == 425:
            raise SegmentNotReadyError(f"{name} of {event_id} is not cut yet")
        if status != 200:
            raise StaminaApiError(f"{name} of {event_id} answered HTTP {status}")
        return body

    async def async_event(self, event_id: str) -> dict[str, Any] | None:
        """Return `GET /v1/events/<id>`, or None once the plugin has forgotten the event."""
        path = f"/v1/events/{quote(event_id, safe='')}"
        status, body, _ = await self._request("GET", path, limit=1024 * 1024)
        if status == 404:
            return None
        if status != 200:
            raise StaminaApiError(f"event {event_id} answered HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError as err:
            raise StaminaApiError("event state is not JSON") from err
        return data if isinstance(data, dict) else None

    async def async_events_since(self, since: float) -> list[dict[str, Any]]:
        """Return the events the plugin still knows that started after `since` (seconds).

        Used to recover after a gap or a reset in the stream. A plugin without the route
        answers 404, which reads as "nothing to recover from here".
        """
        status, body, _ = await self._request(
            "GET", "/v1/events", params={"since": str(int(since * 1000))}
        )
        if status == 404:
            return []
        if status != 200:
            raise StaminaApiError(f"events answered HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError as err:
            raise StaminaApiError("events is not JSON") from err
        items = data.get("events", []) if isinstance(data, dict) else data
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def async_ack(self, event_id: str, up_to: int) -> None:
        """Release segments 0..`up_to` (inclusive)."""
        path = f"/v1/events/{quote(event_id, safe='')}/ack"
        status, _, _ = await self._request(
            "POST", path, json_body={"upTo": up_to}, limit=MAX_SMALL_BYTES
        )
        if status not in (200, 202, 204, 404):
            raise StaminaApiError(f"ack of {event_id} answered HTTP {status}")

    async def async_thumbnail(self, event_id: str) -> bytes | None:
        """Return an event's 320 px JPEG, or None when the plugin has none."""
        path = f"/v1/events/{quote(event_id, safe='')}/thumbnail.jpg"
        status, body, _ = await self._request("GET", path, limit=MAX_THUMBNAIL_BYTES)
        if status in (404, 425):
            return None
        if status != 200:
            raise StaminaApiError(f"thumbnail of {event_id} answered HTTP {status}")
        return body

    async def async_open_raw(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> tuple[aiohttp.ClientResponse, str]:
        """Open a route for the playback proxy: the response and its final path.

        The final path, relative to the API base, is what relative playlist entries resolve
        against (`playback.m3u8` redirects to a session). The caller streams the body and
        releases the response.
        """
        return await self._open("GET", path, params=params, timeout=REQUEST_TIMEOUT + 15)

    async def async_open_stream(self, since: int | None, epoch: str | None) -> Any:
        """Open the SSE stream; the caller owns (and must release) the response."""
        params: dict[str, str] = {}
        headers = {"Accept": "text/event-stream"}
        # `since` without `epoch` is a reset to the plugin (docs/api.md, changes 9): a
        # position is only ever sent whole.
        if since is not None and epoch:
            params["since"] = str(since)
            params["epoch"] = epoch
            # The standard SSE resume header; the plugin prefers it over the query.
            headers["Last-Event-ID"] = f"{epoch}:{since}"
        url = URL(self.route(API_STREAM)).update_query(params) if params else self.route(API_STREAM)
        try:
            response = await self._session.get(
                url,
                headers={**self.headers, **headers},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, connect=REQUEST_TIMEOUT),
                **self._kwargs(),
            )
        except aiohttp.ServerFingerprintMismatch as err:
            raise CertificateChangedError("the plugin's certificate has changed") from err
        except (TimeoutError, aiohttp.ClientError) as err:
            raise StaminaApiError(f"{type(err).__name__} opening the stream") from err
        if response.status in (401, 403):
            response.release()
            raise StaminaAuthError("the plugin refused the token")
        if response.status != 200:
            response.release()
            raise StaminaApiError(f"the stream answered HTTP {response.status}")
        return response


def playback_path(camera: str, start: float, duration: float | None) -> str | None:
    """Return the plugin route for recording playback, or None for a bad camera id."""
    if valid_id(camera) is None:
        return None
    query = f"t={int(start * 1000)}"
    if duration:
        # `d` is seconds, 1 to 3600 (`docs/api.md`).
        query += f"&d={max(1, min(3600, round(duration)))}"
    return f"/v1/cameras/{quote(camera, safe='')}/playback.m3u8?{query}"
