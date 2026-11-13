# Continuity gateway

One HTTP process in front of one authority database. The Zenbook (Arch, hostname `archmain`, the current MAIN) and the Dell (Windows 11) sleep, so the process lives on a machine that stays on. David creates the Oracle account and the VM himself. Serving it does not change `cluster.main_device`.

Code: `jarvis/continuity/gateway.py`, entry `python -m jarvis.continuity.gateway`. It does not read `jarvis.config.json` or an owner private key. Runtime signature/client helpers can import this module; import never starts the gateway.

## What it does

An enrolled installation key signs device routes. A pinned owner public key signs enrol, mode, and transition routes. A fresh database stays `LEGACY_ACTIVE`. `FailoverPolicy` returns `action=none` and `reason=manual_only`, and nothing calls it on a timer. The loader refuses a state file under the repo `.jarvis` tree or `~/.local/state/jarvis` (`refuses_live_state`).

## The VM

Create an Always Free `VM.Standard.A1.Flex` (Ampere A1) Ubuntu instance. The shape must be marked Always Free eligible. Do not raise OCPU, memory, or the boot volume past that label, and do not add another instance on the same free quota. If the cost estimate is not zero, stop. Add your SSH public key, leave password login off, and allow public ingress on TCP 22 only.

## Hardening

`sshd -T` must print `passwordauthentication no`. Then `ufw default deny incoming`, `ufw default allow outgoing`, `ufw allow OpenSSH`, and `ufw enable`. Allow SSH before you enable `ufw`. Do not allow the gateway port. Install Tailscale the way the laptops have it, run `tailscale up`, then `ufw allow in on tailscale0` (the tailnet interface). Publish loopback only, on the same port as `bind_port` (`8766` in the example):

```text
sudo tailscale serve --bg --https=443 http://127.0.0.1:8766
```

Never `tailscale funnel`. Check: `ufw status` allows OpenSSH and `tailscale0` and does not allow the gateway port. `tailscale serve status` lists this HTTPS handler. From the Zenbook, SSH with your key works, and the public address does not answer that port.

## Install

Python 3.10 or newer, and `ssh-keygen` from `openssh-client` (checks run `ssh-keygen -Y verify`). Skip the voice extra. This process does not run the brain.

```text
sudo useradd --system --create-home --home-dir /var/lib/jarvis-gateway --shell /usr/sbin/nologin jarvis-gateway
sudo install -d -o jarvis-gateway -g jarvis-gateway -m 0750 /opt/jarvis /var/lib/jarvis-gateway
sudo -u jarvis-gateway git clone <this-repo> /opt/jarvis
sudo -u jarvis-gateway python3 -m venv /opt/jarvis/.venv
sudo -u jarvis-gateway /opt/jarvis/.venv/bin/pip install -e /opt/jarvis
```

On the Zenbook, create the owner key and leave the private half there: `ssh-keygen -t ed25519 -f ~/.ssh/jarvis-continuity-owner -C ""`. The VM config gets the public blob only.

Write `/var/lib/jarvis-gateway/gateway.json`, owner `jarvis-gateway`, mode `0640`. A symlink, a name starting with `jarvis.config.json`, or an unknown key (including a private-key field) is `bad_config`. `fleet_id` and `authority_id` start with a letter or digit, then letters, digits, `.`, `_`, `:`, or `-`, at most 128 characters.

```json
{
  "state_path": "/var/lib/jarvis-gateway/authority.sqlite3",
  "fleet_id": "david-fleet",
  "authority_id": "gw-2026-11-12",
  "owner_public_keys": ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI..."],
  "bind_host": "127.0.0.1",
  "bind_port": 8766,
  "auto_failover": false
}
```

`owner_public_keys` is one to sixteen `ssh-ed25519` values, type plus base64 blob, no comment. They verify signatures and do not enrol a computer. `bind_host` must be loopback (`refused_bind` otherwise). `bind_port` `0` picks a new port every start. Omit `lease_seconds` and `margin` (test clocks). The store then uses lease 15 seconds, renew 5 seconds, margin 2 seconds.

TLS stays off unless both `tls_cert` and `tls_key` are set. Both must be regular files, not symlinks, outside the live roots above. The socket then requires TLS 1.2 or newer and the ready origin is `https`. One path alone, or a file the TLS stack cannot load, is `bad_config` (exit 2). That key is not the owner key. Leave both out: the `tailscale serve` line speaks HTTP to loopback. There is no CA or certificate-pin key. The first `serve` creates the database as `LEGACY_ACTIVE`. `state_path`'s parent must already exist.

`/etc/systemd/system/jarvis-continuity-gateway.service`:

```ini
[Unit]
Description=JARVIS continuity gateway

[Service]
Type=simple
User=jarvis-gateway
Group=jarvis-gateway
WorkingDirectory=/opt/jarvis
ExecStart=/opt/jarvis/.venv/bin/python -m jarvis.continuity.gateway serve --config /var/lib/jarvis-gateway/gateway.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`systemctl daemon-reload` and `systemctl enable --now jarvis-continuity-gateway`. Check: `journalctl -u jarvis-continuity-gateway` shows one JSON object with `authority_id`, `origin` `http://127.0.0.1:8766`, and `pid`. `ss -ltn` shows `127.0.0.1:8766`. A refusal prints `{"error":"<code>"}` and exits 2. `Restart=on-failure` restarts a bad config. `systemctl stop` stays stopped.

## Enrolment and cutover

The gateway's own CLI has `serve --config`, `backup --config --output`, and `restore --config --input --new-authority-id`. Operators enrol, acknowledge, change mode and remove with `jarvis continuity --config <json> ...` on a laptop holding the owner key (see Gaps); manual recovery has a local hub bridge, but its complete production binding is still pending.

Enrolment is `AuthorityClient.enroll`, POST `/v1/authority/participants`, signed with the owner key on the caller. Fields: `device_id`, `installation_id`, `kind` (`pc`, `server`, or `client`), `public_key`, `capabilities`, and optional `relay_origin`. `relay_origin` must be loopback HTTP (`http://127.0.0.1:<port>`, `http://[::1]:<port>`) or a tailnet HTTPS name with an explicit port (`https://<host>.ts.net:443`); anything else is `bad_origin`. `pc` may use `status`, `lease`, `fence`, `candidate`, `consume`, `reconcile`, `client`. `server` may use `status`, `fence`, `reconcile`. `client` may use `status`, `client`, `reconcile`. `manage_main` is refused. Success returns the ids, kind, and capabilities. The directory row loads again on restart. The running application never calls this, so the Zenbook and the Dell stay unenrolled.

`set_mode` (POST `/v1/authority/mode`, fields `mode` and `expected_mode_version`) walks one edge: `LEGACY_ACTIVE` to `PREPARING_V2`, then `QUIESCED`, then `V2_ONLY_NO_MAIN`, then `ROLLBACK_QUIESCED`, then `LEGACY_ACTIVE`. `PREPARING_V2` and `QUIESCED` can also return to `LEGACY_ACTIVE`. It cannot set `V2_ACTIVE`, `HANDOVER`, or `QUARANTINED`. Entering `V2_ONLY_NO_MAIN` or `LEGACY_ACTIVE` from a quiesced mode needs every eligible participant to have acknowledged the new version. `V2_ACTIVE` happens only when an owner transition (`/v1/authority/transitions`, then `/v1/authority/transitions/step` with `drain`, `fence`, `grant`, `complete`, or `cancel`) reaches `complete` with `canary_ok` true. No MAIN command runs those steps. A restart drops a live lease, and a stored `V2_ACTIVE` or `HANDOVER` opens next as `V2_ONLY_NO_MAIN`.

Leave `auto_failover` unset or `false`. `true` exits 2 with `{"error":"auto_failover_disabled"}` and does not open `state_path`.

## Backup and restore

Stop the unit first. A live serve holds `<state_path>.writer`, so backup returns `writer_busy` and writes nothing. `--output` must be absent. A symlink is `conflict`.

```text
sudo systemctl stop jarvis-continuity-gateway
sudo -u jarvis-gateway /opt/jarvis/.venv/bin/python -m jarvis.continuity.gateway backup --config /var/lib/jarvis-gateway/gateway.json --output /var/lib/jarvis-gateway/authority-backup.sqlite3
sudo systemctl start jarvis-continuity-gateway
```

Stdout is `ok` true plus `output`, `state_path`, `fleet_id`, and `authority_id`. Backup does not construct `AuthorityStore`, so the source incarnation stays put.

Restore publishes a new file with grants removed. Use a second JSON whose `state_path` does not exist, nor that path plus `-wal`, `-shm`, `-journal`, or `.writer`. `fleet_id` and `authority_id` in that JSON are the snapshot's. `--new-authority-id` must differ (`conflict` if it matches the pin).

```text
sudo -u jarvis-gateway /opt/jarvis/.venv/bin/python -m jarvis.continuity.gateway restore --config /var/lib/jarvis-gateway/restore.json --input /var/lib/jarvis-gateway/authority-backup.sqlite3 --new-authority-id gw-restored-1
```

Stdout has `ok` true, `mode` `LEGACY_ACTIVE`, `participants`, `admissions`, `transitions`, and `directory` all 0, and `serve_requires_new_config` true. `authority_id` is the new id. `source_authority_id` is the snapshot's. The config file is not rewritten. Write a serve config with the new id and the new path before starting the unit on that file. The old pin quarantines it. Membership is empty, so enrol and cut over again. The first open still bumps incarnation and epoch.

## Gaps

The process can listen, back up, and restore. The phone still stops when the Zenbook sleeps.

- Live since 2026-11-12 on `oracle-vm` (`https://oracle-vm.tailnet-example.ts.net:443`; the operator `origin` needs the explicit `:443`). It runs `/usr/bin/python3` with `PYTHONPATH=/opt/jarvis` (the gateway is stdlib-only, so no venv), and the Oracle image's iptables stands in for ufw. Operator config: `~/.config/jarvis-continuity/operator.json` on the Zenbook. Enrolled: `zenbook`, `dell` (installation `dell-win11-2026-11-12`, key `C:\Users\jdoe\.ssh\jarvis-continuity-dell` on the Dell, 2026-11-12).
- `jarvis continuity --config <json> status|enroll|ack|mode|remove|recover|lease` (`jarvis/continuity/cli.py`) drives the authority. The JSON holds `origin`, `authority_id`, `device_id`, `installation_id`, `private_key_path`, optional `owner_private_key_path` and an `enrollment` map; never key material. `mode` refuses `V2_ACTIVE` (that comes from a lease). The running application still does not import this module.
- `pin_relay` accepts loopback HTTP and an explicitly pinned HTTPS `*.ts.net` origin. Signed transient envelopes relay ask text, session events, frames, and overview JSON through the gateway; the phone receiver verifies signatures and rejects replayed effects. Camera, locate, and remote control remain refused. A passkey receipt is not an owner proof: the design requires a fresh gateway `OwnerProof` from a separate minter, so passkey-driven MAIN transition stays refused. The phone still uses the Zenbook serve in `docs/handbook/phone.md`; operational cutover is not complete.
- `remove` / the owner-proof `/v1/authority/participants/disable` route disables a participant; the current MAIN is refused (`active_holder`, 409). `set_mode` cannot enter `V2_ACTIVE`. `auto_failover: true` is refused.
- A gateway restart deliberately clears the live lease and waits out the longest grant. MAIN gets it back only through the hub's `RecoveryOrchestrator` when `auto_recover` is on (default off); otherwise run a manual `recover` transition.
- No CA or certificate-pin setting. Tailscale terminates tailnet HTTPS and forwards plaintext to loopback.
- Restore wipes membership and leases and leaves the config pin unchanged.


## MAIN bootstrap adapter status (2026-11-12)

MAIN switching is still blocked; the sequence below is protocol order, not a
working cutover procedure. Do not enter QUIESCED to test these partial adapters.

The existing hub now exposes owner-only POST `/v1/continuity/main` with exactly
`{"device":"dell"}`. Both the peer address and CLI destination must be numeric
loopback addresses. The CLI uses its existing local hub owner credential and
forwards before taking the fleet lock when `continuity.operator_config` is a
locally valid operator JSON matching the local device. The hub refuses unless its
production coordinator is bound. No new server or port is introduced.
`continuity.operator_config` defaults to absent; invalid or absent configuration
retains the previous behavior. Startup does not automatically bind production.

Windows brain transfer has a cluster path adapter selected by explicit
`devices.<id>.brain_transfer_dir` (default absent). Each peer folder must already
exist, be inside that peer's storage roots, and be outside every Git checkout.
Use a private staging folder outside the vault/repo on each participant. With this
adapter selected, every contacted peer needs that setting. No config is edited by
this implementation. It streams bundles over transfer path jobs, verifies hashes
and sizes, and delegates import/freeze to the existing integrity-checked brain
functions. Successful receive deletes staging bytes; pull exports remain private
staging files for manual cleanup. `memory.sqlite3` is never sent to GitHub. The
ordinary managed effect fence still applies: this is not yet a maintenance channel
that can transfer during source drain.

Required operator order after the missing production adapters are completed:

1. On the owner Zenbook, `jarvis continuity --config
   ~/.config/jarvis-continuity/operator.json status`.
2. `jarvis continuity --config ~/.config/jarvis-continuity/operator.json mode
   PREPARING_V2 --expect-version <version-from-status>`, then status again.
3. `jarvis continuity --config ~/.config/jarvis-continuity/operator.json mode
   QUIESCED --expect-version <version-from-status>`.
4. Fence/drain legacy writers on **both** PCs. On each PC, use its own operator
   config and installation key: `jarvis continuity --config <local-operator.json>
   ack --next-version`. Dell's key stays at
   `C:\Users\jdoe\.ssh\jarvis-continuity-dell`. Ack asserts fencing; it does not
   perform a runtime drain.
5. Read status, then `jarvis continuity --config
   ~/.config/jarvis-continuity/operator.json mode V2_ONLY_NO_MAIN
   --expect-version <version-from-status>`.
6. On both participants, acknowledge the current version/epoch with `ack`.
   **Blocked:** the manual recover/lease adapter and CLI now exist, but complete
   production binding and the actual prepared-endpoint canary for the initial
   Zenbook lease are still missing. Do not substitute `mode V2_ACTIVE`.
7. Only after an initial ready lease and complete runtime/transport binding:
   `jarvis cluster main dell`, then on the new MAIN `jarvis cluster main zenbook`.
   These commands are not yet operationally supported by this implementation.

Missing work: startup issuer/executor/coordinator binding and renewal; source and
candidate callbacks over signed jobs; candidate-signed gateway readiness;
authenticated endpoint/key repointing and successor hub connectivity for the
forward-only Dell; canary; transition-scoped brain transport; manual recovery; and
a two-way runtime drill including rollback. `auto_recover` remains off by default.

### Participant and manual-recovery adapters (continuation)

The signed long-poll channel now has transition-scoped participant RPCs for
source drain/release, candidate-local readiness signing, fencing and lease
acceptance. Scoped brain chunks require a live `GRANTED_NOT_READY` grant and
stay inside the participant's configured non-Git staging directory. Ordinary
jobs remain subject to their existing admission rules. Node loading uses its own
operator JSON and installation key; key paths and readiness flags are never
supplied by the source's RPC.

The manual command now exists:

```text
jarvis continuity --config <local-operator.json> --yes recover --hub-config <local-jarvis.config.json> --state <local-.jarvis-directory> --request-id initial-zenbook-main
```

`lease` is an alias of `recover`. The stable request id identifies the manual
request; there is no force or automatic retry/promotion option. This command
uses the current hub's loopback owner API. It requires an explicitly bound
`hub.continuity_recovery`, whose runtime survives the CLI and whose canary checks
the prepared application endpoint and signs a fresh grant-bound receipt with its
own installation key. A failed/unverifiable canary leaves no MAIN.

**Do not run the cutover sequence yet.** Production startup does not bind these
adapters automatically, so the command currently refuses on an unbound real hub
without requesting a lease. The sequence above remains blocked at initial
recovery. Authenticated endpoint/key repointing, successor hub/tunnel lifecycle,
complete production startup/renewal, qualified return-brain activation and the
two-way MAIN drill are still required. Existing memory-conflict checks and source
read-only markers remain intact. No new config defaults enable promotion;
`auto_recover` remains off.

### Signed repointing and explicit return disposition

Candidate endpoint offers now bind the successor endpoint, code-signing key,
installation, target credential and grant tuple to the candidate's installation
signature. A target verifies its own durable fence, probes the actual successor
hub for a fresh signed nonce/key response, then rechecks authority before saving
private state. Runtime config is overlaid from that verified state;
`jarvis.config.json` is not rewritten. Corrupt or partially published endpoint
state refuses instead of reverting to the old endpoint.

`RoleLifecycle` uses the existing autostart/unit and tunnel helpers, with explicit
role-switch restart support. It requires a controller-provided startup-readiness
capability, free old role locks, and the matching live not-ready grant. That
capability is unavailable by default. A pending successor grant is private,
transition-scoped, verified by gateway renewal and consumed once; it is not an
automatic recovery mechanism. Existing Hub/NodeWorker renewal hooks only renew an
already accepted, bound lease and never promote a device.

The return brain intentionally still refuses a conflicting nonempty database or
an unapproved old freeze. After a refused return, first inspect authenticated
status and resolve any active transition. Stop the local former MAIN's hub/node
and its brain writers manually; this agent has not stopped or restarted anything.
Keep the returning source quiesced. Identify the staged incoming bundle and
review these three SHA-256 values: its `memory_sha256`, a consistent SQLite backup
of the current local `memory.sqlite3` (use `brainsync.snapshot_memory`, not a raw
hash when WAL exists), and the raw current `brainsync-handoff.json` freeze file.
Then the explicit local operator command is:

```text
jarvis continuity --config <this-PC-operator.json> --yes return-prepare --hub-config <local-jarvis.config.json> --state <local-.jarvis> --bundle <staged-return.bundle> --incoming-memory-sha256 <reviewed-incoming-memory-hash> --current-memory-sha256 <reviewed-current-backup-hash> --freeze-sha256 <reviewed-freeze-hash>
```

This archives all previous database/WAL/SHM and bundle artifacts privately, leaves
the original freeze active, and authorizes only that incoming memory/source. No
memory is committed to GitHub. Changed data or a busy role lock refuses. An
interrupted archive leaves a review state and is not retried automatically.

After reviewing an interrupted `archiving` or `needs_review` approval and its
private archive, retire that approval with its same three recorded hashes:

```text
jarvis continuity --config <this-PC-operator.json> --yes return-retire --hub-config <local-jarvis.config.json> --state <local-.jarvis> --incoming-memory-sha256 <reviewed-incoming-memory-hash> --current-memory-sha256 <reviewed-current-backup-hash> --freeze-sha256 <reviewed-freeze-hash>
```

This parks only the approval beside its archive. It does not restore or move
memory, remove the freeze, approve another bundle, or complete a handoff. Missing
confirmation, changed hashes, a different local identity, and busy role locks
refuse through the existing return-approval checks.

The scoped return receive still invokes the original conflict checks. After it
receives exactly the approved memory, only the newer matching fenced return grant
can invoke `activate_return`; that callback archives the old freeze before the
successor startup/canary. If the return canary fails and authority says no MAIN at
a newer epoch, preserve incoming memory and restore the freeze with:

```text
jarvis continuity --config <this-PC-operator.json> --yes return-refreeze --state <local-.jarvis>
```

The fake-transport two-way adapter drill now covers this explicit-confirmation
path, signed repointing, unit helper calls, grant consumption and canary/refreeze
rollback. It does not validate a shipped automatic production startup factory.

**The full production sequence remains blocked.** Remaining work is the complete
startup/controller composition, deferred callback/HTTP acknowledgement before
role changes, a verified route to the Dell successor, and distributed owner-proof
routing for the return. The Dell's existing forward-only SSH credential expressly
forbids reverse forwards; changing a lease or a pointer does not change that.
No SSH restrictions were widened. Do not treat the status→mode→ack→recover→MAIN
sequence above as operational until those remaining composition tests pass.

## Deploying the gateway to Oracle

`jarvis/continuity/gateway_deploy.py` ships the local `jarvis/` package to the
Oracle gateway (`/opt/jarvis`, unit `jarvis-continuity-gateway.service`). It
never commits, and `--dry-run` runs nothing remote.

```text
.venv/bin/python -m jarvis.continuity.gateway_deploy --dry-run          # tarball + script + commands only
.venv/bin/python -m jarvis.continuity.gateway_deploy                    # deploy, do NOT restart
.venv/bin/python -m jarvis.continuity.gateway_deploy --restart          # deploy + restart, auto-rollback on failure
.venv/bin/python -m jarvis.continuity.gateway_deploy --config-patch patch.json
.venv/bin/python -m jarvis.continuity.gateway_deploy --rollback [--restart]
```

Defaults: `--host ubuntu@203.0.113.77`, `--key ~/.ssh/oracle-vm.key`.

- The tarball holds `jarvis/` minus `__pycache__`, `*.pyc`, `*.pre-*` backups,
  `.jarvis` and `jarvis/web/` (except `jarvis/web/phone/`). It is `scp`'d to
  `/tmp`, then one `ssh ... bash -s` runs the generated script.
- Remote script: backs up `/opt/jarvis` and `gateway.json` as `*.pre-<UTC ts>`,
  stages `/opt/jarvis.new`, import-checks it as `jarvis-gateway`, swaps, fixes
  owner and mode (0750). Nothing changes on the live tree if a check fails.
- `--config-patch FILE` merges the file's top-level keys into `gateway.json`
  (stays 0600, owner `jarvis-gateway`) and validates the result with
  `load_gateway_config` against the staged code before the swap.
- Without `--restart` it prints the exact restart command for you to run. With
  it, the unit is restarted and must stay active ~15 s; otherwise the tool
  restores the backups, restarts again and exits nonzero.
- `--rollback` restores the newest `/opt/jarvis.pre-*` and `gateway.json.pre-*`.
- Old `*.pre-*` backups pile up on the VM; prune them by hand.
