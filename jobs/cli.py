"""``machine_mgr job …`` and ``machine_mgr env …`` command wiring."""

from __future__ import annotations

import datetime as _dt
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from local_machines import ssh_tools
from local_machines.registry import Machine
from . import catalog, launcher, preflight, queue, tracker
from .contract import job_id_for
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


def say(msg: str) -> None:
    print(f"  {msg}")


def fail(msg: str) -> int:
    print(f"  error: {msg}", file=sys.stderr)
    return 1


# ── resolver ───────────────────────────────────────────────────────────────

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


def _with_resume(ctx, target_factory, machine: str, resume_from: str, extra: str) -> str:
    """Append --resume_model_path for a previous run of the same repo."""
    if not resume_from:
        return extra
    try:
        run = ctx.runs.get(resume_from)
        target = target_factory(ctx, machine)
        addition = queue.resume_extra(target.remote, run)
    except (KeyError, RuntimeError) as exc:
        raise RuntimeError(f"cannot resume from '{resume_from}': {exc}") from None
    if not addition:
        raise RuntimeError(f"no checkpoint found for run '{resume_from}'")
    return f"{extra} {addition}".strip()


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
            extra = _with_resume(ctx, target_for, machine, resume_from, extra)
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


# ── queue ─────────────────────────────────────────────────────────────────

def cmd_queue_add(ctx, machine: str, repo_name: str, job: str, extra: str = "",
                  resume_from: str = "", allow_duplicate: bool = False) -> int:
    entry = queue.QueueEntry(machine=machine, repo=repo_name, job=job,
                             extra=extra, resume_from=resume_from)
    stored = ctx.queue.add(entry, allow_duplicate=allow_duplicate)
    if stored.id != entry.id:
        say(f"already queued as {stored.id} ({stored.status}); skipped")
        return 0
    say(f"queued {stored.id} [#{stored.order}] {repo_name}:{job}")
    if extra:
        say(f"extra: {extra}")
    if resume_from:
        say(f"resume from run {resume_from}")
    return 0


def cmd_queue_list(ctx) -> int:
    entries = ctx.queue.entries
    if not entries:
        say("queue is empty")
        return 0
    print(f"\n  {'id':12s} {'#':>2s} {'machine':16s} {'repo':14s} {'job':44s} {'status':9s} run")
    print("  " + "-" * 108)
    for e in sorted(entries, key=lambda x: x.order):
        print(f"  {e.id:12s} {e.order:2d} {e.machine[:16]:16s} {e.repo[:14]:14s} "
              f"{_short(e.job, 44):44s} {e.status:9s} {e.run_id or '-'}")
    pending = len([e for e in entries if e.status == queue.PENDING])
    running = len([e for e in entries if e.status == queue.RUNNING])
    say(f"{pending} pending, {running} running")
    return 0


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1):]


def cmd_queue_clear(ctx, everything: bool = False) -> int:
    removed = ctx.queue.clear(only_finished=not everything)
    say(f"removed {removed} entr{'y' if removed == 1 else 'ies'}")
    return 0


def cmd_queue_run(ctx, poll: float = 60.0, once: bool = False,
                  dry_run: bool = False) -> int:
    """Drain the queue, respecting max_parallel and the RAM/GPU gates."""
    store = ctx.queue
    if dry_run:
        pending = store.pending()
        if not pending:
            say("queue empty — nothing to plan")
            return 0
        for entry in pending:
            try:
                target = target_for(ctx, entry.machine)
                repo = catalog.resolve_repo(target.remote, target.project_root, entry.repo)
            except (KeyError, RuntimeError) as exc:
                say(f"[{entry.order}] {entry.id} SKIP — {str(exc).strip(chr(39))}")
                continue
            ready, reasons = preflight.readiness(target.remote, repo.path, repo.descriptor)
            state = "would submit" if ready else "would wait"
            say(f"[{entry.order}] {entry.id} {state}: {entry.repo}:{entry.job}")
            if reasons:
                say(f"      blocked by: {'; '.join(reasons)}")
        return 0
    failures = 0
    say(f"queue drain started (poll {poll:.0f}s); Ctrl-C stops the scheduler, "
        f"the job already running keeps going")
    try:
        while True:
            # 1) update running entries
            for entry in store.running():
                try:
                    run = ctx.runs.get(entry.run_id)
                    target = target_for(ctx, entry.machine)
                    tracker.refresh(target.remote, run)
                except (KeyError, RuntimeError):
                    continue
                if run.status != "running":
                    entry.status = {"finished": queue.DONE,
                                    "cancelled": queue.CANCELLED}.get(run.status, queue.FAILED)
                    entry.finished = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
                    entry.note = f"exit {run.exit_code}"
                    if entry.status == queue.FAILED:
                        failures += 1
                    ctx.runs.save()
                    store.save()
                    say(f"[{entry.status}] {entry.id} {entry.repo}:{_short(entry.job, 40)} ({entry.note})")

            pending = store.pending()
            if not pending and not store.running():
                say("queue empty — nothing left")
                break

            started = False
            for entry in pending:
                try:
                    target = target_for(ctx, entry.machine)
                    repo = catalog.resolve_repo(target.remote, target.project_root, entry.repo)
                except (KeyError, RuntimeError) as exc:
                    entry.status = queue.SKIPPED
                    entry.note = str(exc).strip().strip("'")
                    store.save()
                    say(f"[skipped] {entry.id}: {entry.note}")
                    continue

                ready, reasons = preflight.readiness(target.remote, repo.path, repo.descriptor)
                if not ready:
                    say(f"waiting: {'; '.join(reasons)}")
                    break

                extra = entry.extra
                if entry.resume_from:
                    try:
                        extra = _with_resume(ctx, target_for, entry.machine, entry.resume_from, extra)
                    except RuntimeError as exc:
                        entry.status = queue.SKIPPED
                        entry.note = str(exc)
                        store.save()
                        say(f"[skipped] {entry.id}: {exc}")
                        continue

                from .preflight import local_git_state
                local_repo = local_repo_path(ctx, repo.name)
                sha = (local_git_state(local_repo) or {}).get("head", "") if local_repo else ""
                record = launcher.submit(remote=target.remote, repo=repo, job=entry.job,
                                         extra=extra, project_root=target.project_root,
                                         submitter_sha=sha)
                record.update({"machine": target.machine.display, "machine_mac": target.machine.mac})
                run = ctx.runs.add(tracker.Run(**{k: v for k, v in record.items()
                                                  if k in tracker.Run.__dataclass_fields__}))
                entry.status = queue.RUNNING
                entry.run_id = run.id
                entry.started = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
                store.save()
                say(f"[submitted] {entry.id} -> {run.id}")
                say(f"            log {run.run_dir}/log")
                started = True
                break

            if once:
                break
            time.sleep(poll if started else min(poll, 30))
    except KeyboardInterrupt:
        store.save()
        say("scheduler stopped; pending entries are preserved — "
            "`machine_mgr job queue run` resumes them")
        return 130

    return 1 if failures else 0


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
