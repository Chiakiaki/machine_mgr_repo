"""`machine_mgr job queue …` implementations (thin: state lives in the package)."""

from __future__ import annotations

import sys

from .. import catalog
from ..targets import target_for
from . import model, scheduler
from .model import QueueEntry


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


def run(ctx, poll: float = 60.0, once: bool = False, dry_run: bool = False) -> int:
    if dry_run:
        return scheduler.plan(ctx, ctx.queue)
    return scheduler.drain(ctx, ctx.queue, poll=poll, once=once)
