"""Entry status follows the run directory on the target."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.queue import model
from jobs.queue import reconcile
from jobs.queue.model import QueueEntry
from jobs.queue.store import QueueStore


class FakeRuns:
    def __init__(self, status="finished", exit_code=0):
        self.run = SimpleNamespace(status=status, exit_code=exit_code, finished="")
        self.saved = 0

    def get(self, run_id):
        return self.run

    def save(self):
        self.saved += 1


class Ctx:
    def __init__(self, status="finished", exit_code=0):
        self.runs = FakeRuns(status, exit_code)


def _store(tmp_path, status=model.RUNNING):
    store = QueueStore(tmp_path / "queue.yaml")
    entry = store.add(QueueEntry(machine="m", repo="r", job="j.yaml"))
    entry.status = status
    entry.run_id = "run-1"
    store.save()
    return store, entry


def _target_for(*_a, **_kw):
    return SimpleNamespace(remote=object())


def test_finished_maps_to_done(tmp_path, monkeypatch):
    store, entry = _store(tmp_path)
    monkeypatch.setattr(reconcile.tracker, "refresh", lambda remote, run: run)
    changes = reconcile.reconcile(Ctx("finished"), store, _target_for)
    assert changes == [(entry.id, model.DONE)]
    assert entry.finished


def test_lost_maps_to_stale(tmp_path, monkeypatch):
    store, entry = _store(tmp_path)
    monkeypatch.setattr(reconcile.tracker, "refresh", lambda remote, run: run)
    reconcile.reconcile(Ctx("lost"), store, _target_for)
    assert entry.status == model.STALE


def test_cancelled_and_failed(tmp_path, monkeypatch):
    for run_status, expected in (("cancelled", model.CANCELLED), ("failed", model.FAILED)):
        store, entry = _store(tmp_path / run_status)
        monkeypatch.setattr(reconcile.tracker, "refresh", lambda remote, run: run)
        reconcile.reconcile(Ctx(run_status, exit_code=1), store, _target_for)
        assert entry.status == expected


def test_unreachable_machine_keeps_status(tmp_path, monkeypatch):
    store, entry = _store(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("machine offline")

    changes = reconcile.reconcile(Ctx(), store, boom)
    assert changes == []
    assert entry.status == model.RUNNING
    assert "unreachable" in entry.note


def test_missing_run_entry_is_tolerated(tmp_path, monkeypatch):
    store, entry = _store(tmp_path)
    entry.run_id = "does-not-exist"
    store.save()

    class EmptyRuns(FakeRuns):
        def get(self, run_id):
            raise KeyError(run_id)

    ctx = Ctx()
    ctx.runs = EmptyRuns()
    assert reconcile.reconcile(ctx, store, _target_for) == []
    assert entry.status == model.RUNNING
