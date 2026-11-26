"""Thin remote-execution layer used by everything under ``jobs``.

Keeping all ssh calls behind this object means the job modules never build ssh
commands themselves and are easy to fake in tests.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from local_machines import ssh_tools


@dataclass
class Remote:
    ip: str
    user: str
    port: int = 22
    password: str | None = None
    forward_agent: bool = False

    def run(self, command: str, timeout: float = 60.0) -> str:
        proc = ssh_tools.ssh(self.ip, self.user, self.port, command,
                             password=self.password, timeout=timeout,
                             forward_agent=self.forward_agent)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RuntimeError(detail[-1] if detail else f"remote command failed ({proc.returncode})")
        return proc.stdout

    def try_run(self, command: str, timeout: float = 60.0) -> tuple[int, str, str]:
        proc = ssh_tools.ssh(self.ip, self.user, self.port, command,
                             password=self.password, timeout=timeout,
                             forward_agent=self.forward_agent)
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def write_file(self, path: str, content: str, mode: str = "644") -> None:
        """Write a file by piping stdin through ssh (no quoting traps)."""
        proc = ssh_tools.ssh(
            self.ip, self.user, self.port,
            f"cat > {shlex.quote(path)} && chmod {mode} {shlex.quote(path)}",
            password=self.password, timeout=60, forward_agent=self.forward_agent,
            input_text=content,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"could not write {path}: {(proc.stderr or '').strip()}")
        # Guard against a silently empty write (e.g. stdin closed too early).
        code, out, _ = self.try_run(f"wc -c < {shlex.quote(path)}")
        if code != 0 or int(out.strip() or 0) != len(content.encode()):
            raise RuntimeError(f"short write to {path}: expected {len(content.encode())} bytes")

    def read_file(self, path: str) -> str:
        return self.run(f"cat {shlex.quote(path)}")

    def exists(self, path: str) -> bool:
        code, _, _ = self.try_run(f"test -e {shlex.quote(path)}")
        return code == 0

    def is_dir(self, path: str) -> bool:
        code, _, _ = self.try_run(f"test -d {shlex.quote(path)}")
        return code == 0

    def resolve_path(self, path: str) -> str:
        """Expand a leading '~' using the remote $HOME.

        Doing it here (rather than writing "$HOME/..." into a command) keeps
        every later use quotable with shlex.quote, with no shell expansion and
        no injection surface.
        """
        if path == "~" or path.startswith("~/"):
            home = self.run("echo $HOME").strip()
            return home + path[1:]
        return path

    def git_head(self, repo: str) -> str:
        return self.run(f"git -C {shlex.quote(repo)} rev-parse HEAD").strip()
