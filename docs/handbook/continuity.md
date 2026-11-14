# Continuity

*The gateway, manual recovery, and why MAIN is not switched by editing config. Design context is [jarvis-os-architecture.md](../jarvis-os-architecture.md).*

One HTTP process in front of one authority database, on a machine that stays awake. Serving it does not change `cluster.main_device`. The phone still stops when the Zenbook sleeps until an operational cutover exists. `python -m jarvis.continuity.gateway` does not read `jarvis.config.json` or an owner private key. Importing the module does not start it. Startup does not bind a production runtime. `auto_recover` defaults off.

## Gateway host

Create an Always Free `VM.Standard.A1.Flex` Ubuntu instance. Do not raise OCPU, memory, or the boot volume past the Always Free label, and do not add another instance on the same free quota. If the cost estimate is not zero, stop. SSH public key only. Public ingress is TCP 22 only.

`sshd -T` must print `passwordauthentication no`. Then `ufw default deny incoming`, `ufw default allow outgoing`, `ufw allow OpenSSH`, `ufw enable`. Allow SSH before enabling ufw. Do not allow the gateway port on the public interface. Install Tailscale, `tailscale up`, then `ufw allow in on tailscale0`. Publish loopback only:

```text
sudo tailscale serve --bg --https=443 http://127.0.0.1:8766
```

Never `tailscale funnel`. `ufw status` allows OpenSSH and `tailscale0` and does not allow the gateway port. From a laptop, SSH with a key works, and the public address does not answer that port. The operator origin needs the explicit `:443`.

Python 3.10 or newer, and `ssh-keygen` from `openssh-client` (`ssh-keygen -Y verify`). Skip the voice extra. This process does not run the brain.

```text
sudo useradd --system --create-home --home-dir /var/lib/jarvis-gateway --shell /usr/sbin/nologin jarvis-gateway
sudo install -d -o jarvis-gateway -g jarvis-gateway -m 0750 /opt/jarvis /var/lib/jarvis-gateway
sudo -u jarvis-gateway git clone <this-repo> /opt/jarvis
sudo -u jarvis-gateway python3 -m venv /opt/jarvis/.venv
sudo -u jarvis-gateway /opt/jarvis/.venv/bin/pip install -e /opt/jarvis
```

The owner key stays on the Zenbook: `ssh-keygen -t ed25519 -f ~/.ssh/jarvis-continuity-owner -C ""`. The VM gets the public blob only.

`/var/lib/jarvis-gateway/gateway.json` is owner `jarvis-gateway`, mode `0640`. A symlink, a name starting with `jarvis.config.json`, or an unknown key, including a private-key field, is `bad_config`. `fleet_id` and `authority_id` start with a letter or digit, then letters, digits, `.`, `_`, `:`, or `-`, at most 128 characters.

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

`owner_public_keys` is one to sixteen `ssh-ed25519` values, type plus base64, no comment. They verify signatures and do not enrol a computer. `bind_host` must be loopback. `bind_port` `0` picks a new port every start. Omit `lease_seconds` and `margin`. The store then uses lease 15 s, renew 5 s, margin 2 s. The loader refuses a state file under the repo `.jarvis` tree or `~/.local/state/jarvis`.

TLS stays off unless both `tls_cert` and `tls_key` are set. Both must be regular files, not symlinks, outside those live roots. The socket then requires TLS 1.2 or newer. One path alone, or a file the TLS stack cannot load, is `bad_config` (exit 2). That key is not the owner key. Leave both out: Tailscale speaks HTTPS and forwards plaintext to loopback. There is no CA or certificate-pin key. The first `serve` creates the database as `LEGACY_ACTIVE`. `state_path`'s parent must already exist.

`auto_failover: true` exits 2 with `{"error":"auto_failover_disabled"}` and does not open `state_path`. `FailoverPolicy` returns `action=none` and `reason=manual_only`. Nothing calls it on a timer.

The unit is `jarvis-continuity-gateway.service`, user `jarvis-gateway`, `Restart=on-failure`, `RestartSec=5`, `ExecStart` the venv Python `-m jarvis.continuity.gateway serve --config /var/lib/jarvis-gateway/gateway.json`. A refusal prints `{"error":"<code>"}` and exits 2. `Restart=on-failure` will restart a bad config. `systemctl stop` stays stopped. A healthy journal line is one JSON object with `authority_id`, `origin`, and `pid`. `ss -ltn` shows `127.0.0.1:8766`.

## Enrolment and modes

The gateway CLI is `serve`, `backup`, and `restore`. Operators use `jarvis continuity --config <json>` on a laptop that holds the owner key. This checkout's `continuity.operator_config` is `/home/jdoe/.config/jarvis/continuity/operator.json`. The JSON holds `origin`, `authority_id`, `device_id`, `installation_id`, `private_key_path`, optional `owner_private_key_path`, and an `enrollment` map. Never key material.

Enrolment is POST `/v1/authority/participants`, signed with the owner key: `device_id`, `installation_id`, `kind` (`pc`, `server`, or `client`), `public_key`, `capabilities`, optional `relay_origin`. `relay_origin` is loopback HTTP or `https://<host>.ts.net:443`. Anything else is `bad_origin`. `pc` may use `status`, `lease`, `fence`, `candidate`, `consume`, `reconcile`, `client`. `server` may use `status`, `fence`, `reconcile`. `client` may use `status`, `client`, `reconcile`. `manage_main` is refused. The running application does not call enrol by itself.

`set_mode` (POST `/v1/authority/mode`, fields `mode` and `expected_mode_version`) walks one edge: `LEGACY_ACTIVE` → `PREPARING_V2` → `QUIESCED` → `V2_ONLY_NO_MAIN` → `ROLLBACK_QUIESCED` → `LEGACY_ACTIVE`. `PREPARING_V2` and `QUIESCED` can also return to `LEGACY_ACTIVE`. It cannot set `V2_ACTIVE`, `HANDOVER`, or `QUARANTINED`. Entering `V2_ONLY_NO_MAIN` or `LEGACY_ACTIVE` from a quiesced mode needs every eligible participant to have acknowledged the new version. `V2_ACTIVE` happens only when an owner transition (`/v1/authority/transitions`, then `step` of `drain`, `fence`, `grant`, `complete`, or `cancel`) reaches `complete` with `canary_ok` true. A restart drops a live lease. A stored `V2_ACTIVE` or `HANDOVER` opens next as `V2_ONLY_NO_MAIN`. `mode` on the CLI refuses `V2_ACTIVE`.

`remove` and owner-proof POST `/v1/authority/participants/disable` disable a participant. The current MAIN is `active_holder`, 409.

## Backup, restore, deploy

Stop the unit first. A live serve holds `<state_path>.writer`, so backup returns `writer_busy` and writes nothing. `--output` must be absent. A symlink is `conflict`.

```text
sudo systemctl stop jarvis-continuity-gateway
sudo -u jarvis-gateway /opt/jarvis/.venv/bin/python -m jarvis.continuity.gateway backup --config /var/lib/jarvis-gateway/gateway.json --output /var/lib/jarvis-gateway/authority-backup.sqlite3
sudo systemctl start jarvis-continuity-gateway
```

Stdout is `ok` plus `output`, `state_path`, `fleet_id`, and `authority_id`. Backup does not construct `AuthorityStore`, so the source incarnation stays put.

Restore publishes a new file with grants removed. Use a second JSON whose `state_path` does not exist, nor that path plus `-wal`, `-shm`, `-journal`, or `.writer`. `--new-authority-id` must differ. Membership comes back empty. The config file is not rewritten. Write a serve config with the new id and path before starting. The old pin quarantines the previous file. The first open still bumps incarnation and epoch.

```text
sudo -u jarvis-gateway /opt/jarvis/.venv/bin/python -m jarvis.continuity.gateway restore --config /var/lib/jarvis-gateway/restore.json --input /var/lib/jarvis-gateway/authority-backup.sqlite3 --new-authority-id gw-restored-1
```

`jarvis/continuity/gateway_deploy.py` ships the local `jarvis/` package to `/opt/jarvis` and unit `jarvis-continuity-gateway.service`. It never commits. `--dry-run` runs nothing remote. The tarball omits `__pycache__`, `*.pyc`, `*.pre-*`, `.jarvis`, and `jarvis/web/` except `jarvis/web/phone/`.

```text
.venv/bin/python -m jarvis.continuity.gateway_deploy --dry-run
.venv/bin/python -m jarvis.continuity.gateway_deploy
.venv/bin/python -m jarvis.continuity.gateway_deploy --restart
.venv/bin/python -m jarvis.continuity.gateway_deploy --config-patch patch.json
.venv/bin/python -m jarvis.continuity.gateway_deploy --rollback [--restart]
```

`--host` and `--key` override the defaults in that module. The remote script backs up `/opt/jarvis` and `gateway.json` as `*.pre-<UTC>`, stages `/opt/jarvis.new`, import-checks as `jarvis-gateway`, swaps, and sets mode 0750. A failed check leaves the live tree. `--config-patch` merges top-level keys into `gateway.json` (stays 0600) and validates with `load_gateway_config` before the swap. Without `--restart` it prints the restart command. With `--restart` the unit must stay active about 15 s or the tool restores the backups and exits nonzero. `--rollback` restores the newest `*.pre-*`. Prune old backups by hand.

## What is blocked

Do not run the mode walk to test partial adapters. Production startup does not bind the issuer, executor, and coordinator, so a real hub refuses recover without requesting a lease. The Dell's forward-only SSH credential forbids reverse forwards. Changing a lease or a pointer does not change that. No SSH restriction was widened.

The hub exposes owner-only POST `/v1/continuity/main` with exactly `{"device"}` and POST `/v1/continuity/recover` with `{"device","request_id"}`. Both the peer address and the CLI destination must be numeric loopback. No new port. `run_main` refuses when `configured_handoff` returns None. `run_recover` refuses when `hub.continuity_recovery` is not callable. `RuntimeRefusal`, `LinkError`, and `AuthorityError` come back as `unconfirmed` and are not retried. Nothing in `jarvis/` assigns `hub.continuity_recovery` or calls `participant.bind`. With no participant bound, a node answers `managed_unbound`.

Participant actions on a `continuity` job are `readiness`, `drain`, `release`, `accept`, `snapshot`, `fence`, `probe`, `brain`, `canary`, and `activate_return`. One action per payload. Readiness is taken from this PC, not from the caller. A callback does not take a key path, a gateway URL, or a shell command. Drain and release belong to the source. `brain_transfer_dir` has no default, must already exist inside that peer's storage roots, and must be outside every Git checkout. A brain chunk name matches `brain-<32 hex>.bundle`. `memory.sqlite3` is never sent to GitHub. Ordinary jobs stay on their existing admission rules. This path does not bypass a drained runtime's fence.

`ManualRecovery` has no timer, election, force flag, or automatic retry. The canary must return true. `ready` requires `V2_ACTIVE`, a valid ready lease on this device, the same incarnation and epoch, and no open transition.

```text
jarvis continuity --config <local-operator.json> status
jarvis continuity --config <local-operator.json> mode PREPARING_V2 --expect-version <from-status>
jarvis continuity --config <local-operator.json> ack --next-version
jarvis continuity --config <local-operator.json> --yes recover --hub-config <local-jarvis.config.json> --state <local-.jarvis> --request-id <stable-id>
```

`lease` is an alias of `recover`. Ack asserts fencing. It does not drain. Do not substitute `mode V2_ACTIVE`. `jarvis cluster main <id>` is not operationally supported by this implementation.

Candidate endpoint offers bind the successor endpoint, code-signing key, installation, target credential, and grant to the candidate's installation signature. The target probes the successor hub, then rechecks authority before saving private state. Runtime config is overlaid. `jarvis.config.json` is not rewritten. Corrupt or partial endpoint state refuses instead of reverting. A pending successor grant is private, transition-scoped, and consumed once. Existing renewal hooks renew an already accepted lease and never promote a device.

Return of a brain refuses a conflicting nonempty database or an unapproved old freeze. After a refusal, stop the local former MAIN's writers by hand, keep the source quiesced, and compare three SHA-256 values: the bundle's `memory_sha256`, a SQLite backup of the local `memory.sqlite3` (`brainsync.snapshot_memory`, not a raw hash when a WAL exists), and the raw `brainsync-handoff.json` freeze.

```text
jarvis continuity --config <this-PC-operator.json> --yes return-prepare --hub-config <local-jarvis.config.json> --state <local-.jarvis> --bundle <staged-return.bundle> --incoming-memory-sha256 <incoming> --current-memory-sha256 <current-backup> --freeze-sha256 <freeze>
jarvis continuity --config <this-PC-operator.json> --yes return-retire --hub-config <local-jarvis.config.json> --state <local-.jarvis> --incoming-memory-sha256 <incoming> --current-memory-sha256 <current-backup> --freeze-sha256 <freeze>
jarvis continuity --config <this-PC-operator.json> --yes return-refreeze --state <local-.jarvis>
```

`return-prepare` archives previous database, WAL, SHM, and bundle artifacts privately and authorizes only that incoming memory. It does not commit memory to GitHub. An interrupted archive is left for review and is not retried. `return-retire` parks one reviewed approval beside its archive. It does not restore memory or clear the freeze. `return-refreeze` is for a failed return canary when authority says there is no MAIN at a newer epoch: keep the incoming memory and restore the freeze. `activate_return` runs only after the approved memory arrives and only on the newer matching fenced return grant.

Signed repointing, the explicit return hashes, and a fake-transport drill exist. They do not make a production startup factory. Do not treat status → mode → ack → recover → `cluster main` as a cutover.
