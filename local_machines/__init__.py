"""machine_mgr: find, register and reach machines on the local network.

Subpackages
-----------
local_machines
    Discovery (ping sweep + ARP), a MAC-keyed manifest, and SSH helpers.
global_machine
    Reserved for cross-network discovery (Cloudflare Tunnel, Tailscale,
    GlobalProtect). See its README.
"""

__version__ = "0.1.0"
