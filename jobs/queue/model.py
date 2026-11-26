"""Queue entry model and lifecycle states."""

from __future__ import annotations

import datetime as _dt
import secrets
from dataclasses import dataclass, field

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
SKIPPED = "skipped"
STALE = "stale"        # run dir exists but the process is gone
ACTIVE = (PENDING, RUNNING)
FINISHED = (DONE, FAILED, CANCELLED, SKIPPED)


def now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class QueueEntry:
    machine: str
    repo: str
    job: str
    extra: str = ""
    resume_from: str = ""
    status: str = PENDING
    run_id: str = ""
    note: str = ""
    added: str = field(default_factory=now)
    started: str = ""
    finished: str = ""
    checked: str = ""
    id: str = field(default_factory=lambda: f"q{secrets.token_hex(4)}")
    order: int = 0

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Identity for de-duplication: same target, repo, job, extra."""
        return (self.machine, self.repo, self.job, f"{self.extra}|{self.resume_from}")
