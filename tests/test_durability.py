"""Durability guarantees: atomic writes, backup fallback, lock, liveness."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.queue import atomic, lock
from jobs.queue.model import PENDING, QueueEntry
from jobs.queue.store import QueueStore


def _entry(job="a.yaml") -> QueueEntry:
    return QueueEntry(machine="m", repo="r", job=job)


def test_atomic_write_leaves_no_temp_and_makes_backup(tmp_path):
    path = tmp_path / "queue.yaml"
    atomic.atomic_write_text(path, "first\n")
    atomic.atomic_write_text(path, "second\n")
    assert path.read_text() == "second\n"
    assert not (tmp_path / "queue.yaml.tmp").exists()
    assert (tmp_path / "queue.yaml.bak").read_text() == "first\n"


def test_store_roundtrip_has_integrity_fields(tmp_path):
    path = tmp_path / "queue.yaml"
    store = QueueStore(path)
    store.add(_entry("a.yaml"))
    store.add(_entry("b.yaml"))

    raw = path.read_text()
    assert "entries_count: 2" in raw
    assert "entries_sha256:" in raw
    assert QueueStore(path).entries[0].job == "a.yaml"


def _document(entries: list[dict], *, count: int | None = None,
              digest: str | None = None) -> str:
    count = len(entries) if count is None else count
    digest = atomic.checksum(entries) if digest is None else digest
    import yaml as _yaml
    return _yaml.safe_dump({"version": 1, "entries_count": count,
                            "entries_sha256": digest, "entries": entries},
                           sort_keys=False)


def test_truncated_yaml_falls_back_to_backup(tmp_path):
    """A torn file that still parses must lose to the .bak copy."""
    path = tmp_path / "queue.yaml"
    one = [_entry("a.yaml").__dict__]
    two = [_entry("a.yaml").__dict__, _entry("b.yaml").__dict__]

    (tmp_path / "queue.yaml.bak").write_text(_document(one))
    # main lost an entry but still claims two -> count + hash mismatch
    path.write_text(_document(one, count=2))

    recovered = QueueStore(path)
    assert [e.job for e in recovered.entries] == ["a.yaml"]
    assert any("recovered" in w for w in recovered.warnings)


def test_checksum_mismatch_falls_back(tmp_path):
    path = tmp_path / "queue.yaml"
    one = [_entry("a.yaml").__dict__]
    two = [_entry("a.yaml").__dict__, _entry("b.yaml").__dict__]
    (tmp_path / "queue.yaml.bak").write_text(_document(one))
    path.write_text(_document(two, digest="deadbeef"))   # right count, wrong hash

    recovered = QueueStore(path)
    assert [e.job for e in recovered.entries] == ["a.yaml"]


def test_unreadable_queue_never_raises(tmp_path):
    path = tmp_path / "queue.yaml"
    path.write_text("{not: valid: yaml: [\n")
    store = QueueStore(path)           # must not raise
    assert store.entries == []
    assert store.warnings


def test_corrupt_checksum_is_rejected(tmp_path):
    path = tmp_path / "queue.yaml"
    store = QueueStore(path)
    store.add(_entry("a.yaml"))
    path.write_text(path.read_text().replace("entries_sha256: ", "entries_sha256: deadbeef"))
    recovered = QueueStore(path)
    assert recovered.entries == [] or recovered.warnings


def test_scheduler_lock_is_exclusive(tmp_path):
    lock_path = tmp_path / "queue.lock"
    with lock.scheduler_lock(lock_path):
        try:
            with lock.scheduler_lock(lock_path):
                raise AssertionError("second lock should have failed")
        except lock.QueueLocked as exc:
            assert "already running" in str(exc)
    # released: can be taken again
    with lock.scheduler_lock(lock_path):
        pass


def test_lock_reports_holder_pid(tmp_path):
    lock_path = tmp_path / "queue.lock"
    with lock.scheduler_lock(lock_path):
        assert "pid" in lock_path.read_text()


class FakeRemote:
    def __init__(self, out, code=0):
        self.out, self.code = out, code
        self.commands = []

    def try_run(self, command, timeout=60):
        self.commands.append(command)
        return self.code, self.out, ""


def test_probe_run_states():
    from jobs.preflight import probe_run
    assert probe_run(FakeRemote("EXIT:0\n"), "/r") == ("EXIT", 0)
    assert probe_run(FakeRemote("EXIT:143\n"), "/r") == ("EXIT", 143)
    assert probe_run(FakeRemote("RUNNING\n"), "/r") == ("RUNNING", None)
    assert probe_run(FakeRemote("LOST\n"), "/r") == ("LOST", None)


def test_probe_run_requires_cmdline_match():
    from jobs.preflight import probe_run
    probe_run(FakeRemote("LOST\n"), "/r")
    remote = FakeRemote("LOST\n")
    probe_run(remote, "/r")
    assert "cmdline" in remote.commands[0]


def test_active_runs_marks_stale_dirs():
    from jobs.preflight import active_runs
    remote = FakeRemote("run-alive\n")       # dirs with dead pids are not listed
    assert active_runs(remote, "/repo") == ["run-alive"]
    assert "stale" in remote.commands[0]     # dead ones get a stale marker


def test_queue_states_include_stale():
    from jobs.queue import STALE
    assert STALE == "stale"
    entry = _entry()
    assert entry.status == PENDING
