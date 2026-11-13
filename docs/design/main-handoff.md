# MAIN handoff

*Proposed exclusive-authority handoff, plus the local bootstrap commands and the live gates that still block a production cutover.*

The plan is [continuity-design.md](continuity-design.md). Module behavior is [continuity-runtime.md](continuity-runtime.md). Fleet publication of `cluster.main_device` is [fleet-api.md](fleet-api.md). Brain transfer rules are [obsidian-storage.md](obsidian-storage.md). Hash-bound return preparation is [docs/handbook/continuity.md](../handbook/continuity.md).

Changing `cluster.main_device` does not stop an old process, expire an old owner token, fence an issued grant, or stop an old CLI from running local OS tools. Stopping only the HTTP listener is not enough. The contract is a durable exclusive authority mode plus executor-enforced admission. Manual Arch → Dell → Arch and legacy cutover come before mobile recovery. Automatic failover comes after those drills, and stays off until then.

Isolated adapters and a controller factory exist under `jarvis/continuity/` and pass fake-transport tests. That is not a live cutover. Do not enable a managed marker or point `continuity.operator_config` at an operator file until the live gates at the end of this file are closed.

## Invariants

1. A committed authority state has zero or one MAIN. None is an explicit safe state.
2. Every newly admitted ordinary operation names the active MAIN, one target, a digest, and a logical operation identity. Oracle validation does not replace a MAIN-signed action.
3. The target's durable admission records whether work was accepted before a transition. A retry after ambiguous acceptance reconciles that identity. It does not execute again. Target admission is not global logical-result publication.
4. Managed legacy dispatch and local effect paths are disabled before v2 can become active, and stay disabled after reboot.
5. Losing new-work authority does not erase accepted compute or free its resources. Agent-created child actions are new work, even when the parent was already accepted.
6. Local stop, key release, camera-consent withdrawal, and credential revocation stay available as narrow safety operations. They cannot start work.
7. No cloud database stores prompts, workflow bodies, provider sessions, or artifact bodies. Oracle handoff state is identifiers, versions, digests, readiness, and transitions.
8. Manual preference, current role, execution target, and approval station are independent.

Application fencing covers trusted managed JARVIS runtimes. An unmodified old checkout or a malicious unsandboxed process already running as the desktop owner can still call OS APIs. A marker file cannot stop that process. Inventory and stop old launch paths before production cutover. OS privilege separation is not claimed.

## Authority tuple

One Oracle SQLite writer, WAL and FULL, transactional compare-and-set of mode, transition, and lease. One failure domain. Do not add etcd for two PCs. etcd, SQLite, and clock papers are references for the rules below, not dependencies.

`authority_id, incarnation, epoch, main_device_id, mode_version, policy_version, lease_id, lease_validity`

- `authority_id` is the enrolled authority. Do not accept an arbitrary gateway.
- `incarnation` changes after authority restart or restore. Old cached tokens die.
- `epoch` advances inside a valid incarnation on every transfer or recovery. Never restore a prior live lease.
- Proposed renew cadence is 5 seconds, new-work validity at most 15 seconds. Renew names the expected holder, incarnation, and epoch, and cannot race a transition that has stopped renewals. These times are not measurements.
- Lease timing uses the authority's monotonic running-process clock. On restart, discard all lease validity, change incarnation, and wait at least the longest previously permitted offline grant plus the measured safety margin before granting a replacement. Do not reconstruct remaining validity from an old wall-clock expiry.
- A restore must show the old authority service cannot still issue grants. Reusing a backup while another copy is alive is not supported.

Initial v2 validates each new operation online:

1. The target checks caller, MAIN signature, target, capability, the original signed ticket, and the digest. It prepares a durable local receipt and does not run the effect.
2. The target asks the authority to consume the one-use admission. The authority checks mode, incarnation, epoch, and lease, then records consumption or rejects it.
3. The target fsyncs the accepted receipt and only then starts the effect. Client success does not precede that receipt.
4. A crash between steps is reconciled from both records. A consumed identity is not run again under a new id to recover a missing acknowledgement.

The ticket's original issuance and expiry (proposed 24-hour new-admission lifetime) cannot be refreshed by a new MAIN. Consumption before the transfer transaction is already-admitted old-epoch work. A grant prepared but not consumed is rejected. There is no distributed atomic transaction across Oracle and a PC.

A session is admitted once. Continued input uses sequence-numbered short-lived grants renewed only by the current MAIN. No queued input is replayed after a lease or connection change. Do not write one cloud row per video frame. Proposed session-control offline lifetime is no longer than 1 second where practical, and never beyond remaining lease validity. Measure that before promising it. Handoff waits out unacknowledged old session and new-work permits before activation completes.

Linux cached validity uses `CLOCK_BOOTTIME` (suspend counts). Ordinary `CLOCK_MONOTONIC` does not. Do not use process CPU time or persist a raw monotonic timestamp across reboot. Windows timing must be tested against the Python API actually used. If that adapter is missing, online validation is mandatory and sessions pause. Sleep, hibernate, connection loss, incarnation change, and process restart invalidate cached leadership. Revalidate online before new work. A database epoch bump does not fence an unreachable target that still holds a cached permit. The 30-second prepared-failover figure is a measurement target, not a cold-login guarantee.

Stable request, workload, and task ids stay separate from target-bound execution-operation ids. Strict fleet-effect agents need mediated epoch-checked tools. A guarded HTTP endpoint is not enough for a CLI that has its own tools.

## State the stores must keep

| Store | Writer | Recovery rule |
| --- | --- | --- |
| Oracle fleet authority | Sole CAS transaction | Validity dies on restart. Mode is never inferred from old config |
| Oracle transition | CAS. Unique caller plus client request key | Proposed 90-day receipt. One active transition per fleet |
| Oracle participant fence | Authority records the target's authenticated ack | Old or offline participants cannot rejoin ordinary work without a matching mode version |
| Oracle admission consumption | Authority checks the active holder, then consumes | Digest and tuple only. No payload body |
| PC authority mode | Local transaction before the fence ack | Survives reboot. Absent or corrupt after migration is `QUARANTINED`, not legacy |
| PC execution receipt | Target fsync before the effect | Payload stays on the PC. Uncertain effects are kept. Rollback keeps the rows |
| PC coordinator journal | Current MAIN only | Never copied wholesale to Oracle. Standby readiness names the exact checkpoint |

Local state is per installation, not whichever directory `--state` named. A fresh `--state` must not reset an enrolled machine to legacy.

## Cutover machine

```text
LEGACY_ACTIVE -> PREPARING_V2 -> QUIESCED -> V2_ONLY_NO_MAIN -> V2_ACTIVE
V2_ACTIVE -> HANDOVER -> V2_ACTIVE | V2_ONLY_NO_MAIN
V2_ONLY_* -> ROLLBACK_QUIESCED -> LEGACY_ACTIVE
any corrupt or unrecognized mode -> QUARANTINED
```

`PREPARING_V2` still serves only legacy. `QUIESCED` admits no new legacy or v2 work. `V2_ONLY_NO_MAIN` means the durable legacy fence is installed and verified. `V2_ACTIVE` is exactly one v2 MAIN. Rollback is an explicit migration, never two live modes.

1. Inventory service managers, autostart, old checkouts, CLI launchers, overlays, voice, tunnels, owner tokens, and open sessions. Inspect ids, not secret values.
2. Deploy the guard-aware release to every initially eligible effect-bearing PC while still legacy. Unknown or offline participants are excluded from v2 and cannot reconnect on old authority.
3. One managed launch path and one stable mode store per installation. Boot reads it before static config chooses a role. Refuse a release that does not understand the committed mode.
4. `QUIESCED`: stop admission and renewals, drain bounded accepted work, release held input, stop old MAIN, voice, and agent dispatchers. Do not free resources merely because a deadline elapsed.
5. Commit `V2_ONLY_NO_MAIN` on every eligible PC. Rotate legacy dispatch credentials. Collect signed fence acknowledgements for the new mode version. A missing ack blocks activation unless that participant is explicitly removed and its old paths are fenced by deployment.
6. The authority commits fleet v2 only after those acks and no outstanding old authority. The first v2 MAIN is granted only after that transaction. Bootstrap privileges cannot issue ordinary jobs.
7. Legacy windows may show status or proxy. They cannot become a local MAIN.

Writing a marker beside an unmodified old executable does not stop it. The migration has to stop those processes.

## Handover steps

```text
REQUESTED -> VALIDATING -> CATCHING_UP -> DRAINING -> FENCING
         -> GRANTED_NOT_READY -> VERIFYING -> SUCCEEDED
before fence: FAILED or CANCELLED may leave the old MAIN active
at or after fence: failure -> V2_ONLY_NO_MAIN or RECOVERING with a fresh epoch
```

1. One authenticated transition: `manage_main`, expected tuple and policy, original issuance and expiry, one-use admin challenge, idempotent operation id. A second administrator sees that transition or a conflict.
2. Validate candidate identity, release, grants, health, headroom, and readiness profile. Control-ready is typed no-model coordination. AI-ready also needs provider login and PC-local state. The UI must not equate them.
3. The candidate proves checkpoint cursors while the old MAIN still serves. Sensitive bytes travel PC to PC. A missing checkpoint restricts the class. Cloud metadata is not a fabricated context.
4. Stop the old MAIN's new admission and renewal. Proposed drain budget is 15 seconds. Journal accepted work and uncertain outcomes. Reject agent child actions issued after authority loss.
5. Fence targets or wait out permits. In one transaction, clear the old lease and advance the epoch. Prepare the candidate with ordinary admission still closed.
6. Commit one lease as `GRANTED_NOT_READY`. Only a scoped read-only canary is allowed. No ordinary requests.
7. On verified readiness, open ordinary admission and record success. Persist a preference only for `set_preferred_main`. Temporary `make_main_now` does not change the preference. `MainRuntime` does not call `make_main_now`.
8. The demoted PC is an executor node. Interfaces keep their own execution target and approval station. Reconcile from receipts. Never clone a live provider session or replay input.

A canary failure after the fence cannot resurrect the previous lease. Revoke the candidate, enter `V2_ONLY_NO_MAIN`, and recover under a fresh epoch. Cancel before the fence and cancel after the fence are different states. Say which one the UI is in.

A node does not self-elect from its own timeout. After Arch returns it starts as a node with no cached MAIN authority. Automatic return, when later enabled, needs 30 seconds of continuous readiness and a confirmed checkpoint, then the same planned handover. If Oracle is down, automatic and phone promotion stop. Workers do not invent leases. Local emergency stop still works. Recovery restores or fences the same authority. It does not create a second local authority.

A restored older backup may restore previously revoked phone, node, or admin membership. The owner accepted that. It does not permit reviving old MAIN leases or running two authority instances.

## In-flight work

| At the transition | Policy |
| --- | --- |
| Routed, not accepted by a target | Hold or reject. Retry the original operation id. Do not create a duplicate |
| `PREPARED`, admission not consumed | Reject the old tuple. Release the unused reservation after reconciliation |
| Consumed or accepted, completion unknown | Keep the receipt. No automatic side-effect replay |
| Pure finite compute already running | May finish under completion-only scope. Reservations stay until the result is reconciled |
| Coding or AI agent already running | Bounded authorized computation may finish. Every fresh tool call needs current MAIN. Pause the tool channel on demotion. Keep the workspace for review |
| Shell or deploy with external effects | May be partial. Cancellation is not reversal |
| Keyboard or mouse | Release held input. Never replay the buffer |
| Screen or camera | Pause and rebind session control. An uninterrupted stream is a goal, not a guarantee. Frames stay transient |
| Pending approval | An old target, digest, or epoch cannot authorize new work. Re-present on an authorized PC |
| MAIN admin | Separate passkey. It cannot approve OS effects or drop camera and stop restrictions |

A finished pure-compute attempt may return its manifest after handover. That does not publish the logical result or start downstream jobs. The active MAIN's journal commits one result per logical task. Workers publish attempt-specific artifacts, not a shared final filename. A target-local compare-and-set cannot settle a cross-target choice. Missing replicated workflow state blocks the risky rerun. Cloud metadata cannot replace it.

## Wire shapes

- `RoleStatus`: mode, version, active and preferred device ids, tuple, transition, coarse readiness. No credentials or private payloads.
- `TransitionPreview`: action, candidate, expected tuple and policy, consequences, required readiness, whether an expiry wait is needed. Preview grants nothing.
- `TransitionConfirm`: original admin token, client idempotency key, action, candidate, expected tuple and policy, fresh passkey proof bound to a short-lived challenge.
- Progress and cancel use that same transition id. Responses say old-valid-MAIN, no-MAIN, or new-valid-MAIN.
- `FenceMode` / `FenceEpoch`: target-signed ack of durable mode and version, and of stopped new-work admission.
- `ConsumeAdmission`: target-authenticated, MAIN-signed digest, exact current tuple. No body on the metadata store.
- `ReconcileReceipt`: accepted or uncertain, with a result location and digest. No cloud-persisted output body.

Signatures prove origin and integrity, not current ownership. Admission still checks mode and epoch. Unknown schema, state, or incarnation fails closed. A failed cloud metadata request is not permission to dispatch.

An effect context is internal and immutable: mode version, MAIN tuple, operation token, admission id, target, digest, approval reference, and effect class. No `trusted=true` and no loopback header replaces it.

## Delivery gates

H0 inventory and a model test that two admission authorities cannot run, with crash injection at each journal boundary. H1 guard-aware release that still preserves legacy. H2 Oracle CAS authority in an isolated test fleet, no production promotion. H3 quiesce, fence legacy, grant Arch v2, one read-only canary, then reboot Arch and prove old managed paths cannot act alone. H4 manual Arch → Dell → Arch. Rollback is another v2 handover, not editing `main_device`. H5 AI and workload readiness. Loss of the old coordinator cannot produce new agent tool actions. H6 mobile `manage_main` with a passkey. Ordinary requests stay MAIN-routed. H7 shadow, then opt-in automation, only after measured timings fit the envelope. H8 legacy rollback drill: disable v2 admission, drain, wait out permits, fence, `ROLLBACK_QUIESCED`, then exactly one legacy MAIN at a new mode version. Offline v2 participants stay quarantined until reconciled.

A failed H3 or later step may leave no MAIN. That is preferred to reviving old authority. A reachable UI is not proof that ordinary command authority is back.

Acceptance that must stay true once this is live: Dell as v2 MAIN and every old Arch PWA, console, CLI, voice, owner-token hub, and local tool wrapper is rejected or proxied, and none acts alone. Reboot with static config still saying Arch does not start a legacy hub. An alternate `--state` or a downgrade does not reset the mode. Resume after pause cannot consume or renew the old tuple. Suspend longer than the lease invalidates cached permits. A partitioned target blocks candidate activation until its fence ack or the permit expires. Two phones, one epoch: one CAS winner. A lost confirm response returns the existing transition. Crash before the fence leaves the old MAIN. Crash after the fence and before ready is explicit no-MAIN. A failed canary accepts no ordinary work. Each prepared, consumed, and accepted boundary is identifiable and is not replayed.

## Local bootstrap commands

Preflight, prepare, and configure do not write the managed marker, do not change `cluster.main_device`, do not change gateway mode, and do not stop a service. Cutover is a separate confirmed action. It fences local admission only after the local worker has stopped and the authority is the exact reviewed quiescence tuple. It does not rewrite the MAIN role.

The operator JSON's local installation pin must match the public key from `private_key_path`. Authenticated gateway status must name the expected fleet and authority. Every PC pin and successor route must be complete. Private keys are never copied. The settings manifest is the reviewed input for configuration that may be missing from the hub config. It must name a path for every enrolled PC. The local path is checked on this PC and must be outside Git. A remote Windows or POSIX path is syntax-checked here and checked on that peer's own preflight. Server devices such as Oracle do not need brain transfer paths. `brainsync_enabled` is optional. If omitted, the current setting is preserved.

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

These commands preserve existing HTTPS 443 routes. Bootstrap does not modify Tailscale Serve.

Prepare creates metadata and the outside-Git transfer directory only. Configure writes `jarvis.config.json.pre-continuity-bootstrap`, installs the operator pointer under the user application config directory (0600 file, 0700 directory on Unix), takes `config_mutation`, and compares the config with the validated snapshot immediately before publication. A concurrent edit refuses `local_config_changed` and publishes neither the operator file nor the backup.

Cutover requires an active local worker, an exact `QUIESCED` or `V2_ONLY_NO_MAIN` tuple with no transition and no valid MAIN lease, and staged local storage. On Windows, JARVIS requests a nonce-bound cooperative stop via `jarvis.cluster.request_node_worker_stop`, waits up to 40 seconds (the helper's maximum is 45), then checks the receipt and that `cluster-node.lock` was released. It does not signal or kill a PID. Only after the old writer is closed does it bind the local runtime, guard, and issuer and write the durable fence ack. Restore uses `cluster_setup._launch_agent` and waits for a fresh process registration and writer lock. A still-running old writer is never reported as stopped. Linux uses the existing `jarvis.rest` systemd lifecycle. If a failure happens after a managed marker or durable local fence is written, those stay fail-closed and must be reconciled before retry.

Read-only checks, on each PC with that PC's operator file:

```text
jarvis cluster roles
jarvis continuity --config <local-operator.json> status
```

The protocol order below is the intended sequence. It is not safe to run while the live gates are open. Use the version from each status response. Run acknowledgements on each eligible PC with that PC's own operator config. `ack` records a fence. It does not drain and it does not perform the runtime cutover. `mode V2_ACTIVE` cannot grant a lease. A ready lease is required before each role publication.

```text
jarvis continuity --config <owner-operator.json> --yes mode PREPARING_V2 --expect-version <current>
jarvis continuity --config <owner-operator.json> --yes mode QUIESCED --expect-version <returned-version>
jarvis continuity --config <zenbook-operator.json> --yes ack --next-version
jarvis continuity --config <dell-operator.json> --yes ack --next-version
jarvis continuity --config <owner-operator.json> --yes mode V2_ONLY_NO_MAIN --expect-version <returned-version>
jarvis continuity --config <zenbook-operator.json> --yes ack
jarvis continuity --config <dell-operator.json> --yes ack
jarvis continuity --config <zenbook-operator.json> --yes recover \
  --hub-config <zenbook-jarvis.config.json> --state <zenbook-.jarvis> \
  --request-id initial-zenbook-main
jarvis cluster main dell
# After Dell independently shows a ready lease, and only with a supported owner signer there:
jarvis cluster main zenbook
```

`recover` exists and refuses when the hub's recovery coordinator is unbound. `auto_recover` and `auto_failover` stay off. Acceptance of a live roundtrip needs gateway status `V2_ACTIVE` with the intended MAIN and a valid lease, both nodes on the same tested release fingerprint, one ordinary desktop or dispatch job arriving at the current main after each move, and refusal of the old MAIN's fenced operation. A loopback or in-process result is not acceptance.

## What the tree has, and what is still blocking

Present in the working tree, exercised with fake transports: owner-authenticated numeric-loopback `/v1/continuity/main` and `/v1/continuity/recover` (the client does not hold the fleet lock while waiting; an unbound coordinator refuses); signed `continuity` long-poll jobs with code match, deadlines, and replay protection; scoped brain transfer that pins one transition, incarnation, and epoch; `ManualRecovery`; signed repoint that probes the successor before dropping the old tunnel; renewal of an already accepted lease only; an explicit return-brain path (`return-prepare`, `activate_return`, `return-refreeze`) that does not change ordinary receive and conflict logic. `jarvis/continuity/production.py` can build a controller from a local operator config, enrolled pins, and the participant runtime and guard. It rejects missing pins and routes and never copies another installation's private key. A successor without the owner private key can serve a promoted MAIN role, and recovery or another owner handoff refuses `owner_proof_unavailable`. Direct bound handoff cannot bypass the release-parity preflight.

Still required before any live command above:

1. Install one tested release on Zenbook and Dell. Served fingerprints must match. The running hub on a dirty tree and an older served Dell are not a pair.
2. Each PC's own operator file, with its installation private key and the reviewed pins. Keep the authority-owner private key with its current owner. Dell's return path is a scoped proxy: Dell metadata `owner_proxy_device: "zenbook"`, Zenbook `owner_proxy_clients: ["dell"]`. The owner key stays on the Zenbook. The signed job may request only `set_preferred_main` toward the local Zenbook candidate and owner steps for that same open tuple. Copying the Zenbook owner key to the Dell is not acceptable.
3. Managed state through the supported enrollment process, and an open executor journal already fenced at the current tuple on each PC. Load the marker, operator path, `storage.roots`, and per-PC `brain_transfer_dir` only after parity is verified.
4. A reachable successor route. The Dell's existing forward tunnel reaches the Zenbook, and its SSH key forbids reverse forwarding (`permitlisten=127.0.0.1:1`). That restriction stays. The forward link alone cannot provide the opposite connection. A separately prepared pinned HTTPS route can be carried by the signed offer. Confirm it from the Dell with the Dell's own operator file.
5. Publication must agree with the verified effective role. `set_main` re-reads the raw config under the fleet lock. A successor that has only a signed overlay is not the same fact. Reconcile them before treating a role write as the handover.
6. `startup_ready` refuses with `startup_incomplete` when no complete controller is supplied. Missing config must not create a partially bound managed runtime. Renewal hooks do not construct that binding.
7. Nodes must ack callback results before role switches. The old hub must finish the HTTP response before its unit changes. Do not kill the process that still holds an unacknowledged callback.

`cluster.main_device` remains the live unfenced MAIN until that cutover. Phone sleep does not promote the Dell.
