"""Locate a run's newest checkpoint so a killed job can continue."""

from __future__ import annotations

import re

from ..transport import Remote

RUN_DIR_RE = re.compile(r"Run directory:\s*(\S+)")


def latest_checkpoint(remote: Remote, run) -> str | None:
    try:
        log = remote.read_file(f"{run.run_dir}/log")
    except RuntimeError:
        return None
    matches = RUN_DIR_RE.findall(log)
    if not matches:
        return None
    out_dir = matches[-1]
    code, listing, _ = remote.try_run(f"ls -1 {_quote(out_dir)}/models/*.zip 2>/dev/null")
    if code != 0 or not listing.strip():
        return None
    candidates = [line for line in listing.splitlines() if line.strip()]

    def step(path: str) -> int:
        m = re.search(r"step_(\d+)_steps\.zip$", path)
        if m:
            return int(m.group(1))
        return 10**12 if "final_model" in path else 0

    return max(candidates, key=step)


def resume_extra(remote: Remote, run) -> str | None:
    ckpt = latest_checkpoint(remote, run)
    return f"--resume_model_path {ckpt}" if ckpt else None


def _quote(text: str) -> str:
    import shlex
    return shlex.quote(text)
