"""Command-line and interactive front end for machine_mgr."""

from __future__ import annotations

import argparse
import getpass
import shutil
import sys
from pathlib import Path

from . import discovery, ssh_tools
from .discovery import ScanCache, normalize_mac
from .registry import Machine, Registry, slugify, unique_name
from jobs.queue import QueueStore
from jobs.tracker import RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "data" / "machines.yaml"
DEFAULT_SECRETS = REPO_ROOT / "data" / "secrets.yaml"
DEFAULT_CACHE = REPO_ROOT / "data" / "cache" / "last_scan.json"
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_LOCAL_PROJECT = Path.home() / "project"

OK = "\033[32m●\033[0m"
BAD = "\033[31m○\033[0m"
DIM = "\033[2m"
RESET = "\033[0m"


# ── presentation helpers ────────────────────────────────────────────────────

def table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("  (nothing to show)")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    print("  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for row in rows:
        print("  " + "  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


def header(text: str) -> None:
    print(f"\n{text}\n" + "─" * max(len(text), 20))


def say(msg: str) -> None:
    print(f"  {msg}")


def fail(msg: str) -> int:
    print(f"  error: {msg}", file=sys.stderr)
    return 1


# ── shared operations ──────────────────────────────────────────────────────

class Context:
    def __init__(self, manifest: Path, subnet: str | None, timeout: int,
                 jobs: int, no_cache: bool, data_dir: Path | None = None,
                 local_project_root: str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.runs = RunStore(self.data_dir / "jobs.yaml")
        self.queue = QueueStore(self.data_dir / "queue.yaml")
        self.local_project_root = str(local_project_root or DEFAULT_LOCAL_PROJECT)
        self.registry = Registry(manifest)
        self.credentials = ssh_tools.Credentials(DEFAULT_SECRETS)
        self.subnet = subnet
        self.timeout = timeout
        self.jobs = jobs
        self.cache = None if no_cache else ScanCache(DEFAULT_CACHE)

    # discovery
    def scan(self, use_cache: bool = True) -> discovery.ScanResult:
        return discovery.scan(subnet=self.subnet, timeout=self.timeout, jobs=self.jobs,
                              use_cache=use_cache, cache=self.cache)

    def resolve(self, identifier: str) -> Machine:
        return self.registry.find(identifier)

    def ip_of(self, machine: Machine) -> str | None:
        return discovery.find_by_mac(machine.mac, subnet=self.subnet,
                                     timeout=self.timeout, cache=self.cache)

    # ssh details
    def auth_for(self, machine: Machine) -> tuple[str, int, str | None]:
        cred = self.credentials.get(machine.mac) or {}
        user = machine.ssh_user or cred.get("user") or ""
        password = cred.get("password") or ssh_tools.env_password(machine.mac)
        return user, machine.ssh_port or 22, password


def auto_name(machine: Machine, machines: list[Machine]) -> str:
    """Derive a readable, collision-free label.

    Priority: recorded hostname, then OUI vendor + MAC suffix, then a generic
    host-<mac tail>. Duplicates always get -2, -3, … appended.
    """
    base = slugify(machine.hostname) if machine.hostname else ""
    if not base:
        vendor = discovery.vendor_for(machine.mac)
        tail = machine.mac.replace(":", "")[-4:]
        base = f"{vendor}-{tail}" if vendor else f"host-{tail}"
    return unique_name(base, machines, ignore_mac=machine.mac)


def is_auto_named(machine: Machine) -> bool:
    tail = machine.mac.replace(":", "")[-4:]
    return (not machine.name) or machine.name.startswith("host-") or \
        machine.name.endswith(tail)


# ── commands ───────────────────────────────────────────────────────────────

def cmd_scan(ctx: Context, register: bool = False, quiet: bool = False) -> int:
    try:
        result = ctx.scan(use_cache=False)
    except RuntimeError as exc:
        return fail(str(exc))
    if not quiet:
        say(f"interface {result.interface}  subnet {result.subnet}  "
            f"({result.hosts_scanned} hosts, {len(result.alive)} alive, {result.elapsed:.1f}s)")
    rows = []
    for mac, ip in result.entries:
        known = ctx.registry.get_by_mac(mac)
        if known and register:
            known.touch()
        elif register:
            known = ctx.registry.add(Machine(mac=mac))
            known.name = auto_name(known, ctx.registry.machines)
        rows.append([known.display if known else "(unregistered)", mac, ip,
                     "known" if known and known.os else ""])
    if register:
        ctx.registry.save()
    table(["machine", "mac", "current ip (volatile)", "os"], rows)
    if not register:
        say("tip: `scan --register` adds new machines to the manifest (MAC only, no IP).")
    return 0


def cmd_list(ctx: Context) -> int:
    rows = []
    for m in ctx.registry.machines:
        rows.append([m.display, m.mac, m.os_version or m.os or "?",
                     m.ssh_user or "-", m.last_seen or "-"])
    header(f"Registered machines ({len(rows)})")
    table(["name", "mac", "os", "ssh user", "last seen"], rows)
    return 0


def cmd_check(ctx: Context, identifier: str | None, all_machines: bool) -> int:
    if all_machines:
        machines = ctx.registry.machines
        if not machines:
            return fail("manifest is empty; run `scan --register` first")
    else:
        try:
            machines = [ctx.resolve(identifier or "")]
        except KeyError as exc:
            return fail(str(exc).strip("'"))

    try:
        result = ctx.scan(use_cache=False)
    except RuntimeError as exc:
        return fail(str(exc))

    rows, online = [], 0
    for m in machines:
        ip = result.mac_to_ip.get(m.mac)
        alive = bool(ip)
        if alive:
            online += 1
            m.touch()
        rows.append([OK if alive else BAD, m.display, m.mac,
                     ip if ip else "-", "online" if alive else "offline"])
    ctx.registry.save()
    header("Availability")
    table(["", "machine", "mac", "current ip", "state"], rows)
    say(f"{online}/{len(machines)} online  {DIM}(ip is resolved live, never stored){RESET}")
    return 0 if online else 1


def cmd_show(ctx: Context, identifier: str) -> int:
    try:
        m = ctx.resolve(identifier)
    except KeyError as exc:
        return fail(str(exc).strip("'"))
    header(f"Machine {m.display}")
    for key in ("mac", "name", "hostname", "ssh_user", "ssh_port", "os", "os_version",
                "first_seen", "last_seen", "tags", "notes"):
        value = getattr(m, key)
        if isinstance(value, list):
            value = ", ".join(value) or "-"
        print(f"  {key:12s} {value or '-'}")
    ip = ctx.ip_of(m)
    print(f"  {'current ip':12s} {ip or '(offline)'}  {DIM}volatile{RESET}")
    stored = ctx.credentials.get(m.mac)
    if stored:
        print(f"  {'credentials':12s} stored for user {stored.get('user') or '-'} "
              f"{DIM}({ctx.credentials.path.name}){RESET}")
    else:
        print(f"  {'credentials':12s} {'-'}  {DIM}use `register --ask-password` or `key-install`{RESET}")
    return 0


def cmd_register(ctx: Context, mac: str, name: str | None, user: str | None,
                 port: int | None, notes: str | None,
                 password: str | None = None, ask_password: bool = False) -> int:
    try:
        mac = normalize_mac(mac)
    except ValueError as exc:
        return fail(str(exc))
    existing = ctx.registry.get_by_mac(mac)
    if existing:
        say(f"{mac} already registered as '{existing.display}'")
        machine = existing
    else:
        machine = ctx.registry.add(Machine(mac=mac))
    if name:
        machine.name = name
    if user:
        machine.ssh_user = user
    if port:
        machine.ssh_port = port
    if notes:
        machine.notes = notes
    ctx.registry.save()

    # Optional one-shot credential capture, so later `ssh`/`probe` never prompt.
    cred_user = machine.ssh_user
    if (ask_password or password is not None) and not cred_user:
        return fail(f"--user is required to store a password for {mac}")
    if ask_password and password is None:
        password = getpass.getpass(f"  password for {cred_user}@{machine.display}: ")
    if password is not None and cred_user:
        ctx.credentials.set(mac, cred_user, password)
        say(f"stored credentials for {mac} (mode 600, git-ignored)")
    say(f"registered {machine.display} ({machine.mac})")
    return 0


def cmd_rename(ctx: Context, identifier: str, new_name: str | None) -> int:
    try:
        m = ctx.resolve(identifier)
    except KeyError as exc:
        return fail(str(exc).strip("'"))
    if not new_name:
        new_name = auto_name(m, ctx.registry.machines)
        say(f"derived name '{new_name}'")
    cleaned = slugify(new_name)
    if not cleaned:
        return fail("name is empty after normalisation")
    clash = next((x for x in ctx.registry.machines
                  if x.mac != m.mac and x.name.lower() == cleaned), None)
    if clash:
        cleaned = unique_name(cleaned, ctx.registry.machines, ignore_mac=m.mac)
        say(f"'{slugify(new_name)}' taken by {clash.mac}; using '{cleaned}'")
    old, m.name = m.name, cleaned
    ctx.registry.save()
    say(f"renamed {m.mac}: '{old or '-'}' -> '{cleaned}'")
    return 0


def cmd_autoname(ctx: Context, identifier: str | None, all_machines: bool) -> int:
    targets = ctx.registry.machines if all_machines else []
    if not all_machines:
        try:
            targets = [ctx.resolve(identifier or "")]
        except KeyError as exc:
            return fail(str(exc).strip("'"))
    changed = 0
    for m in targets:
        new = auto_name(m, ctx.registry.machines)
        if new != m.name:
            say(f"{m.mac}: '{m.name or '-'}' -> '{new}'")
            m.name = new
            changed += 1
    ctx.registry.save()
    say(f"{changed} machine(s) renamed")
    return 0


def cmd_forget(ctx: Context, identifier: str) -> int:
    try:
        m = ctx.resolve(identifier)
    except KeyError as exc:
        return fail(str(exc).strip("'"))
    ctx.registry.remove(m)
    ctx.registry.save()
    say(f"removed {m.display} ({m.mac})")
    return 0


def cmd_probe(ctx: Context, identifier: str, save: bool = True) -> int:
    try:
        m = ctx.resolve(identifier)
    except KeyError as exc:
        return fail(str(exc).strip("'"))
    ip = ctx.ip_of(m)
    if not ip:
        return fail(f"{m.display} ({m.mac}) is not reachable on {ctx.subnet or 'the local network'}")
    user, port, password = ctx.auth_for(m)
    if not user:
        return fail(f"no ssh user for {m.mac}; run `register --user` first")
    say(f"probing {user}@{ip}:{port} ...")
    try:
        facts = ssh_tools.probe(ip, user, port, password=password)
    except RuntimeError as exc:
        return fail(f"{exc}\n  hint: check the user/password, or install a key with `key-install`")
    header("Remote system")
    for key, value in facts.items():
        print(f"  {key:14s} {value}")
    if save:
        was_auto = is_auto_named(m)
        m.hostname = facts.get("hostname", m.hostname)
        m.os = facts.get("os_id", m.os)
        m.os_version = facts.get("os_pretty") or facts.get("os_version") or m.os_version
        if was_auto and m.hostname:
            new = auto_name(m, ctx.registry.machines)
            if new != m.name:
                say(f"auto-renamed '{m.name or m.mac}' -> '{new}' (from hostname)")
                m.name = new
        m.touch()
        ctx.registry.save()
        say("manifest updated")
    return 0


def cmd_ssh(ctx: Context, identifier: str, command: str | None, tty: bool) -> int:
    try:
        m = ctx.resolve(identifier)
    except KeyError as exc:
        return fail(str(exc).strip("'"))
    ip = ctx.ip_of(m)
    if not ip:
        return fail(f"{m.display} ({m.mac}) is offline")
    user, port, password = ctx.auth_for(m)
    if not user:
        user = getpass.getuser()
        say(f"no ssh user recorded; trying {user}")
    try:
        proc = ssh_tools.ssh(ip, user, port, command, password=password,
                             allocate_tty=tty or command is None, timeout=None
                             if command is None else 3600)
    except RuntimeError as exc:
        return fail(str(exc))
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    return proc.returncode


def cmd_mkdir(ctx: Context, identifier: str, path: str) -> int:
    """Create a directory on the remote host (default ~/project)."""
    try:
        m = ctx.resolve(identifier)
    except KeyError as exc:
        return fail(str(exc).strip("'"))
    ip = ctx.ip_of(m)
    if not ip:
        return fail(f"{m.display} is offline")
    user, port, password = ctx.auth_for(m)
    if not user:
        return fail(f"no ssh user for {m.mac}")
    try:
        resolved = ssh_tools.ensure_remote_dir(ip, user, port, path, password=password)
    except RuntimeError as exc:
        return fail(str(exc))
    say(f"created/verified {user}@{ip}:{resolved}")
    return 0


def cmd_key_install(ctx: Context, identifier: str) -> int:
    try:
        m = ctx.resolve(identifier)
    except KeyError as exc:
        return fail(str(exc).strip("'"))
    ip = ctx.ip_of(m)
    if not ip:
        return fail(f"{m.display} is offline")
    user, port, password = ctx.auth_for(m)
    if not user:
        return fail(f"no ssh user for {m.mac}")
    if not password and not ssh_tools.port_open(ip, port):
        return fail(f"port {port} closed on {ip}; install openssh-server on the target")
    proc = ssh_tools.install_key(ip, user, port, password=password)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out.strip() or "(no output)")
    if proc.returncode != 0:
        return fail("ssh-copy-id failed")
    say("key installed — password no longer needed for this machine")
    return 0


def cmd_password(ctx: Context, identifier: str, password: str | None) -> int:
    try:
        m = ctx.resolve(identifier)
    except KeyError as exc:
        return fail(str(exc).strip("'"))
    user, _, _ = ctx.auth_for(m)
    if not user:
        return fail(f"no ssh user recorded for {m.mac}; pass --user or `register --user`")
    if password is None:
        password = getpass.getpass(f"password for {user}@{m.display}: ")
    ctx.credentials.set(m.mac, user, password)
    ctx.credentials.path.chmod(0o600)
    say(f"stored credentials for {m.mac} in {ctx.credentials.path} (mode 600, git-ignored)")
    return 0


def cmd_doctor(ctx: Context) -> int:
    header("Prerequisites")
    checks = [
        ("ssh client", shutil.which("ssh")),
        ("ssh-copy-id", shutil.which("ssh-copy-id")),
        ("ping", shutil.which("ping")),
        ("ip (iproute2)", shutil.which("ip")),
    ]
    ok = True
    for label, path in checks:
        print(f"  {OK if path else BAD} {label:16s} {path or 'MISSING'}")
        ok &= bool(path)
    try:
        iface, cidr = discovery.lan_interface()
        print(f"  {OK} {'lan interface':16s} {iface} ({cidr})")
    except RuntimeError as exc:
        ok = False
        print(f"  {BAD} {'lan interface':16s} {exc}")
    print(f"  {'  '} {'manifest':16s} {ctx.registry.path} "
          f"({len(ctx.registry.machines)} machines)")
    print(f"  {'  '} {'secrets':16s} {ctx.credentials.path}")
    return 0 if ok else 1


# ── interactive mode ───────────────────────────────────────────────────────

MENU = [
    ("Scan the local network", "scan"),
    ("List registered machines", "list"),
    ("Check whether a machine is online", "check"),
    ("Show machine details", "show"),
    ("Register a machine by MAC", "register"),
    ("Probe a machine over SSH", "probe"),
    ("Open SSH shell / run remote command", "ssh"),
    ("Create a directory on a machine (~/project)", "mkdir"),
    ("Install my SSH key on a machine", "key-install"),
    ("Store SSH password for a machine", "password"),
    ("Rename a machine (or auto-derive a name)", "rename"),
    ("Auto-name machines from hostname/vendor/MAC", "autoname"),
    ("Forget a machine", "forget"),
    ("Submit a job on a machine…", "job-submit"),
    ("Remote job status", "job-status"),
    ("Tail a run log", "job-logs"),
    ("Cancel a run", "job-cancel"),
    ("Deploy/verify a repo environment", "env"),
    ("Doctor: check prerequisites", "doctor"),
]


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"  {prompt}{suffix}: ").strip()
    return value or default


def _choose(prompt: str, options: list[str]) -> str:
    for i, option in enumerate(options, 1):
        print(f"    {i:2d}) {option}")
    raw = input(f"  {prompt} [1]: ").strip() or "1"
    if not raw.isdigit() or not (1 <= int(raw) <= len(options)):
        raise ValueError("invalid selection")
    return options[int(raw) - 1]


def _pick_machine(ctx: Context, require_ssh: bool = False):
    machines = [m for m in ctx.registry.machines if m.ssh_user] if require_ssh \
        else list(ctx.registry.machines)
    if not machines:
        raise ValueError("no machines registered with an ssh user — run `register --user` first")
    labels = [f"{m.display}  ({m.mac})" for m in machines]
    return machines[labels.index(_choose("machine", labels))]


def _job_ctx():
    from jobs import cli as job_cli
    return job_cli


def _interactive_submit(ctx: Context) -> None:
    from jobs import catalog
    job_cli = _job_ctx()
    machine = _pick_machine(ctx, require_ssh=True)
    target = job_cli.target_for(ctx, machine.display)
    repos = catalog.find_repos(target.remote, target.project_root)
    if not repos:
        print("  no repo with a .machine_mgr.yaml on that machine")
        return
    repo_name = _choose("repo", [r.name for r in repos])
    # find_repos() only sees descriptors; resolve_repo() also loads the file list
    repo = catalog.resolve_repo(target.remote, target.project_root, repo_name)

    groups = ["(all jobs)"] + [g.title for g in repo.descriptor.groups]
    group = _choose("filter", groups)
    from jobs.contract import jobs_in_group, list_jobs
    jobs = (list_jobs(repo.descriptor, repo.files) if group == "(all jobs)"
            else jobs_in_group(repo.descriptor, next(g for g in repo.descriptor.groups
                                                    if g.title == group), repo.files))
    if not jobs:
        print("  no jobs match")
        return
    job = _choose(f"job ({len(jobs)} available)", jobs)
    extra = input("  extra arguments (empty = none): ").strip()
    answer = input(f"  submit {repo_name}:{job} on {machine.display}? (y/N) ").strip().lower()
    if not answer.startswith("y"):
        print("  cancelled")
        return
    job_cli.cmd_submit(ctx, machine.display, repo_name, job, extra=extra)


def _interactive_status(ctx: Context) -> None:
    _job_ctx().cmd_status(ctx, None, None)


def _interactive_logs(ctx: Context) -> None:
    runs = ctx.runs.recent()[:10]
    if not runs:
        print("  no runs recorded")
        return
    labels = [f"{r.id}  {r.status:9s} {r.machine}" for r in runs]
    run = runs[labels.index(_choose("run", labels))]
    lines = input("  lines [40]: ").strip() or "40"
    _job_ctx().cmd_logs(ctx, run.id, int(lines))


def _interactive_cancel(ctx: Context) -> None:
    runs = [r for r in ctx.runs.recent()[:10] if r.status in ("running", "lost", "")]
    if not runs:
        print("  no active runs")
        return
    labels = [f"{r.id}  {r.status:9s} {r.machine}" for r in runs]
    run = runs[labels.index(_choose("run to cancel", labels))]
    _job_ctx().cmd_cancel(ctx, run.id)


def _interactive_env(ctx: Context) -> None:
    from jobs import catalog
    job_cli = _job_ctx()
    machine = _pick_machine(ctx, require_ssh=True)
    target = job_cli.target_for(ctx, machine.display, forward_agent=True)
    repos = catalog.find_repos(target.remote, target.project_root)
    if not repos:
        print("  no repo with a .machine_mgr.yaml on that machine")
        return
    repo_name = _choose("repo", [r.name for r in repos])
    action = _choose("action", ["deploy (install/repair)", "verify (check only)"])
    job_cli.cmd_env(ctx, machine.display, repo_name, "verify" if action.startswith("verify") else "deploy")


def interactive(ctx: Context) -> int:
    while True:
        print("\n" + "=" * 52)
        print("  machine_mgr — local network machine manager")
        print("=" * 52)
        for i, (label, _) in enumerate(MENU, 1):
            print(f"  {i:2d}) {label}")
        print("   0) Quit")
        choice = input("\n  select> ").strip().lower()
        if choice in ("0", "q", "quit", "exit", ""):
            return 0
        if not choice.isdigit() or not (1 <= int(choice) <= len(MENU)):
            print("  invalid choice")
            continue
        action = MENU[int(choice) - 1][1]
        try:
            if action == "scan":
                cmd_scan(ctx, register=_ask("also register new machines? (y/N)").lower().startswith("y"))
            elif action == "list":
                cmd_list(ctx)
            elif action == "check":
                ident = _ask("machine (mac/name, or 'all')")
                cmd_check(ctx, ident, ident.lower() in ("all", ""))
            elif action == "show":
                cmd_show(ctx, _ask("machine"))
            elif action == "register":
                mac = _ask("mac address")
                name = _ask("name (optional)") or None
                user = _ask("ssh user (optional)") or None
                store = _ask("store the SSH password now? (y/N)").lower().startswith("y")
                cmd_register(ctx, mac, name, user, None, None,
                             None, ask_password=store)
            elif action == "probe":
                cmd_probe(ctx, _ask("machine"))
            elif action == "ssh":
                ident = _ask("machine")
                remote_cmd = _ask("remote command (empty = interactive shell)")
                cmd_ssh(ctx, ident, remote_cmd or None, False)
            elif action == "mkdir":
                cmd_mkdir(ctx, _ask("machine"), _ask("path", "~/project"))
            elif action == "key-install":
                cmd_key_install(ctx, _ask("machine"))
            elif action == "password":
                cmd_password(ctx, _ask("machine"), None)
            elif action == "rename":
                cmd_rename(ctx, _ask("machine"), _ask("new name (empty = auto)") or None)
            elif action == "autoname":
                ident = _ask("machine (or 'all')")
                cmd_autoname(ctx, None if ident.lower() in ("all", "") else ident,
                             ident.lower() in ("all", ""))
            elif action == "forget":
                cmd_forget(ctx, _ask("machine"))
            elif action == "job-submit":
                _interactive_submit(ctx)
            elif action == "job-status":
                _interactive_status(ctx)
            elif action == "job-logs":
                _interactive_logs(ctx)
            elif action == "job-cancel":
                _interactive_cancel(ctx)
            elif action == "env":
                _interactive_env(ctx)
            elif action == "doctor":
                cmd_doctor(ctx)
        except KeyboardInterrupt:
            print("\n  cancelled")
        except (KeyError, ValueError, RuntimeError) as exc:
            print(f"  error: {str(exc).strip(chr(39))}")
        input("\n  press enter to continue...")


# ── argument parsing ───────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="machine_mgr",
        description="Find, register and SSH into machines on the local network "
                    "(identified by MAC, never by DHCP address). Run with no "
                    "arguments for the interactive menu.",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST,
                        help="manifest path (default: data/machines.yaml)")
    parser.add_argument("--subnet", help="override subnet, e.g. 192.168.5.0/24")
    parser.add_argument("--timeout", type=int, default=1, help="ping timeout per host (s)")
    parser.add_argument("--jobs", type=int, default=64, help="parallel ping workers")
    parser.add_argument("--no-cache", action="store_true", help="ignore the scan cache")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="where jobs.yaml / artifacts live (default: data/)")
    parser.add_argument("--local-project-root", default=str(DEFAULT_LOCAL_PROJECT),
                        help="this machine's clone of the repos, for HEAD comparison")

    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("scan", help="ping-sweep the LAN and list MACs")
    p.add_argument("--register", action="store_true", help="add unknown machines to the manifest")

    sub.add_parser("list", help="list registered machines")

    p = sub.add_parser("check", help="is a registered machine online right now?")
    p.add_argument("machine", nargs="?", help="mac/name, or 'all'")
    p.add_argument("--all", action="store_true", help="check every registered machine")

    p = sub.add_parser("show", help="show one machine in detail")
    p.add_argument("machine")

    p = sub.add_parser("register", help="register a machine by MAC")
    p.add_argument("mac")
    p.add_argument("--name")
    p.add_argument("--user", help="ssh user")
    p.add_argument("--port", type=int)
    p.add_argument("--notes")
    p.add_argument("--password", help="store this SSH password now (mode 600, git-ignored)")
    p.add_argument("--ask-password", action="store_true",
                   help="prompt for the SSH password now and store it")
    p.add_argument("--password-stdin", action="store_true",
                   help="read the SSH password from stdin (keeps it out of ps/history)")

    p = sub.add_parser("rename", help="give a machine a label (deduplicated)")
    p.add_argument("machine")
    p.add_argument("name", nargs="?", help="new label; omit to derive one automatically")

    p = sub.add_parser("autoname", help="derive labels from hostname/vendor/MAC")
    p.add_argument("machine", nargs="?")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("forget", help="remove a machine from the manifest")
    p.add_argument("machine")

    p = sub.add_parser("probe", help="SSH in and record system info")
    p.add_argument("machine")
    p.add_argument("--no-save", action="store_true")

    p = sub.add_parser("ssh", help="resolve the machine's current IP and run SSH")
    p.add_argument("machine")
    p.add_argument("remote_command", nargs=argparse.REMAINDER)

    p = sub.add_parser("mkdir", help="create a directory on a machine (default ~/project)")
    p.add_argument("machine")
    p.add_argument("path", nargs="?", default="~/project")

    p = sub.add_parser("key-install", help="install this host's public key")
    p.add_argument("machine")

    p = sub.add_parser("password", help="store the SSH password for a machine")
    p.add_argument("machine")
    p.add_argument("--password", help="non-interactive (prefer the prompt or env var)")

    p = sub.add_parser("job", help="submit and track jobs on a machine")
    jsub = p.add_subparsers(dest="job_command", required=True)
    q = jsub.add_parser("repos", help="repos with a .machine_mgr.yaml on the target")
    q.add_argument("machine")
    q = jsub.add_parser("list", help="list jobs in a repo")
    q.add_argument("machine")
    q.add_argument("--repo")
    q.add_argument("--group")
    q = jsub.add_parser("submit", help="submit a job (detached on the target)")
    q.add_argument("machine")
    q.add_argument("repo")
    q.add_argument("job")
    q.add_argument("--extra", default="", help="extra arguments appended to the command")
    q.add_argument("--dry-run", action="store_true", help="print the script, submit nothing")
    q.add_argument("--allow-head-mismatch", action="store_true")
    q.add_argument("--force", action="store_true", help="ignore preflight errors")
    q.add_argument("--resume-from", default="", help="continue from a previous run's checkpoint")
    q = jsub.add_parser("status", help="run status")
    q.add_argument("run_id", nargs="?")
    q.add_argument("--machine")
    q = jsub.add_parser("logs", help="tail a run log")
    q.add_argument("run_id")
    q.add_argument("-n", type=int, default=40)
    q = jsub.add_parser("cancel", help="terminate a run's process group")
    q.add_argument("run_id")
    q = jsub.add_parser("fetch", help="copy a declared artifact back here")
    q.add_argument("run_id")
    q.add_argument("--artifact", default=None)
    q.add_argument("--dest")
    q = jsub.add_parser("queue", help="batch submissions that run one at a time")
    qsub = q.add_subparsers(dest="queue_command", required=True)
    r = qsub.add_parser("add", help="enqueue a job")
    r.add_argument("machine")
    r.add_argument("repo")
    r.add_argument("job")
    r.add_argument("--extra", default="")
    r.add_argument("--resume-from", default="")
    r.add_argument("--allow-duplicate", action="store_true")
    r = qsub.add_parser("list", help="show the queue")
    r = qsub.add_parser("run", help="drain the queue (one job at a time)")
    r.add_argument("--poll", type=float, default=60.0, help="seconds between checks")
    r.add_argument("--once", action="store_true", help="single scheduling pass, then exit")
    r.add_argument("--dry-run", action="store_true", help="plan only: check readiness, submit nothing")
    r = qsub.add_parser("clear", help="drop finished entries")
    r.add_argument("--all", action="store_true", help="drop everything, including pending")

    q = jsub.add_parser("sync", help="git pull the target checkout")
    q.add_argument("machine")
    q.add_argument("repo")

    p = sub.add_parser("env", help="deploy or verify a repo environment")
    esub = p.add_subparsers(dest="env_command", required=True)
    q = esub.add_parser("deploy", help="run the repo's bootstrap script on the target")
    q.add_argument("machine")
    q.add_argument("repo")
    q = esub.add_parser("verify", help="run the repo's verify script on the target")
    q.add_argument("machine")
    q.add_argument("repo")

    sub.add_parser("doctor", help="check local prerequisites")
    sub.add_parser("interactive", help="explicitly start the interactive menu")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context(args.manifest, args.subnet, args.timeout, args.jobs, args.no_cache,
                  data_dir=args.data_dir, local_project_root=args.local_project_root)

    if args.command in (None, "interactive"):
        try:
            return interactive(ctx)
        except (KeyboardInterrupt, EOFError):
            print()
            return 0

    if args.command == "job":
        from jobs import cli as job_cli
        cmd = args.job_command
        if cmd == "repos":
            return job_cli.cmd_repos(ctx, args.machine)
        if cmd == "list":
            return job_cli.cmd_list(ctx, args.machine, args.repo, args.group)
        if cmd == "submit":
            return job_cli.cmd_submit(ctx, args.machine, args.repo, args.job,
                                      extra=args.extra, dry_run=args.dry_run,
                                      allow_head_mismatch=args.allow_head_mismatch,
                                      force=args.force, resume_from=args.resume_from)
        if cmd == "status":
            return job_cli.cmd_status(ctx, args.run_id, args.machine)
        if cmd == "logs":
            return job_cli.cmd_logs(ctx, args.run_id, args.n)
        if cmd == "cancel":
            return job_cli.cmd_cancel(ctx, args.run_id)
        if cmd == "fetch":
            return job_cli.cmd_fetch(ctx, args.run_id, args.artifact, args.dest)
        if cmd == "sync":
            return job_cli.cmd_sync(ctx, args.machine, args.repo)
        if cmd == "queue":
            sub = args.queue_command
            if sub == "add":
                return job_cli.cmd_queue_add(ctx, args.machine, args.repo, args.job,
                                             args.extra, args.resume_from, args.allow_duplicate)
            if sub == "list":
                return job_cli.cmd_queue_list(ctx)
            if sub == "run":
                return job_cli.cmd_queue_run(ctx, args.poll, args.once, args.dry_run)
            if sub == "clear":
                return job_cli.cmd_queue_clear(ctx, args.all)

    if args.command == "env":
        from jobs import cli as job_cli
        return job_cli.cmd_env(ctx, args.machine, args.repo, args.env_command)

    if args.command == "scan":
        return cmd_scan(ctx, register=args.register)
    if args.command == "list":
        return cmd_list(ctx)
    if args.command == "check":
        return cmd_check(ctx, args.machine, args.all or (args.machine or "").lower() == "all")
    if args.command == "show":
        return cmd_show(ctx, args.machine)
    if args.command == "register":
        password = args.password
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        return cmd_register(ctx, args.mac, args.name, args.user, args.port, args.notes,
                            password, args.ask_password or args.password_stdin)
    if args.command == "rename":
        return cmd_rename(ctx, args.machine, args.name)
    if args.command == "autoname":
        return cmd_autoname(ctx, args.machine, args.all)
    if args.command == "forget":
        return cmd_forget(ctx, args.machine)
    if args.command == "probe":
        return cmd_probe(ctx, args.machine, save=not args.no_save)
    if args.command == "ssh":
        cmd = " ".join(args.remote_command).strip() or None
        return cmd_ssh(ctx, args.machine, cmd, False)
    if args.command == "mkdir":
        return cmd_mkdir(ctx, args.machine, args.path)
    if args.command == "key-install":
        return cmd_key_install(ctx, args.machine)
    if args.command == "password":
        return cmd_password(ctx, args.machine, args.password)
    if args.command == "doctor":
        return cmd_doctor(ctx)
    parser.print_help()
    return 2
