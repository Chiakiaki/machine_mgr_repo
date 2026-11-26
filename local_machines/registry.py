"""Manifest of known machines, keyed by MAC address.

The manifest deliberately stores **no IP address**: DHCP reassigns leases, so an
IP is only ever a runtime value obtained from discovery. MAC addresses are
stable, so they are the primary key.

Storage is YAML when PyYAML is available (it usually is) and a minimal built-in
parser/writer matching this exact schema otherwise, so the CLI never hard-fails
just because PyYAML is missing.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .discovery import normalize_mac

try:  # preferred
    import yaml as _yaml
except ImportError:  # pragma: no cover - fallback path
    _yaml = None

SCHEMA_FIELDS = [
    "mac", "name", "hostname", "ssh_user", "ssh_port", "project_root",
    "os", "os_version", "first_seen", "last_seen", "tags", "notes",
]


NAME_MAX = 24


def slugify(text: str, *, max_len: int = NAME_MAX) -> str:
    """Lower-case, hyphen-separated label: 'Ruoyu_Alienware m17' -> 'ruoyu-alienware-m17'."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if not slug:
        return ""
    if len(slug) <= max_len:
        return slug
    # cut at a token boundary rather than mid-word
    out = ""
    for token in slug.split("-"):
        candidate = f"{out}-{token}" if out else token
        if len(candidate) > max_len:
            break
        out = candidate
    return out or slug[:max_len].strip("-")


def unique_name(base: str, machines: list["Machine"], ignore_mac: str | None = None) -> str:
    """Return ``base`` or ``base-2``, ``base-3``… so labels never collide."""
    taken = {m.name.lower() for m in machines if m.name and m.mac != ignore_mac}
    candidate, counter = base, 2
    while candidate.lower() in taken:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Machine:
    mac: str
    name: str = ""
    hostname: str = ""
    ssh_user: str = ""
    ssh_port: int = 22
    project_root: str = "~/project"
    os: str = ""
    os_version: str = ""
    first_seen: str = ""
    last_seen: str = ""
    tags: list[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        # Normalise here so every construction path (CLI, loader, tests) agrees.
        self.mac = normalize_mac(self.mac)
        self.ssh_port = int(self.ssh_port or 22)
        self.tags = list(self.tags or [])

    @property
    def display(self) -> str:
        return self.name or self.hostname or self.mac

    @property
    def is_ubuntu(self) -> bool:
        return "ubuntu" in (self.os or "").lower()

    def touch(self) -> None:
        self.last_seen = _now()

    def to_dict(self) -> dict:
        data = asdict(self)
        return {k: data[k] for k in SCHEMA_FIELDS}


class Registry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.machines: list[Machine] = []
        self.load()

    # ── persistence ────────────────────────────────────────────────────────
    def load(self) -> None:
        if not self.path.exists():
            self.machines = []
            return
        text = self.path.read_text(encoding="utf-8")
        data = _yaml.safe_load(text) if _yaml else parse_minimal_yaml(text)
        entries = (data or {}).get("machines") or []
        machines = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("mac"):
                continue
            try:
                entry = {k: v for k, v in entry.items() if k in SCHEMA_FIELDS}
                entry["mac"] = normalize_mac(str(entry["mac"]))
                entry["tags"] = list(entry.get("tags") or [])
                entry["ssh_port"] = int(entry.get("ssh_port") or 22)
                machines.append(Machine(**entry))
            except (TypeError, ValueError):
                continue
        self.machines = machines

    def save(self) -> None:
        payload = {
            "version": 1,
            "updated": _now(),
            "machines": [m.to_dict() for m in sorted(self.machines, key=lambda m: m.mac)],
        }
        header = (
            "# machine_mgr manifest — one entry per machine, keyed by MAC address.\n"
            "# IP addresses are intentionally NOT stored: DHCP reassigns them.\n"
            "# Use `./machine_mgr check <mac|name>` to resolve the current address.\n"
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if _yaml is not None:
            body = _yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        else:  # pragma: no cover - fallback path
            body = dump_minimal_yaml(payload)
        self.path.write_text(header + body, encoding="utf-8")

    # ── queries ────────────────────────────────────────────────────────────
    def find(self, identifier: str) -> Machine:
        ident = identifier.strip().lower()
        for m in self.machines:
            if m.mac == ident:
                return m
        for m in self.machines:
            if m.name.lower() == ident or m.hostname.lower() == ident:
                return m
        matches = [m for m in self.machines if m.mac.startswith(ident) or
                   (m.name and m.name.lower().startswith(ident))]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise KeyError(f"ambiguous machine '{identifier}': " +
                           ", ".join(m.mac for m in matches))
        raise KeyError(f"no registered machine matches '{identifier}'")

    def get_by_mac(self, mac: str) -> Machine | None:
        mac = normalize_mac(mac)
        return next((m for m in self.machines if m.mac == mac), None)

    def add(self, machine: Machine) -> Machine:
        existing = self.get_by_mac(machine.mac)
        if existing:
            return existing
        machine.first_seen = machine.first_seen or _now()
        machine.touch()
        self.machines.append(machine)
        return machine

    def remove(self, machine: Machine) -> None:
        self.machines = [m for m in self.machines if m.mac != machine.mac]


# ── minimal YAML fallback (only for this manifest's schema) ─────────────────

def _scalar(value: str):
    v = value.strip()
    if v in ("", "~", "null"):
        return ""
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [s.strip().strip("'\"") for s in inner.split(",") if s.strip()] if inner else []
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def parse_minimal_yaml(text: str) -> dict:  # pragma: no cover - fallback path
    result: dict = {}
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^machines:\s*$", line):
            result["machines"] = []
            continue
        m_item = re.match(r"^\s*-\s+([A-Za-z_]+):\s*(.*)$", line)
        if m_item and "machines" in result:
            current = {m_item.group(1): _scalar(m_item.group(2))}
            result["machines"].append(current)
            continue
        m_key = re.match(r"^\s+([A-Za-z_]+):\s*(.*)$", line)
        if m_key and current is not None:
            current[m_key.group(1)] = _scalar(m_key.group(2))
            continue
        m_top = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if m_top:
            result[m_top.group(1)] = _scalar(m_top.group(2))
    return result


def dump_minimal_yaml(payload: dict) -> str:  # pragma: no cover - fallback path
    lines = [f"version: {payload.get('version', 1)}", f"updated: {payload.get('updated', '')}", "machines:"]
    for m in payload.get("machines", []):
        first = True
        for key, value in m.items():
            if isinstance(value, list):
                rendered = "[" + ", ".join(str(v) for v in value) + "]"
            else:
                rendered = str(value)
                if not rendered or any(c in rendered for c in ":#-[]{}\"'") or rendered != rendered.strip():
                    rendered = '"' + rendered.replace('"', '\\"') + '"'
            prefix = "  - " if first else "    "
            lines.append(f"{prefix}{key}: {rendered}")
            first = False
    return "\n".join(lines) + "\n"
