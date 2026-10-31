"""The SSE link to the plugin: resume by `seq`, reconnect with backoff, notice when it is lost.

Three things the rest of the integration relies on:

* **Every message once, in order.** The stream is resumed with `?since=<seq>&epoch=<epoch>`
  and anything at or below the last `seq` seen in the same epoch is skipped, so a replay
  after a reconnect does not double-count.
* **An epoch change is announced.** The plugin restarting starts a new sequence; whatever
  was open under the old epoch can no longer be followed and is handed to
  `on_epoch_change` to be orphaned. The plugin says so in the `hello` that opens every
  connection (`reset`, or simply a new epoch). A `hello` with `gap` (the plugin's ring no
  longer holds what we asked for, so messages were lost) is handed to `on_reset` for the
  same reason.
* **A lost link is a fact with a time.** `healthy` is true only while connected and hearing
  from the plugin. After `LINK_LOST_SECONDS` without that, `on_lost` fires once — which is
  what orphans pending events (architecture §3.1).

Silence is treated as loss: the plugin sends a keepalive comment well inside
`STREAM_IDLE_TIMEOUT`, so a socket that has gone quiet for longer is dead even if TCP has not
noticed yet.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import json
import logging
import time
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .api import StaminaApiError, StaminaAuthError, StaminaClient
from .const import BACKOFF_INITIAL, BACKOFF_MAX, LINK_LOST_SECONDS, STREAM_IDLE_TIMEOUT
from .protocol import MSG_HELLO, Message, ProtocolError, parse_message
from .redact import scrub_credentials

_LOGGER = logging.getLogger(__name__)


class _HandlerError(Exception):
    """A message could not be handled; the stream is reopened to replay it."""


# A single SSE message (all its data lines) larger than this is not one the plugin sends.
_MAX_MESSAGE = 1024 * 1024
# A message whose handling fails is retried (by reconnecting, which replays it) this many
# times before it is skipped; a message that can never be handled must not stop the stream.
_MAX_ATTEMPTS = 3


class StaminaLink:
    """Keeps one SSE stream open and hands its messages over in order."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: StaminaClient,
        *,
        seq: int | None,
        epoch: str | None,
        on_message: Callable[[Message], Awaitable[None]],
        on_epoch_change: Callable[[str | None, str], Awaitable[None]],
        on_reset: Callable[[], Awaitable[None]],
        on_lost: Callable[[], Awaitable[None]],
        on_state: Callable[[], None] | None = None,
        on_auth_failed: Callable[[], None] | None = None,
        on_skipped: Callable[[Message], Awaitable[None]] | None = None,
        lost_after: float = LINK_LOST_SECONDS,
        idle_timeout: float = STREAM_IDLE_TIMEOUT,
        backoff: tuple[float, float] = (BACKOFF_INITIAL, BACKOFF_MAX),
    ) -> None:
        """Configure the link; nothing connects until `async_start`."""
        self._hass = hass
        self._client = client
        self.seq = seq
        self.epoch = epoch
        self._on_message = on_message
        self._on_epoch_change = on_epoch_change
        self._on_reset = on_reset
        self._on_lost = on_lost
        self._on_state = on_state
        self._on_auth_failed = on_auth_failed
        self._on_skipped = on_skipped
        self._lost_after = lost_after
        self._idle_timeout = idle_timeout
        self._backoff = backoff
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._lost_timer: CALLBACK_TYPE | None = None
        self.connected = False
        # Caught up: the `hello` handled and the replay read up to its `last_seq`.
        self.caught_up = False
        self._catch_up_to: int | None = None
        self._failures: dict[int, int] = {}
        self._live_only = False
        # Starts "lost" from the moment of setup: a link that never connects is lost too.
        self.lost_since: float | None = time.time()
        self.lost_fired = False
        self.connects = 0
        self.last_message: float | None = None
        self.last_error: str | None = None

    @property
    def healthy(self) -> bool:
        """Whether the link is connected *and caught up*.

        Being connected is not enough to decide anything: until the replay of what was
        missed has been read, a verdict that would keep an event may still be on its way.
        """
        return self.connected and self.caught_up

    def async_start(self) -> None:
        """Start the connect-and-read loop in the background."""
        self._arm_lost_timer()
        self._task = self._hass.async_create_background_task(
            self._async_run(), "scrypted_stamina link"
        )

    async def async_stop(self) -> None:
        """Stop reading and cancel the timers."""
        self._stopping = True
        self._cancel_lost_timer()
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected = False

    # ------------------------------------------------------------------ health

    @callback
    def _arm_lost_timer(self) -> None:
        self._cancel_lost_timer()
        if self.lost_fired or self._stopping:
            return
        elapsed = time.time() - (self.lost_since or time.time())
        self._lost_timer = async_call_later(
            self._hass, max(0.0, self._lost_after - elapsed), self._lost_timer_fired
        )

    @callback
    def _cancel_lost_timer(self) -> None:
        if self._lost_timer is not None:
            self._lost_timer()
            self._lost_timer = None

    @callback
    def _lost_timer_fired(self, _now: Any) -> None:
        self._lost_timer = None
        if self.connected or self.lost_fired:
            return
        self.lost_fired = True
        _LOGGER.warning(
            "Link to the Stamina plugin lost for %.0f s; open events are kept as orphaned",
            self._lost_after,
        )
        self._hass.async_create_task(self._on_lost())

    @callback
    def _set_caught_up(self, caught_up: bool) -> None:
        if caught_up == self.caught_up:
            return
        self.caught_up = caught_up
        if self._on_state is not None:
            self._on_state()

    @callback
    def _set_connected(self, connected: bool) -> None:
        if not connected:
            self.caught_up = False
        if connected == self.connected:
            return
        self.connected = connected
        if connected:
            self.lost_since = None
            self.lost_fired = False
            self._cancel_lost_timer()
        else:
            self.lost_since = time.time()
            self._arm_lost_timer()
        if self._on_state is not None:
            self._on_state()

    # ------------------------------------------------------------------ loop

    async def _async_run(self) -> None:
        """Connect, read until the stream ends, wait, and connect again."""
        delay = self._backoff[0]
        while True:
            try:
                await self._async_read_stream()
                delay = self._backoff[0]
            except asyncio.CancelledError:
                raise
            except StaminaAuthError as err:
                self.last_error = scrub_credentials(str(err), secrets=(self._client.token,))
                if self._on_auth_failed is not None:
                    self._on_auth_failed()
                delay = self._backoff[1]
            except (StaminaApiError, TimeoutError, OSError) as err:
                self.last_error = scrub_credentials(str(err), secrets=(self._client.token,))
                _LOGGER.debug("Stamina stream dropped: %s", self.last_error)
            except _HandlerError as err:
                self.last_error = scrub_credentials(str(err), secrets=(self._client.token,))
                delay = self._backoff[0]
            except Exception:
                self.last_error = "unexpected error"
                _LOGGER.exception("Unexpected error on the Stamina stream")
            finally:
                self._set_connected(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._backoff[1])

    async def _async_read_stream(self) -> None:
        """Read one connection's worth of messages."""
        # Without a position the plugin replays nothing: caught up as soon as it says hello.
        self._live_only = self.seq is None or self.epoch is None
        response = await self._client.async_open_stream(self.seq, self.epoch)
        try:
            self.connects += 1
            self._catch_up_to = None
            self._set_connected(True)
            data: list[str] = []
            size = 0
            while True:
                async with asyncio.timeout(self._idle_timeout):
                    raw = await response.content.readline()
                if not raw:
                    return  # the server closed the stream
                size += len(raw)
                if size > _MAX_MESSAGE:
                    raise StaminaApiError("an SSE message is too large")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self.last_message = time.time()
                if not line:
                    if data:
                        await self._async_dispatch("\n".join(data))
                        data = []
                    size = 0
                    continue
                if line.startswith(":"):
                    continue  # keepalive comment
                field, _, value = line.partition(":")
                if field == "data":
                    data.append(value[1:] if value.startswith(" ") else value)
        finally:
            response.release()

    async def _async_dispatch(self, payload: str) -> None:
        """Parse one message and hand it over, once and in order."""
        try:
            message = parse_message(json.loads(payload))
        except (ValueError, ProtocolError) as err:
            _LOGGER.debug("Dropping a message from the plugin: %s", err)
            return

        new_epoch = bool(message.epoch) and message.epoch != self.epoch
        if new_epoch or (message.type == MSG_HELLO and message.reset):
            previous = self.epoch
            self.epoch = message.epoch or self.epoch
            self.seq = None
            if previous is not None:
                _LOGGER.info("The Stamina plugin restarted (new epoch); open events orphaned")
                await self._on_epoch_change(previous, self.epoch or "")

        if message.type == MSG_HELLO:
            if message.gap and not message.reset:
                _LOGGER.info("Messages from the Stamina plugin were lost; open events orphaned")
                await self._on_reset()
            self._catch_up_to = message.last_seq
            self._check_caught_up()
            return
        if message.seq is None:
            return
        if self.seq is not None and message.seq <= self.seq:
            return  # a replay of something already handled
        try:
            await self._on_message(message)
        except Exception as err:
            attempts = self._failures.get(message.seq, 0) + 1
            self._failures[message.seq] = attempts
            if attempts < _MAX_ATTEMPTS:
                _LOGGER.warning("Handling message %s failed; it will be replayed", message.seq)
                # Not advancing `seq`: reconnecting resumes from before this message.
                raise _HandlerError(f"message {message.seq} failed: {err}") from err
            _LOGGER.exception("Skipping message %s after %s failures", message.seq, attempts)
            if self._on_skipped is not None:
                # Whatever it said about its event is lost: that event is kept, not guessed.
                with suppress(Exception):
                    await self._on_skipped(message)
        self._failures.pop(message.seq, None)
        self.seq = message.seq
        self._check_caught_up()

    @callback
    def _check_caught_up(self) -> None:
        target = self._catch_up_to
        if (
            self._live_only
            or target is None
            or target == 0
            or (self.seq is not None and self.seq >= target)
        ):
            self._set_caught_up(True)
