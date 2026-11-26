"""SSH plumbing: key/password auth, remote probing, key installation.

Password auth is done without `sshpass`: OpenSSH >= 8.4 honours
``SSH_ASKPASS_REQUIRE=force``, so a throwaway askpass helper plus a session with
no controlling terminal is enough. That keeps the tool dependency-free beyond
the stock OpenSSH client.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

from .discovery import normalize_mac

try:  # preferred
    import yaml as _yaml
except ImportError:  # pragma: no cover - fallback path
    _yaml = None

ASKPASS_HELPER = "#!/bin/sh\nprintf '%s\\n' \"$MACHINE_MGR_PASSWORD\"\n"

PROBE_SCRIPT = r"""
echo "hostname=$(hostname 2>/dev/null)"
echo "user=$(id -un)"
if [ -r /etc/os-release ]; then
  . /etc/os-release 2>/dev/null
  echo "os_id=${ID:-unknown}"
  echo "os_version=${VERSION_ID:-}"
  echo "os_pretty=${PRETTY_NAME:-}"
fi
echo "kernel=$(uname -r)"
echo "arch=$(uname -m)"
if [ -r /etc/ssh/sshd_config ]; then echo "sshd_config=yes"; fi
"""


def _base_opts(port: int) -> list[str]:
    return [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-o", "PreferredAuthentications=publickey,password,keyboard-interactive",
        "-p", str(port),
    ]


def _askpass_env(password: str) -> tuple[dict, str]:
    """Create a private askpass helper; return (env, tempdir)."""
    tmpdir = tempfile.mkdtemp(prefix="machine-mgr-")
    helper = Path(tmpdir) / "askpass.sh"
    helper.write_text(ASKPASS_HELPER)
    helper.chmod(0o700)
    env = dict(os.environ)
    env.update({
        "SSH_ASKPASS": str(helper),
        "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": env.get("DISPLAY", ":0"),
        "MACHINE_MGR_PASSWORD": password,
    })
    return env, tmpdir


def ssh(ip: str, user: str, port: int = 22, command: str | None = None,
        password: str | None = None, timeout: float = 60.0,
        allocate_tty: bool = False, forward_agent: bool = False,
        stream: bool = False) -> subprocess.CompletedProcess:
    """Run ``command`` (or open a shell) over SSH.

    Returns the CompletedProcess; the caller decides how to treat the code.
    """
    args = ["ssh", *_base_opts(port)]
    if forward_agent:
        args.append("-A")
    if password:
        args += ["-o", "BatchMode=no", "-o", "NumberOfPasswordPrompts=1"]
    elif command is not None:
        args += ["-o", "BatchMode=yes"]
    if allocate_tty:
        args += ["-tt"]
    args.append(f"{user}@{ip}" if user else ip)
    if command is not None:
        args.append(command)

    env = None
    tmpdir = None
    if password:
        env, tmpdir = _askpass_env(password)
    try:
        return subprocess.run(
            args,
            capture_output=command is not None and not stream,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL if command is not None else None,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ssh timed out after {timeout:.0f}s ({user}@{ip})") from None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def port_open(ip: str, port: int = 22, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def ssh_banner(ip: str, port: int = 22, timeout: float = 4.0) -> str:
    """Version banner, e.g. 'SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.20'."""
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            data = sock.recv(256)
        return data.decode("utf-8", "replace").strip()
    except OSError:
        return ""


def probe(ip: str, user: str, port: int = 22,
          password: str | None = None) -> dict[str, str]:
    """Collect identity/system facts from the remote host."""
    proc = ssh(ip, user, port, PROBE_SCRIPT, password=password, timeout=45)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else f"ssh failed with code {proc.returncode}")
    facts: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            facts[key.strip()] = value.strip()
    return facts


def install_key(ip: str, user: str, port: int = 22, password: str | None = None,
                key: str | None = None) -> subprocess.CompletedProcess:
    """Append our public key to the remote authorized_keys via ssh-copy-id."""
    if shutil.which("ssh-copy-id") is None:
        raise RuntimeError("ssh-copy-id not found (Debian: sudo apt install openssh-client)")
    args = ["ssh-copy-id"]
    if key:
        args += ["-i", key]
    # Options must precede the target; ssh-copy-id rejects trailing flags.
    args += ["-o", "StrictHostKeyChecking=accept-new", "-p", str(port)]
    if password:
        args += ["-o", "NumberOfPasswordPrompts=1"]
    args.append(f"{user}@{ip}")
    env, tmpdir = (None, None)
    if password:
        env, tmpdir = _askpass_env(password)
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=90,
                              env=env, stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def remote_path(path: str) -> str:
    """Rewrite a leading ``~`` to ``$HOME`` for safe quoting in remote commands.

    A quoted "~/x" does not expand and creates a directory literally named
    ``~``; "$HOME/x" inside double quotes does expand remotely.
    """
    if path == "~":
        return "$HOME"
    if path.startswith("~/"):
        return "$HOME/" + path[2:]
    return path


def ensure_remote_dir(ip: str, user: str, port: int = 22, path: str = "~/project",
                      password: str | None = None) -> str:
    """mkdir -p a directory on the remote host; returns its absolute path."""
    remote = remote_path(path)
    proc = ssh(ip, user, port, f'mkdir -p "{remote}" && cd "{remote}" && pwd',
               password=password, timeout=45)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "mkdir failed").strip())
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else path


# ── credential store ────────────────────────────────────────────────────────

class Credentials:
    """Per-MAC credentials in data/secrets.yaml (mode 0600, git-ignored).

    Passwords are stored in plain text on purpose: obfuscating them would imply
    a security property the file does not have. Keep the directory private, or
    prefer key auth via `key-install`.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
            data = _yaml.safe_load(text) if _yaml else {}
        except Exception:
            return
        for entry in (data or {}).get("credentials", []) or []:
            try:
                mac = normalize_mac(str(entry["mac"]))
            except (KeyError, ValueError):
                continue
            self.entries[mac] = {
                "mac": mac,
                "user": entry.get("user", ""),
                "password": entry.get("password", ""),
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"credentials": sorted(self.entries.values(), key=lambda e: e["mac"])}
        text = _yaml.safe_dump(payload, sort_keys=False) if _yaml else ""
        self.path.write_text(text, encoding="utf-8")
        self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def get(self, mac: str) -> dict | None:
        return self.entries.get(normalize_mac(mac))

    def set(self, mac: str, user: str, password: str) -> None:
        mac = normalize_mac(mac)
        self.entries[mac] = {"mac": mac, "user": user, "password": password}
        self.save()

    def remove(self, mac: str) -> bool:
        if self.entries.pop(normalize_mac(mac), None) is not None:
            self.save()
            return True
        return False


MAC_IN_KEY = re.compile(r"[^0-9a-z]")


def env_password(mac: str) -> str | None:
    """Password from MACHINE_MGR_PASSWORD_<MAC> (e.g. ..._CC_5E_F8_07_36_13)."""
    key = "MACHINE_MGR_PASSWORD_" + MAC_IN_KEY.sub("_", normalize_mac(mac)).upper()
    return os.environ.get(key)
