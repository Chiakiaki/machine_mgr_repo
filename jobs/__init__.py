"""Generic, repo-agnostic job orchestration.

Modules
-------
transport   ssh wrapper used by everything else
contract    the .machine_mgr.yaml schema (shared; repos supply values only)
catalog     discover descriptors/jobs on a target
preflight   git HEAD match, dirty tree, GPU and busy checks
launcher    render + detached submit with a frozen command snapshot
tracker     run registry (data/jobs.yaml), status, logs, cancel, fetch
cli         `machine_mgr job …` / `machine_mgr env …`
"""
