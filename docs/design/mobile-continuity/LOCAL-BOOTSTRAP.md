# Local continuity bootstrap

This is the supported per-PC setup path. Preflight is read-only. Prepare stages
operator metadata and claims a dedicated local transfer directory. Configure
backs up and updates only the local settings reviewed in the manifest plus the
operator pointer. None of those three actions writes the managed marker, changes
`cluster.main_device`, changes gateway mode, or stops a service. Cutover is a
separate confirmed action that fences local admission only after the local
worker has stopped and the authority is at the exact reviewed quiescence tuple.

The operator JSON's local installation pin must match the public key derived
from `private_key_path`; authenticated gateway status must identify the expected
fleet and authority. Every PC pin and successor route must be complete. The
private key files are never copied. The settings manifest is the explicit,
operator-reviewed input for configuration that may be missing from the current
hub config:

```json
{
  "schema": 1,
  "staging_dirs": {
    "zenbook": "/home/owner/.local/share/jarvis/continuity-transfer",
    "dell": "C:\\Users\\owner\\AppData\\Local\\JARVIS\\continuity-transfer"
  },
  "brainsync_enabled": true
}
```

The manifest must provide a path for every enrolled PC participant. The local
path is checked against this PC's filesystem and must be outside Git. A remote
Windows or POSIX path is syntax-checked only on this PC; it is checked against
the peer's filesystem when that peer runs its own local preflight/prepare. Server
devices such as Oracle do not need brain transfer paths. `brainsync_enabled` is
optional; if omitted, the current setting is preserved.

## Commands

Run locally on each PC, using the proposed operator JSON and reviewed manifest
delivered for that PC. These commands preserve the existing HTTPS 443 routes;
bootstrap does not modify Tailscale Serve configuration.

```sh
jarvis continuity bootstrap preflight \
  --hub-config /path/to/jarvis.config.json --state /path/to/.jarvis \
  --operator-config /path/to/operator.proposed.json \
  --settings-manifest /path/to/transfer-settings.proposed.json

jarvis continuity bootstrap prepare \
  --hub-config /path/to/jarvis.config.json \
  --operator-config /path/to/operator.proposed.json \
  --staging-dir "$HOME/.local/share/jarvis/continuity-bootstrap" \
  --settings-manifest /path/to/transfer-settings.proposed.json

jarvis continuity bootstrap configure \
  --hub-config /path/to/jarvis.config.json --state /path/to/.jarvis \
  --operator-config "$HOME/.local/share/jarvis/continuity-bootstrap/continuity-operator.json" \
  --settings-manifest "$HOME/.local/share/jarvis/continuity-bootstrap/continuity-settings-manifest.json" \
  --yes

jarvis continuity bootstrap cutover \
  --hub-config /path/to/jarvis.config.json --state /path/to/.jarvis \
  --operator-config "$HOME/.config/jarvis/continuity/operator.json" \
  --expect-mode QUIESCED --expect-version VERSION --yes
```

Prepare creates only metadata and the dedicated outside-Git transfer directory.
It never copies private keys or changes the hub config. Configure creates a
durable `jarvis.config.json.pre-continuity-bootstrap` backup and installs the
operator pointer under the current user's application config directory (0600
file; 0700 directory on Unix). It uses the config mutation lock and compares the
current config with the validated snapshot immediately before publication. A
concurrent config edit refuses with `local_config_changed` without publishing
the operator file or backup.

Cutover requires an active local worker, an exact `QUIESCED` or
`V2_ONLY_NO_MAIN` authority tuple with no transition and no valid MAIN lease,
and already staged local storage. On Windows, JARVIS does not use systemd. It
requests a nonce-bound cooperative stop from the existing `NodeWorker` via
`jarvis.cluster.request_node_worker_stop`, waits up to 40 seconds by default
(the helper supports at most 45 seconds), then independently verifies the exact
receipt and release of `cluster-node.lock`. It does not signal or kill a PID.
Only after the old writer is closed does it bind the local runtime, guard and
issuer and write the durable fence/ack. Restoration uses the existing
`cluster_setup._launch_agent` helper and waits for a fresh process registration
and writer lock. Failure to stop or restore is reported as a refusal; a still
running old writer is never acknowledged as stopped.

Linux uses the existing `jarvis.rest` systemd lifecycle. On either platform,
the cutover command does not rewrite the MAIN role. If a failure occurs after a
managed marker or durable local fence is written, those remain fail-closed and
must be reconciled before retrying. Fixture tests validate ordering and failure
paths, not a live Dell or Arch cutover. Root performs operational acceptance
after the tested release and both local setups are ready.
