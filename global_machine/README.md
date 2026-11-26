# global_machine — reserved for cross-network discovery

Local discovery (`../local_machines/`) only works when both hosts share an L2
network, because it relies on ARP. Everything in this folder is for the case
where the target is *not* on the same Wi-Fi/LAN — a different home network, a
lab machine behind NAT, or a laptop on a mobile hotspot.

Nothing is implemented yet; this file records the intended design so the
interfaces stay compatible.

## Planned back-ends

| Back-end | How it finds the machine | When to use |
| --- | --- | --- |
| **Cloudflare Tunnel** (`cloudflared`) | Target dials out and registers a stable hostname; you SSH to the hostname through the tunnel | Target is behind NAT and can reach the internet; no port forwarding |
| **Tailscale / Headscale** | Both hosts join a tailnet; the target gets a stable `100.x.y.z` and MagicDNS name | Easiest general answer; needs an account and an agent on the target |
| **GlobalProtect** | Corporate VPN client provides the route to the internal host | University/company networks that mandate GP |
| **Rendezvous / reverse SSH** | Target keeps an outbound SSH session to a public box (`ssh -R`), you hop through it | No third-party service allowed |
| **Manual (dynamic DNS)** | Target updates an AAAA/A record; resolve then SSH over IPv6 | Works if you control DNS |

## Design constraints inherited from `local_machines`

1. **Identity stays the MAC.** A global back-end maps MAC → endpoint (hostname,
   tailnet IP, tunnel name) rather than inventing a second identity scheme.
2. **No volatile address in the manifest.** Resolved endpoints are runtime
   values, like the LAN IP today; `check` should answer "reachable?" without
   persisting anything.
3. **Same CLI verbs.** `check`, `probe`, `ssh`, `key-install` should work
   unchanged once a resolver is registered, i.e. discovery becomes pluggable:

   ```python
   # planned shape
   class Resolver(Protocol):
       name: str
       def resolve(self, machine: Machine) -> str | None: ...
   ```

   `machine_mgr --resolver auto|local|tailscale|cloudflare` would then pick the
   first resolver that returns an endpoint.

## Status

Not implemented. Contributions should add a `resolver` module plus its README
section, and must keep `local_machines` free of global-back-end imports so the
LAN-only path stays dependency-free.
