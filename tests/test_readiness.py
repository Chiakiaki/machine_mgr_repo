"""Resource gates used by the queue scheduler."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobs.contract import parse_descriptor
from jobs.preflight import readiness

DESC = """
version: 1
name: demo
runner: {command: "python3 x.py"}
jobs: {discover: ["*.yaml"]}
limits: {max_parallel: 1, min_free_gpu_mem_gb: 2, min_free_ram_gb: 6}
"""


class FakeRemote:
    def __init__(self, running=(), free_mb=10240, gpu=(8192, 1024)):
        self.running = list(running)
        self.free_mb = free_mb
        self.gpu = gpu

    def try_run(self, command: str, timeout: float = 60):
        if ".machine_mgr/runs" in command:
            return 0, "\n".join(self.running) + ("\n" if self.running else ""), ""
        if "free -m" in command:
            return 0, f"{self.free_mb}\n", ""
        if "nvidia-smi" in command:
            return 0, f"{self.gpu[0]}, {self.gpu[1]}\n", ""
        return 0, "", ""


def test_ready_when_idle_and_roomy():
    ok, reasons = readiness(FakeRemote(), "/repo", parse_descriptor(DESC))
    assert ok, reasons


def test_blocked_by_active_run():
    remote = FakeRemote(running=["run-1"])
    ok, reasons = readiness(remote, "/repo", parse_descriptor(DESC))
    assert not ok and "already running" in reasons[0]


def test_blocked_by_low_ram():
    remote = FakeRemote(free_mb=2048)  # 2 GiB < 6 GiB
    ok, reasons = readiness(remote, "/repo", parse_descriptor(DESC))
    assert not ok and "RAM free" in reasons[0]


def test_blocked_by_busy_gpu():
    remote = FakeRemote(gpu=(8192, 7500))  # 0.7 GiB free < 2
    ok, reasons = readiness(remote, "/repo", parse_descriptor(DESC))
    assert not ok and "GPU free" in reasons[0]


def test_ram_defaults_to_zero_when_unset():
    desc = parse_descriptor(DESC.replace(", min_free_ram_gb: 6", ""))
    assert desc.limits.min_free_ram_gb == 0.0
    ok, _ = readiness(FakeRemote(free_mb=10), "/repo", desc)
    assert ok  # no RAM gate configured
