"""Bring queue entries back in line with what the target actually says.

Entry status is a cache of scheduling intent; the run directory on the target is
the source of truth. Reconciliation runs at the start of every scheduler pass and
on demand from `queue list --refresh`.
"""

from __future__ import annotations

from .. import tracker
from .model import (CANCELLED, DONE, FAILED, RUNNING, STALE, QueueEntry, now)

# run status -> queue entry status
RUN_TO_ENTRY = {
    "finished": DONE,
    "cancelled": CANCELLED,
    "failed": FAILED,
    "lost": STALE,
}

CHANGED = {DONE, FAILED, CANCELLED, STALE}


def refresh_entry(ctx, entry: QueueEntry, target_for) -> str | None:
    """Return the new status when it changed, else None."""
    if not entry.run_id:
        return None
    try:
        run = ctx.runs.get(entry.run_id)
        target = target_for(ctx, entry.machine)
        tracker.refresh(target.remote, run)
    except (KeyError, RuntimeError) as exc:
        entry.note = f"unreachable: {exc}"
        entry.checked = now()
        return None

    entry.checked = now()
    new_status = RUN_TO_ENTRY.get(run.status)
    if new_status is None or new_status == entry.status:
        return None
    entry.status = new_status
    entry.note = f"exit {run.exit_code}" if run.exit_code is not None else run.status
    if new_status in CHANGED:
        entry.finished = now()
    return new_status


def reconcile(ctx, store, target_for, verbose: bool = False) -> list[tuple[str, str]]:
    """Refresh every running entry; returns [(entry_id, new_status)]."""
    changes: list[tuple[str, str]] = []
    for entry in store.running():
        new_status = refresh_entry(ctx, entry, target_for)
        if new_status:
            changes.append((entry.id, new_status))
            if verbose:
                print(f"  [{new_status}] {entry.id} {entry.repo}:{entry.job}")
    if changes:
        store.save()
        ctx.runs.save()
    return changes
