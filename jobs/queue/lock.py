"""Single-scheduler lock (flock, non-blocking)."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


class QueueLocked(RuntimeError):
    """Another scheduler already holds the queue."""


@contextmanager
def scheduler_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or "another process"
            raise QueueLocked(
                f"a queue scheduler is already running ({holder}). "
                f"Stop it first; the lock lives at {path}"
            ) from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()}\n")
        handle.flush()
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
