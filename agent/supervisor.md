# Supervisor brief

You were woken by a scheduled reminder. Read this file, run the check, act only
when the decision table says so, and answer in the session that woke you.

Keep the report to the template at the bottom. Do not narrate what you are about
to do; do it.

## Where things are

| Thing | Value |
| --- | --- |
| Controller host | this laptop, repo `~/project/machine_mgr_repo` |
| Training host | `alienware-m17` (MAC `cc:5e:f8:07:36:13`), Ubuntu 24.04, RTX 3070 Ti, repo `~/project/bdp_benchmark` |
| Batch | ICRA 2027 TRPO sidecars: 10 jobs, sequential (`max_parallel: 1`), 2M steps each |
| Queue | `data/queue.yaml` (per host, git-ignored) |
| Run registry | `data/jobs.yaml` (per host) |
| Scheduler | systemd user unit `machine-mgr-queue` (`./machine_mgr job queue run --detach --poll 120`) |
| Logs / models | stay on the training host; fetch with `./machine_mgr job fetch <run> --artifact logs` |

Machine identity is MAC-keyed; `data/inventory.yaml` is the curated fleet,
`data/machines.yaml` is this host's overlay. Never edit those to "fix" a job.

## Step 1 — gather state (read-only)

```bash
cd ~/project/machine_mgr_repo
./machine_mgr job queue status
./machine_mgr job status | head -5
systemctl --user is-active machine-mgr-queue
RUN=$(./machine_mgr job status | awk '/running/{print $1}' | head -1)
[ -n "$RUN" ] && ./machine_mgr job logs "$RUN" -n 60 | tail -30
```

## Step 2 — decide

| Observation | Action |
| --- | --- |
| Scheduler active, a job is running, queue has pending entries | Nothing to do: report `healthy` and stop. |
| Nothing running, **all entries done** | Report the final tally (done / failed counts). Ask whether to delete this reminder. |
| Scheduler **inactive** and pending > 0 | Restart: `./machine_mgr job queue run --detach --poll 120`, then re-check and report `restarted`. |
| A run is `failed` or `lost` | `./machine_mgr job logs <run> -n 200`, classify it (below), act, report. |
| Target unreachable (`job queue status` cannot resolve it) | `./machine_mgr check alienware-m17`; if offline, report `blocked` and stop. Do not guess or retry blindly. |
| GPU or RAM exhausted on the target | Report `blocked` with the numbers; start nothing new. |

## Step 3 — classify a failure

**Code bug** — traceback in our code, config/schema error, failing test:

1. reproduce cheaply if possible (`python3 -m pytest tests -q` in `machine_mgr_repo`);
2. fix the root cause, not the symptom;
3. run the relevant tests; add one if the bug was untested;
4. commit with a message that states cause and fix;
5. `git push`;
6. sync the target: `./machine_mgr job sync alienware-m17 bdp_benchmark` (or `machine_mgr_repo`);
7. `./machine_mgr job queue retry <entry-id>` for the affected entry;
8. restart the scheduler if it is down, then report `fixed`.

**Environment** — OOM kill, reboot, network/proxy failure, disk full, GPU reset:

1. `./machine_mgr job queue retry <entry-id>`;
2. make sure the scheduler runs (`--detach`);
3. report `restarted` with the cause you saw in the log (quote one line).

If the same problem survives **two** attempts, stop and report `blocked` with what
you tried. The user prefers a clear stop over a loop.

## Rules

- Never `git push --force`; never delete run directories or queue entries
  unasked; never rewrite history.
- Do not change training hyperparameters, seeds or job YAMLs while supervising.
- Commit only after tests pass; one logical change per commit.
- Do not start extra work beyond the queue; if the batch is finished, say so.
- Long output stays out of the reply: quote at most one relevant log line.
- If the reminder that woke you is a one-shot probe, do **not** modify code —
  gather state and report only.

## Report template (one block, no preamble)

```text
status   <healthy|restarted|fixed|blocked>
running  <run-id> at <steps>/2M, <fps> fps     (or: none)
queue    <n> pending, <n> done, <n> failed
cause    <one line from the log, or ->
action   <what you changed/ran, or none>
next     <what happens next / what you need from the user>
```
