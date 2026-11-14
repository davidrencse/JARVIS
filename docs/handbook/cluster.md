# Cluster

*The main, the nodes, and how a dropped link comes back. Code rollout is [release.md](release.md).*

`cluster.main_device` is the main: brain, memory, vault, and the hub on `127.0.0.1:<listen_port>`. This config sets `zenbook`, tunnel `forward`, `listen_port` 7780, `main_url` `http://127.0.0.1:7780`. Everything else in `devices` is a node that long-polls the hub. Port 7778 is the Grokbot tunnel, not the hub. Because `main_device` is one id, two mains cannot be configured.

```sh
jq .cluster jarvis.config.json
python -c "from jarvis.platforms import detect; print(detect())"
jarvis cluster roles
jarvis cluster main <id>
jarvis cluster init
jarvis cluster invite <node>
jarvis cluster join <code>
jarvis cluster doctor
jarvis cluster status | leave | revoke | caps
jarvis cluster update <node> [--rollback]
jarvis cluster tunnel start | stop | status
```

| Key | Default in code | This repo |
|---|---|---|
| `enabled` | `false` | `true` |
| `main_device` | none | `zenbook` |
| `tunnel` | `reverse` | `forward` |
| `listen_port` | `7780` | `7780` |
| `auto_release` | `true` | not set |
| `link_discovery` | `true` | not set |
| `announce_port` | UDP `47781` | not set |
| `fleet_id` | `jarvis` | not set |
| `pin_node_address` | off | do not enable for the Dell |

`forward` means each node runs `ssh -L` to the main, so only the main needs sshd. `reverse` means the main runs `ssh -R` into each node. An invite is single-use and lasts 24 hours. It carries the node's hub credential, the main's pinned host key, and a fresh ed25519 key restricted to `permitopen="127.0.0.1:<port>"` with `command="echo jarvis-node-link"`. The node stores the link in `.jarvis/cluster-node.json` and `.jarvis/cluster-node.key`, never in `jarvis.config.json`. `invite` refuses to overwrite an existing output file before it creates keys. `revoke` removes the credential and the authorized-keys line.

Jobs are signed. A node checks the signature and refuses a fingerprint mismatch. Dispatched actions are not replayed. A node is fresh for 10 s after a check-in. It is eligible for a job while a poll is held open, it was seen in the last 10 s, or it is running a job. The broker gives up after 75 s. Nothing is sent to the Dell unless David says it is available.

## Auth and the bundle

A 429 is a failed hub auth and nothing else. `AuthBackoff` counts them in a 300 s window: 5 from one source, or 50 from every source. Past that, the answer waits `min(60, 2**(n-5))` seconds and says so in `Retry-After`. A call with a valid owner or node token is served anyway, so a live node cannot be locked out. The count clears after 300 s and applies again after a hub restart.

`cluster.pin_node_address: true` opts in to a `from=` pin on the node's authorized-keys line. The address has to be one the code already knows, and a value that could inject into the line is refused. Leave it off for the Dell: its address changes between LAN and Tailscale, and a wrong pin kills the tunnel.

The versioned bundle and `LEGACY_UNVERSIONED_UNTIL` (2027-01-15 UTC) are in [release.md](release.md).

## Link recovery

The node agent supervises its SSH tunnel and installs login startup with the existing helper (Windows Startup VBS, Linux systemd, macOS LaunchAgent). It checks an authenticated handshake through the forwarded hub, rotates candidates after 10 unhealthy seconds, and caps retry backoff at 30 s. Learned addresses are validated, bounded, and saved per device in `.jarvis/cluster-addresses.json`, never in `jarvis.config.json`. MagicDNS names survive DHCP.

`jarvis cluster tunnel stop` writes `.jarvis/cluster-tunnel.manual-stop` for this boot and this role. It does not edit `jarvis.config.json`. The cooperative `cluster-tunnel.stop` marker only asks the current worker to exit; the next worker deletes it, so that marker is not the manual stop. While the record matches, the supervisor and `cluster doctor --fix` do not start the worker, because both go through `ensure_tunnel`, and `--fix` does not clear the record. Passive `cluster doctor` does not start the worker. `jarvis cluster tunnel start` deletes the record and starts the worker. A role change, a promotion, or a continuity link that turns the forward tunnel off drops a record that does not match, and those transitions do not write a new one. A later boot (boot time more than 120 s away) drops it too. If the boot time cannot be read, the record stays until an explicit start.

With every known address down, the node probes SSH on its own subnets: at most 8 IPv4 interfaces, skipping loopback and link-local, each narrowed to the /22 that contains the interface address (at most 1,022 hosts), at most 32 workers, one 45-second budget for the whole pass. A TCP probe is capped at 150 ms and a pin check at 3 s, and both shrink to the time left. A candidate is saved only when the pinned check finishes inside the budget and returns `jarvis-node-link` for the existing host key. An open port 22 is not enough. The main's UDP identity scan uses the same caps. A proof that arrives after the deadline is dropped. Scans run at most once every five minutes across worker restarts.

The node agent binds UDP `cluster.announce_port` (47781) on its own LAN addresses and its own Tailscale address, never on a wildcard. Allow inbound UDP 47781 on the LAN firewall if a scan cannot see the node. The main scans after a node has not polled for 60 s, once per five minutes per node. A nonce challenge uses the enrolled credential hash and a separate HMAC domain. It does not reveal or replace the bearer. Only then does the main send addresses signed by its code-signing key. A node accepts only the current main's pinned key, a matching fleet, a timestamp within 120 s, and an unused nonce. Packets are capped at 4096 bytes and four per second. A valid announce learns addresses and requests a tunnel reconnect. It cannot run work, change a role, or install a key.

`linkfinder.py` resolves the current main through verified continuity repoint state when that state is present. Discovery cannot start a MAIN transition. Old main signatures stop being accepted when the signed repoint updates the pin. Legacy fleets use `cluster.fleet_id`. Managed fleets use their signed fleet id.

`jarvis cluster doctor` on a node says the supervisor fixes a reachable-but-down tunnel within 30 s. That line does not start the worker. `jarvis cluster doctor --fix` calls `ensure_tunnel` and re-ensures the clipboard watcher and the Lan Mouse guard, and it does not undo `cluster tunnel stop`. The main sends one `notify-send` when a node that was online stays offline for more than three minutes, once per outage.

On Windows, Tailscale **Run unattended** is what brings MagicDNS back after a reboot with nobody logged in. JARVIS never runs `tailscale up`. Doctor reports "tailscale installed but not connected" when the client is installed and down.

If the Dell is already offline when new tunnel code has to load, from its checkout run `jarvis cluster tunnel start --autostart`, then `jarvis cluster run` if its agent is stopped.

## Clipboard and sshd

`clipsync.py` runs on each side. The cluster keep-alive restarts it if it dies. `clipsync.stop()` leaves a marker, so a stop by hand holds until the next boot, and `clipboard.lock` allows one watcher per machine. The node starts it only after it holds the node lock. Copies are at most 32 MB. Password-manager secrets stay put. Files are not clipboard items; use `jarvis send`.

The Grokbot tunnel is a reverse forward on `127.0.0.1:7778` and reconnects every 5 s. A dead session on the Zenbook holds that port unless sshd checks liveness:

```bash
printf 'ClientAliveInterval 15\nClientAliveCountMax 3\n' | sudo tee /etc/ssh/sshd_config.d/50-jarvis-alive.conf
sudo systemctl reload sshd
```

Windows boot time for these markers is the console logon time, so it stays stable across agent restarts. Fallback SSH targets are tried in order. While on a fallback, port 22 of the earlier targets is checked every 60 s, backing off to 30 minutes if a switch back fails.
