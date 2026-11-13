# Continuity design

*Draft plan for one leased PC MAIN, capability-granted nodes, and a tailnet-only Oracle gateway. Not owner-approved and not permission to deploy, spend, or cut over.*

Entity field schemas live in [design.json](design.json). This file does not repeat them. Protocol detail is [main-handoff.md](main-handoff.md). The isolated prototype's contracts are [continuity-contract.md](continuity-contract.md) and [continuity-runtime.md](continuity-runtime.md). Personal storage is [obsidian-storage.md](obsidian-storage.md). Operator runbook intent is [docs/handbook/continuity.md](../handbook/continuity.md).

## Status

The design is coherent and still a draft. Approval of the topology is not authorization for credentials, paid services, or a real-machine migration. Isolated modules under `jarvis/continuity/` exist and are exercised in temp stores. They are not production failover. `cluster.main_device` stays the live unfenced MAIN until an explicit managed cutover. Automatic failover and failback stay off. Default installed behavior is legacy.

Production composition is not a release. A controller factory can be built from a local operator file in tests. Live Zenbook/Dell/Oracle cutover is not available: matched fingerprints, per-PC operator files, a reachable successor route, and a distributed owner-proof path are still required. See [main-handoff.md](main-handoff.md).

## Invariants

- Exactly one leased PC MAIN, or an explicit none. Oracle is a restricted tailnet-only gateway, lease store, and metadata relay. It is not an AI host and not a personal store.
- Ordinary work is client → gateway → MAIN → executor. Scoped artifact movement is a data plane and carries no scheduling rights. Role recovery is the narrow path allowed when there is no MAIN.
- Capabilities are the intersection of what the node was granted and what the action requires. A phone pairing or a `manage_main` grant is not a PC approval station. Risky OS actions still need the PC.
- Personal AI state and provider sessions stay on PCs. A public Funnel is not required.
- Authority is fenced by epoch, expiry, operation identity, and executor checks. A new MAIN does not refresh an old ticket's age.
- Compute is task-level. It is not pooled RAM or VRAM and not live process migration.
- No mobile location product.
- Changes stay incremental and reversible. Both legacy and v2 must never be live authorities at once.

## Topology and modes

Oracle gateway and metadata authority start as one deployable with one SQLite writer. MAIN is a role on an eligible PC and may use that PC's local executor. A standby PC may hold verified replicas. It does not schedule without MAIN authority.

Operator-requested edges:

| From | To |
| --- | --- |
| `LEGACY_ACTIVE` | `PREPARING_V2` |
| `PREPARING_V2` | `QUIESCED` or back to `LEGACY_ACTIVE` |
| `QUIESCED` | `V2_ONLY_NO_MAIN` or back to `LEGACY_ACTIVE` |

`V2_ACTIVE`, `HANDOVER`, and `V2_ONLY_NO_MAIN` move through grants, transitions, and release, not a free `set_mode`. `V2_ACTIVE` is not an operator-settable mode and cannot grant a lease by itself. Rollback uses only `ROLLBACK_QUIESCED`. A corrupt store becomes `QUARANTINED` and does not fall open. A stale Oracle backup can restore a previously revoked identity; the owner accepted that residual risk. Restored leases and incarnations must not revive.

## Identity, tickets, clocks

Signature namespaces are `jarvis-continuity-device-v1`, `jarvis-continuity-owner-v1`, and `jarvis-continuity-ticket-v1`. They are not `jarvis-code`.

One ticket field tuple, owned by `authority.TICKET_FIELDS`: `admission_id`, `target`, `operation_id`, `attempt_id`, `digest`, `main`, `incarnation`, `epoch`, `issued_at`, `admit_until`, `issuer_incarnation`. Ticket signature cap is `MAX_SIGNATURE` 1800. Admission lifetime is at most 24 hours and is immutable. Issuance more than 5 seconds ahead of the wall clock is `bad_ticket`.

Stable request, workload, and task ids are separate from target-bound execution-operation ids. A retry on a new target gets a new execution ticket under the same logical task. The old ticket is not edited.

Linux cached validity uses `CLOCK_BOOTTIME`. Windows and macOS have no equivalent clock in this design (`clock=None`) and must validate online. Tuple order is lexicographic `(incarnation, epoch)`.

## What concurrency means

One MAIN may schedule an independent Linux build and a Windows test at the same time, then a report after both results commit. Each attempt has immutable inputs, isolated outputs, a local admission receipt, and its own resource reservation. Scheduling decisions and global result commits serialize through the MAIN's private journal. Two targets reporting success does not publish the logical result twice.

A user-pinned target is a hard constraint. Cross-worker placement requires an explicit Auto choice. No speculative duplicate external effects. Pure tasks may retry only after reconciliation. Effects are never blind-replayed.

| Lane | Rule |
| --- | --- |
| Membership / lease | One active MAIN and one active transition. No promotion without authority. |
| Routing | Proposed 32 pending metadata operations per device. Relay age 10 seconds. No private disk spool. |
| Workflow | One logical writer. Barrier and cursor before takeover. Uncertain workflows pause. |
| Interactive | Bounded ordered input. Reserve capacity ahead of batch work. Lost owner presence releases input. No input replay. |
| Batch | Start at 1 batch task per PC and 2 fleet-wide. |
| GPU | 1 admitted task per GPU domain. No fake aggregate VRAM. |
| Artifacts | Proposed 2 transfers per fleet. Resume validated ranges. Verify the manifest before publication. |
| Providers | 1 session per provider per PC, inside provider quota. Unmediated sessions stop on loss. Strict fleet-effect agents need mediated, epoch-checked tools. |

These limits are proposals, not measurements. `SAFETY_MARGIN` of 2 seconds is an unmeasured placeholder. Also proposed, not measured: lease renew about 5 seconds and expiry 15 seconds; prepared recovery at most 30 seconds; failback stability 30 seconds; invitation TTL 10 minutes; workload scheduling lifetime 7 days; metadata retention 30 days and receipts 90 days; relay frames at most 2 streams and 2 MiB, newest frame only; camera owner-presence grace at most 15 seconds; preserve about 25% RAM. Worker budget is the minimum of the owner grant and real headroom. Never reserve more than the physical domain has. Control must stay responsive while a long build runs.

## Placement

- Oracle: gateway API, metadata, lease, bounded in-memory relays, encrypted off-VM metadata backup. No inference, prompt or file persistence, provider credentials, or personal memory.
- Each PC: fleet agent, control lane, admission and receipt journal, isolated executors. Provider and personal-state modules turn on only when that PC is ready.
- MAIN PC: scheduler and the sole current workflow writer. Its own OS effects use the same executor admission as a remote target.
- Linux supervision is a service plus process groups. Windows background compute is a tested process tree. The desktop executor stays in the interactive user session, never Session 0.
- Phone disconnect keeps the job identity, does not replay input, and does not change MAIN. The client asks operation status before a retry. Accepted computation is separate from the connection lifetime.

Four situations, as proposed behavior, not a live monitor:

1. Arch online and leased: Arch is MAIN. The phone and the Dell send ordinary work there.
2. Arch off, Dell promoted through the authority path: Dell is MAIN. Arch's old permits are fenced. The gateway does not invent a MAIN.
3. The phone loses signal: MAIN stays Arch. In-flight input is not replayed.
4. No eligible MAIN: the gateway is not a scheduler. Recovery is the narrow authority path only.

## Work still in front of a release

Ordered packages. A later package does not start because an earlier UI exists. Prototype tests may simulate barriers. Production promotion waits for the guards that package names.

| Package | Done means |
| --- | --- |
| P0 | Effect-path inventory, state-owner map, measured baseline. No runtime change. |
| P1 | Facades around today's topology, registry, Remote, providers, state, phone, and console. No port or state move. |
| P2 | Stable installation identity, mode guard outside per-command `--state`, original tickets, attempts, receipts. Stale epochs, changed targets, duplicate effects, and downgrade bypasses are rejected. Stay legacy until cutover. |
| P3 | Oracle CAS authority in an isolated test fleet. Crash, interleaving, suspend, and partition covered. No live dual authority. |
| P4 | One v2 MAIN. Old managed paths cannot execute on their own. Manual Arch → Dell → Arch. Rollback never re-enables both modes. |
| P5 | Real concurrent cross-host compute. Pinning and Auto honored. Disable placement to roll back. |
| P6 | PC-only journals, final frozen-writer barrier, provider readiness separate from "control ready". A late old-MAIN write cannot be missed. A demoted provider tool is denied. |
| P7 | Mobile role preview and confirm with `manage_main` plus a passkey. Phone admin can be turned off without losing PC admin. |
| P8 | Opt-in automatic handover, including sleep and resume invalidation. Drills show no stale new work and no input replay. Automation can be pinned back to one MAIN. |
| P9 | Real phone disconnect, mixed versions, restore, measured latency and egress, owner-approved soak. |
| P10 | Optional extra cloud, GPU, or gateway HA. Separate scope and cost approval. |

Release A is P0–P4 (manual recovery). B is P5 (compute, no failover claim). C is P6–P7. D is P8–P9. Initial control-ready MAIN does not claim AI or workflow continuity before P6.

A feature is done only with contract tests, real adapters, crash and partition tests, a real-phone and real-PC run, precise failures, inspected data placement, a rollback, and a named owner for the boundary. Claim a number only after it is measured.

## Decisions kept

- Durable exclusive legacy/v2 cutover, original operation age, target-specific execution ids, isolated writes, and entity-specific schemas (those schemas are in `design.json`).
- Frozen-writer final barrier and conservative recovery after an abrupt loss.
- Guarded HTTP endpoints are not enough for an independent CLI that has its own tools. Supported provider effect classes must be mediated.
- Mobile loss never changes MAIN and never replays input.
- Rejected: pooled memory, live process migration, Oracle as MAIN or as the personal store, a phone as the approval station, a location product, and running legacy and v2 authorities together.

Pending validation: every timing and resource number above, actual Oracle and Windows behavior, provider mediation, the old-process inventory, and a complete PC-state audit.
