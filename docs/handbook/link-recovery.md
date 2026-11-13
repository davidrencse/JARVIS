# Cluster link recovery

The node agent supervises its SSH tunnel and installs its login startup using the
existing Windows Startup VBS / Linux systemd / macOS LaunchAgent helper. The worker
checks an authenticated handshake through the forwarded hub, rotates candidates
after ten unhealthy seconds, and caps retry backoff at 30 seconds. Address lists
are validated, bounded, learned from authenticated polls, and saved per device in
`.jarvis/cluster-addresses.json`, never in the user config. MagicDNS candidates
survive DHCP changes. Clipboard and Lan Mouse checks run at startup, after recovery,
and periodically; existing manual stops and opt-outs still apply.

`jarvis cluster tunnel stop` records `.jarvis/cluster-tunnel.manual-stop` for this
boot and this role (local device, main device, tunnel mode, and whether a verified
continuity link is present). It does not edit `jarvis.config.json`. The cooperative
`cluster-tunnel.stop` marker only asks the current worker to exit; the next worker
deletes that marker, so the marker is not the manual stop. While the record matches, the node supervisor and `cluster doctor --fix` do not
start the worker: both go through `ensure_tunnel`, and `--fix` does not clear the
record. Passive `cluster doctor` does not start the worker. A promoted supervisor
still calls `ensure_tunnel`, which drops a record for the previous role and does
not start a tunnel while this machine is the main. `jarvis cluster tunnel start` deletes it and starts the
worker. Explicit lifecycle activation with a link does the same, because it calls
start. A role change drops a record that does not match the new role, so a
promotion, a demotion, or a continuity link that turns the forward tunnel off
cannot leave the tunnel stuck disabled. Those transitions do not write the record.
A later boot (boot time more than 120 seconds away) drops it too. If the boot time
cannot be read, the record stays until an explicit start: an unknown boot is not
permission to start. A stop from role lifecycle (`prepare_successor`, `switch_node`)
or from leave is only the cooperative marker.

With every known address down, the node probes SSH on its own interface subnets
with at most 32 workers. At most eight IPv4 interfaces are considered, skipping
loopback and link-local. Each is narrowed to the /22 containing the interface IP
(including non-RFC1918 LANs), so one interface contributes at most 1,022 hosts.
The whole pass, TCP probes and pinned SSH checks together, has one 45-second
budget. It is not 45 seconds per host. A TCP probe is capped at 150 ms and a pin
check at 3 seconds, and both shrink to the time still left. No new attempt starts
after the deadline, and the scan does not wait for the pool once the deadline has
passed. A candidate is persisted only when the pinned check finished inside the
budget and returned the `jarvis-node-link` proof for the existing host key. An
open TCP/22 without that check is not acceptance. The main's UDP identity scan
uses the same interface cap, worker cap, and 45-second budget; a proof that
arrives after the deadline is dropped. Scans are limited to one every five minutes
across worker restarts.

`cluster.link_discovery` defaults to true; `cluster.announce_port` defaults to UDP
47781. The existing node agent binds this port separately on its own LAN addresses and its
own Tailscale address, never on a wildcard or another virtual interface. Allow inbound UDP 47781 on the local LAN firewall if needed.
The current main scans after a node has not polled for 60 seconds, once per five
minutes per node. A nonce challenge verifies the node using its already enrolled
credential hash, with a separate HMAC domain; it never reveals or replaces the
bearer credential. Only then does the main send its addresses signed by its existing
code-signing key. Nodes accept only the current main's pinned key, matching fleet,
a timestamp within 120 seconds and an unused nonce. Packets are capped at 4096
bytes and four per second globally. A valid announce learns addresses and requests
a tunnel reconnect; it cannot execute work, change a role or install a key.

`linkfinder.py` resolves current roles through verified continuity repoint state
when present. The repoint path uses the same module. Lifecycle activation remains
explicit; discovery cannot initiate a MAIN transition. Old main signatures cease
to be accepted when the signed repoint updates the current main and its pin.
Continuity remains inert unless explicitly configured. Legacy fleets use
`cluster.fleet_id` (default `jarvis`); managed fleets use their signed fleet id.

## Doctor and alerts

`jarvis cluster doctor` on a node says “reachable but tunnel down: the supervisor fixes it
within 30 s” when the main answers over SSH but the forwarded hub does not. That line
is the passive check. It does not start the worker. `jarvis cluster doctor --fix`
calls `ensure_tunnel` and re-ensures the clipboard watcher and the Lan Mouse guard.
Neither undoes an operator `cluster tunnel stop`; `jarvis cluster tunnel start` clears
that stop. The main sends one `notify-send` when a node
that was online stays offline for more than three minutes (one per outage).

## Run unattended

On Windows, open Tailscale settings and enable **Run unattended**, then connect it, so
the VPN (and the main's MagicDNS name, which survives DHCP changes) comes back after a
reboot without anyone logging in. JARVIS never invokes `tailscale up`. Doctor explicitly reports
“tailscale installed but not connected” when the installed client is disconnected.

After deploying these Python changes, the existing main and node processes must
load the new code. David can use the normal tested-release / cluster-update flow.
If the Dell is already offline, from its checkout run once:
`jarvis cluster tunnel start --autostart`, then `jarvis cluster run` if its agent
is stopped. Do not enroll again just because a DHCP address changed.
