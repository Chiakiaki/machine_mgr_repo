"""``machine_mgr job …`` and ``machine_mgr env …`` command wiring."""

from __future__ import annotations

import shlex
import sys

from local_machines import ssh_tools
from . import catalog, launcher, preflight, tracker
from .contract import job_id_for
from .targets import Target, local_repo_path, target_for, with_resume


def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def fail(msg: str) -> int:
    print(f"  error: {msg}", file=sys.stderr)
    return 1


# ── job subcommands ────────────────────────────────────────────────────────

def cmd_repos(ctx, machine: str) -> int:
    try:
        target = target_for(ctx, machine)
        repos = catalog.find_repos(target.remote, target.project_root)
    except (RuntimeError, KeyError) as exc:
        return fail(str(exc).strip("'"))
    print(f"\n  {machine} ({target.ip}) — repos with a descriptor under {target.project_root}\n")
    for repo in repos:
        files = catalog.repo_files(target.remote, repo.path)
        from .contract import list_jobs
        jobs = list_jobs(repo.descriptor, files)
        print(f"  {repo.name:18s} {len(jobs):3d} jobs   {repo.path}")
        if repo.descriptor.groups:
            for group in repo.descriptor.groups:
                print(f"      └ {group.title}")
    return 0


def cmd_list(ctx, machine: str, repo_name: str | None, group: str | None) -> int:
    try:
        target = target_for(ctx, machine)
        if repo_name:
            repos = [catalog.resolve_repo(target.remote, target.project_root, repo_name)]
        else:
            repos = catalog.find_repos(target.remote, target.project_root)
    except (RuntimeError, KeyError) as exc:
        return fail(str(exc).strip("'"))

    for repo in repos:
        files = catalog.repo_files(target.remote, repo.path)
        jobs = catalog.list_jobs_for(repo, files) if hasattr(catalog, "list_jobs_for") else None
        from .contract import jobs_in_group, list_jobs
        jobs = list_jobs(repo.descriptor, files)
        if group:
            match = next((g for g in repo.descriptor.groups if group.lower() in g.title.lower()), None)
            if not match:
                continue
            jobs = jobs_in_group(repo.descriptor, match, files)
        print(f"\n  {repo.name} — {len(jobs)} job(s)\n")
        for path in jobs:
            print(f"    {job_id_for(path):58s} {path}")
    return 0


def cmd_submit(ctx, machine: str, repo_name: str, job: str, *, extra: str = "",
               dry_run: bool = False, allow_head_mismatch: bool = False,
               force: bool = False, resume_from: str = "") -> int:
    try:
        target = target_for(ctx, machine)
        repo = catalog.resolve_repo(target.remote, target.project_root, repo_name)
    except (RuntimeError, KeyError) as exc:
        return fail(str(exc).strip("'"))

    if resume_from:
        try:
            extra = with_resume(ctx, machine, resume_from, extra)
        except RuntimeError as exc:
            return fail(str(exc))

    if job not in repo.jobs:
        matches = [j for j in repo.jobs if j.endswith(job) or job in j]
        if len(matches) == 1:
            job = matches[0]
        else:
            hint = "\n    ".join(matches[:10]) if matches else "\n    ".join(repo.jobs[:10])
            return fail(f"job '{job}' not found in {repo.name}. Candidates:\n    {hint}")

    descriptor = repo.descriptor
    local_repo = local_repo_path(ctx, repo.name)
    issues = []
    if local_repo:
        issues += preflight.git_issues(local_repo, target.remote, repo.path, descriptor)
    else:
        issues.append(preflight.Issue("warn", f"local checkout of {repo.name} not found under {ctx.local_project_root}"))
    issues += preflight.gpu_issues(target.remote, descriptor)
    issues += preflight.busy_issues(target.remote, repo.path, descriptor)

    errors = [i for i in issues if i.level == preflight.ERROR]
    if allow_head_mismatch:
        errors = [i for i in errors if "HEAD mismatch" not in i.message]
    for issue in issues:
        print(f"  {issue}")
    if errors and not force:
        return fail("preflight failed (use --force to override, --allow-head-mismatch for git only)")

    if dry_run:
        from .launcher import build_script, build_run_id, run_dir_for
        run_id = build_run_id(repo.name, job)
        values = {"repo": repo.name, "job": f"{repo.path}/{job}", "job_id": job_id_for(job),
                  "project_root": target.project_root, "run_id": run_id,
                  "extra": extra, "env_name": descriptor.env.name}
        print(f"\n  --- dry run: {repo.path}/.machine_mgr/runs/{run_id}/command.sh ---\n")
        print(build_script(repo_path=repo.path, descriptor=descriptor, values=values,
                           run_dir=run_dir_for(repo.path, run_id), extra=extra))
        return 0

    from .preflight import local_git_state
    sha = (local_git_state(local_repo) or {}).get("head", "") if local_repo else ""
    record = launcher.submit(remote=target.remote, repo=repo, job=job, extra=extra,
                             project_root=target.project_root, submitter_sha=sha)
    record.update({"machine": target.machine.display, "machine_mac": target.machine.mac})
    run = ctx.runs.add(tracker.Run(**{k: v for k, v in record.items()
                                      if k in tracker.Run.__dataclass_fields__}))
    print(f"\n  submitted {run.id}")
    say(f"repo     {repo.name}:{job}")
    say(f"log      {run.run_dir}/log")
    say(f"follow   ./machine_mgr job logs {run.id} -n 50")
    return 0


def cmd_status(ctx, run_id: str | None, machine: str | None, limit: int = 12) -> int:
    runs = [ctx.runs.get(run_id)] if run_id else ctx.runs.recent(machine)[:limit]
    if not runs:
        return fail("no runs recorded yet")
    print(f"\n  {'run id':34s} {'machine':16s} {'job':30s} {'status':9s} exit")
    print("  " + "-" * 96)
    for run in runs:
        try:
            target = target_for(ctx, run.machine or (machine or ""))
            tracker.refresh(target.remote, run)
        except (RuntimeError, KeyError):
            pass
        exit_txt = "-" if run.exit_code is None else str(run.exit_code)
        job = job_id_for(run.job)[:30]
        print(f"  {run.id:34s} {run.machine[:16]:16s} {job:30s} {run.status:9s} {exit_txt}")
    ctx.runs.save()
    return 0


def cmd_logs(ctx, run_id: str, lines: int = 40) -> int:
    try:
        run = ctx.runs.get(run_id)
        target = target_for(ctx, run.machine)
    except (RuntimeError, KeyError) as exc:
        return fail(str(exc).strip("'"))
    print(tracker.logs(target.remote, run, lines))
    return 0


def cmd_cancel(ctx, run_id: str) -> int:
    try:
        run = ctx.runs.get(run_id)
        target = target_for(ctx, run.machine)
    except (RuntimeError, KeyError) as exc:
        return fail(str(exc).strip("'"))
    say(tracker.cancel(target.remote, run))
    tracker.refresh(target.remote, run)
    ctx.runs.save()
    say(f"status: {run.status}")
    return 0


def cmd_fetch(ctx, run_id: str, artifact: str, dest: str | None) -> int:
    try:
        run = ctx.runs.get(run_id)
        target = target_for(ctx, run.machine)
        repo = catalog.resolve_repo(target.remote, target.project_root, run.repo)
    except (RuntimeError, KeyError) as exc:
        return fail(str(exc).strip("'"))
    spec = repo.descriptor.artifacts.get(artifact)
    if not spec:
        have = ", ".join(repo.descriptor.artifacts) or "none declared"
        return fail(f"artifact '{artifact}' not declared in {run.repo}.machine_mgr.yaml (have: {have})")
    out_dir = Path(dest) if dest else Path(ctx.data_dir) / "artifacts" / run.id
    try:
        tracker.fetch(target.remote, run, spec.paths, out_dir)
    except RuntimeError as exc:
        return fail(str(exc))
    say(f"fetched {', '.join(spec.paths)} -> {out_dir}")
    return 0


def cmd_sync(ctx, machine: str, repo_name: str) -> int:
    """Fast-forward the target checkout (uses agent forwarding for private repos)."""
    try:
        target = target_for(ctx, machine, forward_agent=True)
        repo = catalog.resolve_repo(target.remote, target.project_root, repo_name)
    except (RuntimeError, KeyError) as exc:
        return fail(str(exc).strip("'"))
    code, out, err = target.remote.try_run(
        f"git -C {shlex.quote(repo.path)} pull --ff-only 2>&1", timeout=300
    )
    print((out or err).strip())
    if code != 0:
        return fail("git pull failed (push your commits first, or fix the remote)")
    say(f"{repo.name} at {target.remote.git_head(repo.path)[:8]}")
    return 0


# ── queue (thin wrappers; implementation lives in jobs/queue/) ─────────────

def cmd_queue_add(ctx, machine, repo_name, job, extra="", resume_from="",
                  allow_duplicate=False) -> int:
    from .queue import commands as qcmd
    return qcmd.add(ctx, machine, repo_name, job, extra, resume_from, allow_duplicate)


def cmd_queue_list(ctx, refresh: bool = False) -> int:
    from .queue import commands as qcmd
    return qcmd.list_entries(ctx, refresh=refresh)


def cmd_queue_clear(ctx, everything: bool = False) -> int:
    from .queue import commands as qcmd
    return qcmd.clear(ctx, everything)


def cmd_queue_run(ctx, poll: float = 60.0, once: bool = False, dry_run: bool = False,
                  detach: bool = False) -> int:
    from .queue import commands as qcmd
    return qcmd.run(ctx, poll=poll, once=once, dry_run=dry_run, detach=detach)


def cmd_queue_status(ctx) -> int:
    from .queue import commands as qcmd
    return qcmd.status(ctx)


# ── env subcommands ────────────────────────────────────────────────────────

def cmd_env(ctx, machine: str, repo_name: str, action: str) -> int:
    try:
        target = target_for(ctx, machine, forward_agent=True)
        repo = catalog.resolve_repo(target.remote, target.project_root, repo_name)
    except (RuntimeError, KeyError) as exc:
        return fail(str(exc).strip("'"))

    script = repo.descriptor.env.verify if action == "verify" else repo.descriptor.env.bootstrap
    if not script:
        return fail(f"{repo.name}.machine_mgr.yaml declares no env.{'verify' if action == 'verify' else 'bootstrap'}")
    cmd = f"cd {shlex.quote(repo.path)} && {script}"
    print(f"  {target.user}@{target.ip}: {cmd}\n")
    proc = ssh_tools.ssh(target.ip, target.user, target.port, cmd,
                         password=target.password, timeout=3600 * 4,
                         forward_agent=True, stream=True)
    return proc.returncode
