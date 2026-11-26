"""Discover descriptors and job files on a remote machine."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from .contract import Descriptor, list_jobs, parse_descriptor
from .transport import Remote

DESCRIPTOR_NAME = ".machine_mgr.yaml"


@dataclass
class RepoRef:
    name: str
    path: str                 # absolute path on the remote
    descriptor: Descriptor
    files: list[str]          # repo-relative yaml files

    @property
    def jobs(self) -> list[str]:
        return list_jobs(self.descriptor, self.files)


def find_repos(remote: Remote, project_root: str) -> list[RepoRef]:
    """All repos under ``project_root`` that ship a descriptor."""
    root = remote.resolve_path(project_root)
    listing = remote.run(
        f"find {shlex.quote(root)} -maxdepth 2 -name {DESCRIPTOR_NAME} "
        f"-not -path '*/.git/*' -print 2>/dev/null | sort"
    )
    repos: list[RepoRef] = []
    for line in listing.splitlines():
        path = line.strip()
        if not path:
            continue
        repo_path = path.rsplit("/", 1)[0]
        try:
            descriptor = parse_descriptor(remote.read_file(path), source=path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            raise RuntimeError(f"{path}: {exc}") from None
        repos.append(RepoRef(descriptor.name, repo_path, descriptor, []))
    return repos


def repo_files(remote: Remote, repo_path: str) -> list[str]:
    """Repo-relative *.yaml files, excluding .git and machine state."""
    listing = remote.run(
        f"cd {shlex.quote(repo_path)} && "
        f"find . -name '*.yaml' -not -path './.git/*' -not -path './.machine_mgr/*' -print"
    )
    return sorted(line[2:] for line in listing.splitlines() if line.startswith("./"))


def resolve_repo(remote: Remote, project_root: str, name: str) -> RepoRef:
    repos = find_repos(remote, project_root)
    for repo in repos:
        if repo.name == name or repo.path.rsplit("/", 1)[-1] == name:
            repo.files = repo_files(remote, repo.path)
            return repo
    available = ", ".join(r.name for r in repos) or "none"
    raise KeyError(f"no repo named '{name}' on the target (have: {available})")
