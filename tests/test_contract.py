"""Contract parsing and job-glob tests (no network, no ssh)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.contract import (ContractError, glob_to_regex, job_id_for, jobs_in_group,
                           list_jobs, parse_descriptor)

MINIMAL = """
version: 1
name: demo
runner:
  command: python3 train.py --config {job}
jobs:
  discover: ["scripts/jobs/**/*.yaml"]
"""


def test_parse_minimal_defaults():
    d = parse_descriptor(MINIMAL)
    assert d.name == "demo"
    assert d.git.require_match is True
    assert d.runner.workdir == "."
    assert d.limits.max_parallel == 1
    assert d.artifacts == {}


def test_parse_full_descriptor():
    text = """
version: 1
name: bdp_benchmark
git: {require_match: false, protocol: https}
env: {name: bdp, bootstrap: ./redeploy.sh, verify: "./redeploy.sh --verify"}
runner:
  command: python3 scripts/train.py --config {job}
  workdir: "."
  env: {PYTHONPATH: "{project_root}"}
jobs:
  discover: ["scripts/job_scripts/**/*.yaml"]
  exclude: ["**/*visual_check*.yaml"]
  groups:
    - {title: ICRA 2027, glob: "scripts/job_scripts/icra2027*/*.yaml"}
artifacts:
  logs: {paths: ["logs/benchmark"], kind: dir}
limits: {max_parallel: 2, min_free_gpu_mem_gb: 4}
"""
    d = parse_descriptor(text)
    assert d.git.require_match is False
    assert d.env.name == "bdp"
    assert d.runner.env == {"PYTHONPATH": "{project_root}"}
    assert d.exclude == ["**/*visual_check*.yaml"]
    assert d.groups[0].title == "ICRA 2027"
    assert d.artifacts["logs"].paths == ["logs/benchmark"]
    assert d.limits.max_parallel == 2


@pytest.mark.parametrize("text,needle", [
    ("version: 1\nname: x\n", "runner.command is required"),
    ("version: 1\nrunner: {command: x}\n", "'name' is required"),
    ("version: 1\nname: x\nrunner: {command: x}\njobs: {discover: []}\n", "jobs.discover"),
    ("version: 9\nname: x\nrunner: {command: x}\njobs: {discover: [a]}\n", "not supported"),
    ("version: 1\nname: x\nrunner: {command: x}\njobs: {discover: [a]}\ngit: {protocol: ftp}\n", "auto|https|ssh"),
])
def test_invalid_descriptors(text, needle):
    with pytest.raises(ContractError) as exc:
        parse_descriptor(text)
    assert needle in str(exc.value)


def test_glob_double_star():
    files = ["scripts/jobs/a.yaml",
             "scripts/jobs/bundle/b.yaml",
             "scripts/jobs/bundle/deep/c.yaml",
             "docs/d.yaml"]
    d = parse_descriptor(MINIMAL)
    assert list_jobs(d, files) == [
        "scripts/jobs/a.yaml",
        "scripts/jobs/bundle/b.yaml",
        "scripts/jobs/bundle/deep/c.yaml",
    ]


def test_exclude_wins():
    text = MINIMAL.replace('discover: ["scripts/jobs/**/*.yaml"]',
                           'discover: ["scripts/jobs/**/*.yaml"]\n  exclude: ["**/visual_check*.yaml"]')
    d = parse_descriptor(text)
    files = ["scripts/jobs/a.yaml", "scripts/jobs/visual_check_a.yaml"]
    assert list_jobs(d, files) == ["scripts/jobs/a.yaml"]


def test_groups_filter():
    text = MINIMAL.replace('  discover: ["scripts/jobs/**/*.yaml"]',
                           '  discover: ["scripts/jobs/**/*.yaml"]\n'
                           '  groups:\n    - {title: PPO vs TRPO, glob: "scripts/jobs/icra*/*.yaml"}')
    d = parse_descriptor(text)
    files = ["scripts/jobs/icra1/a.yaml", "scripts/jobs/other/b.yaml"]
    assert jobs_in_group(d, d.groups[0], files) == ["scripts/jobs/icra1/a.yaml"]


def test_job_id_trims_common_parents():
    assert job_id_for("scripts/job_scripts/x.yaml") == "x"
    assert job_id_for("scripts/job_scripts/bundle/x.yaml") == "bundle/x"


def test_glob_regex_anchoring():
    assert glob_to_regex("**/*.yaml").match("a/b/c.yaml")
    assert not glob_to_regex("*.yaml").match("a/b.yaml")
