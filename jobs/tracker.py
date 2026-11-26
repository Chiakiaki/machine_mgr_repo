"""Run registry, liveness, logs, cancellation and artifact fetch."""

from __future__ import annotations

import io
import shlex
import subprocess
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from .preflight import probe_run
from .queue import atomic
from .transport import Remote

RUN_FIELDS = ("id", "machine", "machine_mac", "repo", "repo_path", "job", "run_dir",
              "pid", "status", "exit_code", "started", "finished", "git_head", "note")


@dataclass
class Run:
    id: str
    machine: str = ""
    machine_mac: str = ""
    repo: str = ""
    repo_path: str = ""
    job: str = ""
    run_dir: str = ""
    pid: str = ""
    status: str = "running"
    exit_code: int | None = None
    started: str = ""
    finished: str = ""
    git_head: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k in RUN_FIELDS}


class RunStore:
    """Local index of submitted runs (data/jobs.yaml)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.runs: list[Run] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        data, _source, _note = atomic.load_document(self.path, list_key="runs")
        if data is None:
            return
        for entry in data.get("runs", []):
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            kwargs = {k: entry.get(k) for k in RUN_FIELDS if k in entry}
            kwargs.setdefault("id", entry["id"])
            self.runs.append(Run(**kwargs))

    def save(self) -> None:
        runs = [r.to_dict() for r in self.runs]
        payload = {"version": 1, "runs_count": len(runs),
                   "runs_sha256": atomic.checksum(runs), "runs": runs}
        atomic.write_document(
            self.path, payload,
            header="# machine_mgr run registry — one entry per submitted job.\n",
        )

    def add(self, run: Run) -> Run:
        self.runs.append(run)
        self.save()
        return run

    def get(self, run_id: str) -> Run:
        for run in self.runs:
            if run.id == run_id:
                return run
        matches = [r for r in self.runs if r.id.startswith(run_id)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"no run id matches '{run_id}'")
        raise KeyError(f"ambiguous run id '{run_id}': {', '.join(r.id for r in matches)}")

    def recent(self, machine: str | None = None) -> list[Run]:
        runs = [r for r in self.runs if not machine or r.machine == machine]
        return sorted(runs, key=lambda r: r.started or "", reverse=True)


# ── remote operations ───────────────────────────────────────────────────────

def refresh(remote: Remote, run: Run) -> Run:
    """Update status from the run directory, using liveness-aware probing."""
    state, code = probe_run(remote, run.run_dir)
    if state == "EXIT":
        run.exit_code = code
        marker, _, _ = remote.try_run(f"test -f {shlex.quote(run.run_dir + '/cancel')}")
        if marker == 0:
            run.status = "cancelled"
        else:
            run.status = "finished" if code == 0 else "failed"
        if not run.finished:
            import datetime as _dt
            run.finished = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    elif state == "RUNNING":
        run.status = "running"
    else:
        run.status = "lost"          # dir exists, process gone (kill/OOM/reboot)
    return run


def logs(remote: Remote, run: Run, lines: int = 40) -> str:
    code, out, err = remote.try_run(
        f"tail -n {int(lines)} {shlex.quote(run.run_dir + '/log')} 2>&1"
    )
    return out if code == 0 else (err or out or "(no log yet)")


def cancel(remote: Remote, run: Run) -> str:
    """SIGTERM the whole process group started by setsid."""
    cmd = (
        f"touch {shlex.quote(run.run_dir + '/cancel')} 2>/dev/null; "
        f"pid=$(cat {shlex.quote(run.run_dir + '/pid')} 2>/dev/null); "
        f"[ -n \"$pid\" ] && kill -TERM -- -$pid 2>/dev/null || kill -TERM $pid 2>/dev/null; "
        f"sleep 2; kill -0 $pid 2>/dev/null && echo STILL_RUNNING || echo STOPPED"
    )
    code, out, err = remote.try_run(cmd)
    return (out or err).strip() or f"exit {code}"


def fetch(remote: Remote, run: Run, artifact_paths: list[str],
          dest: Path) -> Path:
    """Tar the requested repo-relative paths on the target and extract locally."""
    # Artifacts are streamed over a plain ssh pipe, so key auth is required;
    # a *stored* password is fine as long as the key also works.
    # Only tar paths that actually exist (a declared dir may be empty/absent).
    probe = " ".join(f"test -e {shlex.quote(p)} && echo {shlex.quote(p)};" for p in artifact_paths)
    present = remote.run(f"cd {shlex.quote(run.repo_path)} && {probe}").split()
    if not present:
        raise RuntimeError(
            f"none of {', '.join(artifact_paths)} exist on the target for this run"
        )
    rel = " ".join(shlex.quote(p) for p in present)
    args = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
            "-o", "BatchMode=yes", "-p", str(remote.port)]
    if remote.forward_agent:
        args.append("-A")
    args += [f"{remote.user}@{remote.ip}",
             f"cd {shlex.quote(run.repo_path)} && tar czf - {rel} 2>/dev/null"]
    proc = subprocess.run(args, capture_output=True, timeout=1800)
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        if "Permission denied" in err or "publickey" in err:
            raise RuntimeError(
                "artifact fetch needs key auth on the target — run "
                "`machine_mgr key-install <machine>` once (a stored password is not enough)"
            )
        raise RuntimeError(f"remote tar failed: {err[:300]}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")  # noqa: S202 - our own remote tree
    return dest
