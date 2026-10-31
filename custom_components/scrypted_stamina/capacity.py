"""How many segment uploads this machine can have in flight at once.

Ported from Reolink Stamina's `cloud/capacity.py`, where whole clips were assembled in
memory and the number of slots fell out of how much memory was free. Segments are small, so
memory rarely binds here, but the same arithmetic still applies — a segment is held in
memory from fetch to upload, for every destination — and the same two halves are kept apart:
reading what the machine has (platform-specific, untestable) and turning it into a number of
slots (arithmetic, tested).

Where the machine cannot be read at all, the answer is one: never wrong, only slow.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .const import MAX_PARALLEL_UPLOADS

# The share of free memory segments in flight may take, and an absolute ceiling on it.
MEMORY_SHARE = 0.10
MEMORY_CEILING = 256 * 1024 * 1024

_LOGGER = logging.getLogger(__name__)

# cgroup v2, then v1. Read before the host's own figures because a container is usually given
# far less than the machine it runs on, and Home Assistant is usually in one: `/proc/meminfo`
# inside a container cheerfully reports the whole host, which is how a 256 MB container decides
# it has 16 GB to play with.
_CGROUP2_MAX = Path("/sys/fs/cgroup/memory.max")
_CGROUP2_CURRENT = Path("/sys/fs/cgroup/memory.current")
_CGROUP1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_CGROUP1_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")

# A cgroup with no limit reports a number close to the whole address space rather than saying
# so. Anything above this is "unlimited", and the host's figures are the honest answer.
_UNLIMITED = 1 << 60


def _read_int(path: Path) -> int | None:
    """Return one integer from a /sys or /proc file, or None if it cannot be read."""
    try:
        text = path.read_text().strip()
    except (OSError, ValueError):
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _cgroup_available() -> int | None:
    """Return what a container has left of its own memory limit, if it is in one."""
    for limit_path, usage_path in (
        (_CGROUP2_MAX, _CGROUP2_CURRENT),
        (_CGROUP1_LIMIT, _CGROUP1_USAGE),
    ):
        limit = _read_int(limit_path)
        if limit is None or limit <= 0 or limit >= _UNLIMITED:
            continue
        used = _read_int(usage_path) or 0
        return max(0, limit - used)
    return None


def _host_available() -> int | None:
    """Return the host's free memory.

    `sysconf` rather than `/proc/meminfo` because it answers on macOS as well as Linux, and a
    development machine reporting nothing would otherwise be pinned to one slot for no reason.
    `SC_AVPHYS_PAGES` is free pages rather than total, which is the number worth budgeting a
    share of.
    """
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if pages <= 0 or size <= 0:
        return None
    return pages * size


def available_bytes() -> int | None:
    """Return the memory this process can reasonably expect to be able to use.

    None where nothing could be read, which the caller treats as "assume the smallest machine".

    Reads files, so it belongs in the executor. They are a few bytes of `/sys` and answer
    instantly, but "instantly" is not "without blocking" and Home Assistant is right to say so:
    call this through `async_add_executor_job`, never straight from the event loop.
    """
    return _cgroup_available() or _host_available()


def slots(available: int | None, expected_segment: int) -> int:
    """Return how many segment uploads may be in flight at once.

    `available` is free memory in bytes, or None where it could not be read.
    `expected_segment` is what one segment is expected to cost, measured from recent ones.

    Never zero, and never above `MAX_PARALLEL_UPLOADS`. Reolink Stamina also capped this at
    one per CPU, because each clip was cut by an ffmpeg of its own; a segment upload is only
    waiting on a socket, so that cap is dropped here.
    """
    if available is None or available <= 0 or expected_segment <= 0:
        return 1
    budget = min(int(available * MEMORY_SHARE), MEMORY_CEILING)
    return max(1, min(int(budget // expected_segment), MAX_PARALLEL_UPLOADS))


def expected_bytes(sizes: list[int], assumed: int) -> int:
    """Return what the next segment should be budgeted at: the largest recent one."""
    return max([*sizes, 1]) if sizes else assumed


def describe(available: int | None, expected_segment: int) -> str:
    """Return a line for diagnostics, so a slot count is never a mystery."""
    if available is None:
        return f"1 upload at a time (memory unreadable, {expected_segment / 1e3:.0f} kB each)"
    return (
        f"{slots(available, expected_segment)} uploads at a time "
        f"({available / 1e6:.0f} MB free, {expected_segment / 1e3:.0f} kB each)"
    )
