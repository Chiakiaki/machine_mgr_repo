"""Execution backends: one interface, two transports.

Everything above this module (catalog, launcher, tracker, preflight, queue) only
speaks to the methods below, so a machine is reachable regardless of whether it
is this host (no ssh) or a remote host (ssh). Local support is therefore an
implementation detail of one class, not a special case sprinkled through the job
layer.

Interface (matched by jobs.transport.Remote for ssh):

    run(command, timeout) -> str
    try_run(command, timeout) -> (code, stdout, stderr)
    write_file(path, content, mode)
    read_file(path) -> str
    exists(path) -> bool
    is_dir(path) -> bool
    resolve_path(path) -> str        # expands a leading ~
    git_head(repo) -> str
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .transport import Remote


@dataclass
class LocalBackend:
    """Run on this host. Commands keep the same shell semantics as ssh ones."""

    @property
    def is_local(self) -> bool:
        return True

    def _exec(self, command: str, timeout: float = 60.0,
              input_text: str | None = None) -> subprocess.CompletedProcess:
        kwargs: dict = dict(capture_output=True, text=True, timeout=timeout)
        if input_text is not None:
            kwargs["input"] = input_text
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        return subprocess.run(["bash", "-c", command], **kwargs)

    def run(self, command: str, timeout: float = 60.0) -> str:
        proc = self._exec(command, timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"command failed ({proc.returncode})")
        return proc.stdout

    def try_run(self, command: str, timeout: float = 60.0) -> tuple[int, str, str]:
        proc = self._exec(command, timeout)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def write_file(self, path: str, content: str, mode: str = "644") -> None:
        target = Path(os.path.expanduser(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(int(mode, 8))
        if target.stat().st_size != len(content.encode()):
            raise RuntimeError(f"short write to {path}")

    def read_file(self, path: str) -> str:
        return Path(os.path.expanduser(path)).read_text(encoding="utf-8")

    def exists(self, path: str) -> bool:
        return Path(os.path.expanduser(path)).exists()

    def is_dir(self, path: str) -> bool:
        return Path(os.path.expanduser(path)).is_dir()

    def resolve_path(self, path: str) -> str:
        return os.path.expanduser(path)

    def git_head(self, repo: str) -> str:
        return self.run(f"git -C {shlex_quote(repo)} rev-parse HEAD").strip()

    def has_tool(self, name: str) -> bool:
        return shutil.which(name) is not None


def shlex_quote(text: str) -> str:
    import shlex
    return shlex.quote(text)


def make_backend(*, local: bool, ip: str = "", user: str = "", port: int = 22,
                 password: str | None = None, forward_agent: bool = False):
    """Return the backend for a machine record."""
    if local:
        return LocalBackend()
    return Remote(ip, user, port, password, forward_agent=forward_agent)
