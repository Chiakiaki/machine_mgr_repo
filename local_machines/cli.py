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

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "data" / "machines.yaml"
DEFAULT_SECRETS = REPO_ROOT / "data" / "secrets.yaml"
DEFAULT_CACHE = REPO_ROOT / "data" / "cache" / "last_scan.json"

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
                 jobs: int, no_cache: bool) -> None:
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
    ("Doctor: check prerequisites", "doctor"),
]


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"  {prompt}{suffix}: ").strip()
    return value or default


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
            elif action == "doctor":
                cmd_doctor(ctx)
        except KeyboardInterrupt:
            print("\n  cancelled")
        except KeyError as exc:
            print(f"  error: {exc}")
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

    sub.add_parser("doctor", help="check local prerequisites")
    sub.add_parser("interactive", help="explicitly start the interactive menu")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context(args.manifest, args.subnet, args.timeout, args.jobs, args.no_cache)

    if args.command in (None, "interactive"):
        try:
            return interactive(ctx)
        except (KeyboardInterrupt, EOFError):
            print()
            return 0

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
