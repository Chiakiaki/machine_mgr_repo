"""Two-layer machine inventory.

Layer 1 — ``inventory.yaml`` (tracked in git)
    Curated fleet: identity and reachability facts that are true for everyone
    (mac, name, hostname, ssh_user, ssh_port, project_root, os, tags, notes).
    Static by design: no timestamps, no ``backend``, so it never churns.

Layer 2 — ``data/machines.yaml`` (per host, git-ignored)
    Local facts: which record is *this* host (``backend: local``), when it was
    last seen, and any host-specific override of a shared field.

The merged view is what every command sees. Writes go to the overlay unless
``register --shared`` (or ``promote``) explicitly edits the curated layer.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover - dependency check
    raise SystemExit(
        "machine_mgr needs PyYAML (Debian/Ubuntu: sudo apt install python3-yaml)"
    ) from None

from .discovery import normalize_mac

SHARED_FIELDS = ("mac", "name", "hostname", "ssh_user", "ssh_port",
                 "project_root", "os", "os_version", "tags", "notes")
# Fields the overlay may override: host-specific reachability, not identity.
# Curated identity (name, hostname, os, notes…) always wins over a stale
# overlay value, otherwise an old auto-name would mask the curated one.
OVERLAY_OVERRIDE_FIELDS = ("ssh_user", "ssh_port", "project_root")
LOCAL_ONLY_FIELDS = ("backend", "first_seen", "last_seen")
SCHEMA_FIELDS = SHARED_FIELDS + LOCAL_ONLY_FIELDS

HEADER_SHARED = (
    "# Curated machine inventory — tracked in git, shared by every host.\n"
    "# Static facts only: no timestamps, no per-host state.\n"
    "# Add with: ./machine_mgr register <mac> ... --shared\n"
    "# Machines are keyed by MAC; IPs are resolved at call time.\n"
)
HEADER_OVERLAY = (
    "# Per-host overlay — git-ignored. Holds this host's view: which record is\n"
    "# local, when machines were last seen, and any host-specific override.\n"
)


NAME_MAX = 24


def slugify(text: str, *, max_len: int = NAME_MAX) -> str:
    """Lower-case, hyphen-separated label, cut at a token boundary."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if not slug:
        return ""
    if len(slug) <= max_len:
        return slug
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
    backend: str = "ssh"          # overlay-only: ssh | local
    os: str = ""
    os_version: str = ""
    first_seen: str = ""          # overlay-only
    last_seen: str = ""           # overlay-only
    tags: list[str] = field(default_factory=list)
    notes: str = ""

    def __post_init__(self) -> None:
        self.mac = normalize_mac(self.mac)
        self.ssh_port = int(self.ssh_port or 22)
        self.tags = list(self.tags or [])

    @property
    def display(self) -> str:
        return self.name or self.hostname or self.mac

    @property
    def is_local(self) -> bool:
        return (self.backend or "ssh").lower() == "local"

    @property
    def is_ubuntu(self) -> bool:
        return "ubuntu" in (self.os or "").lower()

    def touch(self) -> None:
        self.last_seen = _now()

    def shared_dict(self) -> dict:
        return {k: getattr(self, k) for k in SHARED_FIELDS}

    def overlay_dict(self) -> dict:
        return {k: getattr(self, k) for k in LOCAL_ONLY_FIELDS}


class Registry:
    def __init__(self, overlay_path: Path, shared_path: Path | None = None) -> None:
        self.overlay_path = Path(overlay_path)
        self.shared_path = Path(shared_path) if shared_path else None
        self.shared: dict[str, Machine] = {}
        self.overlay: dict[str, Machine] = {}
        self.warnings: list[str] = []
        self._shared_dirty = False
        self.load()

    # ── loading ────────────────────────────────────────────────────────────
    def _parse(self, path: Path) -> dict[str, Machine]:
        if not path.exists():
            return {}
        try:
            data = _yaml.safe_load(path.read_text()) or {}
        except _yaml.YAMLError as exc:
            self.warnings.append(f"{path.name}: unreadable ({exc})")
            return {}
        out: dict[str, Machine] = {}
        for raw in data.get("machines") or []:
            if not isinstance(raw, dict) or not raw.get("mac"):
                continue
            try:
                fields = {k: v for k, v in raw.items() if k in SCHEMA_FIELDS}
                out[normalize_mac(str(raw["mac"]))] = Machine(**fields)
            except (TypeError, ValueError):
                continue
        return out

    def load(self) -> None:
        if self.shared_path:
            self.shared = self._parse(self.shared_path)
        self.overlay = self._parse(self.overlay_path)

    # ── merged view ────────────────────────────────────────────────────────
    @property
    def machines(self) -> list[Machine]:
        merged: dict[str, Machine] = {}
        for mac, machine in self.shared.items():
            merged[mac] = Machine(**{k: getattr(machine, k) for k in SCHEMA_FIELDS})
        for mac, local in self.overlay.items():
            base = merged.get(mac)
            if base is None:
                merged[mac] = Machine(**{k: getattr(local, k) for k in SCHEMA_FIELDS})
                continue
            for field_name in SHARED_FIELDS:
                value = getattr(local, field_name)
                if field_name not in OVERLAY_OVERRIDE_FIELDS and getattr(base, field_name):
                    continue          # curated identity wins
                if isinstance(value, list):
                    if value:
                        setattr(base, field_name, value)
                elif value not in ("", None):
                    setattr(base, field_name, value)
            for field_name in LOCAL_ONLY_FIELDS:
                value = getattr(local, field_name)
                if value not in ("", None):
                    setattr(base, field_name, value)
        return sorted(merged.values(), key=lambda m: m.mac)

    def get_by_mac(self, mac: str) -> Machine | None:
        mac = normalize_mac(mac)
        return next((m for m in self.machines if m.mac == mac), None)

    def find(self, identifier: str) -> Machine:
        ident = identifier.strip().lower()
        machines = self.machines
        for machine in machines:
            if machine.mac == ident:
                return machine
        for machine in machines:
            if machine.name.lower() == ident or machine.hostname.lower() == ident:
                return machine
        matches = [m for m in machines
                   if m.mac.startswith(ident) or (m.name and m.name.lower().startswith(ident))]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise KeyError("ambiguous machine '" + identifier + "': " +
                           ", ".join(m.mac for m in matches))
        raise KeyError(f"no registered machine matches '{identifier}'")

    # ── mutation ───────────────────────────────────────────────────────────
    def upsert(self, machine: Machine, *, shared: bool = False) -> Machine:
        """Create or update a record in the overlay (default) or shared layer."""
        if shared:
            if self.shared_path is None:
                raise RuntimeError("no shared inventory configured")
            existing = self.shared.get(machine.mac) or machine
            for field_name in SHARED_FIELDS:
                value = getattr(machine, field_name)
                if value not in ("", None) and value != []:
                    setattr(existing, field_name, value)
            self.shared[machine.mac] = existing
            self._shared_dirty = True
            self.save()
            return existing

        existing = self.overlay.get(machine.mac) or Machine(mac=machine.mac)
        for field_name in SCHEMA_FIELDS:
            value = getattr(machine, field_name)
            if field_name in LOCAL_ONLY_FIELDS or value not in ("", None, []):
                setattr(existing, field_name, value)
        if not existing.first_seen:
            existing.first_seen = _now()
        self.overlay[machine.mac] = existing
        self.save()
        return existing

    def touch(self, machine: Machine) -> None:
        """Refresh last_seen in the overlay (shared layer stays static)."""
        local = self.overlay.get(machine.mac) or Machine(mac=machine.mac)
        local.last_seen = _now()
        if not local.first_seen:
            local.first_seen = _now()
        self.overlay[machine.mac] = local
        self.save()

    def promote(self, machine: Machine) -> None:
        """Copy a machine's static facts into the curated shared inventory."""
        self.upsert(machine, shared=True)

    def remove(self, machine: Machine) -> None:
        self.overlay.pop(machine.mac, None)
        if machine.mac in self.shared:
            del self.shared[machine.mac]
            self._shared_dirty = True
        self.save()

    # ── persistence ────────────────────────────────────────────────────────
    def save(self) -> None:
        self.overlay_path.parent.mkdir(parents=True, exist_ok=True)
        self.overlay_path.write_text(
            HEADER_OVERLAY + _yaml.safe_dump(
                {"version": 1, "updated": _now(), "overlay_count": len(self.overlay),
                 "machines": [asdict(m) for m in sorted(self.overlay.values(), key=lambda m: m.mac)]},
                sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        if self._shared_dirty and self.shared_path is not None:
            self.shared_path.write_text(
                HEADER_SHARED + _yaml.safe_dump(
                    {"version": 1, "machines": [m.shared_dict() for m in
                                                sorted(self.shared.values(), key=lambda m: m.mac)]},
                    sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            self._shared_dirty = False

    def dumps_shared(self) -> str:
        return HEADER_SHARED + _yaml.safe_dump(
            {"version": 1, "machines": [m.shared_dict() for m in
                                        sorted(self.shared.values(), key=lambda m: m.mac)]},
            sort_keys=False, allow_unicode=True)
