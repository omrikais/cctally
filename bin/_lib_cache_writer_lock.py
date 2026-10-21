"""Ordered cross-process writer locks for the shared cache.db family.

Every cache.db mutator takes the global writer lock first. Codex mutators then
take the provider lock before opening a SQLite write transaction. A single
deadline covers the complete lock set so adding the provider lock cannot double
the caller's bounded-wait contract.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import Callable


def acquire_ordered_flocks(
    locks: list[tuple[Path, int]],
    *,
    timeout: float | None = None,
    on_failure: Callable[[Path, BaseException], None] | None = None,
) -> list[int] | None:
    """Acquire ``(path, LOCK_SH|LOCK_EX)`` entries in caller-supplied order.

    ``on_failure(path, exc)``, when given, is told which lock the set lost to
    before this returns None (``exc`` a ``BlockingIOError``: the lock was
    held) or re-raises an ``OSError`` (an I/O error no holder caused), so a
    caller can wait for exactly that lock's release (#825/#832).
    """
    deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
    held: list[int] = []
    current: Path | None = None
    try:
        for path, mode in locks:
            current = path
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            held.append(fd)
            while True:
                try:
                    fcntl.flock(fd, mode | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if deadline is None or time.monotonic() >= deadline:
                        release_cache_writer_flocks(held)
                        if on_failure is not None:
                            on_failure(path, exc)
                        return None
                    time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return held
    except BaseException as exc:
        release_cache_writer_flocks(held)
        if on_failure is not None and current is not None and isinstance(exc, OSError):
            on_failure(current, exc)
        raise


def acquire_cache_writer_flocks(
    writer_path: Path,
    provider_path: Path | None = None,
    *,
    timeout: float | None = None,
    on_failure: Callable[[Path, BaseException], None] | None = None,
) -> list[int] | None:
    """Acquire global then optional provider flock, or return ``None``.

    ``timeout=None`` performs one non-blocking attempt for the entire set.
    A non-negative timeout bounds the complete acquisition, not each lock.
    Returned descriptors must be released with
    :func:`release_cache_writer_flocks`.
    """
    paths = [writer_path]
    if provider_path is not None and provider_path != writer_path:
        paths.append(provider_path)
    return acquire_ordered_flocks(
        [(path, fcntl.LOCK_EX) for path in paths],
        timeout=timeout,
        on_failure=on_failure,
    )


def release_cache_writer_flocks(held: list[int]) -> None:
    """Release an acquired lock set in reverse order."""
    for fd in reversed(held):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
