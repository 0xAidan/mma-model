"""Overlap protection compatible with one-writer flock (DWCS-205)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Platform flock primitive. Documented fallback: environments without fcntl
# (e.g. Windows) cannot use FileFlockLock; callers must inject another
# OverlapProtection implementation for those platforms.
try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


class OverlapError(RuntimeError):
    """Raised when another writer already holds the job lock."""


class OverlapProtection(Protocol):
    """Interface compatible with host systemd ``flock -n`` one-writer semantics."""

    def acquire(self) -> None: ...

    def release(self) -> None: ...


@dataclass
class FileFlockLock:
    """Non-blocking exclusive flock around a lock file path."""

    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if fcntl is None:
            raise OverlapError(
                "fcntl is unavailable on this platform; inject OverlapProtection"
            )
        if self._fd is not None:
            raise OverlapError(f"lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise OverlapError(f"another writer holds {self.path}") from exc
        except OSError as exc:
            os.close(fd)
            raise OverlapError(f"failed to flock {self.path}: {exc}") from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        if fcntl is None:
            os.close(self._fd)
            self._fd = None
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


@contextmanager
def hold_overlap_lock(lock: OverlapProtection) -> Iterator[None]:
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


__all__ = [
    "FileFlockLock",
    "OverlapError",
    "OverlapProtection",
    "hold_overlap_lock",
]
