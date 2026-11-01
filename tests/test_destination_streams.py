"""Streamed stores and ranged reads, against fakes shaped like the real client libraries.

Each fake method has the signature of the library version Home Assistant pins (aiowebdav2
0.6.2, py-synologydsm-api 2.10.3, asyncssh 2.24.0), so a call the real one would refuse —
a wrong keyword, a positional where it takes keywords — fails here too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
import pytest

from custom_components.scrypted_stamina.destinations import (
    DestinationError,
    GoogleDriveDestination,
    SftpDestination,
    SynologyDestination,
    WebDavDestination,
    base,
    google_drive,
)
from custom_components.scrypted_stamina.destinations.base import RANGE_READ_LIMIT, rechunk

from .conftest import FakeDestination
from .test_destinations import loaded

VIDEO = "scrypted-stamina/events/garden-front-door-261031_210509.mp4"
DATA = b"0123456789abcdef"


async def chunked(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# ------------------------------------------------------------------ the shared pieces


async def test_rechunk_passes_a_chunk_of_the_right_size_through() -> None:
    whole = b"abcd"
    out = [chunk async for chunk in rechunk(chunked(whole, b"ef", b"gh", b"ij"), 4)]
    assert out == [b"abcd", b"efgh", b"ij"]
    assert out[0] is whole  # not copied


async def test_the_default_range_read_refuses_a_large_file() -> None:
    fake = FakeDestination()
    fake.files[VIDEO] = b"0123456789"
    assert await super(FakeDestination, fake).async_read_range(VIDEO, 2, 5) == b"2345"
    fake.files[VIDEO] = b"x" * (RANGE_READ_LIMIT + 1)
    with pytest.raises(DestinationError, match="too large"):
        await super(FakeDestination, fake).async_read_range(VIDEO, 0, 1)
    assert ("read", VIDEO) not in fake.ops[1:]  # never read whole


# ------------------------------------------------------------------ WebDAV (aiowebdav2 0.6.2)


class FakeResponse:
    """The parts of `aiohttp.ClientResponse` a ranged read uses."""

    def __init__(self, status: int, body: bytes, chunk: int = 4) -> None:
        self.status = status
        self._body = body
        self.pulled = 0
        self.closed = False
        self.content = SimpleNamespace(read=self._read)
        self._chunk = chunk

    async def _read(self, n: int = -1) -> bytes:
        size = min(n, self._chunk) if n > 0 else len(self._body)
        data = self._body[self.pulled : self.pulled + size]
        self.pulled += len(data)
        return data

    async def read(self) -> bytes:
        self.pulled = len(self._body)
        return self._body

    def close(self) -> None:
        self.closed = True


class FakeWebDavClient:
    """`aiowebdav2.Client`, as far as a destination uses it."""

    def __init__(self, *, honours_range: bool = True) -> None:
        self.files: dict[str, bytes] = {}
        self.honours_range = honours_range
        self.requests: list[tuple[str, str, dict[str, str] | None]] = []
        self.response: FakeResponse | None = None

    async def check(self, remote_path: str = "/") -> bool:
        return True

    async def mkdir(self, remote_path: str, *, recursive: bool = False) -> bool:
        return True

    async def upload_iter(
        self,
        buff: Any,
        remote_path: str,
        *,
        timeout: Any = None,
        content_length: int | None = None,
        progress: Any = None,
    ) -> None:
        assert isinstance(buff, AsyncIterator)
        self.files[remote_path] = b"".join([chunk async for chunk in buff])

    async def info(self, remote_path: str) -> dict[str, str]:
        return {"size": str(len(self.files[remote_path]))}

    async def list_with_infos(
        self, remote_path: str = "/", *, recursive: bool = False
    ) -> list[dict[str, str]]:
        prefix = f"{remote_path}/"
        return [
            {"path": f"/{path}", "isdir": "False", "size": str(len(data))}
            for path, data in self.files.items()
            if path.startswith(prefix)
        ]

    async def execute_request(
        self,
        action: str,
        path: str,
        data: Any = None,
        headers_ext: dict[str, str] | None = None,
        *,
        timeout: Any = None,
    ) -> FakeResponse:
        self.requests.append((action, path, headers_ext))
        body = self.files[path.lstrip("/")]
        if self.honours_range and headers_ext and "Range" in headers_ext:
            first, last = headers_ext["Range"].removeprefix("bytes=").split("-")
            self.response = FakeResponse(206, body[int(first) : int(last) + 1])
        else:
            self.response = FakeResponse(200, body)
        return self.response


async def test_webdav_streams_a_store() -> None:
    client = FakeWebDavClient()
    destination = WebDavDestination(MagicMock(), loaded("webdav", client))
    await destination.async_store_video(VIDEO, len(DATA), chunked(DATA[:5], DATA[5:]))
    assert client.files[VIDEO] == DATA


async def test_webdav_asks_for_a_range() -> None:
    client = FakeWebDavClient()
    client.files[VIDEO] = DATA
    destination = WebDavDestination(MagicMock(), loaded("webdav", client))
    assert await destination.async_read_range(VIDEO, 3, 9) == b"3456789"
    assert client.requests == [("download", f"/{VIDEO}", {"Range": "bytes=3-9"})]


async def test_webdav_streams_and_stops_when_the_server_ignores_the_range() -> None:
    client = FakeWebDavClient(honours_range=False)
    client.files[VIDEO] = DATA
    destination = WebDavDestination(MagicMock(), loaded("webdav", client))
    assert await destination.async_read_range(VIDEO, 3, 9) == b"3456789"
    assert client.response.pulled == 12  # three chunks of four: the last never asked for
    assert client.response.closed


# ------------------------------------------------------------------ Synology (2.10.3)


class FakeStreamReader:
    """`aiohttp.StreamReader`, which File Station's download returns."""

    def __init__(self, body: bytes, chunk: int = 4) -> None:
        self._body = body
        self._chunk = chunk
        self.pulled = 0

    async def read(self, n: int = -1) -> bytes:
        size = min(n, self._chunk) if n > 0 else len(self._body)
        data = self._body[self.pulled : self.pulled + size]
        self.pulled += len(data)
        return data


class FakeFileStation:
    """`synology_dsm.api.file_station.SynoFileStation`, as far as a destination uses it."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.reader: FakeStreamReader | None = None

    async def upload_file(
        self, path: str, filename: str, source: Any, create_parents: bool = False
    ) -> bool | None:
        assert isinstance(source, AsyncIterator)
        self.files[f"{path}/{filename}"] = b"".join([chunk async for chunk in source])
        return True

    async def get_files(self, path: str, offset: int = 0, limit: int = 100) -> list | None:
        prefix = f"{path}/"
        return [
            SimpleNamespace(
                is_dir=False, name=name[len(prefix) :], additional=SimpleNamespace(size=len(data))
            )
            for name, data in self.files.items()
            if name.startswith(prefix)
        ][offset : offset + limit]

    async def download_file(
        self, path: str, filename: str, target_file: str | None = None
    ) -> FakeStreamReader | bool | None:
        self.reader = FakeStreamReader(self.files[f"{path}/{filename}"])
        return self.reader


def _synology(station: FakeFileStation) -> SynologyDestination:
    runtime = SimpleNamespace(api=SimpleNamespace(file_station=station, dsm=None))
    return SynologyDestination(MagicMock(), loaded("synology_dsm", runtime))


async def test_synology_streams_a_store() -> None:
    station = FakeFileStation()
    await _synology(station).async_store_video(VIDEO, len(DATA), chunked(DATA[:5], DATA[5:]))
    assert station.files[f"/{VIDEO}"] == DATA


async def test_synology_reads_a_range_and_stops_after_it() -> None:
    station = FakeFileStation()
    station.files[f"/{VIDEO}"] = DATA
    assert await _synology(station).async_read_range(VIDEO, 3, 9) == b"3456789"
    assert station.reader.pulled == 12  # the last chunk was never read


# ------------------------------------------------------------------ SFTP (asyncssh 2.24.0)


class FakeSftpFile:
    """`asyncssh.SFTPClientFile`."""

    def __init__(self, sftp: FakeSftp, path: str) -> None:
        self._sftp = sftp
        self._path = path

    async def write(self, data: bytes, offset: int | None = None) -> int:
        current = self._sftp.files.get(self._path, b"")
        at = len(current) if offset is None else offset
        self._sftp.files[self._path] = current[:at] + data
        return len(data)

    async def close(self) -> None:
        pass


class FakeSftp:
    """`asyncssh.SFTPClient`, as far as a destination stores with it."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def makedirs(self, path: str, attrs: Any = None, exist_ok: bool = False) -> None:
        pass

    async def open(
        self,
        path: str,
        pflags_or_mode: int | str = 1,
        attrs: Any = None,
        encoding: str | None = "utf-8",
        errors: str = "strict",
        block_size: int = -1,
        max_requests: int = -1,
    ) -> FakeSftpFile:
        assert pflags_or_mode == "wb"
        self.files[path] = b""
        return FakeSftpFile(self, path)

    async def stat(self, path: str, flags: int = 0, *, follow_symlinks: bool = True) -> Any:
        return SimpleNamespace(size=len(self.files[path]))


async def test_sftp_streams_a_store_at_each_offset() -> None:
    sftp = FakeSftp()
    config = SimpleNamespace(backup_location="/backups")
    destination = SftpDestination(MagicMock(), loaded("sftp_storage", config))
    destination._sftp = sftp
    await destination.async_store_stream(VIDEO, len(DATA), chunked(DATA[:5], DATA[5:]))
    assert sftp.files[f"/backups/{VIDEO}"] == DATA


# ------------------------------------------------------------------ Google Drive


async def test_google_drive_resumes_through_308s_and_retries_a_chunk(
    hass, socket_enabled, aiohttp_server, monkeypatch
) -> None:
    """A real HTTP exchange: Drive answers 308 to every chunk but the last."""
    received = bytearray()
    ranges: list[str] = []
    failed_once = False

    async def files(request: web.Request) -> web.Response:
        return web.json_response({"files": [{"id": "v"}]})

    async def open_session(request: web.Request) -> web.Response:
        assert request.query["uploadType"] == "resumable"
        assert request.headers["X-Upload-Content-Length"] == str(len(DATA))
        return web.Response(headers={"Location": str(request.url.with_path("/session"))})

    async def chunk(request: web.Request) -> web.Response:
        nonlocal failed_once
        span = request.headers["Content-Range"]
        ranges.append(span)
        body = await request.read()
        if span == "bytes 4-7/16" and not failed_once:
            failed_once = True
            return web.Response(status=503)  # dropped: this chunk is sent again
        received.extend(body)
        if len(received) < len(DATA):
            return web.Response(status=308, headers={"Range": f"bytes=0-{len(received) - 1}"})
        return web.json_response({"id": "v", "size": str(len(received))})

    app = web.Application()
    app.router.add_get("/drive/files", files)
    app.router.add_patch("/upload/files/v", open_session)
    app.router.add_put("/session", chunk)
    server = await aiohttp_server(app)
    monkeypatch.setattr(google_drive, "DRIVE", str(server.make_url("/drive")))
    monkeypatch.setattr(google_drive, "UPLOAD", str(server.make_url("/upload")))
    monkeypatch.setattr(google_drive, "SIMPLE_UPLOAD_LIMIT", 4)
    monkeypatch.setattr(google_drive, "STREAM_CHUNK", 4)
    monkeypatch.setattr(base, "BACKOFF_SECONDS", 0)

    destination = GoogleDriveDestination(hass, loaded("google_drive"))
    destination._async_token = AsyncMock(return_value="token")
    destination._folders = {"scrypted-stamina/events": "events"}
    await destination.async_store_stream(VIDEO, len(DATA), chunked(DATA[:6], DATA[6:]))
    assert bytes(received) == DATA
    assert ranges == [
        "bytes 0-3/16",
        "bytes 4-7/16",
        "bytes 4-7/16",
        "bytes 8-11/16",
        "bytes 12-15/16",
    ]


async def test_google_drive_looks_a_file_id_up_once_for_ranged_reads() -> None:
    destination = GoogleDriveDestination(MagicMock(), loaded("google_drive"))
    destination._folders = {"scrypted-stamina/events": "events"}
    lookups = 0

    async def request(method, url, **kwargs):
        nonlocal lookups
        if url.endswith("/files"):
            lookups += 1
            return {"files": [{"id": "v"}]}
        if method == "DELETE":
            return None
        first, last = kwargs["headers"]["Range"][6:].split("-")
        return DATA[int(first) : int(last) + 1]

    destination._async_request = request
    assert await destination.async_read_range(VIDEO, 0, 3) == b"0123"
    assert await destination.async_read_range(VIDEO, 4, 7) == b"4567"
    assert lookups == 1
    await destination.async_delete(VIDEO)  # a deleted file's id is forgotten
    assert VIDEO not in destination._files
