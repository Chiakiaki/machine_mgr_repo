"""Pre-submit checks: git synchronisation, busy-machine and GPU capacity."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

from .contract import Descriptor
from .transport import Remote

ERROR = "error"
WARN = "warn"


@dataclass
class Issue:
    level: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.message}"


def local_git_state(repo_path: str) -> dict:
    """HEAD sha + dirty flag for a local checkout (empty dict if not a repo)."""
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", repo_path, *args],
                                  capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""

    head = git("rev-parse", "HEAD")
    if not head:
        return {}
    return {
        "head": head,
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "dirty_count": len([l for l in git("status", "--porcelain").splitlines() if l.strip()]),
        "unpushed": len([l for l in git("log", "--oneline", "@{u}..").splitlines() if l.strip()]),
    }


def git_issues(local_repo: str, remote: Remote, remote_repo: str,
               descriptor: Descriptor) -> list[Issue]:
    """Compare local HEAD with the target checkout."""
    issues: list[Issue] = []
    local = local_git_state(local_repo)
    if not local:
        issues.append(Issue(WARN, f"local {local_repo} is not a git checkout; cannot verify the target matches"))
        return issues

    try:
        remote_head = remote.git_head(remote_repo)
    except RuntimeError:
        issues.append(Issue(ERROR, f"{remote_repo} is not a git checkout on the target"))
        return issues

    if local["head"] != remote_head:
        level = ERROR if descriptor.git.require_match else WARN
        issues.append(Issue(
            level,
            f"HEAD mismatch: local {local['head'][:8]} ({local['branch']}) vs target "
            f"{remote_head[:8]} — run `machine_mgr job sync` (target) after pushing, "
            f"or pass --allow-head-mismatch",
        ))
    if local["dirty"]:
        issues.append(Issue(
            WARN,
            f"{local['dirty_count']} uncommitted file(s) locally are NOT on the target",
        ))
    if local["unpushed"]:
        issues.append(Issue(
            WARN,
            f"{local['unpushed']} local commit(s) not pushed to origin",
        ))
    return issues


def gpu_issues(remote: Remote, descriptor: Descriptor) -> list[Issue]:
    """Advisory GPU capacity check using nvidia-smi (skipped when absent)."""
    need = descriptor.limits.min_free_gpu_mem_gb
    code, out, _ = remote.try_run(
        "nvidia-smi --query-gpu=memory.total,memory.used --format=csv,noheader,nounits 2>/dev/null"
    )
    if code != 0 or not out.strip():
        return [Issue(WARN, "no GPU detected on the target (nvidia-smi missing or no device)")]
    try:
        total, used = (int(x) for x in out.strip().splitlines()[0].split(",")[:2])
    except ValueError:
        return []
    free_gb = (total - used) / 1024
    if need and free_gb < need:
        return [Issue(ERROR, f"GPU has {free_gb:.1f} GiB free, job requires {need:.1f} GiB")]
    return [Issue(WARN, f"GPU free: {free_gb:.1f} GiB of {total / 1024:.1f} GiB")]


def active_runs(remote: Remote, repo_path: str) -> list[str]:
    """Run ids on the target whose exit file is absent (best effort)."""
    code, out, _ = remote.try_run(
        f"cd {shlex.quote(repo_path)} 2>/dev/null && "
        f"for d in .machine_mgr/runs/*/; do [ -d \"$d\" ] || continue; "
        f"[ -f \"$d/exit\" ] || basename \"$d\"; done"
    )
    return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []


def busy_issues(remote: Remote, repo_path: str, descriptor: Descriptor) -> list[Issue]:
    running = active_runs(remote, repo_path)
    if running and len(running) >= descriptor.limits.max_parallel:
        return [Issue(ERROR, f"{len(running)} job(s) still active in this repo: {', '.join(running)}")]
    return []
