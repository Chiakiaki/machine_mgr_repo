"""The machine_mgr job contract: shared schema, repo-specific values.

Every repository that wants to expose runnable jobs ships ``.machine_mgr.yaml``
at its root. This module is the only place that knows the schema; the job code
never contains repository names or training-specific logic.

Contract (version 1) — all keys optional except ``name`` and ``runner.command``::

    version: 1
    name: bdp_benchmark
    git:      {require_match: true, protocol: auto}
    env:      {name: bdp, bootstrap: ./redeploy.sh, verify: "./redeploy.sh --verify"}
    runner:   {command: "python3 scripts/train.py --config {job}",
               workdir: ".", shell: bash, env: {PYTHONPATH: "{project_root}"}}
    jobs:     {discover: ["scripts/job_scripts/**/*.yaml"],
               exclude: ["**/*visual_check*.yaml"],
               groups: [{title: "ICRA 2027", glob: "scripts/job_scripts/icra2027*/*.yaml"}]}
    artifacts: {logs: {paths: ["logs/benchmark"], kind: dir}}
    limits:   {max_parallel: 1, min_free_gpu_mem_gb: 2}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import yaml

CONTRACT_VERSION = 1


class ContractError(ValueError):
    """Descriptor is malformed or unsupported."""


@dataclass
class GitPolicy:
    require_match: bool = True
    protocol: str = "auto"          # auto | https | ssh


@dataclass
class EnvSpec:
    name: str = ""
    bootstrap: str = ""
    verify: str = ""


@dataclass
class Runner:
    command: str = ""
    workdir: str = "."
    shell: str = "bash"
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Group:
    title: str
    glob: str


@dataclass
class Artifact:
    paths: list[str] = field(default_factory=list)
    kind: str = "dir"               # dir | file
    exclude: list[str] = field(default_factory=list)


@dataclass
class Limits:
    max_parallel: int = 1
    min_free_gpu_mem_gb: float = 0.0


@dataclass
class Descriptor:
    name: str
    version: int = CONTRACT_VERSION
    git: GitPolicy = field(default_factory=GitPolicy)
    env: EnvSpec = field(default_factory=EnvSpec)
    runner: Runner = field(default_factory=Runner)
    discover: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)


def _require_mapping(value, what: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ContractError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def parse_descriptor(text: str, *, source: str = "<descriptor>") -> Descriptor:
    """Parse and validate a ``.machine_mgr.yaml`` document."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ContractError(f"{source}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise ContractError(f"{source}: top level must be a mapping")

    version = int(data.get("version", CONTRACT_VERSION))
    if version != CONTRACT_VERSION:
        raise ContractError(
            f"{source}: contract version {version} is not supported "
            f"(machine_mgr speaks version {CONTRACT_VERSION})"
        )

    name = str(data.get("name") or "").strip()
    if not name:
        raise ContractError(f"{source}: 'name' is required")

    git_raw = _require_mapping(data.get("git"), "git")
    env_raw = _require_mapping(data.get("env"), "env")
    runner_raw = _require_mapping(data.get("runner"), "runner")
    jobs_raw = _require_mapping(data.get("jobs"), "jobs")
    limits_raw = _require_mapping(data.get("limits"), "limits")

    command = str(runner_raw.get("command") or "").strip()
    if not command:
        raise ContractError(f"{source}: runner.command is required")

    protocol = str(git_raw.get("protocol", "auto")).lower()
    if protocol not in ("auto", "https", "ssh"):
        raise ContractError(f"{source}: git.protocol must be auto|https|ssh")

    discover = [str(x) for x in (jobs_raw.get("discover") or [])]
    exclude = [str(x) for x in (jobs_raw.get("exclude") or [])]
    if not discover:
        raise ContractError(f"{source}: jobs.discover needs at least one glob")

    groups = [
        Group(title=str(g["title"]), glob=str(g["glob"]))
        for g in (jobs_raw.get("groups") or [])
        if isinstance(g, dict) and g.get("title") and g.get("glob")
    ]

    artifacts: dict[str, Artifact] = {}
    for key, spec in _require_mapping(data.get("artifacts"), "artifacts").items():
        spec = _require_mapping(spec, f"artifacts.{key}")
        paths = spec.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        artifacts[str(key)] = Artifact(
            paths=[str(p) for p in paths],
            kind=str(spec.get("kind", "dir")),
            exclude=[str(x) for x in (spec.get("exclude") or [])],
        )

    return Descriptor(
        name=name,
        version=version,
        git=GitPolicy(
            require_match=bool(git_raw.get("require_match", True)),
            protocol=protocol,
        ),
        env=EnvSpec(
            name=str(env_raw.get("name", "")),
            bootstrap=str(env_raw.get("bootstrap", "")),
            verify=str(env_raw.get("verify", "")),
        ),
        runner=Runner(
            command=command,
            workdir=str(runner_raw.get("workdir", ".") or "."),
            shell=str(runner_raw.get("shell", "bash") or "bash"),
            env={str(k): str(v) for k, v in (runner_raw.get("env") or {}).items()},
        ),
        discover=discover,
        exclude=exclude,
        groups=groups,
        artifacts=artifacts,
        limits=Limits(
            max_parallel=int(limits_raw.get("max_parallel", 1)),
            min_free_gpu_mem_gb=float(limits_raw.get("min_free_gpu_mem_gb", 0.0)),
        ),
    )


# ── job discovery (pure: takes a file list, returns matching paths) ─────────

def glob_to_regex(pattern: str) -> re.Pattern:
    """Translate a posix glob (with ``**``) into an anchored regex."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def _matches(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


def list_jobs(descriptor: Descriptor, repo_files: list[str]) -> list[str]:
    """Apply discover/exclude globs to a repo-relative file list."""
    found = [f for f in repo_files if _matches(f, descriptor.discover)]
    kept = [f for f in found if not _matches(f, descriptor.exclude)]
    return sorted(kept)


def jobs_in_group(descriptor: Descriptor, group: Group, repo_files: list[str]) -> list[str]:
    return [f for f in list_jobs(descriptor, repo_files)
            if glob_to_regex(group.glob).match(f)]


def job_id_for(path: str) -> str:
    """Stable short id from a job path: dir/stem, e.g. icra2027_trpo_vs_ppo…/foo."""
    p = PurePosixPath(path)
    parent = p.parent.name
    return p.stem if parent in ("job_scripts", ".", "") else f"{parent}/{p.stem}"
