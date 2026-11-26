"""`machine_mgr job queue …` implementations (thin: state lives in the package)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .. import catalog
from ..targets import target_for
from . import model, scheduler
from .model import QueueEntry

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT = "machine-mgr-queue"


def _say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1):]


def add(ctx, machine: str, repo: str, job: str, extra: str = "",
        resume_from: str = "", allow_duplicate: bool = False) -> int:
    # Validate here: enqueuing a typo would otherwise fail hours later, when the
    # scheduler reaches that entry.
    try:
        target = target_for(ctx, machine)
        repo_ref = catalog.resolve_repo(target.remote, target.project_root, repo)
    except (KeyError, RuntimeError) as exc:
        _say(f"error: {str(exc).strip(chr(39))}")
        return 1
    if job not in repo_ref.jobs:
        matches = [j for j in repo_ref.jobs if j.endswith(job) or job in j]
        if len(matches) == 1:
            job = matches[0]
        else:
            hint = "\n      ".join(matches[:8] or repo_ref.jobs[:8])
            _say(f"error: '{job}' is not a job in {repo_ref.name}. Candidates:\n      {hint}")
            return 1

    entry = QueueEntry(machine=machine, repo=repo_ref.name, job=job,
                       extra=extra, resume_from=resume_from)
    stored = ctx.queue.add(entry, allow_duplicate=allow_duplicate)
    if stored.id != entry.id:
        _say(f"already queued as {stored.id} ({stored.status}); skipped")
        return 0
    _say(f"queued {stored.id} [#{stored.order}] {repo}:{job}")
    if extra:
        _say(f"extra: {extra}")
    if resume_from:
        _say(f"resume from run {resume_from}")
    return 0


def list_entries(ctx, refresh: bool = False) -> int:
    store = ctx.queue
    if refresh:
        from .reconcile import reconcile
        for entry_id, status in reconcile(ctx, store, target_for, verbose=True):
            _say(f"[{status}] {entry_id}")
    entries = sorted(store.entries, key=lambda e: e.order)
    if not entries:
        _say("queue is empty")
        return 0
    print(f"\n  {'id':12s} {'#':>2s} {'machine':16s} {'repo':14s} {'job':42s} "
          f"{'status':9s} {'checked':20s} run")
    print("  " + "-" * 122)
    for e in entries:
        print(f"  {e.id:12s} {e.order:2d} {e.machine[:16]:16s} {e.repo[:14]:14s} "
              f"{_short(e.job, 42):42s} {e.status:9s} {e.checked or '-':20s} {e.run_id or '-'}")
    pending = len([e for e in entries if e.status == model.PENDING])
    running = len([e for e in entries if e.status == model.RUNNING])
    stale = len([e for e in entries if e.status == model.STALE])
    _say(f"{pending} pending, {running} running, {stale} stale")
    for warning in store.warnings:
        _say(f"warning: {warning}")
    return 0


def clear(ctx, everything: bool = False) -> int:
    removed = ctx.queue.clear(only_finished=not everything)
    _say(f"removed {removed} entr{'y' if removed == 1 else 'ies'}")
    return 0


def run(ctx, poll: float = 60.0, once: bool = False, dry_run: bool = False,
        detach: bool = False) -> int:
    if dry_run:
        return scheduler.plan(ctx, ctx.queue)
    if detach:
        return detach_scheduler(ctx, poll)
    return scheduler.drain(ctx, ctx.queue, poll=poll, once=once)


def detach_scheduler(ctx, poll: float) -> int:
    """Run the scheduler outside this ssh/shell session.

    A plain `setsid nohup` still dies when the launching session's cgroup is
    torn down (observed: systemd stopped the dsh subprocess scope and took the
    scheduler with it, while the remote job kept training). A transient user
    unit is independent of that scope.
    """
    log = Path.home() / ".cache" / "machine_mgr-queue.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    inner = (f"exec ./machine_mgr job queue run --poll {poll:g} "
             f"> {log} 2>&1")
    if _have_systemd_run():
        subprocess.run(["systemctl", "--user", "reset-failed", UNIT],
                       capture_output=True, text=True)
        proc = subprocess.run([
            "systemd-run", "--user", "--unit", UNIT, "--collect",
            "--working-directory", str(REPO_ROOT),
            "--", "bash", "-c", inner,
        ], capture_output=True, text=True)
        if proc.returncode == 0:
            _say(f"scheduler started as user unit {UNIT}")
            _say(f"log      {log}")
            _say(f"status   systemctl --user status {UNIT}")
            _say(f"stop     systemctl --user stop {UNIT}")
            return 0
        _say(f"systemd-run failed ({proc.stderr.strip()[:160]}) — falling back to nohup")
    subprocess.Popen(["setsid", "bash", "-c", inner], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _say(f"scheduler started with setsid (log {log})")
    _say("warning: setsid survives the shell but NOT a session-scope teardown; "
         "prefer systemd-run where available")
    return 0


def _have_systemd_run() -> bool:
    from shutil import which
    if not which("systemd-run") or not which("systemctl"):
        return False
    try:
        proc = subprocess.run(["systemctl", "--user", "is-system-running"],
                              capture_output=True, text=True, timeout=5)
        return proc.stdout.strip() in ("running", "degraded")
    except (OSError, subprocess.TimeoutExpired):
        return False


def status(ctx) -> int:
    """Report scheduler liveness + queue counts."""
    store = ctx.queue
    lock_path = store.path.with_name(store.path.name + ".lock")
    holder = ""
    if lock_path.exists():
        holder = lock_path.read_text().strip()
    alive = False
    if holder.startswith("pid "):
        try:
            os.kill(int(holder.split()[1]), 0)
            alive = True
        except (ValueError, ProcessLookupError, PermissionError):
            alive = False
    unit_state = ""
    try:
        proc = subprocess.run(["systemctl", "--user", "is-active", UNIT],
                              capture_output=True, text=True, timeout=5)
        unit_state = proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    pending = len(store.pending())
    running = len(store.running())
    _say(f"queue     {len(store.entries)} entries: {pending} pending, {running} running")
    if alive:
        _say(f"scheduler running ({holder}, lock {lock_path.name})")
    else:
        _say(f"scheduler NOT running (stale lock: {holder or 'none'})")
        if pending:
            _say("resume with: ./machine_mgr job queue run --detach")
    if unit_state:
        _say(f"systemd unit {UNIT}: {unit_state}")
    for warning in store.warnings:
        _say(f"warning: {warning}")
    return 0 if alive or not pending else 1
