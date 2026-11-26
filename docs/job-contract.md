# The job contract (version 1)

`machine_mgr` knows how to *reach* machines and how to *run and track* jobs. It
deliberately knows nothing about training code. Every repository that exposes
jobs ships one file at its root, `.machine_mgr.yaml`, containing **values only**.
All behaviour lives in `machine_mgr/jobs/`.

That split is the whole design:

| Owner | Knows |
| --- | --- |
| `machine_mgr` | schema, validation, rendering, submission, tracking, logs, cancel, fetch |
| each repo | which files are jobs, how to invoke its entry point, what to fetch |

Adding a new repo (e.g. `hrl_nav`) means adding one YAML file. No machine_mgr
code changes, no `if repo == …` branches anywhere.

Rules:

1. **No executable extension points.** No hooks, no plugins. If a repo needs
   behaviour the schema lacks, the honest options are (a) use an existing field,
   (b) change the command, (c) request a shared schema field. This keeps review
   and maintenance bounded.
2. **Values are shell-quoted.** `{placeholders}` are substituted with
   `shlex.quote`, so paths containing spaces or `;` cannot break out of the
   command. The only unquoted text is what you type in `--extra`.
3. **Versioned.** `machine_mgr` speaks version 1 and refuses other versions with
   a clear error.

## Schema

```yaml
version: 1                     # required, must be 1
name: bdp_benchmark            # required, unique per target

git:                           # optional
  require_match: true          # refuse submit when local HEAD ≠ target HEAD
  protocol: auto               # auto | https | ssh (informational)

env:                           # optional, used by `machine_mgr env …`
  name: bdp                    # conda env exposed to the job as {env_name}
  bootstrap: ./redeploy.sh     # run on the target by `env deploy`
  verify: "./redeploy.sh --verify"

runner:                        # required
  command: "python3 scripts/train.py --config {job}"   # required
  workdir: "."                 # relative to the repo root
  shell: bash
  env:                         # exported before the command
    PYTHONPATH: "{project_root}"

jobs:                          # required
  discover: ["scripts/job_scripts/**/*.yaml"]          # required, ≥1 glob
  exclude:  ["**/*visual_check*.yaml"]                 # optional
  groups:                                              # optional, for the picker
    - {title: "ICRA 2027 PPO vs TRPO", glob: "scripts/job_scripts/icra2027*/*.yaml"}

artifacts:                     # optional, used by `job fetch`
  logs:
    paths: ["logs/benchmark"]  # repo-relative
    kind: dir                  # dir | file
    exclude: ["**/*.png"]

limits:                        # optional
  max_parallel: 1              # refuse a second concurrent run in this repo
  min_free_gpu_mem_gb: 2       # preflight error when the GPU is too busy
```

### Placeholders

| Placeholder | Value |
| --- | --- |
| `{repo}` | descriptor `name` |
| `{job}` | absolute path of the job file on the target |
| `{job_id}` | short id, e.g. `icra2027_trpo_vs_ppo_12mps/x` |
| `{project_root}` | the machine's project root (`~/project` by default) |
| `{run_id}` | unique run id, e.g. `20261126-0615-x-a1b2` |
| `{env_name}` | `env.name` |
| `{extra}` | the `--extra` string, appended verbatim |

## Git synchronisation

The host pushes; the target pulls. `job submit` compares `git rev-parse HEAD`
locally with the target checkout and refuses on mismatch (override with
`--allow-head-mismatch`), because a job whose YAML is not on the target is not
reproducible. Uncommitted files and unpushed commits produce warnings — they
exist only on this machine.

Public repos pull over HTTPS with no credential. Private repos (`critic_based_rl`)
need a credential on the target; `machine_mgr` uses **agent forwarding**
(`ssh -A`), so nothing is stored remotely:

```bash
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519   # once per session
./machine_mgr job sync alienware-m17 critic_based_rl
```

## What a submitted run looks like

```
<repo>/.machine_mgr/runs/<run_id>/
  command.sh        frozen, exact script that runs (reviewable after the fact)
  submitted.yaml    the job YAML as submitted
  env_name          conda env name recorded at submit time
  log               stdout+stderr
  pid  exit         liveness and result
```

The process is started with `setsid`, so it survives the ssh session ending.
`command.sh` traps `EXIT` and writes the real exit status, which is what
`job status` reads — no log scraping.
