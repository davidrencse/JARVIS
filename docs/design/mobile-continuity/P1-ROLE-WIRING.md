---
status: implemented-partial
authorization: owner-requested-start-2026-11-11
---
# P1 role wiring — Remote target and confirmation station

Second P1 slice. Behavior-preserving. No authority, lease, fencing, ticket, election, config,
state-path, envelope or dispatch change. `Roles` stays descriptive: it grants nothing.

## What changed

| File | Change |
| --- | --- |
| `jarvis/fleet/legacy_topology.py` | New `confirmation_station(origin)` = `APPROVAL_ORIGIN.get() or origin`. `Roles.confirmation_station()` shares the same one-line rule (`_station`). Result unchanged. |
| `jarvis/topology.py` | Re-exports `confirmation_station`. |
| `jarvis/remote.py` `Remote.call` | `target = Roles(local, main, ORIGIN.get(), None).execution_target(device)` replaces `device or ORIGIN.get() or local`. `local, main` still come from `jarvis.topology.identity`. |
| `jarvis/pipeline.py` `_run_turn` | Two park sites use `confirmation_station(origin)` (was `APPROVAL_ORIGIN.get() or origin`). |
| `jarvis/assistant.py` `_ask` | Agent-task park uses `confirmation_station(origin)`. |

## Why this shape

- **Explicit origin, not `roles()`.** pipeline's `origin` argument may differ from the ORIGIN
  ContextVar (callers pass it; fallback is `remote.here()`). `Roles.confirmation_station()` would
  substitute ORIGIN, changing where items park. So the helper takes the already-resolved origin
  and reads only the approval override.
- **No identity read in the confirmation path.** The cluster-off path resolves `origin` via
  `Remote.here()` / Grokbot and may have no or malformed cluster config. `roles()` would raise
  there; `confirmation_station()` cannot.
- **Remote.call builds `Roles` from already-resolved identity** instead of calling `roles(config)`:
  `roles()` uses the legacy module's own `identity`, bypassing `mock.patch("jarvis.topology.identity")`.
  The tests pin that patch is still honoured. The cluster-off branch is untouched (explicit `here()`).
- Phone case preserved: `request_from(dell)` + `approvals_on(main)` parks on MAIN while the
  command still targets the Dell.

## Left explicit (not forced onto Roles)

- `assistant.confirmation_origin` (consumer side: `ORIGIN.get() or Remote.here()`) ignores
  `APPROVAL_ORIGIN`, as before. Items parked on an approval override are consumed from that
  station by its own origin binding; changing it would alter approval semantics.
- `ORIGIN.get() or Remote(...).here()` in `assistant.py` (3 places), `Remote.here()`, `resolve()`'s
  `main` alias, `is_node` and `run_here`'s `local == main` test: still inline; they need
  config-off handling or patch-compat and gain nothing from `Roles` yet.

## Limits

- `main_device` is still config; `Roles.coordinator` is not authority.
- The unknown-origin case stays: `request_from("phone")` with no explicit device returns
  `Unknown node 'phone'` (pinned in tests).
- The `assistant._ask` test needs many stubs; most likely to break on an `assistant.py` refactor.

## Evidence

`tests/cluster/test_fleet_role_routing.py` (58 tests, fakes only: `run_here`, `Client`, `dispatch`,
`Grokbot.here`; real ConfirmQueue/gates/registry/pipeline). Mutation checks: ignoring the approval
override fails 6 tests; swapping explicit/origin precedence in `Remote.call` fails 4.

Targeted runs (2026-11-11): tests/cluster 276, tests/mobile 184, tests/intelligence 508 (1 skip),
tests/desktop 433 (1 skip), tests/security 214, tests/runtime 396 (2 skip): all OK.
Parent full integration suite: `.venv/bin/python -m unittest discover -s tests -q`
ran 3061 tests in 231.544s, OK (7 skipped, 1 expected failure). Log:
`/tmp/jarvis-harness-role-wiring-suite.log`. Resource warnings were emitted; they are not
claimed resolved. Python restart needed; no restart or deploy done.

## Next P1 seams

1. Consumer-side `confirmation_origin` / `ORIGIN.get() or here()` as one `request_origin(fallback)` helper.
2. `Remote.here()`/`resolve()`/`run_here(local==main)` onto `Roles` once cluster-off fallback is modelled.
3. Registry, provider, state and phone facades.
