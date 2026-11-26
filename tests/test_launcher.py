"""Command rendering and script generation."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.contract import parse_descriptor
from jobs.launcher import build_run_id, build_script, render

VALUES = {
    "repo": "bdp_benchmark",
    "job": "/home/u/project/bdp_benchmark/scripts/jobs/a b;rm -rf x.yaml",
    "job_id": "bundle/a b;rm -rf x",
    "project_root": "/home/u/project",
    "run_id": "20260101-000000-x-ab12",
    "extra": "--num_timesteps 10",
    "env_name": "bdp",
}


def test_render_quotes_values():
    out = render("python3 scripts/train.py --config {job}", VALUES)
    # the whole path survives as ONE shell word, quotes or escapes included
    assert shlex.split(out)[-1] == VALUES["job"]


def test_render_leaves_unknown_braces_alone():
    assert render("echo {unknown} {repo}", VALUES) == f"echo {{unknown}} bdp_benchmark"


def test_build_script_has_cd_exports_trap_and_command():
    descriptor = parse_descriptor("""
version: 1
name: demo
runner:
  command: python3 scripts/train.py --config {job} {extra}
  env: {PYTHONPATH: "{project_root}"}
jobs: {discover: ["*.yaml"]}
""")
    script = build_script(
        repo_path="/home/u/project/demo", descriptor=descriptor,
        values=VALUES, run_dir="/home/u/project/demo/.machine_mgr/runs/r1",
        extra=VALUES["extra"],
    )
    assert "cd /home/u/project/demo" in script
    assert "export PYTHONPATH=/home/u/project" in script
    assert "trap 'rc=$?; echo $rc > " in script
    assert "--num_timesteps 10" in script
    assert script.startswith("#!/usr/bin/env bash")


def test_run_id_is_sortable_and_unique():
    a = build_run_id("demo", "scripts/job_scripts/bundle/x.yaml")
    b = build_run_id("demo", "scripts/job_scripts/bundle/x.yaml")
    assert a != b
    assert a[:8].isdigit()
    assert "bundle-x" in a or "bundle/x" not in a
