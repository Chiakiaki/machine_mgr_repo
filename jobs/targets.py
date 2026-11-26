"""Resolve a machine identifier into something the job layer can talk to."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from local_machines.registry import Machine

from . import queue
from .transport import Remote


@dataclass
class Target:
    machine: Machine
    ip: str
    user: str
    port: int
    password: str | None
    remote: Remote

    @property
    def project_root(self) -> str:
        return self.machine.project_root or "~/project"


def target_for(ctx, identifier: str, *, forward_agent: bool = False) -> Target:
    machine = ctx.resolve(identifier)
    ip = ctx.ip_of(machine)
    if not ip:
        raise RuntimeError(f"{machine.display} ({machine.mac}) is offline")
    user, port, password = ctx.auth_for(machine)
    if not user:
        raise RuntimeError(f"no ssh user recorded for {machine.mac}")
    remote = Remote(ip, user, port, password, forward_agent=forward_agent)
    try:
        machine.project_root = remote.resolve_path(machine.project_root or "~/project")
    except RuntimeError:
        pass
    return Target(machine, ip, user, port, password, remote)


def local_repo_path(ctx, repo_name: str) -> str | None:
    """Path to the same repo on THIS machine, for the HEAD comparison."""
    candidate = Path(ctx.local_project_root) / repo_name
    return str(candidate) if candidate.is_dir() else None


def with_resume(ctx, machine: str, resume_from: str, extra: str) -> str:
    """Append --resume_model_path for a previous run of the same machine."""
    if not resume_from:
        return extra
    try:
        run = ctx.runs.get(resume_from)
        target = target_for(ctx, machine)
        addition = queue.resume_extra(target.remote, run)
    except (KeyError, RuntimeError) as exc:
        raise RuntimeError(f"cannot resume from '{resume_from}': {exc}") from None
    if not addition:
        raise RuntimeError(f"no checkpoint found for run '{resume_from}'")
    return f"{extra} {addition}".strip()
