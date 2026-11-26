# machine_mgr

Find, register and SSH into machines on your local network — reliably across
DHCP changes, because machines are identified by **MAC address**, never by IP.

The IP a router hands out changes on reboot and lease expiry, so nothing here
persists an address. The manifest stores the MAC (stable, burned into the NIC)
plus facts you can read: hostname, OS, SSH user, notes. The current address is
resolved live every time you need it, from a ping sweep + the kernel ARP table.

```
machine_mgr_repo/
  machine_mgr              ← the CLI (run ./machine_mgr)
  local_machines/          ← same-LAN discovery, registry, SSH helpers
  global_machine/          ← reserved: cross-network discovery (Cloudflare/Tailscale/GP)
  data/
    machines.yaml          ← the manifest (MAC-keyed, human readable)
    secrets.yaml           ← per-MAC SSH passwords, mode 600, git-ignored
    cache/                 ← 45 s scan cache, git-ignored
  tests/
```

## Install

Python 3.10+ is the only runtime requirement; PyYAML is used when present and a
built-in parser takes over otherwise. No pip installs are needed.

On **this** (controller) machine:

```bash
# Debian/Ubuntu
sudo apt install iproute2 iputils-ping openssh-client
```

On **each target** machine — this is the blocker people hit first — an SSH
**server** must be running:

```bash
sudo apt install openssh-server     # Ubuntu/Debian
sudo systemctl enable --now ssh
sudo ufw allow 22/tcp               # if ufw is active
```

`./machine_mgr doctor` checks all of the above and reports what is missing.

## Quick start

```bash
cd machine_mgr_repo
./machine_mgr                 # interactive menu (numbered, press 1..11)
./machine_mgr doctor          # prerequisites
./machine_mgr scan            # ping-sweep the LAN, list MAC ↔ current IP
./machine_mgr scan --register # also add unknown MACs to the manifest
./machine_mgr list            # what is registered
```

## Commands

| Command | What it does |
| --- | --- |
| `scan [--register]` | Ping-sweep the subnet, read the ARP table, print MAC → current IP |
| `list` | Registered machines from the manifest |
| `check <mac\|name\|all>` | **Is it online right now?** Resolves the live IP or reports offline |
| `show <mac\|name>` | Full manifest entry + current (volatile) address |
| `register <mac> [--name --user --port --notes] [--ask-password\|--password-stdin]` | Add/annotate a machine, optionally storing its SSH credentials |
| `forget <mac\|name>` | Remove it from the manifest |
| `probe <mac\|name>` | SSH in, record hostname/OS/kernel into the manifest |
| `ssh <mac\|name> [cmd…]` | Resolve current IP, then open a shell or run a command |
| `mkdir <mac\|name> [path]` | Create/verify a directory on the remote host (default `~/project`) |
| `key-install <mac\|name>` | `ssh-copy-id` so password auth is no longer needed |
| `password <mac\|name>` | Store the SSH password (keyed by MAC, mode 600) |
| `doctor` | Check `ssh`, `ssh-copy-id`, `ping`, `ip`, LAN interface, manifest |

Identifiers accept a full MAC (`cc:5e:f8:07:36:13`), a unique MAC prefix, a
registered name, or the hostname.

## Typical first-contact workflow

This is exactly the sequence used to bring up the `dell` machine in the sample
manifest, and the one to follow for a new host:

```bash
./machine_mgr doctor                    # ssh/ssh-copy-id/ping/ip present?
./machine_mgr scan --register           # find MACs on the LAN
./machine_mgr register <mac> --name dell --user ruoyu --ask-password
./machine_mgr check dell                # online, and which IP right now?
./machine_mgr probe dell                # hostname/OS/kernel recorded into the manifest
./machine_mgr key-install dell          # password used once, then key auth
./machine_mgr mkdir dell                # create ~/project on the target
./machine_mgr ssh dell                  # passwordless shell from now on
```

`probe` stamps `hostname`, `os` and `os_version`; `check` updates `last_seen`.
Neither writes an IP address.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Permission denied (publickey,password)` | Wrong user, or no key installed yet: run `key-install` with the password, or check `--user` |
| `Connection refused` on port 22 | No SSH **server** on the target: `sudo apt install openssh-server` |
| `Host key verification failed` | The target's IP was reused by another machine; `ssh-keygen -R <ip>` then retry |
| Scan finds nothing but the host is up | VPN/TUN default route: pin the LAN with `--subnet 192.168.5.0/24` |
| `mkdir` made a directory literally called `~` | Fixed in `remote_path()`; make sure you are not on an old checkout |
| Password rejected when using `--ask-password` | User was not registered first; pass `--user` together with the password |

## Manifest

`data/machines.yaml`, one entry per machine, keyed by MAC:

```yaml
version: 1
updated: '2026-11-26T05:40:00+00:00'
machines:
- mac: cc:5e:f8:07:36:13
  name: dell-ubuntu
  hostname: dell
  ssh_user: sry
  ssh_port: 22
  os: ubuntu
  os_version: 'Ubuntu 24.04.3 LTS'
  first_seen: '2026-11-26T05:31:12+00:00'
  last_seen: '2026-11-26T05:45:03+00:00'
  tags: []
  notes: ''
```

There is deliberately **no `ip` field**. `check`/`ssh` resolve it on demand.

## Hosts behind a VPN / proxy (e.g. Clash Verge TUN)

TUN mode commonly hijacks DNS (fake-ip answers) and may take over the default
route. Two consequences, both handled here:

- Discovery sweeps the **LAN interface's own subnet** (`lan_interface()` skips
  `tun*`, `Meta`, `wg*`, `tailscale*`), so a VPN default route does not hide
  the local network.
- Everything is addressed by **IP**, never by hostname, so fake-ip DNS cannot
  misdirect a connection.

If a sweep looks wrong, pin the subnet explicitly:
`./machine_mgr scan --subnet 192.168.5.0/24`.

## Credentials: store once, connect forever

The intended workflow is to register a machine **with** its login, so every
later `check` / `probe` / `ssh` reuses it without prompting:

```bash
# interactive: prompts for the password with no echo
./machine_mgr register cc:5e:f8:07:36:13 --name dell --user sry --ask-password

# scripted: the password goes through stdin, so it never appears in `ps`
# or your shell history
printf '%s\n' "$PW" | ./machine_mgr register cc:5e:f8:07:36:13 \
    --name dell --user sry --password-stdin

# or retro-fit an already-registered machine
./machine_mgr password dell
```

Stored under `data/secrets.yaml` (mode 600, git-ignored), keyed by MAC. From
then on `./machine_mgr ssh dell` and interactive option **7** open a shell
directly. Interactive option **5** (register) asks whether to store the
password in the same step. `./machine_mgr show <mac>` reports whether
credentials exist for an entry.

## Password vs key

Key auth is strongly preferred. `key-install` copies this host's public key to
the target (it needs the password once). Password auth, when used, goes through
OpenSSH's `SSH_ASKPASS_REQUIRE=force` path, so no `sshpass` is required:

1. `./machine_mgr password <mac>` — stores it in `data/secrets.yaml` (mode 600,
   git-ignored), or
2. export `MACHINE_MGR_PASSWORD_CC_5E_F8_07_36_13` in the environment.

Passwords are stored in plain text on purpose: obfuscation would imply a
security property the file does not have. Keep the repo private.

## Tests

```bash
python3 -m pytest tests -q          # or: python3 tests/test_registry.py
```

## Roadmap

`global_machine/` is the placeholder for reaching machines across networks —
Cloudflare Tunnel, Tailscale, GlobalProtect, or a rendezvous server. It is
intentionally empty apart from design notes; see
[global_machine/README.md](global_machine/README.md).
