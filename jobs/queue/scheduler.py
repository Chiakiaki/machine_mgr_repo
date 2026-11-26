"""The drain loop: one job at a time, gated by the repo's limits."""

from __future__ import annotations

import datetime as _dt
import sys
import time

from .. import catalog, launcher, preflight, tracker
from ..targets import local_repo_path, target_for, with_resume
from . import lock, model
from .reconcile import reconcile


def _say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def plan(ctx, store) -> int:
    """--dry-run: report what would happen, submit nothing."""
    pending = store.pending()
    if not pending:
        _say("queue empty — nothing to plan")
        return 0
    for entry in pending:
        try:
            target = target_for(ctx, entry.machine)
            repo = catalog.resolve_repo(target.remote, target.project_root, entry.repo)
        except (KeyError, RuntimeError) as exc:
            _say(f"[{entry.order}] {entry.id} SKIP — {str(exc).strip(chr(39))}")
            continue
        ready, reasons = preflight.readiness(target.remote, repo.path, repo.descriptor)
        _say(f"[{entry.order}] {entry.id} {'would submit' if ready else 'would wait'}: "
             f"{entry.repo}:{entry.job}")
        if reasons:
            _say(f"      blocked by: {'; '.join(reasons)}")
    return 0


def drain(ctx, store, poll: float = 60.0, once: bool = False) -> int:
    failures = 0
    try:
        lock_path = store.path.with_name(store.path.name + ".lock")
        with lock.scheduler_lock(lock_path):
            _say(f"queue drain started (poll {poll:.0f}s, lock {lock_path.name}); "
                 f"Ctrl-C stops the scheduler, the running job keeps going")
            while True:
                for entry_id, status in reconcile(ctx, store, target_for, verbose=False):
                    _say(f"[{status}] {entry_id}")
                    if status == model.FAILED:
                        failures += 1

                pending = store.pending()
                if not pending and not store.running():
                    _say("queue empty — nothing left")
                    break

                started = False
                for entry in list(pending):
                    try:
                        target = target_for(ctx, entry.machine)
                        repo = catalog.resolve_repo(target.remote, target.project_root, entry.repo)
                    except (KeyError, RuntimeError) as exc:
                        entry.status = model.SKIPPED
                        entry.note = str(exc).strip("'")
                        store.save()
                        _say(f"[skipped] {entry.id}: {entry.note}")
                        continue

                    ready, reasons = preflight.readiness(target.remote, repo.path, repo.descriptor)
                    if not ready:
                        _say(f"waiting: {'; '.join(reasons)}")
                        break

                    try:
                        extra = with_resume(ctx, entry.machine, entry.resume_from, entry.extra)
                    except RuntimeError as exc:
                        entry.status = model.SKIPPED
                        entry.note = str(exc)
                        store.save()
                        _say(f"[skipped] {entry.id}: {exc}")
                        continue

                    from ..preflight import local_git_state
                    local_repo = local_repo_path(ctx, repo.name)
                    sha = (local_git_state(local_repo) or {}).get("head", "") if local_repo else ""
                    try:
                        record = launcher.submit(remote=target.remote, repo=repo,
                                                 job=entry.job, extra=extra,
                                                 project_root=target.project_root,
                                                 submitter_sha=sha)
                    except RuntimeError as exc:
                        entry.status = model.FAILED
                        entry.note = str(exc)[:300]
                        entry.finished = _dt.datetime.now().astimezone() \
                            .isoformat(timespec="seconds")
                        store.save()
                        failures += 1
                        _say(f"[failed] {entry.id}: {entry.note}")
                        continue
                    record.update({"machine": target.machine.display,
                                   "machine_mac": target.machine.mac})
                    run = ctx.runs.add(tracker.Run(**{
                        k: v for k, v in record.items() if k in tracker.Run.__dataclass_fields__
                    }))
                    entry.status = model.RUNNING
                    entry.run_id = run.id
                    entry.started = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
                    store.save()
                    _say(f"[submitted] {entry.id} -> {run.id}")
                    _say(f"            log {run.run_dir}/log")
                    started = True
                    break

                if once:
                    break
                time.sleep(poll if started else min(poll, 30))
    except lock.QueueLocked as exc:
        _say(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        store.save()
        _say("scheduler stopped; pending entries are preserved — "
             "`machine_mgr job queue run` resumes them")
        return 130

    return 1 if failures else 0
