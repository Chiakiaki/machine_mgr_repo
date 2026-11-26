"""Queue persistence, de-duplication and resume-checkpoint discovery."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.queue import (CANCELLED, DONE, PENDING, RUNNING, QueueEntry, QueueStore,
                        latest_checkpoint)


class FakeRemote:
    """Minimal stand-in for jobs.transport.Remote."""

    def __init__(self, log: str = "", listing: str = "") -> None:
        self.log = log
        self.listing = listing
        self.calls: list[str] = []

    def read_file(self, path: str) -> str:
        self.calls.append(f"cat {path}")
        if not self.log:
            raise RuntimeError("no such file")
        return self.log

    def try_run(self, command: str, timeout: float = 60):
        self.calls.append(command)
        if "ls -1" in command:
            return (0 if self.listing else 1), self.listing, ""
        return 0, "", ""


def _entry(**kw) -> QueueEntry:
    base = dict(machine="alienware-m17", repo="bdp_benchmark", job="scripts/job_scripts/a.yaml")
    base.update(kw)
    return QueueEntry(**base)


def test_queue_roundtrip_and_order(tmp_path):
    path = tmp_path / "queue.yaml"
    store = QueueStore(path)
    store.add(_entry(job="a.yaml"))
    store.add(_entry(job="b.yaml"))
    assert [e.order for e in store.entries] == [1, 2]

    reloaded = QueueStore(path)
    assert len(reloaded.entries) == 2
    assert reloaded.pending()[0].job == "a.yaml"


def test_queue_dedupes_active_entries(tmp_path):
    store = QueueStore(tmp_path / "queue.yaml")
    first = store.add(_entry())
    second = store.add(_entry())
    assert first.id == second.id
    assert len(store.entries) == 1

    # finished entries may be queued again
    first.status = DONE
    store.save()
    third = store.add(_entry())
    assert third.id != first.id


def test_queue_allow_duplicate(tmp_path):
    store = QueueStore(tmp_path / "queue.yaml")
    store.add(_entry())
    store.add(_entry(), allow_duplicate=True)
    assert len(store.entries) == 2


def test_clear_keeps_active_only(tmp_path):
    store = QueueStore(tmp_path / "queue.yaml")
    done = store.add(_entry(job="a.yaml")); done.status = DONE
    running = store.add(_entry(job="b.yaml")); running.status = RUNNING
    cancelled = store.add(_entry(job="c.yaml")); cancelled.status = CANCELLED
    store.save()

    removed = store.clear(only_finished=True)
    assert removed == 2
    assert [e.job for e in store.entries] == ["b.yaml"]

    store.clear(only_finished=False)
    assert store.entries == []


def test_get_by_prefix(tmp_path):
    store = QueueStore(tmp_path / "queue.yaml")
    entry = store.add(_entry())
    assert store.get(entry.id[:6]).id == entry.id
    try:
        store.get("nope")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_latest_checkpoint_prefers_final_model():
    log = "blah\nRun directory: /home/u/proj/logs/benchmark/20260101_x\n"
    remote = FakeRemote(log=log, listing=(
        "/home/u/proj/logs/benchmark/20260101_x/models/step_400000_steps.zip\n"
        "/home/u/proj/logs/benchmark/20260101_x/models/step_2000000_steps.zip\n"
        "/home/u/proj/logs/benchmark/20260101_x/models/PPO_BDP_final_model.zip\n"
    ))

    class Run:
        run_dir = "/home/u/proj/.machine_mgr/runs/r1"

    assert latest_checkpoint(remote, Run()).endswith("PPO_BDP_final_model.zip")


def test_latest_checkpoint_without_log():
    class Run:
        run_dir = "/x"
    assert latest_checkpoint(FakeRemote(), Run()) is None
