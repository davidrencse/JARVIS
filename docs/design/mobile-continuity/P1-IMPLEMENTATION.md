---
status: implemented-partial
authorization: owner-requested-start-2026-11-11
---
# P1 implementation note — legacy topology facade

Scope: first slice of P1 ("in-place module contracts") from ACTION-PLAN.md. Behavior-preserving
only. No authority, lease, epoch, ticket, persistence, port, provider or election change.

## What changed

- New `jarvis/fleet/legacy_topology.py`: the implementation formerly in `jarvis/topology.py`
  (`enabled`, `identity`, `endpoint`, `request_from`, `approvals_on`, `ORIGIN`,
  `APPROVAL_ORIGIN`, `DEVICE_ID`, `DEFAULT_PORT`) moved verbatim, plus a read-only
  `Roles` snapshot and `roles(config)`.
- `jarvis/topology.py` is now a re-export shim. `is_node` stays there and composes the
  module-level `enabled`/`identity`, so `mock.patch("jarvis.topology.identity")` still reaches it.
  `import socket` stays so `mock.patch("jarvis.topology.socket.gethostname")` keeps working.
- No other production file changed; all ~60 call sites still import from `jarvis.topology`.

## Role separation (names only, from what the legacy API exposes)

| Role | Legacy source | Facade name |
| --- | --- | --- |
| Coordinator | `cluster.main_device` (static config) | `Roles.coordinator` |
| This PC | `cluster.local_device` / unique hostname match | `Roles.local` |
| Caller/origin | `ORIGIN` ContextVar via `request_from` | `Roles.origin` |
| Approval station | `APPROVAL_ORIGIN` via `approvals_on` | `Roles.approval_station` |
| Execution target | rule inline in `remote.Remote.call`: `device or ORIGIN or local` | `Roles.execution_target(explicit)` |

`Roles.confirmation_station()` = approval override, else origin, else local. This matches
`pipeline.py`/`assistant.py` (`APPROVAL_ORIGIN.get() or origin`, where `origin` itself falls
back to the local PC when the cluster is on). Those callers were not rewired; they still compute
the rule inline.

## Limits (read before relying on this)

- **Legacy-only.** `main_device` is config, not a lease: two PCs with different configs can both
  report themselves coordinator, and editing the value changes roles instantly. Nothing here
  enforces single-MAIN, fencing, original-ticket age, target-bound execution IDs or approval-station
  binding. That is P2+ (protocol/identity) and P3/P4 (authority, cutover).
- `Roles` is descriptive. It grants nothing and must not gate effects.
- Patch-point change: `topology.identity`, `enabled`, `endpoint` and the ContextVars are now
  re-exported names. Patching them no longer alters facade internals (only `is_node` follows
  `enabled`/`identity`). Nothing in the repo patches them except `enabled`, `identity`, `is_node`
  and `socket.gethostname`, all verified.
- `roles()` does not check `enabled`; it raises exactly what `identity` raises when unconfigured.
- Not done: wiring `remote.py`, `pipeline.py`, `assistant.py` to `Roles` (outside P1 file ownership;
  a later step once those files are free); registry/Remote/provider/state/phone facades.

## Parity evidence

`tests/cluster/test_fleet_contracts.py` (39 tests, fake only) embeds a verbatim copy of the
original `topology.py` as oracle and compares value or exception type+message over 27 `identity`
configs, ~40 endpoint URLs, `is_node` over 7 configs x 4 hostnames, `enabled`, `DEVICE_ID`;
checks ContextVar names/identity, nesting/exception restore/thread isolation, patch points, and
`Roles` precedence rules. Mutation spot-checks (swapped precedence, loosened endpoint, replaced
`socket`) each failed a test.

Run: `.venv/bin/python -m unittest tests.cluster.test_fleet_contracts`.
Compatibility set run with it: cluster, cluster_setup, cluster_roles, cluster_hardening,
cluster_proxy, cluster_validate, audit_cluster, mobile phone + phone_e2e, desktop app_ui,
intelligence pipeline/interpret/brain_audit, security confirm: 444 tests OK.

Parent integration verification (2026-11-11): `.venv/bin/python -m unittest discover -s tests -q`
ran 2999 tests in 231.269s, OK (7 skipped, 1 expected failure). Log:
`/tmp/jarvis-harness-p1-suite.log`. The run emitted resource warnings; a passing result is not
a claim that all resource-lifecycle warnings have been resolved. Parent review also clarified
module documentation about re-export patch limits; no functional edits followed the test run.

Python processes need a restart to load these changes, and nodes need the normal tested
release update. Neither restart nor deployment was performed. This verifies this P1 slice
only; the rest of P1 and P2+ remain incomplete.
