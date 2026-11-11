---
status: draft
approval: pending
---
# Consolidated action plan

Canonical contracts: DESIGN.md. Topology/concurrency: ARCHITECTURE.md. Detailed authority
migration: MAIN-HANDOFF.md (H0–H8). This is a design proposal; no code/deployment approval is
inferred. The final independent structural review is recorded in REVIEW.md; implementation evidence
and owner sign-off remain required.

## Outcome and immutable scope

Build one modular harness with one MAIN and capability-granted NODEs, including cloud machines.
Mobile/desktop/voice/CLI all use the MAIN for ordinary commands; Oracle NODE hosts stable
interface, pairing, resource/status metadata and narrow role authority. Personal AI state and
provider sessions stay on PCs. No mobile location features. Concurrent task-level compute,
not pooled RAM/VRAM or transparent process/session migration.

## Ordered work packages

| Package | Implementation scope (proposed files/modules) | Evidence before advancing | Rollback |
| --- | --- | --- | --- |
| P0 Inventory and threat/failure baseline | Existing topology/Hub/CLI/local effects, supervisors, data and provider stores; Oracle resources/network; current tests | Complete effect-path/old-process inventory, state-owner map and measured baseline; only credential locations read | No runtime changes |
| P1 In-place module contracts | Facades around topology, registry, Remote, provider lanes, state, phone/Console | Existing behavior parity; explicit caller/target/approval/coordinator separation; no port/state moves | Revert facade wiring |
| P2 Protocol/identity/durable modes | Stable installation identity, mode guard outside per-command --state, original tickets, attempts, receipts and negotiation | Reject stale epochs/tickets, target/digest changes, duplicate effects and downgrade/alternate-state bypasses | Remain exclusively legacy until cutover |
| P3 Authority prototype and worker concurrency | Oracle CAS authority in isolated test fleet; control/job lanes, local resource/workspace admission and process supervision | Crash/interleaving/suspend/partition tests; stop/control responsive during batch job; no live legacy/v2 dual authority | Remove test enrollment; legacy production unchanged |
| P4 Exclusive production cutover and manual controls | Guard-aware compatibility release, disable old managed paths, target fence acknowledgements, one v2 MAIN, gateway pilot | Reboot/old CLI/voice/console cannot independently execute; manual Arch->Dell->Arch control-ready tests; real phone->MAIN->Dell | Fresh controlled authority rollback; never re-enable both modes |
| P5 Concurrent compute MVP | Resource snapshots, deterministic scheduler/DAG, scoped artifacts, isolated attempts, global logical-result journal | Real Linux build and Windows test concurrently; pinning/Auto honored; outputs verified; no shared-index/VRAM corruption | Disable distributed placement; reconcile accepted attempts |
| P6 Prepared state and safe agents | PC-only state/workflow journals/replicas, final barrier, separate provider readiness, broker-mediated fleet-effect tools | Late old-MAIN journal write cannot be missed; missing proof pauses workflow; demoted real provider tool denied | Disable AI/workflow promotion and unsupported agent classes |
| P7 Mobile MAIN management | Role status/preview/confirm/status, manage_main+passkey, immutable transition IDs | MAIN-off mobile recovery, concurrent admins, stale proof, lost reply and invalid candidate tests | Disable mobile admin, retain authorized PC administration |
| P8 Shadow and opt-in automatic handover | Preference/failover state machine, sleep/resume invalidation, permit drain, journal barrier and reconciliation | Hard-off/partition/flapping/Oracle restart drills; prepared recovery measured; no stale new work or input replay | Disable automation and pin one valid MAIN using fresh authority |
| P9 Production soak and restore | PWA origin migration, signed updates, off-VM metadata/PC backups, privacy and resource limits | Real cellular phone disconnect/reconnect; mixed-version/reboot/restore; latency/egress measured; owner-approved soak | Reconcile accepted IDs, preserve receipts/patches; controlled rollback runbook |
| P10 Optional extensions | Extra cloud compute, specialized GPU runtimes, data-parallel adapters or gateway HA | Separate scope, private-data policy, cost approval, adapter tests and rollback | Disable extension without rewriting identities/contracts |

Mapping: P0–P4 cover earlier phases0–4/H0–H4; P5 covers5A; P6 covers5B/H5 and the final
review corrections; P7 coversH6; P8 covers6/H7; P9 covers7/H8; P10 covers8. Earlier numbering is
cross-reference only, not another work queue. Prototype tests may simulate barriers/leases;
production promotion waits for all required guards. Initial typed control-ready MAIN does not
claim AI/workflow continuity before P6.

## Review decision log

- Accepted residual risk: stale Oracle backup may restore a previously revoked identity.
- Required and incorporated: durable exclusive legacy/v2 authority cutover, original operation
  age, target-specific execution IDs, isolated writes, entity-specific schemas.
- Required and incorporated: frozen-writer final barrier and conservative abrupt-loss recovery.
- Required and incorporated: supported provider effect classes; guarded endpoints alone are
  insufficient for an independent CLI's own tools.
- Required and incorporated: mobile loss never changes MAIN, never replays inputs, and queries
  operation status before retry; accepted computation is separate from connection lifetime.
- Pending validation: every proposed security/timing/resource guarantee; actual Oracle/Windows
  behavior; provider mediation support; old-process inventory; complete PC-state audit.

## Release slices

A: single-authority infrastructure and manually recovered mobile controls (P0–P4).
B: actual concurrent cross-host compute (P5), without unsupported agent/failover claims.
C: prepared AI/workflow continuity and mobile role administration (P6–P7).
D: opt-in automatic recovery and operational sign-off (P8–P9).
These are incremental deliveries, not permission to silently omit the compute core.

## Definition of done

No feature is complete because it has a UI button or a mock test. Require contract/unit tests,
real adapters, adversarial process/crash/partition tests, a real-phone/real-PC acceptance run,
precise failure messages, observed receipts/outcomes, data-placement inspection, documented
rollback and a stated owner for each boundary. Claim measured performance only after measurement.
Do not run a full real-machine migration until the design is approved and deployment is authorized.

## Proposed owner sign-off

Accept or revise the topology, privacy boundary, concurrency classes/default limits, final-barrier
recovery policy, provider mediation restrictions and staged work packages. Design approval alone
is not authorization for credentials, paid services or deployment. No implementation has started
as part of creating this plan; the final audit found no remaining blocking structural defect; unverified production gates
remain explicit in REVIEW.md.
