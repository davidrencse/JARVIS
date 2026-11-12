# Program redundancy audit (Luna)

**Scope:** read-only review of config writers, receipts, liveness, phone projection, Lan Mouse
availability, archive loading, and provider/replay logic. This audit makes no production-code
changes. Existing working-tree edits were present during the review and were left untouched.

## Findings

### Config writes: one canonical writer, one useful adapter

- **Canonical owner:** [`jarvis/config_lock.py`](../../../jarvis/config_lock.py), especially
  `config_mutation`, `patch_config`, and `save_delta`. It owns the managed cross-process lock,
  drift checks, backup behavior, and atomic replacement.
- [`jarvis/fleet_management.py`](../../../jarvis/fleet_management.py) defines
  `patch_config_file`, but it delegates to `config_lock.patch_config` and translates the three
  lock exceptions into the fleet API's stable error codes. `_write_device` is a domain-specific
  patch operation and calls that adapter. This is not a second config-writing implementation.
- [`jarvis/settings.py`](../../../jarvis/settings.py) owns settings-specific validation and the
  `LOOSENS` confirmation policy, then delegates the write to `config_lock.patch_config`. This is
  another domain adapter, not an independent writer. Do not remove its confirmation policy or
  fleet role/tunnel guards.
- [`jarvis/cluster.py`](../../../jarvis/cluster.py) `save_json` writes node runtime state
  (`cluster-node.json`), not the managed user config. It is not redundant with config writes.

**Deletion:** none confirmed. Keep the fleet adapter unless its stable `FleetError` mapping is
 moved into an equivalent API boundary. A future consolidation may add narrow adapters on
`config_lock`; it must retain latest-on-disk mutation, guard-before-write, backup, drift detection,
atomicity, and existing error codes.

**Required tests before any refactor:** `tests/cluster/test_config_writers.py`,
`tests/cluster/test_fleet_management.py`,
`tests/web/test_ui.py` settings write coverage, plus lock contention, malformed config, guard
rejection, backup, concurrent unrelated-key preservation, and injected atomic-write failure.

### Receipts: similar names, distinct authorities; keep all three

- **Invite/enrollment retry fence:** [`jarvis/fleet_management.py`](../../../jarvis/fleet_management.py)
  `_load_receipts` / `_store_receipts` / `mutate_receipts` / `assert_node_admitted`, alongside
  [`jarvis/cluster_setup.py`](../../../jarvis/cluster_setup.py) invite recovery. The file records
  uncertain invite attempts and blocks duplicate enrollment for a node.
- **Authority admission ledger:** [`jarvis/continuity/authority.py`](../../../jarvis/continuity/authority.py)
  `AuthorityStore.consume` records one use of an admission ticket and reconciles retries across
  a handover.
- **Target effect journal:** [`jarvis/continuity/executor.py`](../../../jarvis/continuity/executor.py)
  `ExecutorGuard` stores local effect states before calling the sink, preventing an uncertain
  effect from being restarted.
- **Ordinary cluster job results:** [`jarvis/cluster_store.py`](../../../jarvis/cluster_store.py)
  `Broker.finish` is an acknowledgement/result path, not an enrollment or authority receipt.

These records have different writers, keys, lifetimes, and failure semantics. No deletion or
shared receipt abstraction is justified. In particular, do not collapse uncertainty fences into
a generic “receipt” module.

**Required tests for future receipt changes:** `tests/cluster/test_fleet_management.py`,
`tests/cluster/test_cluster_setup.py`, `tests/continuity/test_authority.py`,
`tests/continuity/test_executor.py`, and cluster-store lost-ack/retry tests. Keep the invariants:
same identity reconciles; changed content conflicts; ambiguous external effects never replay.

### Liveness: canonical word exists; one projection should be considered for consolidation

- **Canonical vocabulary and busy derivation:** [`jarvis/connections.py`](../../../jarvis/connections.py)
  `presence(seen, busy, now)` defines `online | busy | offline`; `busy_nodes` is a read-only broker
  query. `fleet_management.inventory` already imports these.
- **Duplicate projection:** [`jarvis/phone.py`](../../../jarvis/phone.py) fleet projection
  (`PhoneServer` fleet code near line 1075) has an explicit `"online" if online else _presence(...)`
  branch. Where a broker row exists, this is logically equivalent to calling
  `connections.presence(seen, busy, now)` because broker `online` is derived from the check-in
  window, but it repeats the rule and can drift. Its special fallback when there is only a
  `shown` row and no broker row is meaningful and must remain explicit.
- [`jarvis/fleet_management.py`](../../../jarvis/fleet_management.py) inventory should be checked
  for its own representation: public API `online`/`presence` fields are not automatically
  interchangeable. `busy` is deliberately not proof of online presence.

**Safe candidate:** in a follow-up patch, use `connections.presence` for the phone path when a
broker row is present, retain its no-broker fallback and the phone's `presence=None` meters
projection for `busy`. Do not delete either exported field or alter thresholds. This is a small
consolidation candidate, not a confirmed safe deletion today.

**Required tests:** `tests/cluster/test_surface_liveness.py`, phone fleet tests
(`tests/mobile/test_phone_fleet.py`), and `tests/cluster/test_integrated_fleet_gaps.py`; cover
fresh check-in, stale-but-running job, expired job, no broker row, and the exact online cutoff.

### Phone projection: single owner, no duplicate found

- **Canonical owner:** [`jarvis/phone.py`](../../../jarvis/phone.py) `redact`, `project_step`,
  `request_usage`, and `Trace`. Call sites are in the phone trace event handling and request result
  path in the same module.
- Search found no second Python implementation of the phone's step/detail/usage projection.
  Browser code consumes the already-projected phone payload; it is presentation, not another
  privacy boundary.

**Deletion:** none. Do not remove redaction, URL/path/secret filtering, allowlisted detail rules,
or numeric usage filtering as “duplicated sanitization.”

**Required tests for changes:** `tests/mobile/test_phone.py`, `tests/mobile/test_phone_web.py`,
`tests/mobile/test_phone_e2e.py`; ensure secrets and sensitive details remain absent from serialized
phone events.

### Lan Mouse availability: layered probes and gates are intentional

- **Process/build observation:** [`jarvis/sharedinput.py`](../../../jarvis/sharedinput.py)
  `lan_mouse_status` is the detailed cross-platform probe. `_quick_status` is a cheaper polling
  probe used on Windows to avoid repeated PowerShell/CIM work. They are intentionally different
  cost/accuracy contracts.
- **Sharing availability projection:** `sharing_availability` reports proximity, apart/unknown,
  pause, and guard memo; `with_switch` applies the saved enable switch.
- **Action gates:** `start_blocked` and `pair_blocked` share the projection but have deliberately
  different strictness: pairing refuses settled-apart before writing configuration, while a start
  path accounts for current uncertainty. `peripherals.py`, `probe.py`, and `remote_view.py` consume
  these functions.

**Deletion:** none confirmed. Do not merge `lan_mouse_status` with `_quick_status`, or collapse
`start_blocked` and `pair_blocked`. Their call sites and tests show distinct latency and safety
contracts.

**Required tests:** `tests/remote/test_lanmouse_guard.py`,
`tests/remote/test_sharedinput.py`, `tests/remote/test_lanmouse_fuzz.py`,
`tests/desktop/test_platform_backends.py`, plus the remote-view lifecycle tests. Preserve manual
override, pause/resume, unknown proximity, Tailscale exclusion, and elevated Windows behavior.

### Archive loading: one authoritative loader, no orphan identified

- **Canonical note and chunk loader:** [`jarvis/second_brain.py`](../../../jarvis/second_brain.py)
  `SecondBrain._load` calls `parse_note`, then `_assemble` verifies chunk manifests, bounded size,
  per-chunk hashes, whole-payload hash, and frontmatter agreement.
- [`jarvis/second_brain_runtime.py`](../../../jarvis/second_brain_runtime.py) owns scheduling,
  status, CLI, and calls into `SecondBrain`; it does not implement an alternate note parser.
- [`jarvis/second_brain_queue.py`](../../../jarvis/second_brain_queue.py) is the SQLite capture,
  queue, and drain implementation. It constructs `SecondBrain` for publishing; that is a separate
  archive workflow, not a duplicate loader.
- `SecondBrain._Source` only owns generator/snapshot-connection cleanup; it is not another loader.

**Deletion:** none. Keep validation centralized in `SecondBrain._load` / `_assemble`; do not add
parallel runtime or queue parsing. No dead archive loader found in the inspected call graph.

**Required tests:** `tests/runtime/test_archive_cli.py`,
`tests/runtime/test_archive_history_faults.py`, `tests/runtime/test_second_brain_runtime.py`,
`tests/data/test_second_brain.py`, `tests/data/test_second_brain_overflow.py`, and
`tests/data/test_second_brain_storage.py`. Preserve
truncated/missing chunks, wrong hashes, path containment, bounds, and failure-to-ack behavior.

### Provider retries and replay: separate guarantees, no safe deletion

- **Provider quota failover:** [`jarvis/inference.py`](../../../jarvis/inference.py) `Failover`
  moves an ask from ChatGPT to Claude when limits permit. It tracks tool starts/results so a
  fallback is not launched after a tool may have had an effect. This is provider-lane policy.
- **Orchestration recovery:** [`jarvis/orchestrator.py`](../../../jarvis/orchestrator.py) persists
  wave dispatch/results and resumes task IDs. It prevents a memory-indexing or response-loss fault
  from creating duplicate work.
- **Continuity replay fence:** `AuthorityStore.consume` and `ExecutorGuard` handle signed
  admission and effect execution; `gateway.py` authenticates/relays, `ingress.py` owns phone
  request idempotency, and `runtime.py` coordinates lifecycle. Their repeated identity/digest
  validation is boundary validation, not a single duplicated replay implementation.
- `jarvis/continuity/gateway.py` documentation says the module is not imported by the running
  application; `runtime.py`, `executor.py`, and `ingress.py` also document prototype/unwired
  status. They are not orphaned: tests and the continuity docs exercise this isolated design.

**Deletion:** none. Do not conflate provider failover (which may repeat a pure model request only
before a tool start) with operation replay or effect reconciliation. Do not delete the isolated
continuity modules solely because they are not wired into production; they are active prototype
surface with dedicated tests/docs.

**Required tests:** `tests/runtime/test_provider_limits.py`,
`tests/intelligence/test_orchestration_lanes.py`, `tests/intelligence/test_plan_recovery.py`,
`tests/intelligence/test_inference_started_tools.py`,
`tests/continuity/test_authority.py`, `tests/continuity/test_executor.py`,
`tests/continuity/test_ingress.py`, `tests/continuity/test_gateway.py`, and
`tests/continuity/test_operational_e2e.py`. Preserve no-fallback-after-tool-start, same-attempt
reconciliation, changed-digest conflict, and uncertain-effect non-replay.

## Deletion plan

No production deletion is confirmed by this audit. The only concrete cleanup candidate is the
phone's broker-row liveness branch, which can likely call the canonical `connections.presence`
helper while preserving its no-broker fallback and API projections. Sonnet should make that as a
small follow-up change only with the liveness and mobile fleet tests above. The remaining apparent
duplicates are wrappers, projections with separate consumers, or independent safety ledgers.

Before any deletion, use `rg` to re-check every named symbol's callers and compare the current
working diff; the repository already contained uncommitted changes during this audit.
