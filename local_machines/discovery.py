"""Local-network machine discovery and MAC-address resolution.

Everything here is stdlib-only. Discovery works by ping-sweeping the local
subnet and then reading the kernel ARP table, because MAC addresses are stable
while DHCP leases are not. No IP address is ever persisted by this module; the
scan cache is short-lived and lives under data/cache/.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

# Small curated OUI table so auto-named machines get a recognisable hint.
# Extend freely; unknown prefixes simply fall back to the MAC suffix.
OUI_VENDORS = {
    "00:1c:b3": "apple", "3c:22:fb": "apple", "f0:18:98": "apple", "a4:83:e7": "apple",
    "18:65:90": "apple", "bc:92:6b": "apple", "dc:a6:32": "raspberrypi",
    "e4:5f:01": "raspberrypi", "2c:cf:67": "raspberrypi", "b8:27:eb": "raspberrypi",
    "00:1a:11": "google", "f4:f5:d8": "google", "24:0a:c4": "espressif",
    "84:0d:8e": "espressif", "3c:61:05": "espressif", "7c:df:a1": "espressif",
    "00:1e:c9": "dell", "14:fe:b5": "dell", "f8:bc:12": "dell", "84:2b:2b": "dell",
    "00:21:5a": "hp", "3c:d9:2b": "hp", "94:57:a5": "hp",
    "00:23:24": "lenovo", "54:ee:75": "lenovo", "e8:6a:64": "lenovo",
    "00:26:bb": "apple", "00:1b:21": "intel", "3c:97:0e": "intel", "8c:16:45": "intel",
    "00:1f:3a": "samsung", "34:23:87": "samsung", "78:1f:db": "samsung",
    "18:59:36": "xiaomi", "64:cc:2e": "xiaomi", "78:11:dc": "xiaomi",
    "00:25:9c": "cisco", "00:1a:a1": "cisco", "0c:68:03": "cisco",
    "44:d9:e7": "ubiquiti", "78:8a:20": "ubiquiti", "f0:9f:c2": "ubiquiti",
    "2c:3a:e8": "tplink", "50:c7:bf": "tplink", "98:da:c4": "tplink",
    "00:1f:33": "netgear", "2c:30:33": "netgear", "a0:40:a0": "netgear",
    "00:24:e8": "sonos", "48:a6:b8": "sonos", "5c:aa:fd": "sonos",
    "00:16:3e": "xen", "52:54:00": "qemu", "08:00:27": "virtualbox",
}


def vendor_for(mac: str) -> str:
    """Best-effort vendor name from the first three octets ('' when unknown)."""
    return OUI_VENDORS.get(mac[:8].lower(), "")


# ── small helpers ───────────────────────────────────────────────────────────

def normalize_mac(value: str) -> str:
    """Return a lower-case colon-separated MAC, or raise ValueError."""
    raw = value.strip().lower().replace("-", ":").replace(".", ":")
    if len(raw) == 12 and ":" not in raw:
        raw = ":".join(raw[i:i + 2] for i in range(0, 12, 2))
    if not MAC_RE.match(raw):
        raise ValueError(f"not a MAC address: {value!r}")
    return raw


def is_local_mac(mac: str) -> bool:
    """True for multicast/broadcast-ish MACs that can never be a host."""
    first = int(mac.split(":")[0], 16)
    return bool(first & 0x01)


def run(cmd: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def require(tool: str) -> None:
    if shutil.which(tool) is None:
        raise RuntimeError(
            f"required tool '{tool}' is not installed. "
            "Install iproute2/util-linux (Debian: sudo apt install iproute2 iputils-ping)"
        )


# ── interface / subnet detection ────────────────────────────────────────────

def default_route() -> tuple[str, str]:
    """Return (interface, source_ip) for outbound traffic.

    Uses a public but unrouted address so the kernel picks the default route.
    On hosts where a VPN/tun device captures the default route this may return
    the tun device; callers should prefer a device that has a private IPv4
    address in the same subnet as the target.
    """
    require("ip")
    out = run(["ip", "-4", "route", "get", "203.0.113.1"]).stdout
    dev = src = ""
    m_dev = re.search(r"\bdev\s+(\S+)", out)
    m_src = re.search(r"\bsrc\s+(\S+)", out)
    if m_dev:
        dev = m_dev.group(1)
    if m_src:
        src = m_src.group(1)
    return dev, src


def ipv4_cidr(dev: str) -> str | None:
    """IPv4 CIDR of ``dev`` (e.g. 192.168.5.87/24)."""
    out = run(["ip", "-4", "-o", "addr", "show", "dev", dev]).stdout
    m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", out)
    return m.group(1) if m else None


def lan_interface() -> tuple[str, str]:
    """(interface, cidr) for the local LAN, skipping tun/vpn devices."""
    require("ip")
    candidates: list[tuple[str, str]] = []
    out = run(["ip", "-4", "-o", "addr", "show"]).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        dev = parts[1]
        m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not m:
            continue
        cidr = m.group(1)
        if dev == "lo" or dev.startswith(("tun", "utun", "Meta", "wg", "tailscale")):
            continue
        if ipaddress.ip_interface(cidr).ip.is_private:
            candidates.append((dev, cidr))
    if not candidates:
        raise RuntimeError("no private IPv4 LAN interface found")
    # Prefer the interface the default route uses when it is a candidate.
    dev, _ = default_route()
    for cand_dev, cand_cidr in candidates:
        if cand_dev == dev:
            return cand_dev, cand_cidr
    return candidates[0]


# ── ARP table ───────────────────────────────────────────────────────────────

def arp_table(dev: str | None = None) -> dict[str, str]:
    """ip -> mac for entries with a resolved lladdr (skips FAILED/INCOMPLETE)."""
    require("ip")
    cmd = ["ip", "-4", "neigh", "show"]
    if dev:
        cmd += ["dev", dev]
    entries: dict[str, str] = {}
    for line in run(cmd).stdout.splitlines():
        m = re.match(r"^(\d+\.\d+\.\d+\.\d+)\s+.*\blladdr\s+([0-9a-fA-F:]{17})\b", line)
        if not m:
            continue
        if "INCOMPLETE" in line or "FAILED" in line:
            continue
        entries[m.group(1)] = normalize_mac(m.group(2))
    return entries


# ── scanning ────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    interface: str
    subnet: str
    hosts_scanned: int
    alive: list[str] = field(default_factory=list)
    mac_to_ip: dict[str, str] = field(default_factory=dict)
    ip_to_mac: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0

    @property
    def entries(self) -> list[tuple[str, str]]:
        return sorted(self.mac_to_ip.items(), key=lambda kv: tuple(int(p, 16) for p in kv[0].split(":")))


def _ping(ip: str, timeout: int) -> bool:
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), "-n", "-q", ip],
            capture_output=True, timeout=timeout + 3,
        )
        return proc.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def sweep(cidr: str, timeout: int = 1, jobs: int = 64) -> list[str]:
    """Ping every host in ``cidr`` (excluding our own / network / broadcast)."""
    require("ping")
    net = ipaddress.ip_interface(cidr)
    own = net.ip
    hosts = [str(h) for h in net.network.hosts() if h != own]
    alive: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(hosts)))) as pool:
        for ip, ok in zip(hosts, pool.map(lambda h: _ping(h, timeout), hosts)):
            if ok:
                alive.append(ip)
    return alive


# ── cache (transient, never authoritative) ──────────────────────────────────

class ScanCache:
    """Short-lived cache of the last scan, keyed by MAC.

    This exists only so that repeated `check` calls do not re-sweep the /24.
    It is git-ignored and never merged into the manifest as an identity.
    """

    def __init__(self, path: Path, ttl: float = 45.0) -> None:
        self.path = path
        self.ttl = ttl

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - data.get("ts", 0) > self.ttl:
            return None
        return data

    def store(self, result: ScanResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "ts": time.time(),
            "interface": result.interface,
            "subnet": result.subnet,
            "mac_to_ip": result.mac_to_ip,
        }, indent=2) + "\n")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def scan(subnet: str | None = None, timeout: int = 1, jobs: int = 64,
         use_cache: bool = True, cache: ScanCache | None = None) -> ScanResult:
    """Ping-sweep the LAN and map live hosts to MAC addresses."""
    started = time.time()
    if subnet:
        dev, _ = default_route()
        try:
            dev, _ = lan_interface()
        except RuntimeError:
            pass
        cidr = subnet
    else:
        dev, cidr = lan_interface()

    if use_cache and cache is not None:
        cached = cache.load()
        if cached and cached.get("subnet") == cidr:
            mac_to_ip = dict(cached.get("mac_to_ip", {}))
            ip_to_mac = {ip: mac for mac, ip in mac_to_ip.items()}
            return ScanResult(dev, cidr, 0, list(ip_to_mac), mac_to_ip, ip_to_mac,
                              time.time() - started)

    before = arp_table(dev)
    alive = sweep(cidr, timeout=timeout, jobs=jobs)
    after = arp_table(dev)

    ip_to_mac: dict[str, str] = {}
    # Hosts that answered the sweep are authoritative.
    for ip in alive:
        mac = after.get(ip) or before.get(ip)
        if mac:
            ip_to_mac[ip] = mac
    # Recently communicated peers may not answer ICMP but are in the ARP table.
    for ip, mac in after.items():
        ip_to_mac.setdefault(ip, mac)

    mac_to_ip: dict[str, str] = {}
    for ip, mac in sorted(ip_to_mac.items(), key=lambda kv: ipaddress.ip_address(kv[0])):
        if is_local_mac(mac):
            continue
        mac_to_ip.setdefault(mac, ip)

    result = ScanResult(dev, cidr, len([h for h in ipaddress.ip_interface(cidr).network.hosts()]),
                        alive, mac_to_ip, ip_to_mac, time.time() - started)
    if cache is not None:
        cache.store(result)
    return result


def find_by_mac(mac: str, subnet: str | None = None, timeout: int = 1,
                use_cache: bool = True, cache: ScanCache | None = None) -> str | None:
    """Current IP of ``mac``, or None if it is not on the network.

    Fast path: if the ARP table already knows the MAC, ping just that address
    instead of sweeping the whole subnet.
    """
    mac = normalize_mac(mac)
    try:
        dev, cidr = (lan_interface() if not subnet else (default_route()[0], subnet))
    except RuntimeError:
        return None

    for ip, known in arp_table(dev).items():
        if known == mac and _ping(ip, timeout):
            return ip

    result = scan(subnet=cidr, timeout=timeout, use_cache=use_cache, cache=cache)
    return result.mac_to_ip.get(mac)
