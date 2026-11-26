"""Persistent submission queue for unattended batches.

The queue lives in ``data/queue.yaml`` so it survives process restarts: stopping
the scheduler (Ctrl-C, laptop sleep) never loses pending work, and the job that
is already running on the target keeps running because it is detached there.
"""

from __future__ import annotations

import datetime as _dt
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .transport import Remote

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
SKIPPED = "skipped"
ACTIVE = (PENDING, RUNNING)


def _now() -> str:
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
    added: str = field(default_factory=_now)
    started: str = ""
    finished: str = ""
    id: str = field(default_factory=lambda: f"q{secrets.token_hex(4)}")
    order: int = 0

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Identity for de-duplication: same target, repo, job, extra."""
        return (self.machine, self.repo, self.job, f"{self.extra}|{self.resume_from}")


class QueueStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[QueueEntry] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        data = yaml.safe_load(self.path.read_text()) or {}
        for raw in data.get("entries", []):
            if not isinstance(raw, dict) or not raw.get("job"):
                continue
            known = {f for f in QueueEntry.__dataclass_fields__}
            self.entries.append(QueueEntry(**{k: v for k, v in raw.items() if k in known}))
        self._reindex()

    def _reindex(self) -> None:
        for i, entry in enumerate(sorted(self.entries, key=lambda e: (e.order, e.added)), 1):
            entry.order = i

    def save(self) -> None:
        self._reindex()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated": _now(),
                   "entries": [asdict(e) for e in sorted(self.entries, key=lambda e: e.order)]}
        self.path.write_text("# machine_mgr submission queue — safe to edit while nothing is running\n"
                             + yaml.safe_dump(payload, sort_keys=False))

    # ── queries ────────────────────────────────────────────────────────────
    def add(self, entry: QueueEntry, allow_duplicate: bool = False) -> QueueEntry:
        if not allow_duplicate:
            for existing in self.entries:
                if existing.key == entry.key and existing.status in ACTIVE:
                    return existing
        entry.order = len(self.entries) + 1
        self.entries.append(entry)
        self.save()
        return entry

    def get(self, entry_id: str) -> QueueEntry:
        for entry in self.entries:
            if entry.id == entry_id or entry.id.startswith(entry_id):
                return entry
        raise KeyError(f"no queue entry matches '{entry_id}'")

    def pending(self) -> list[QueueEntry]:
        return [e for e in sorted(self.entries, key=lambda e: e.order) if e.status == PENDING]

    def running(self) -> list[QueueEntry]:
        return [e for e in self.entries if e.status == RUNNING]

    def clear(self, only_finished: bool = True) -> int:
        before = len(self.entries)
        if only_finished:
            self.entries = [e for e in self.entries if e.status in ACTIVE]
        else:
            self.entries = []
        self.save()
        return before - len(self.entries)


# ── resume support ──────────────────────────────────────────────────────────

RUN_DIR_RE = re.compile(r"Run directory:\s*(\S+)")


def latest_checkpoint(remote: Remote, run) -> str | None:
    """Newest SB3 checkpoint of a finished/unfinished run, or None.

    The training code prints its output directory; checkpoints live in
    ``<run dir>/models/*_steps.zip``. A final model is preferred when present.
    """
    try:
        log = remote.read_file(f"{run.run_dir}/log")
    except RuntimeError:
        return None
    matches = RUN_DIR_RE.findall(log)
    if not matches:
        return None
    out_dir = matches[-1]
    code, listing, _ = remote.try_run(f"ls -1 {_q(out_dir)}/models/*.zip 2>/dev/null")
    if code != 0 or not listing.strip():
        return None
    candidates = [line for line in listing.splitlines() if line.strip()]

    def step(path: str) -> int:
        m = re.search(r"step_(\d+)_steps\.zip$", path)
        if m:
            return int(m.group(1))
        return 10**12 if "final_model" in path else 0

    return max(candidates, key=step)


def resume_extra(remote: Remote, run) -> str | None:
    ckpt = latest_checkpoint(remote, run)
    return f"--resume_model_path {ckpt}" if ckpt else None


def _q(text: str) -> str:
    import shlex
    return shlex.quote(text)
