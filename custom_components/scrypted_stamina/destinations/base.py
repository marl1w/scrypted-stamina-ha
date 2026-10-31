"""What a destination has to be, and the one thing they all get wrong the same way.

Ported from Reolink Stamina, where a destination stored, deleted and listed whole clips.
Scrypted Stamina writes one file per segment into a pending folder while the event lasts,
then commits it as one video: the segments read back, joined, stored, and the folder
deleted. So the interface grows:

* `async_read` — read a file back, to join the segments and to play a committed event
  through Home Assistant;
* `async_store_video` — store the joined video and confirm its size: `async_store`, then
  `async_size`. A provider with a per-request limit lower than a long event overrides
  `async_store` with its chunked upload;
* `async_size` — one file's size, by listing its folder unless the provider can ask;
* `async_delete_folder` — remove a pending folder and everything in it.

Retrying is still the implementations' problem, and treated as one: a throttled or briefly
unavailable service is waited out here, with the service's own `Retry-After` honoured, and
`async_store` returns only once the service has confirmed what it stored.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import mimetypes

_LOGGER = logging.getLogger(__name__)

# A segment that fails is worth a few more tries before the uploader hears about it.
REQUEST_ATTEMPTS = 4
BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0

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
    ".m3u8": "application/vnd.apple.mpegurl",
    ".json": "application/json",
    ".jpg": "image/jpeg",
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
    """Raised when a file exceeds what this destination will take in one go."""


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

    async def async_store_video(self, path: str, data: bytes) -> None:
        """Store a committed event's video, and confirm the destination holds all of it."""
        await self.async_store(path, data)
        stored = await self.async_size(path)
        if stored != len(data):
            raise DestinationError(
                f"{self.label} holds {stored} bytes of {path}, not the {len(data)} stored"
            )

    async def async_delete_folder(self, folder: str) -> None:
        """Remove a folder and the files in it. Succeeds quietly when it is already gone.

        The default deletes file by file, which every provider can do; the ones with a
        recursive delete override it with one call.
        """
        for path in await self.async_list(folder):
            await self.async_delete(path)

    async def async_close(self) -> None:  # noqa: B027 - deliberately optional
        """Let go of anything held open. Called when the integration unloads."""
