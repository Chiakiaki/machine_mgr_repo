# machine_mgr — motivation and design

Human documentation. The agent-consumed operational brief lives in
[`agent/supervisor.md`](../agent/supervisor.md); this file explains *why* the
tool exists and *how* its parts are arranged.

## 1. Motivation

Three concrete problems drove this tool, each discovered the hard way.

**1.1 Machines move, IPs change.** A workstation reached at `192.168.5.101`
comes back from a reboot on `.117`, and every script, note and shell alias that
hard-coded the address is wrong. What does not change is the NIC's MAC address.
So identity is the MAC, and the address is resolved at call time — from the
kernel ARP table after a ping sweep — never stored.

**1.2 A batch of long trainings must survive an operator walking away.** Ten
2M-step TRPO runs take ~14 h. Submitting them one at a time is babysitting; a
naive `for job in …; do ssh …; done` loses everything when the shell dies. So:
a persisted queue, a detached scheduler, per-run logs on the training host, and
a registry that can be reconciled after any interruption.

**1.3 The scheduler died and the queue silently stopped.** The first attempt
started the scheduler with `setsid nohup` from an agent tool shell. systemd
tore down that tool's cgroup (`Stopped dsh-subprocess-…scope`) and took the
scheduler with it, while the *training job* — detached on the remote host —
kept running and finished. The lesson: process detachment is not lifetime
independence, and "the queue is still going" must never be assumed. Detach now
means a transient **systemd user unit** (`machine-mgr-queue`), and queue
state is reconciled against the remote run directories on every pass.

**1.4 Repos are the unit of work.** Every experiment is a git-tracked job YAML
plus a train script. That is location-independent by construction: any machine
that has the repo can run the job. So the tool adds transport and lifecycle,
never training logic, and the *same* utility can drive a local run and a remote
one.

## 2. Goals and non-goals

Goals:

- find machines on the local network and remember them across DHCP
  reassignments, keyed by MAC;
- register, probe, reach and provision those machines (SSH, keys, directories);
- submit jobs defined by repositories, detached on the target, with
  reproducible snapshots;
- track, tail, cancel, resume and fetch results of those jobs;
- run a batch unattended, respecting GPU/RAM limits, and converge to the truth
  after crashes, kills and reboots;
- let *any* host in the fleet act as controller for itself or for others.

Non-goals:

- no training, scheduling or metric logic of its own — that belongs to the repo
  being run;
- no IP bookkeeping, no DNS, no service discovery beyond the local segment;
- no orchestration that requires a resident central server;
- no credentials on the training host (keys or agent forwarding only).

## 3. Layers

```
agent/           instructions the agent reads when a reminder wakes it
docs/            this file (human documentation)
global_machine/  reserved: cross-network discovery (tunnel, tailnet, VPN)
jobs/            repo-agnostic orchestration (contract, catalog, launcher, …)
local_machines/  discovery, inventory, ssh plumbing, CLI
```

Dependency direction is one-way: `jobs/` imports `local_machines/`, never the
reverse, and nothing imports `agent/`. The training repositories depend on
nothing here beyond one YAML file of their own.

### 3.1 `local_machines/`

| Module | Responsibility |
| --- | --- |
| `discovery.py` | LAN interface/subnet detection (skips `tun*`, `Meta`, `wg*`, `tailscale*`), parallel ping sweep, ARP parsing, a short-lived scan cache, OUI vendor hints, `self_machine()` |
| `registry.py` | two-layer inventory (below), MAC normalization, naming (`slugify`, `unique_name`), merge semantics |
| `ssh_tools.py` | `ssh` wrapper (`-A` forwarding, `SSH_ASKPASS` password mode), probe script, `ssh-copy-id`, remote `mkdir`, per-MAC credential store |
| `cli.py` | argument mode and the numbered interactive menu |

### 3.2 Two-layer inventory

| File | Tracked | Holds |
| --- | --- | --- |
| `inventory.yaml` | yes | curated identity/reachability: `mac, name, hostname, ssh_user, ssh_port, project_root, os, os_version, tags, notes` — **no timestamps, no backend** |
| `data/machines.yaml` | no (per host) | `backend: local`, `first_seen`/`last_seen`, host-specific overrides of `ssh_user`/`ssh_port`/`project_root` |

Why split: the manifest is written by ordinary commands, so a tracked copy
would churn on every `check`; and `backend: local` means "this record is *me*",
which is host-relative — sharing it would let one host believe it is another.
Merge rule: **curated identity wins**, the overlay may override reachability
only. That way a stale auto-name in the overlay can never mask a curated name.

Machines are discovered by `scan --register` (MAC-keyed, never duplicated) and
by `self --register` for the host itself — a host never ARPs its own address, so
it cannot find itself by scanning.

### 3.3 Backends

`jobs/backend.py` defines one interface — `run`, `try_run`, `write_file`,
`read_file`, `exists`, `is_dir`, `resolve_path`, `git_head` — implemented twice:

- `LocalBackend` (this host, `bash -c`, no credentials, no ssh);
- `Remote`/ssh (`jobs/transport.py`, the same methods over SSH).

Everything above it (catalog, launcher, tracker, preflight, queue) is written
against the interface, so a local job and a remote job differ in exactly one
place: which backend `target_for()` returns.

## 4. Jobs

### 4.1 The contract lives in the repository

Each repo ships `.machine_mgr.yaml` — **values only**, validated by
`jobs/contract.py`:

```yaml
version: 1
name: bdp_benchmark
env:    {name: bdp, bootstrap: ./redeploy.sh, verify: "./redeploy.sh --verify"}
runner: {command: '"$HOME/miniconda3/bin/conda" run --no-capture-output -n {env_name} python3 scripts/train.py --config {job}',
         workdir: ".", shell: bash, env: {PYTHONPATH: "{project_root}"}}
jobs:   {discover: ["scripts/job_scripts/**/*.yaml"], exclude: ["**/*visual_check*.yaml"],
         groups: [{title: "ICRA 2027", glob: "scripts/job_scripts/icra2027*/*.yaml"}]}
artifacts: {logs: {paths: ["logs/benchmark"], kind: dir}}
git:    {require_match: true}
limits: {max_parallel: 1, min_free_gpu_mem_gb: 2, min_free_ram_gb: 6}
```

Rules that keep this maintainable:

- **no hooks, no plugins** — if an experiment needs different behaviour, it
  chooses different values or the shared schema gains a field;
- job parameters stay in the repo's own job YAMLs; the descriptor only says
  which files are jobs and how to invoke the entry point;
- `{placeholders}` are shell-quoted, so paths with spaces or `;` cannot break
  out of the rendered command; only `--extra` is passed through verbatim.

### 4.2 Preflight

Before a submit the tool checks: local HEAD vs the target checkout (refuse on
mismatch, since a job whose YAML is not on the target is not reproducible),
uncommitted and unpushed local work (warn), GPU free memory, free RAM, and
whether the repo already has a run active. Errors can be overridden explicitly
(`--allow-head-mismatch`, `--force`).

### 4.3 A run on the target

```
<repo>/.machine_mgr/runs/<run_id>/
  command.sh      frozen, reviewable script (trap writes the exit status)
  start.sh        cds first, then setsid + records $! (pid) — never `cd X && setsid … &`
  submitted.yaml  the job YAML as submitted
  env_name        conda env recorded at submit time
  log  pid  exit  runtime state read by the tracker
  cancel  stale   markers written by cancel / by a liveness check
```

The run directory is the source of truth. `queue.yaml` and `jobs.yaml` are
caches of intent that are reconciled against it.

## 5. Queue

### 5.1 Durability

- **Atomic writes**: serialise → `*.tmp` → `fsync` → copy current to `.bak` →
  `os.replace`. Readers see the old file or the new one, never a partial one.
- **Integrity header**: `entries_count` + `entries_sha256` (and the same for the
  run registry). A truncated YAML can still *parse* while silently losing
  entries; the hash is what catches that, and `load_document` then falls back to
  `.bak`.
- **Tolerant load**: a corrupt queue never raises; it warns and starts empty, so
  it cannot break unrelated commands.
- **Single scheduler**: `flock` on `data/queue.yaml.lock`; a second scheduler
  exits and names the holder pid.

### 5.2 Lifecycle and liveness

States: `pending → running → done | failed | cancelled | stale | skipped`.

A run is alive only if its pid exists **and** `/proc/<pid>/cmdline` still
matches `command.sh`. This matters twice: a `kill -0` alone reports a reused pid
after a reboot as running, and the exit file is written by an `EXIT` trap that
`SIGKILL`, OOM and reboots never run — so "no exit file" does not mean "running".
Dead directories get a `stale` marker and stop blocking the queue.

Reconciliation maps the run state onto the entry: `finished → done`,
`cancelled → cancelled`, `failed → failed`, `lost → stale`. It runs at the start
of every scheduler pass and on `queue list --refresh`.

### 5.3 Scheduling

`job queue run` drains the queue one job at a time while the repo's
`max_parallel` and memory gates allow it, sleeping between checks. Ctrl-C stops
the scheduler; the job already running continues on the target, and pending
entries stay pending for the next run.

`--detach` starts the scheduler in a transient **systemd user unit**, which is
independent of the launching shell's cgroup; `setsid` is only the fallback, and
the command says so when it is used. `queue status` reports lock holder, unit
state and counts, and prints the resume command when the scheduler is gone.

### 5.4 Failure model

| Failure | Result |
| --- | --- |
| Controller crashes while writing | atomic replace: no partial file |
| File truncated/edited badly | count/hash mismatch → `.bak` fallback |
| Two schedulers | second refuses (flock) |
| Job SIGKILL/OOM/reboot | liveness check → `lost` → entry `stale`, queue continues |
| Machine unreachable | entry left as-is with a note; nothing is guessed |
| Controller dies between remote start and local save | entry stays `pending`; `max_parallel` prevents a concurrent duplicate, worst case is one redundant serial re-run |
| Scheduler killed with its session | `systemd-run` unit survives; if not, restart + reconcile |

### 5.5 Resume

`--resume-from <run-id>` finds that run's newest checkpoint (parse the log's
`Run directory:`, then `models/*.zip`, final model preferred) and appends
`--resume_model_path …`, which the training code supports. `queue retry <entry>`
puts a cancelled/failed entry back to `pending` without touching its history.

## 6. Inventory distribution

The curated layer is distributed by git. A new machine clones the repo and sees
the fleet immediately; `self --register` marks its own record in its overlay.
`inventory push/pull` was implemented and then removed: once the curated file is
in git, a second distribution mechanism only adds ambiguity. Because the
repository is public, the curated file contains only machines a human explicitly
added — discovery output never lands in git.

## 7. Security

- **No credentials on training hosts**: private repos are fetched by agent
  forwarding; nothing is stored remotely.
- **Passwords, when unavoidable**, live in `data/secrets.yaml` (mode 600,
  git-ignored), keyed by MAC, and are used only to install a key.
- **Shell construction** quotes every placeholder; only operator-typed `--extra`
  is raw.
- **No force-push** in any automated path; sync is `git pull --ff-only`.

## 8. Evidence that the design was tested, not assumed

- Reproducing the paper's PPO tail statistics from raw episodes matched
  `training_stats.csv` to 5.7e-14 — the comparison statistic is the paper's own.
- A 10-job overnight batch ran with `max_parallel: 1`, produced 10 runs with
  exit 0, and the queue reported `queue empty — nothing left`.
- A scheduler killed with its session was diagnosed from the journal, fixed with
  the systemd unit, and the existing run was recovered by reconciliation.
- Two structural bugs were found by running it: an empty `command.sh` (stdin
  closed too early) and `cd X && setsid … &` backgrounding the wrong process.
- Synthetic run directories on the target verified stale detection: a dead pid
  is not counted and gets a marker, a live `command.sh` is counted.

## 9. Known limitations

- Reminders that wake the agent need the DSH host running; a missed occurrence
  delivers once, late.
- Queues are per host: there is no global view of what another machine is
  scheduling (visibility is `job status`/`logs`, or fetching artifacts).
- `job sync` only knows repositories that ship a descriptor; tooling repos are
  pulled by hand.
- Discovery is L2-segment and ICMP-dependent; ICMP-blocked or isolated hosts
  stay invisible until registered.
- One controller may be mistaken about a target it cannot reach; the tool
  reports that rather than guessing.

## 10. Roadmap

- **Phase B**: hand pending entries to a training machine and let *its*
  scheduler run them (`queue push`), so continuity does not depend on the
  controller.
- `job status --all` / `queue peek <machine>`: read-only aggregation across the
  fleet.
- A `--verb evaluate` in the contract, so evaluations are tracked like training.
- Adoption-by-marker: stamp `command.sh` with the queue entry id so a crash can
  never cause even the redundant serial re-run.
- Optional: expose the scheduler as a long-lived user service with the same
  lock, for hosts that always run batches.
