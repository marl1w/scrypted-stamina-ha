"""What a destination has to be, and how they all retry.

Ported from Reolink Stamina, where a destination stored, deleted and listed whole clips.
Scrypted Stamina writes one file per segment into a pending folder while the event lasts,
then commits it as one video, streamed from the box's local spool of those segments, and
deletes the folder. Every provider implements five members — `label`, `async_store`,
`async_delete`, `async_list` and `async_read` (a segment back when the spool is gone; a
committed video for playback) — and the rest have defaults it overrides when it can do
better:

* `async_store_stream` — write one file from chunks, without holding it whole: an upload
  session where the provider has one; the default buffers, up to `BUFFER_LIMIT`;
* `async_store_video` — `async_store_stream`, then confirm the size with `async_size`;
* `async_size` — one file's size, by listing its folder unless the provider can ask;
* `async_delete_folder` — remove a pending folder and everything in it.

Retrying is still the implementations' problem, and treated as one: a throttled or briefly
unavailable service is waited out here, with the service's own `Retry-After` honoured, and
`async_store` returns only once the service has confirmed what it stored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import mimetypes

_LOGGER = logging.getLogger(__name__)

# A segment that fails is worth a few more tries before the uploader hears about it.
REQUEST_ATTEMPTS = 4
BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0

# A committed video goes up in chunks of this size: a multiple of both OneDrive's 320 KiB
# and Google Drive's 256 KiB, and small enough to hold one in memory on a small box.
STREAM_CHUNK = 10 * 1024 * 1024
# What a provider without a streaming upload may buffer to store one file.
BUFFER_LIMIT = 64 * 1024 * 1024

# How many attempts an operation gets, when the caller does its own retrying. The uploader
# sets it to one for segment stores, so a destination's backoff never sleeps while holding
# an upload slot: the lane retries, with the slot released.
_ATTEMPTS: ContextVar[int | None] = ContextVar("destination_attempts", default=None)


@contextmanager
def attempts_limited(attempts: int) -> Iterator[None]:
    """Run destination operations in this block with at most `attempts` attempts each."""
    token = _ATTEMPTS.set(attempts)
    try:
        yield
    finally:
        _ATTEMPTS.reset(token)


_TYPES = {
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
}


def content_type(path: str) -> str:
    """Return the MIME type a file is uploaded with."""
    _, dot, extension = path.rpartition(".")
    if dot and (known := _TYPES.get(f".{extension.lower()}")):
        return known
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


class DestinationError(Exception):
    """Raised when a destination cannot do what was asked of it."""


class UploadTooLargeError(DestinationError):
    """Raised when a file exceeds what this destination will take: permanent, never retried."""


async def rechunk(chunks: AsyncIterable[bytes], size: int = STREAM_CHUNK) -> AsyncIterator[bytes]:
    """Yield `chunks` again as pieces of exactly `size` bytes, the last one shorter."""
    buffer = bytearray()
    async for chunk in chunks:
        buffer += chunk
        while len(buffer) >= size:
            yield bytes(buffer[:size])
            del buffer[:size]
    if buffer:
        yield bytes(buffer)


async def streamed(data: bytes) -> AsyncIterator[bytes]:
    """Return bytes already in memory as a stream of one chunk."""
    yield data


async def gather(chunks: AsyncIterable[bytes], size: int) -> bytes:
    """Return a stream of `size` bytes as one buffer, refusing to hold more than that."""
    data = bytearray()
    async for chunk in chunks:
        data += chunk
        if len(data) > size:
            break
    if len(data) != size:
        raise DestinationError(f"a stream of {size} bytes gave {len(data)}")
    return bytes(data)


class TransientError(Exception):
    """A failure worth retrying, with the service's own wait if it named one.

    Never raised out of a destination: `async_attempt` either succeeds or converts the last
    one into a `DestinationError`, so callers see one failure type.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Record what failed and how long to wait."""
        super().__init__(message)
        self.retry_after = retry_after


async def async_attempt[T](
    what: str,
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = REQUEST_ATTEMPTS,
) -> T:
    """Run one destination operation until it stops failing transiently.

    `operation` is a factory rather than a coroutine because a retried request has to be
    built again: a consumed body or an expired token cannot be sent twice.
    """
    if (limited := _ATTEMPTS.get()) is not None:
        attempts = min(attempts, limited)
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except TransientError as err:
            if attempt == attempts:
                raise DestinationError(f"{what} failed after {attempt} attempts: {err}") from err
            delay = err.retry_after or min(
                BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_BACKOFF_SECONDS
            )
            _LOGGER.debug(
                "%s failed (%s); retrying in %.0fs (attempt %s of %s)",
                what,
                err,
                delay,
                attempt + 1,
                attempts,
            )
            await asyncio.sleep(delay)
    # Unreachable: the loop either returns or raises on its last attempt.
    raise DestinationError(f"{what} failed")


class Destination(ABC):
    """Somewhere events can be kept: pending folders of segments, then one video each."""

    @property
    @abstractmethod
    def label(self) -> str:
        """Return a short human-readable name, for logs and entity attributes."""

    @abstractmethod
    async def async_store(self, path: str, data: bytes) -> None:
        """Write one file, creating any folders it needs."""

    @abstractmethod
    async def async_delete(self, path: str) -> None:
        """Remove one file. Succeeds quietly when it is already gone."""

    @abstractmethod
    async def async_list(self, folder: str) -> dict[str, int]:
        """Return `path -> size` for the files directly under one folder."""

    @abstractmethod
    async def async_read(self, path: str) -> bytes:
        """Return one file's content."""

    async def async_size(self, path: str) -> int | None:
        """Return one file's size, or None when it is not there.

        The default lists the file's folder, which every provider can do; the ones that can
        ask about one file override it.
        """
        folder, _, _ = path.rpartition("/")
        return (await self.async_list(folder)).get(path)

    async def async_store_stream(self, path: str, size: int, chunks: AsyncIterable[bytes]) -> None:
        """Write one file of `size` bytes from `chunks`, creating any folders it needs.

        A stream cannot be read twice, so a failure is the caller's to retry, whole. The
        default buffers it for `async_store`, up to `BUFFER_LIMIT`; providers with an upload
        session or a streaming write override it.
        """
        if size > BUFFER_LIMIT:
            raise UploadTooLargeError(
                f"{self.label} cannot stream; {size} bytes exceeds {BUFFER_LIMIT} buffered"
            )
        await self.async_store(path, await gather(chunks, size))

    async def async_store_video(self, path: str, size: int, chunks: AsyncIterable[bytes]) -> None:
        """Store one file of a committed video, and confirm the destination holds all of it."""
        await self.async_store_stream(path, size, chunks)
        stored = await self.async_size(path)
        if stored != size:
            raise DestinationError(f"{self.label} holds {stored} bytes of {path}, not {size}")

    async def async_delete_folder(self, folder: str) -> None:
        """Remove a folder and the files in it. Succeeds quietly when it is already gone.

        The default deletes file by file, which every provider can do; the ones with a
        recursive delete override it with one call.
        """
        for path in await self.async_list(folder):
            await self.async_delete(path)

    async def async_close(self) -> None:  # noqa: B027 - deliberately optional
        """Let go of anything held open. Called when the integration unloads."""

    async def async_read_range(self, path: str, start: int, end: int) -> bytes:
        """Return bytes `start..end` (inclusive) of one file, to play it by range.

        The default reads the file and slices it, which every provider can do; the ones that
        can ask for a range (or stream and stop) override it, so playback never fetches a
        whole video for one segment.
        """
        return bytes(memoryview(await self.async_read(path))[start : end + 1])
