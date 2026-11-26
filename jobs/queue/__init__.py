"""Submission queue: durable, single-scheduler, restart-safe.

Layout
------
atomic.py      atomic write + checksum + .bak fallback (used by both stores)
lock.py        flock so two schedulers cannot both submit
model.py       QueueEntry and lifecycle states
store.py       QueueStore on top of atomic.py
reconcile.py   entry status <- run directory on the target (source of truth)
resume.py      find a run's newest checkpoint for --resume-from
scheduler.py   the drain loop
commands.py    `machine_mgr job queue …` implementations
"""

from .model import (ACTIVE, CANCELLED, DONE, FAILED, PENDING, RUNNING, SKIPPED,
                    STALE, QueueEntry)
from .resume import latest_checkpoint, resume_extra
from .store import QueueStore

__all__ = [
    "ACTIVE", "CANCELLED", "DONE", "FAILED", "PENDING", "RUNNING", "SKIPPED",
    "STALE", "QueueEntry", "QueueStore", "latest_checkpoint", "resume_extra",
]
