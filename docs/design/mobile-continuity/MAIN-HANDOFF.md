---
title: JARVIS MAIN designation and exclusive-authority handoff
status: draft
approval: pending
updated: 2026-11-11
scope: implementation plan only; no runtime changes
---

# MAIN handoff implementation plan

Recommendation: implement a **durable exclusive authority mode plus executor-enforced admission**, not a configuration-file swap. First prove manual Arch → Dell → Arch handover and legacy cutover. Only then enable mobile recovery, and only after those drills consider automatic failover.

This document is a proposed implementation contract supporting the canonical `DESIGN.md`. It does not claim any mechanism below is already implemented. Exactly one PC holds MAIN new-work authority; every other machine, including Oracle, remains a NODE. Oracle hosts the bounded membership/lease authority and mobile interface but does not schedule ordinary work. Ordinary requests remain `interface → gateway → MAIN → target`; MAIN-local effects use the same executor checks. Accepted work and safety-stop are distinguished from new work.

## 1. What exists and why changing main_device is insufficient

Repository inspected 2026-11-11, with uncommitted changes present. References are working-tree locations, not immutable commit-level proofs:

| Existing path | Observed behavior | Needed change |
| --- | --- | --- |
| `jarvis/topology.py:19–43` | `identity()` reads configured main and local IDs; `is_node()` compares them | Keep physical identity permanent; obtain active role from authority state |
| `jarvis/cluster.py:437–452` | `Hub` starts only on configured main and obtains local owner token | Separate role authority, routing/hub facade and executor; owner token alone cannot grant leadership |
| `jarvis/cluster.py:488–500` | Endpoint identity/header plus bearer token determine current permissions | Require current fleet mode, authority incarnation, epoch and scoped operation authorization |
| `jarvis/cluster.py:582–631` | `dispatch()` directly executes MAIN-local actions or enqueues node jobs | Guard both paths; a local MAIN must not bypass fencing |
| `jarvis/cluster.py:409–433` | `execute_local()` calls desktop/storage/transfer/remote/camera implementations | Centralize effect admission and delegate a verified internal execution context |
| `jarvis/cluster_proxy.py:218–260` | Main/node startup and phone hosting derive from configured role; an extra window can use the old owner token | Start from durable authority mode; old windows become read-only/proxy interfaces, never another MAIN |
| `jarvis/__main__.py:404–410` | `jarvis desktop` can call `Remote` directly | Managed CLI must resolve current authority, not infer privilege from local config |
| `jarvis/remote.py:106–147,211+` | `run_here()` directly invokes screen/input/native/terminal tools; remote dispatch defaults from origin | Guard local and remote entry points and preserve independent execution target |
| `jarvis/actions.py:339+`, `control.py:578+`, `terminal.py:643+`, `app_ui.py:402+` | Tool/effect paths exist outside the Hub request handler | Each effect sink requires execution context; planning permission alone is insufficient |
| `jarvis/runner.py:126–154` | Model tasks require static main and launch processes in their selected workspace | Check current authority; spawned agent tools must not retain new-work rights after demotion |
| `jarvis/ui.py:825–875` | Job worker inherits origin/approval context and calls assistant | Add immutable coordinator/operation context; reject old queued requests before execution |
| `jarvis/confirm.py:73+`, `assistant.py:708+`, `pipeline.py:319+` | PC approvals park/consume commands and run confirmed actions | Bind/verify target, digest, operation and epoch again when approval is consumed |
| `jarvis/cluster_store.py:40–70` | Arch-local SQLite owns node credentials and jobs | Introduce separate metadata authority and PC receipt journals; do not copy this database to Oracle wholesale |

A configuration update cannot stop an old process, expire an old owner token, fence an already issued grant, or prevent an old CLI from running local OS tools. Stopping only the HTTP listener is not enough.

## 2. Invariants and threat boundary

1. A committed authority state has **zero or one** MAIN, never two. The absence of MAIN is an explicit safe state.
2. Every newly admitted ordinary operation is attributable to the active MAIN and one target, digest and logical operation identity. Oracle validation alone does not replace a MAIN-signed intended action.
3. Target-local durable admission records whether work was accepted before a transition. A retry after ambiguous acceptance queries/reconciles that identity; it does not execute again. Target admission is not global logical-result publication across different workers.
4. All managed legacy dispatch and local effect paths are disabled before v2 authority can become active, and remain disabled after reboot.
5. Losing MAIN new-work authority does not automatically erase accepted compute or release its resources. Agent-created child actions remain **new work**, even if the parent agent process was previously accepted.
6. Local control stop, key/button release, camera-consent withdrawal and credential revocation remain available as narrowly defined safety operations. They cannot start work or gain broader rights.
7. No cloud database/log stores prompts, personal workflow bodies, provider session state or artifact bodies. Handoff state on Oracle is identifiers, versions, digests, readiness and transitions only.
8. Manual preference, current role, execution target and approval station are independent.

**Limit:** application-level fencing controls trusted managed JARVIS runtimes. An unmodified old checkout or malicious unsandboxed process already running as the desktop owner can invoke OS APIs outside it. A marker file cannot cryptographically stop that process. Before production cutover, inventory and stop old launch paths; upgraded effect sinks enforce the marker. Stronger enforcement against arbitrary old/owner processes would require OS privilege separation and is not claimed here.

## 3. Recommended authority/clock design

Use one Oracle SQLite writer with explicit WAL/FULL configuration and transactional compare-and-set of fleet mode, transition and lease. This is one failure domain, not a distributed consensus service. Do not introduce etcd merely for two PCs; the etcd references below illustrate that owning a lock must be checked transactionally, not assumed to protect a separate external OS resource.

### 3.1 Proposed authority tuple

`authority_id, incarnation, epoch, main_device_id, mode_version, policy_version, lease_id, lease_validity`

- `authority_id` is the enrolled authority's pinned identity; do not accept an arbitrary gateway as authority.
- `incarnation` changes after authority process restart or restore; old cached session/admission tokens are unusable afterward.
- `epoch` is monotonically advanced inside a valid incarnation on every authority transfer/recovery; never restore a prior live lease.
- MAIN renews on the proposed 5-second cadence with maximum 15-second new-work validity. Renew uses expected holder/incarnation/epoch and cannot race a transition that has stopped renewals.
- Lease timing is interpreted by the authority's monotonic running-process clock. On authority restart, discard all lease validity, change incarnation and wait at least maximum previously permitted offline grant lifetime plus measured safety margin before granting a replacement. Do not reconstruct remaining validity from an old wall-clock expiry.
- A restore must establish that the old authority service/VM cannot still issue grants. Reusing a backup while another active authority copy survives is not supported. Operator fencing of the old service/origin is required before a replacement is published.

### 3.2 Prefer online admission validation initially

For initial v2, the target validates/consumes each **new operation's** admission with the authority, under the current MAIN tuple. This adds one bounded metadata RTT but avoids making detached PC wall clocks the sole mutual-exclusion mechanism. Oracle sees a digest and scope, not the action's private body; it only validates authority, while MAIN chooses and signs the operation.

Proposed transaction sequence:

1. Target validates caller, MAIN signature, target, capability, original signed OperationTicket and digest; it prepares a durable local receipt/reservation without running the effect.
2. Target asks the authority to consume the one-use admission identity. The authority atomically checks current mode/incarnation/epoch/lease and records consumption, or rejects it.
3. Target durably records the accepted receipt referring to that consumption and only then starts the bounded effect. Client success must not precede the accepted receipt.
4. A crash/timeout between steps is reconciled using both records. A consumed identity is not run a second time under a new ID merely to recover a missing acknowledgement.

Use the canonical immutable OperationTicket: its original authenticated issuance/expiry (proposed 24-hour new-admission lifetime) cannot be refreshed by a new MAIN grant. Define consumption before the authority's transfer transaction as already-admitted old-epoch work. If its local receipt finalization is delayed, it must be reconciled as that admission, never silently recategorized as a new command. A grant prepared but not consumed before transfer is rejected. Include this interleaving in protocol tests; there is no claim of a distributed atomic transaction across Oracle and PC SQLite.

Do not use one cloud database write per video frame or pointer move. A session is admitted once; continued input uses sequence-numbered, short-lived bounded session grants renewed only by current MAIN. No queued/replayed input after a lease/connection change. Stream/media buffers are transient. Initial session-control offline lifetime should be no longer than 1 second where practical, and never beyond remaining MAIN lease validity; measure the tradeoff before promising it. Handoff waits out any unacknowledged old session/new-work permits before completing activation.

### 3.3 Clock, pause, sleep and reboot rules

A monotonic clock is not automatically suspend-aware. Python documents Linux `CLOCK_BOOTTIME` as including suspended time; ordinary Linux `CLOCK_MONOTONIC` excludes it. Do not use process CPU time or persist a raw monotonic timestamp across reboot. Windows timing behavior must be validated against the actual Python/native API in use, not assumed from a different runtime's tick-count function.

- Boot/process identity is part of every cached validity observation.
- Sleep/resume, hibernate, connection loss, authority-incarnation change and process restart invalidate cached leadership/session authorization. Revalidate online **before** admitting new work/input. Do not wait for a queued UI callback to notice an old role.
- Linux uses a tested suspend-aware elapsed-time adapter for any permitted cached validity; Windows uses an explicitly tested adapter. If unavailable or timing health is uncertain, online validation is mandatory and sessions pause.
- Use local elapsed request duration to reduce permitted validity; reject stale/delayed grant responses. Never start a fresh full TTL merely when an old response finally arrives.
- Planned fencing requires target acknowledgements or expiry of all outstanding permits. A DB epoch bump by itself is insufficient for an unreachable target that holds usable cached authority.

The prior ≤30-second prepared failover target includes lease expiry, permit drain, readiness and safety margin. It is a measurement target, not a guarantee for cold login/state repair.

## 4. Durable state contracts (proposed, not executable DDL)

| Store/entity | Keys and essential fields | Writer / acknowledgement | Retention and recovery |
| --- | --- | --- | --- |
| Oracle `FleetAuthority` | PK fleet_id; mode; mode_version; holder; incarnation; epoch; policy_version; current_transition_id; process-local lease validity reference | Sole authority transaction; WAL/FULL commit | Current state durable; validity invalid on restart; mode never inferred from old config |
| Oracle `MainTransition` | PK transition_id; unique caller+client_request_key; source/candidate IDs; expected incarnation/epoch/policy; action; readiness profile; state; requested preference; committed result | Authority CAS transaction | Proposed 90-day transition receipt; original issuance/expiry bound as in canonical operation protocol |
| Oracle `ParticipantFence` | PK fleet_id+device_id; installation identity; verified release; local mode_version; max observed incarnation/epoch; boot_id; fence acknowledgement | Authority records authenticated target acknowledgement | Old/offline participants cannot rejoin ordinary work without matching mode/version |
| Oracle `AdmissionConsumption` | PK admission_id; unique target+operation_id+attempt_id; digest; MAIN tuple; status; consumed-at authority record | Authority transaction checks active holder then consumes | Retain according to operation-age/dedup contract; no payload body |
| PC `AuthorityMode` | PK fleet_id; installation UUID; mode; mode_version; pinned authority_id; migration_id; last accepted tuple; implementation version | Local SQLite durable transaction before acknowledging fence | Must survive upgrade/reboot; absent/corrupt after migration is QUARANTINED, not LEGACY |
| PC `ExecutionReceipt` | PK target+operation_id+attempt_id; original operation token hash; digest; admission_id; state; process identity; result pointer; resources; originating epoch | Target fsync before effect acceptance | PC-local payload/result only; uncertain effects retained for reconciliation; mode rollback preserves these rows |
| PC `CoordinatorJournal` | PK workload_id+sequence; private DAG/context/checkpoint cursor; target receipts; outstanding transitions | Current MAIN local durable writer; prepared replica follows cursor | Never copied wholesale to Oracle; standby readiness states exact available checkpoint |

Local state is per installation, not whichever working directory supplied `--state`. A stable mode guard must also cover alternate state directories or refuse them in a managed installation. The exact protected path/ACL is selected during platform implementation; tests must not allow `--state /tmp/fresh` to reset an enrolled machine to legacy authority.

## 5. Exclusive legacy-to-v2 cutover state machine

```text
LEGACY_ACTIVE
  -> PREPARING_V2        (only legacy may serve ordinary work)
  -> QUIESCED           (no new legacy/v2 work)
  -> V2_ONLY_NO_MAIN    (durable legacy fence installed and verified)
  -> V2_ACTIVE          (exactly one v2 MAIN)

V2_ACTIVE -> HANDOVER -> V2_ACTIVE | V2_ONLY_NO_MAIN
V2_ONLY_* -> ROLLBACK_QUIESCED -> LEGACY_ACTIVE  (explicit controlled migration only)
any inconsistent/corrupt/unrecognized mode -> QUARANTINED
```

Cutover procedure:

1. Inventory service managers, autostart tasks, old checkouts/venvs, CLI launchers, desktop overlays, voice processes, reverse/forward tunnels, old owner tokens and open sessions. Preserve credentials locally; inspect locations/IDs, not secret values.
2. Deploy the guard-aware compatibility release to **all initially eligible effect-bearing PCs** while still in legacy mode. Run parity tests. Unknown/offline participants are excluded from v2 and cannot later reconnect on old authority.
3. Install a single managed launch path and stable authority-mode store per installation. Verify boot startup reads it **before** static config chooses a role. Refuse downgrade to a release that does not understand the committed mode.
4. Enter `QUIESCED`: stop admission and renewals, drain bounded accepted work, close legacy control sessions/release held input, stop old MAIN/voice/agent dispatchers and old alternate launches. Do not free resources merely because a deadline elapsed.
5. Commit `V2_ONLY_NO_MAIN` locally on every eligible PC; rotate/revoke legacy remote owner/node dispatch credentials and disable legacy effect handlers/routes. Obtain signed fence acknowledgements for the new mode version. A missing acknowledgement blocks activation, unless that participant is explicitly removed and unreachable old service paths are fenced by deployment/network/credential controls.
6. The authority commits fleet v2 mode only after these acknowledgements and no-outstanding-old-authority condition. Grant the first v2 MAIN only after that transaction. Bootstrap privileges cannot issue ordinary jobs.
7. Open interfaces against the current MAIN. Legacy bookmarks/console windows may display migration status or act as authenticated proxies; they cannot silently become local MAIN. New desktop/voice/CLI requests obey the same role context.

**Critical limitation:** merely writing a marker beside an unmodified old executable does not stop it. The initial controlled migration must actually stop/disable those managed old processes and entry points. This plan does not certify arbitrary user-run old source against same-user OS privileges. If that stronger guarantee is required, design an OS-isolated privileged executor with constrained service identities before deployment.

## 6. Planned MAIN handover (Arch to Dell or back)

State machine:

```text
REQUESTED -> VALIDATING -> CATCHING_UP -> DRAINING
          -> FENCING -> GRANTED_NOT_READY -> VERIFYING -> SUCCEEDED
before fence: FAILED/CANCELLED may leave old valid MAIN active
at/after fence: failure -> V2_ONLY_NO_MAIN or RECOVERING with a fresh epoch
```

1. Accept one authenticated transition with `manage_main`, expected authority tuple/policy version, original issuance/expiry, one-use fresh admin challenge and idempotent operation ID. A second administrator sees the existing transition or a conflict; no simultaneous transfers.
2. Validate candidate identity, signed release/protocol, effective grants, connected health, resource headroom and requested readiness profile. **Control-ready** permits typed no-model coordination; **AI-ready** additionally requires provider login and PC-local state/workflow readiness. The UI cannot equate them.
3. Candidate proves checkpoint cursors. Catch up while old MAIN still serves. Sensitive contents travel PC-to-PC only. A missing checkpoint restricts operation class/recovery; no fabricated context from cloud metadata.
4. Stop old MAIN's new request admission and lease renewal for this transition. Drain for the proposed 15-second budget. Journal accepted work and uncertain outcomes; reject newly issued agent child actions after authority loss.
5. Fence effect-bearing targets/session permits or wait for outstanding validity to end. In a transaction, clear the old lease and advance epoch; no contender can renew the old tuple. Prepare candidate without exposing ordinary admission yet.
6. Commit one candidate lease as `GRANTED_NOT_READY`. Only a tightly scoped read-only readiness canary is allowed; no ordinary requests or side effects. Candidate starts its fresh local coordinator/provider session as required.
7. On verified readiness, activate ordinary admission and atomically record success. Persist the preference change only for `set_preferred_main`; temporary `make_main_now` does not alter it. Expose whether automatic return is enabled.
8. Demoted PC remains an executor NODE. All user interfaces keep their execution target and approval station independently. Reconcile pending work using receipts/checkpoint; never clone live provider sessions or replay input.

A canary failure after fencing cannot resurrect the previous lease. Revoke candidate admission, enter `V2_ONLY_NO_MAIN`, and recover a suitable PC under a fresh transition/epoch. Cancel means different things before/after the fence; UI must explain the resulting authority state.

## 7. Failure promotion, resume and automatic return

### Arch lost or partitioned

Stop renewing its lease. Authority refuses new ordinary admission under a lost/expired tuple. After expiry and cached-permit drain, validate the prepared replacement and follow the same fence/grant/canary protocol. A node does not self-elect from its own timeout. Accepted finite pure compute may finish and report receipts; no new subordinate jobs/tool calls may be created under an old tuple.

### Arch returns

Its durable mode remains v2. It starts as a NODE with no cached MAIN authority, advertises current boot/release and reconciles outstanding receipts. Initial default is no automatic role change. When opt-in automatic return is enabled, require the preferred PC's 30-second continuous readiness and confirmed checkpoint catch-up, then perform the normal planned handover. Manual temporary selection needs an explicit hold/return policy so the scheduler does not immediately undo it.

### No eligible MAIN or Oracle unavailable

The stable dashboard can show last-known metadata, exact readiness failures and authorized role-recovery requests. No ordinary work is accepted without a valid MAIN. If Oracle is unavailable, automatic and phone-initiated promotion stop; workers do not invent leases. Local emergency stop remains functional. Initial administrator recovery restores/fences the same authority; it does not secretly create another local authority.

### Authority restart / restore

Start with no live lease, a new incarnation and an admission barrier for all old permits; target reconnect clears cached validity. Require old authority service fencing before replacement. Preserve operation/receipt reconciliation. **Accepted residual risk:** a restored older backup may restore previously revoked phone/node/admin membership. The owner explicitly accepted this; no external non-rollback revocation service or mandatory full re-pair mechanism is required by this plan. State that limitation instead of promising revoked identities can never revive. This acceptance does not permit reviving old MAIN execution leases or running two authority instances.

## 8. In-flight work, streams and approvals

| Work at transition | Policy |
| --- | --- |
| Received/routed only, no target acceptance | Hold or reject while transferring; retry original operation identity, never create a duplicate by default |
| Target PREPARED, admission not consumed | Recheck/reject old tuple; release only unused reservation after reconciliation |
| Admission consumed/accepted but completion unknown | Preserve receipt and reconcile; no automatic side-effect replay |
| Pure finite compute already running | May finish under completion-only scope; reservations remain until verified exit/result reconciliation |
| Coding/AI agent already running | May finish already authorized bounded computation; every fresh tool action/child dispatch needs current MAIN authority. Pause/revoke its tool channel on demotion; independent workspace changes stay isolated for later review |
| Shell/deployment with accepted external effects | Can be partial/uncertain; cancellation is not reversal. Reconcile before any replacement attempt |
| Remote keyboard/mouse | Release held input on interruption; resume only with current authority/sequence and target consent; never replay buffered actions |
| Screen/camera stream | Gateway/interface remains stable; pause/rebind expiring session control to new MAIN, verify ownership/consent; uninterrupted stream is a goal, not a guarantee. Frame bodies remain transient |
| Pending sensitive approval | Old target/digest/epoch approval cannot authorize new work after transfer; cancel/re-present on an authorized PC station |
| MAIN admin request | Separate role permission/passkey; cannot approve OS effects or remove camera/stop restrictions |

### Logical results across retry targets

The target may finish an old accepted pure-compute attempt and return its immutable result
manifest after MAIN handover. That does **not** grant it permission to publish the workload's
logical result or trigger downstream jobs. The active MAIN's PC-local durable workflow journal
arbitrates one result per logical task using the known attempt generation and conditional
commit. Workers publish attempt-specific immutable artifacts, not a shared final filename.
When retrying on another target, the active MAIN must reconcile the prior attempt, prevent its
logical result from being committed a second time, and verify the selected artifact digest
before unlocking dependencies. A target-local compare-and-set cannot settle that global
cross-target choice. Missing verified replicated workflow state blocks risky reruns and
fan-in publication; cloud metadata cannot substitute for it.

## 9. File/module implementation responsibilities

All names below are proposed work ownership, not code created by this document.

| Work package | Proposed ownership | Existing integration seams |
| --- | --- | --- |
| Authority modes and clocks | `jarvis/fleet/authority.py`, `mode.py`, `clock.py` | topology identity, config loader, managed CLI/bootstrap, platform startup |
| Oracle transition/lease store | `jarvis/gateway/authority_store.py`, `role_service.py` | typed gateway interface; replace static identity/owner-token authority for v2 |
| Protocol/admission | `jarvis/protocol/leadership.py`, `admission.py`, receipt schemas | Hub auth/dispatch, node validation, local execution, target journal |
| Effect-sink guard | `jarvis/executors/context.py`, `guard.py` | actions, Remote/run_here, control, app_ui, terminal, storage/transfer, camera/remote sessions and runner |
| Coordinator lifecycle | `jarvis/coordinator/lifecycle.py`, handover orchestration | Console jobs, assistant/pipeline, provider sessions, private workload journal |
| Legacy adapter/cutover | `jarvis/runtime/migrate_authority.py`, supervisor bootstrap | cluster_proxy, CLI, Linux units, Windows interactive startup, old endpoints/tunnels |
| Mobile administration | gateway role API + `web/phone` MAIN panel | current paired auth; separate admin enrollment, fresh challenge, transition ID/status |
| Compatibility/recovery tests | subsystem tests in `tests/cluster`, `security`, `runtime`, `remote`, `mobile`, `intelligence` | real multi-process mixed-version and crash harness, not only mock Hub methods |

An effect context is internal and immutable: mode version, MAIN tuple, operation token, admission identity, target, digest, approval reference and allowed effect class. No boolean `trusted=True` or loopback header may substitute for it. Background autostart and standalone CLI calls must obtain or reject this context as well as web requests. Local owner emergency-stop paths are narrowly enumerated exceptions.

## 10. Proposed wire/API semantics

- `RoleStatus`: fleet mode, version, active/preferred device IDs, authority tuple, transition, coarse readiness profiles. No credentials or private payloads.
- `TransitionPreview`: requested action/candidate, expected tuple/policy, consequences, required readiness and whether expiry wait is needed. Preview grants no authority.
- `TransitionConfirm`: original admin operation token, client idempotency key, action/candidate, expected tuple/policy and fresh passkey proof bound to a short-lived challenge.
- `TransitionProgress/Cancel`: operate on that same transition ID; responses state old-valid-MAIN, no-MAIN or new-valid-MAIN rather than generic success.
- `FenceMode/FenceEpoch`: target-signed acknowledgement of durable mode/version and stopped new-work admission/session-control permits.
- `ConsumeAdmission`: target-authenticated request referencing MAIN-signed operation digest and exact current tuple. No body/prompt transfer to the metadata store.
- `ReconcileReceipt`: target status for accepted/uncertain operations, with result location/digest but no cloud-persisted output body.

Specify canonical encoding/signatures, replay protection, schema defaults and deadlines before writers ship. Signatures prove origin and integrity, not current ownership; admission still verifies mode/epoch. Transition mutations use compare-and-set and one active transition per fleet. Unknown schema, state or incarnation fails closed. Failed cloud metadata requests are not ordinary task dispatch authorization.

## 11. Delivery sequence and rollback

1. **H0 — Source/effect inventory and test model:** map every user/agent/bootstrap path; enumerate managed old services/launchers/state directories. Add a deterministic model test for no concurrent admission authority and crash injection at each journal/transaction boundary. Output a migration manifest.
2. **H1 — Guard-aware compatibility release, no role changes:** implement stable local mode store and effect context, initially preserving legacy behavior under a single explicit legacy lease/owner mode. Test direct CLI/local OS sinks and boot/downgrade handling. Rollback is removing compatibility wiring while still wholly legacy.
3. **H2 — Oracle metadata authority + v2 test fleet:** implement CAS transitions, leases/incarnations, tickets/receipts and clock adapters. Simulate partitions/sleep/restarts. No production promotion. Rollback drops the isolated test enrollment, not production identity.
4. **H3 — Controlled production cutover:** quiesce the old fleet; durably fence legacy dispatch; rotate old dispatch credentials, verify target acknowledgements and managed startup; grant Arch v2 MAIN. Execute one read-only canary. Gate: reboot Arch and prove every managed old path remains unable to act independently.
5. **H4 — Manual handovers/control-only:** Arch→Dell→Arch with typed controls. Test candidate failure before/after fencing, concurrent administrators and target pinning. Rollback is another valid v2 handover—not changing `main_device` back.
6. **H5 — AI/workload readiness integration:** prepared state cursors/provider logins, isolated mutating workspaces, receipt recovery and bounded accepted compute. Gate: loss of old coordinator cannot produce new agent tool actions. No claim of live session migration.
7. **H6 — Mobile MAIN management:** explicit `manage_main` enrollment, passkey challenge, preview/progress/cancel and recovery. Test phone connection loss and stale challenges; all ordinary requests remain MAIN-routed.
8. **H7 — Shadow then opt-in automation:** observe candidate choices without promotion; perform hard-off/partition/resume drills; then enable auto failover/failback by policy. Actual timings must fit the measured validity/readiness envelope before advertising ≤30-second recovery.
9. **H8 — Controlled legacy rollback drill:** disable v2 new admission/renewal, drain/reconcile accepted IDs and streams, wait out all v2 permits, fence/ack participants, stop authority dispatch, enter `ROLLBACK_QUIESCED`, then authorize exactly one legacy MAIN with a new mode version. Only then restore managed legacy dispatch credentials/startup. Offline v2 participants must remain excluded/quarantined until reconciled. Legacy bookmarks alone never initiate this transition.

A failed H3+ step can leave the fleet with no MAIN. Availability is deliberately sacrificed rather than reviving old authority. A reachable UI is not proof that ordinary command authority is restored. The legacy rollback procedure is a disaster tool to test, not two live parallel operation modes.

## 12. Falsifiable acceptance matrix

| Failure/test | Required observable result |
| --- | --- |
| Dell is v2 MAIN; invoke old managed Arch PWA, console, CLI, voice, owner-token hub and local tool wrapper | Rejected or proxied to Dell under v2; none acts independently |
| Reboot Arch with static config still saying main=Arch | Starts NODE/quarantined according to durable v2 mode; no legacy hub/new work |
| Use an alternate `--state` directory or downgrade managed release after cutover | Does not reset installation mode; incompatible release refused |
| Pause Arch process before handover, resume it afterward | Old tuple cannot consume new admission or renew; local tools cannot bypass guard |
| Suspend/hibernate Arch longer than lease; resume with local monotonic assumptions | Cached roles/permits invalid; no input/new work before online validation |
| Partition a target holding a session permit while transferring | Candidate activation waits target fence acknowledgement or bounded permit expiry; old input cannot overlap new control authority |
| Two phones confirm different candidates under the same expected epoch | One transition wins CAS; the other conflicts or observes the winning operation |
| Lose response after transition confirmation | Same operation returns existing transition; no second promotion/preference write |
| Crash before fence | Old valid MAIN may continue; preview/cancel creates no hidden role change |
| Crash after old fence but before candidate ready | Explicit no-MAIN; recovery requires fresh tuple, never old lease revival |
| Candidate canary fails | No ordinary work accepted by candidate; recover another eligible PC using fresh transition |
| Old operation prepared/consumed/accepted at each boundary before transfer | Ledger and local receipt identify its exact admission; no duplicate external effect on recovery |
| AI agent keeps running after demotion | Already-authorized computation may finish; fresh tool calls/child jobs denied, workspace preserved for review |
| Accepted GPU process outlives MAIN lease | Reservation held/quarantined until actual process completion/reconciliation |
| Old worker finishes after a pure-compute replacement on another target | Both may report attempt artifacts; only active MAIN journal may commit one logical result/unlock children; missing checkpoint blocks speculative rerun |
| Node locked or provider signed out | Correct readiness class; control-ready and AI-ready are not conflated |
| Approval queued before handover then approved afterward | Cannot consume stale epoch; re-present/cancel on approved PC |
| Oracle restart/restore while old grants exist | Fresh incarnation, old permits drained/invalidated, no restored live lease; old authority service proved fenced |
| Old backup predates a credential revocation | Previously revoked membership may reappear: accepted risk, clearly reported; it still cannot revive an old execution lease |
| Oracle unavailable | No new ordinary admission/promotion; emergency stop still works; accepted work follows completion-only policy |
| Full legacy rollback and then reboot a forgotten v2 participant | It cannot independently dispatch; must reconcile the new mode before rejoining |

Validate with both deterministic interleaving/model tests and a real multi-process fleet. Final evidence requires actual Arch sleep/hard-off, Windows Dell interactive-session execution and a real phone over the tailnet. Unit mocks or successful report diagrams do not certify handoff.

## 13. Decisions requiring owner confirmation before build

Recommended defaults are supplied so implementation can later proceed against a concrete contract:

- **Initial authority check:** online consume for each new ordinary operation; bounded short session permits for streaming input. This intentionally depends on Oracle availability. Reconsider only after latency measurements and protocol proof, not by weakening checks ad hoc.
- **Automatic mode:** disabled initially; manual handover/recovery first. Temporary MAIN selection defaults to hold until explicit return, unless the mobile preview explicitly selects automatic return.
- **Fallback profile:** control-ready manual recovery can precede AI-ready failover; UI must identify the reduced capabilities.
- **Readiness canary:** read-only health/target routing first; actual desktop mutation remains a separately authorized test.
- **Legacy coexistence:** retained UI may proxy; retained independent command authority may not. Unsupported unmodified old copies are outside managed-runtime guarantees; stronger OS isolation is a separate requirement.
- **Restore revocation:** accepted owner risk, not a blocker demanding an external revocation database. Record restore age and uncertain membership; retain non-revival of old leases.
- **Precise timing:** 5s renewal/15s lease/≤30s prepared recovery are starting targets. Choose and test per-platform clock/permit margins and network deadlines before promising them.

## 13A. Consolidated final-decision amendments

Canonical DESIGN.md sections16A.11–14 now control these details:

1. Initial catch-up is followed by a frozen-writer final workflow barrier. Replicate and
   durably acknowledge that exact sequence/hash before successor workflow continuation.
   Results arriving during the barrier remain on targets for successor reconciliation.
   Abrupt loss without a proven barrier leaves affected workflows uncertain, not automatically
   replayed; recovery of MAIN availability is distinct from full workflow continuation.
2. Stable request/workload/task IDs are separate from target-bound execution-operation IDs.
   A cross-target retry creates a new attempt/ticket only after reconciliation and policy
   checks; a client retry of the same execution keeps its original ticket and admission age.
3. Strict fleet-effect provider agents require mediated, epoch-checked tools and disabled
   unmediated builtin effects, validated per provider. Legacy unmediated process trees stop
   on demotion; this does not prove zero effects during the stop race. Accepted bounded
   sandbox computation is a separate class with no independent fleet rights.
4. Mobile disconnection preserves accepted-job identity/status but never replays interactive
   input. It does not change MAIN. See ARCHITECTURE.md and ACTION-PLAN.md for execution lanes
   and release gates. These are proposed contracts, not evidence of hardware verification.

## 14. Research sources and what remains unconfirmed

Accessed **2026-11-11**; primary sources only. The recommendations above are JARVIS-specific design inferences, not claims that these sources implement JARVIS for us.

- [etcd concurrency API](https://etcd.io/docs/v3.6/dev-guide/api_concurrency_reference_v3/): lock/election ownership is represented by a key/revision and checked in transactions; an external OS effect does not inherit transactional fencing automatically. Used as a correctness reference, **not a recommendation to add etcd**.
- [SQLite WAL](https://www.sqlite.org/wal.html) and [SQLite synchronous pragma](https://www.sqlite.org/pragma.html#pragma_synchronous): WAL/FULL supports the chosen local durability acknowledgement; WAL is not a network-filesystem multi-host writer solution. It does not create a transaction spanning Oracle and target PC.
- [Python time documentation](https://docs.python.org/3/library/time.html#time.CLOCK_BOOTTIME), [PEP418](https://peps.python.org/pep-0418/) and [Linux timekeeping](https://kernel.org/doc/html/next/core-api/timekeeping.html): monotonicity and suspend accounting differ; Linux BOOTTIME includes suspend. This matters directly for the reported Arch-sleep case.
- [Microsoft QueryInterruptTime](https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-queryinterrupttime) and [QueryInterruptTimePrecise](https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-queryinterrupttimeprecise): native elapsed-time APIs and their units/clock semantics require deliberate adapter selection. Exact installed Python/Windows behavior has **not** been tested here.

Unconfirmed: actual Oracle reachability/resources/clock/durability behavior; complete service/old-checkout inventory; OS protection against accidental downgrade; installed provider credential/session readiness; actual standby journal completeness; latency cost of online admission; hardware suspend/resume timing; all proposed protocol interleavings and real-phone handovers. No credentials were accessed, no real desktop/cloud actions were run, and no runtime implementation was made. Only this planning document was written.
