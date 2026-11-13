# Main handoff implementation session — Team A

Date: 2026-11-13. Scope: authority, runtime, recovery, renewal/clock,
lifecycle, executor, participant, and core regression coverage. This note is
for the coordinating lead; it does not authorize or record a live role change.

## Starting evidence

- The repository already contains a substantial isolated continuity protocol,
  adapters, and fake-backed drills. The implementation notes and
  `MAIN-BOOTSTRAP-BLOCKERS.md` explicitly distinguish these from a working
  production handoff.
- `startup_ready` is unavailable without the production startup/controller
  factory. CLI role publication still reads raw config; owner proof for a
  return from Dell is not distributed; the Dell lacks a verified reverse
  successor route. Therefore a tested library transition is not evidence that
  Arch → Dell → Arch is live-safe.
- This session will only repair the Team A-owned core modules and tests. Team B
  owns startup, integration, gateway, CLI, repointing and operational scripts.
- Existing unrelated dirty phone files remain outside this scope.

## Acceptance boundary

No live authority, service, role, node release, infrastructure, host power,
mouse or keyboard action is part of this implementation pass. Fixtures and
isolated adapters can establish protocol behavior only. After integration, the
coordinating lead must confirm all production blockers are closed, run focused
and full tests, then coordinate the authorized live Arch → Dell → Arch
roundtrip separately. Until then, report production handoff as blocked.

## Worker report index

The lead will append the ten Grok 4.7 worker outcomes here after their async
completion notifications. Each entry should include files changed, exact
focused test command/result, and unresolved blocker(s).

### E2E audit (readonly)

- No files changed. The `RoundTripAdapters` and `test_operational_e2e` suites
  provide fake-transport and temp-loopback drills only; they do not verify
  Oracle, Dell, Tailscale, production `Hub.start`, or the startup controller.
- The two auto-recovery tests pass through a hand-built `Participant.tick()`;
  they do not configure production or test `start_bound`. Their participants
  lack a canary, and the missing-canary path currently counts control readiness
  as success. The lease-renewer background thread is not exercised.
- CLI return-prepare/refreeze are not tested by the adapter drill, and there is
  no CLI retire action for interrupted archive state. Several recovery/return
  edge branches remain uncovered (including `archiving`, symlink archive,
  confirmation requirement, and WAL/SHM names).
- The two focused auto-recovery tests passed (2 tests, 8.110s). The worker did
  not run either complete module. Commands supported for isolated verification
  are `tests.continuity.test_roundtrip_adapters` and
  `tests.continuity.test_operational_e2e` with `JARVIS_NO_MODELS=1` and Codex
  environment variables stripped. Do not run handbook live commands while
  `MAIN-BOOTSTRAP-BLOCKERS.md` still marks production composition unavailable.

### Independent core review (readonly)

Reviewed at `fb08c1b`; no edits or tests. Cross-check these against the owners'
completion diffs before integration:

1. Remote `SafeHandoff` uncertain callback path cannot pause candidate renewal:
   `RemoteRuntime` has no pause action, so a not-ready lease may keep renewing
   and the handoff remains stuck. Add a remotely delivered pause and a
   regression that ticks the candidate renewer after uncertainty.
2. `RoleLifecycle.prepare_successor` closes `ExecutorGuard` before
   `role_autostart`; a failed start drops the local journal, so retry fails
   `target_unfenced`. Close the guard only after successful role start; retain
   state after an exception.
3. `SelfRecovery._activate` treats a missing canary as success when control
   readiness passes. Missing canary must fail closed; test configured-main
   self-recovery with no canary.
4. `SelfRecovery._refresh` can preserve an old enabled policy indefinitely
   after refresh errors. Disable once the last successful policy is stale.
5. Smaller: successor repoint writes node/endpoint state before tunnel
   activation; successor grant acceptance renews before checking grant
   readiness, potentially extending a rejected grant; `CachedAuthority.observe`
   advances the seen tuple before validating `valid_for`, leaving a rejected
   higher tuple to poison later observations.

Production limits reaffirmed: no production startup/controller factory,
distributed return owner proof, or verified reverse route; live config remains
the role source; authority leases are in RAM; Windows/macOS suspend-aware cache
timing remains unverified. This review does not establish live handoff
readiness.

### Executor drain and effect admission

- Files: `jarvis/continuity/executor.py`, `tests/continuity/test_executor.py`.
- A ready-looking status no longer starts a sink when an open transition is
  already visible, or when drain, handover, or a lapsed lease lands before
  consume returns. The post-consume status read matters because an identical
  admission replay does not itself recheck drain. `no_admission` leaves
  `PREPARED` or `CONSUMED` unchanged and is repeatable. `ACCEPTED` stays
  uncertain across drain and a journal reopen, and is not run again.
- A truthy non-bool `main_ready` or `lease_valid` does not admit. A regressed
  tuple still does not lower the high-water mark or consume.
- Focused command: `JARVIS_NO_MODELS=1` with `CODEX_*` unset,
  `.venv/bin/python -m unittest tests.continuity.test_executor`.
  Result: Ran 37 tests in 0.075s, OK.
- Not live failover. Store replay of an already-consumed ticket still ignores
  drain; the guard now refuses from a second status read. A revoke that lands
  after `ACCEPTED` is written can still overlap the running sink.

### Renewal and clock

- Files: `jarvis/continuity/clock.py`, `jarvis/continuity/renewal.py`,
  `tests/continuity/test_clock.py`, `tests/continuity/test_renewal.py`.
  Integration, runtime, and authority were not edited.
- A refused `observe` no longer advances `seen` or drops a still-valid grant.
  Bool and non-int tuples, non-bool `ready`, and non-finite request starts are
  rejected. An older answer cannot extend a newer shorter deadline. A pid
  change voids the grant. On Linux, a BOOTTIME−MONOTONIC gap above 0.05s voids
  it before the deadline. The host adapter test takes one quiet sample and
  does not claim a suspend was measured. `windows_clock_untested` remains:
  Windows still has expiry plus an explicit invalidate only.
- Suspend (`runtime._block == 'suspend'`) does not renew, recover, or sync
  services. A restarted runtime with no in-memory lease does not call
  `renew_once`. Waits stay at most 5s and at most half of a positive
  remaining, with the 0.5s floor unless that half is shorter. A finished
  `close` can `start` again. A join that times out still refuses `start`.
- `SelfRecovery` no longer treats a missing canary as success, and a policy
  refresh that fails after the 300s TTL drops the enabled policy.
- Focused command: `JARVIS_NO_MODELS=1` with `CODEX_*` unset,
  `.venv/bin/python -m unittest tests.continuity.test_clock tests.continuity.test_renewal`.
  Result: 43 tests OK (28 clock, 15 renewal) in the combined 74-test run that
  also ran `tests.continuity.test_runtime` (31 OK, caller check only).
- Not a live suspend, reboot, or Arch→Dell handoff. No production startup
  factory, and the Dell clock is still untested on hardware.

### Concurrency audit (readonly)

- `test_audit_races` + `test_executor_concurrency`: 31 tests passed in 0.163s;
  no edits. The current tests still miss these forced races:
  1. `ExecutorGuard.close` ignores an admit blocked in `consume` before
     `ACCEPTED`; it can close `_db`, leave `PREPARED` on disk, then a reopened
     guard runs the sink.
  2. `begin_drain` can overwrite a `suspend` latch after busy release, and
     `_drain_finished` can clear a non-drain latch, enabling renewal.
  3. Participant revocation after the second successful `consume` but before
     `ACCEPTED` is not checked by the second status read; sink can run.
  4. Interactive stop during the second `consume` leaves `CONSUMED` open and
     wedges retries as `in_progress`.
  5. `_HandoffJournal` with `path=None` lacks a lock; concurrent claimers can
     both claim the same hook.
- Suggested regressions: park `consume` around close / stop / revoke; release a
  busy suspend through a drain; and synchronize two in-memory `brain` claims.
  Also pin the observed `PREPARING_V2` overlap (legacy still open during
  acknowledgement) so it does not spread to a fenced install.
- Additional paths that held under forced interleavings: suspend racing a
  parked renewal, local admission closing during a parked status read, and v2
  executor fencing while `acknowledge` is parked. `ManualRecovery._cancel`
  retries after failed cancel but does not clear a not-ready grant on that
  failure. Brain bundle tests do not race exports/sweeps.
- Do not claim these as fixed until cross-checked against active executor and
  runtime changes. No live transition is established.

#### Coordinator repairs after audit

- Fixed the executor close race by making `close()` wait for active admissions,
  including those parked in authority calls, as well as running sinks. Fixed
  interactive stop during consume by transitioning a `CONSUMED` receipt to
  terminal `RELEASED` before returning the refusal.
- Added regressions for close during a parked consume (then reopen/replay) and
  interactive stop during the second consume. Focused
  `tests.continuity.test_executor`: 39 tests passed in 0.141s.
- Remaining concurrency audit finding: participant revocation after an
  identical consume returns but before local `ACCEPTED` is not visible in
  direct-store status. This needs an explicit protocol ordering decision. A
  successful consume can be defined as the authority-side admission
  linearization point, making later revocation stop subsequent admissions while
  already consumed work drains; making revocation win after that point requires
  a new protocol step and still needs its own linearization point. No change was
  made to this behavior pending that decision.

### Participant acknowledgements

- Files: `jarvis/continuity/participant.py`,
  `tests/continuity/test_participant_channel.py`.
- A fence RPC now acknowledges the epoch the transition opened under
  (`expected_epoch`), including a replay while status is already `FENCING`.
  It signs that closed epoch with the installation key and returns the same
  receipt on an identical replay without a second authority write. A replay
  after `fence()` advances the epoch does not acknowledge the new MAIN's
  epoch. `CandidatePort` collects peer acks only while the transition is
  `DRAINING`, and a receipt bound to a different epoch blocks the owner step.
- Role and phase failures (wrong requester, candidate drain/release, source
  accept, fence before drain, fence after grant) and a guard `revoked`
  failure raise before any acknowledge.
- Focused command: `JARVIS_NO_MODELS=1` with `CODEX_*` unset,
  `.venv/bin/python -m unittest tests.continuity.test_participant_channel`.
  Result: Ran 21 tests in 13.537s, OK. Caller check only:
  `tests.continuity.test_repoint_lifecycle` and
  `tests.continuity.test_roundtrip_adapters`, 28 tests OK in 45.323s.
- Not a live fence or Arch→Dell handoff. The store still records the max
  epoch a guard has already fenced; this path refuses to ask for a higher one
  when the local tuple is already past the closed epoch. Production startup
  still does not bind these adapters.

### Manual recovery retries

- Files: `jarvis/continuity/recovery.py`,
  `tests/continuity/test_handoff_recovery.py`.
- A client key is now one retry-safe attempt. Same-key retries reuse the
  authority row and do not repeat grant, canary, completion, or rollback. A
  persisted optional receipt is mode 0600 and contains only request metadata
  and canary bit; lease-id notes are ignored. Lost receipt plus lost pre-fence
  tuple fails `key_reused` rather than guessing.
- Uncertain canary remains `GRANTED_NOT_READY`, not re-run or rolled back.
  Failed `complete` stays partition and reuses saved canary result on retry.
- Focused `tests.continuity.test_handoff_recovery`: 17 tests passed. Related
  audit-race and participant-recovery caller checks: 8 tests passed.
- In-process store/fake-port only. No production hub binding exists; uncertain
  canary remains granted until a later call observes lease lapse. No live
  Arch→Dell handoff is established.

### Successor lifecycle and repoint retry

- Files: `jarvis/continuity/lifecycle.py`,
  `tests/continuity/test_repoint_lifecycle.py`.
- The unit-start retry path now preserves a private grant and retries the unit
  helper without reopening/re-writing the journal. It refuses incomplete
  startup, stale lease, busy tunnel, and a mismatched signed node note. After
  helper success, a second call does not start the unit again.
- A node role switch refuses to reach the role helper while the tunnel is down;
  retry after it comes up does not stop it, and an expired grant refuses before
  side effects.
- Focused lifecycle tests: 19 passed; adapter-roundtrip caller check: 14
  passed. These are fixture results only.
- Review carefully: endpoint publication still precedes tunnel activation, and
  accepting a successor grant still renews to prove lease id before rejecting
  a grant that became ready. `startup_ready` still has no production provider;
  this does not make the roundtrip live.

### Runtime fencing

- Files: `jarvis/continuity/runtime.py`,
  `tests/continuity/test_runtime.py`.
- Runtime tuple validation rejects bool/float counters. `renew_once` now reads
  status again after a successful renew and drops its cache if holder/tuple
  moved. `require_authority` checks the held lease against live plain counters
  before readiness; stale same-device epoch is `stale`, another holder is
  `not_holder`. Warm cache drops for no-main, legacy, quarantine, and stale
  state. HANDOVER may accept not-ready grant, but admission remains closed.
- Focused `test_runtime`: 38 tests passed. Combined runtime + safe handoff +
  audit races: 83 passed in 4.412s.
- `state()` remains a snapshot; until a later `require_authority` or
  `renew_once` reads the moved tuple, the prior `admission_open` value may
  remain. The runtime changes are still isolated fixtures; `cluster.main_device`
  remains the live role and no production integration is present.

### Lifecycle prepare/release

- Files: `jarvis/continuity/lifecycle.py`,
  `tests/continuity/test_repoint_lifecycle.py`.
- The executor journal still closes before the successor unit starts, so the
  new process can take the writer lock. A failed unit start leaves the private
  grant and retries only the unit helper. The retry does not reopen the
  journal, rewrite the grant, or replace a newer endpoint. Startup incomplete,
  a stale lease, a busy tunnel, or a note that no longer matches the signed
  instruction refuses before that helper runs. A later call after the helper
  has already returned does not start the unit again.
- `switch_node` still publishes through the installer before tunnel activation.
  A tunnel that stays down never calls the role helper. A retry after the
  tunnel is up does not stop that tunnel. An expired grant on that retry
  refuses before either stop or another unit start.
- Focused command: `JARVIS_NO_MODELS=1` with `CODEX_*` unset,
  `.venv/bin/python -m unittest tests.continuity.test_repoint_lifecycle`.
  Result: Ran 19 tests in 25.636s, OK. Caller check:
  `tests.continuity.test_roundtrip_adapters`, Ran 14 tests in 37.170s, OK.
- Not a live role change. `startup_ready` still has no production provider.
  Accepting a successor grant still proves the lease id with renew, so a grant
  that becomes ready in that window is extended once and then refused.


### Authority transfer CAS

- Files: `jarvis/continuity/authority.py`, `tests/continuity/test_authority.py`.
- An open transition blocks `set_mode` (`busy`). Fence and cancel share one quorum: zero eligible participants cannot retire a live lease, and a non-transfer mode cannot fence. The expected tuple is checked before fence, drain, and readiness writes. A fence retry does not bump the epoch. A grant retry returns the same not-ready lease and its remaining validity. `complete` on a moved fenced epoch is `stale` and neither activates nor fails the fence. A `mode_version+1` ack is stored only in `QUIESCED` or `ROLLBACK_QUIESCED`. The writer lock is `<realpath(database)>.writer`; a symlink lock file is `bad_request`.
- Focused command: `JARVIS_NO_MODELS=1` with `CODEX_*` unset, `.venv/bin/python -m unittest tests.continuity.test_authority`. Result: Ran 67 tests in 0.084s, OK. Caller checks only: `tests.continuity.test_authority_membership` 13 OK; `tests.continuity.test_executor.Legacy.test_legacy_reopens_only_after_authority_rollback` 1 OK.
- Isolated store fixtures only. This does not prove a live Arch → Dell → Arch handoff. `cluster.main_device` stays the unfenced MAIN.

### Coordinator race repairs and final continuity run

- In `runtime.py`, `begin_drain` no longer overwrites a prior `suspend` latch,
  and `_drain_finished` only clears an actual `draining` latch. Regression:
  busy release → drain → cancel → renew remains `suspend`, with no renew RPC.
- `_HandoffJournal(path=None)` now uses an in-process lock around copy/merge/
  publish. Regression synchronizes two claims and proves only one gets
  `claimed`.
- The concurrency additions passed with
  `JARVIS_NO_MODELS=1 ... unittest tests.continuity.test_runtime
  tests.continuity.test_safe_handoff tests.continuity.test_audit_races
  tests.continuity.test_executor_concurrency`: 93 tests, OK in 2.262s.
- Participant fence regression now matches the idempotent authority contract:
  second fence returns stored `FENCING`, while assertions retain the closed
  acknowledgement epoch and verify the authority epoch does not increment
  again. The targeted test passed (1 test, 2.161s).
- Full continuity discovery (started before the last regression update) ran 653
  tests and failed 10 assertions (2 expected failures): 8 failures are the two
  operational automatic-promotion tests remaining without a canary; one
  `test_drill_handoff` expects `revoked` while the gateway now returns
  `unauthorized` for a disabled participant; one is the old participant
  assertion now corrected and passed in isolation. The two promotion tests
  deliberately do not fabricate a canary and therefore remain a production
  integration gap; the gateway error-code mismatch belongs to Team B. Root has
  a separate full-suite run underway. Do not report the continuity package as
  green.
- After fixing the stale participant assertion, the owned Team A core selection
  passed: `JARVIS_NO_MODELS=1` with `CODEX_*` unset,
  `.venv/bin/python -m unittest tests.continuity.test_authority
  tests.continuity.test_authority_membership tests.continuity.test_clock
  tests.continuity.test_executor tests.continuity.test_executor_concurrency
  tests.continuity.test_handoff_recovery tests.continuity.test_participant_channel
  tests.continuity.test_repoint_lifecycle tests.continuity.test_renewal
  tests.continuity.test_runtime tests.continuity.test_safe_handoff
  tests.continuity.test_audit_races tests.continuity.test_roundtrip_adapters`:
  326 tests in 69.443s, OK. `git diff --check` on the Team A-owned source,
  tests, and session note is clean.
- Live acceptance was not attempted. The coordinating lead reported sshd
  stopped on Dell and the protected config read was refused; neither condition
  was bypassed. Team B is addressing production wiring and lifecycle pivot.

## Local operator bootstrap implementation

- Added `continuity bootstrap preflight|stage|configure|cutover` in
  `jarvis/continuity/bootstrap.py` and the local continuity CLI. Preflight reads
  authenticated status without creating state. Stage validates this PC's key
  against its enrollment pin, copies only operator metadata, and creates the
  configured outside-Git transfer directory. Configure backs up the config,
  installs the operator JSON in the per-user config directory, and adds only
  `continuity.operator_config`; no marker or role change occurs.
- Cutover requires the installed pointer, exact authority tuple in
  `QUIESCED` or `V2_ONLY_NO_MAIN`, no transition or valid lease, staged storage,
  and an active local `jarvis-cluster.service`. It pauses through `jarvis.rest`,
  verifies the service stopped, binds the real runtime/guard/issuer, durably
  fences and acknowledges, then restores services. Existing Serve port 8443
  route configuration is not touched. The current `jarvis.rest` lifecycle is
  Linux/systemd-only; Windows cutover refuses closed.
- Operator sequence and limits: `docs/design/mobile-continuity/LOCAL-BOOTSTRAP.md`.
  Focused command: `JARVIS_NO_MODELS=1` with `CODEX_*` unset,
  `.venv/bin/python -m unittest tests.continuity.test_bootstrap
  tests.continuity.test_cli` — 26 tests passed. Preflight/stage/configure CLI
  execution and the cutover ordering gates are fixture-tested; no live config,
  marker, service, gateway mode, or role was changed.
- Additional caller check over bootstrap, CLI, startup binding, and production
  composition: 43 tests run with two failures outside this implementation.
  `test_startup_binding.StartupBindingTest.test_service_sync_preserves_not_ready_grant_until_cancelled`
  expects `ProductionController.close`, which is currently absent. The
  production-composition fixture `test_factory_completes_one_way_handoff_with_real_broker_and_brain_transport`
  is refused with `code_mismatch` before recovery. These belong to Team B's
  production integration; no Team B-owned files were changed.

### Reviewed staging settings and Windows cutover lifecycle

- Root added a config-mutation lock and validated-snapshot comparison around
  final configure publication. A concurrent config/role edit now refuses as
  `local_config_changed` before the operator pointer or backup is published.
  The continuity CLI dispatch is also tested before global config/state setup;
  those entrypoint changes and race tests are root-owned and were preserved.
- The reviewed schema-1 transfer manifest explicitly supplies one staging path
  per enrolled PC and may explicitly set `brainsync_enabled`. Local paths are
  filesystem-validated outside Git. Foreign Windows/POSIX paths remain literal
  and syntax-only until that peer validates them locally; server devices do not
  need transfer roots. Configure applies only these manifest paths, optional
  sync toggle, and the operator pointer, with a config backup and no marker or
  role write. Root reports Arch read-only preflight and PREPARE succeeded; no
  configure or activation was run by this implementation.
- Windows cutover wraps Team B's existing `NodeWorker` cooperative stop/ack
  protocol (`cluster-process.json` and `request_node_worker_stop`) instead of
  maintaining a second process protocol. The wrapper waits for the exact
  stopped receipt and then for release of `cluster-node.lock`; only that closed
  writer state qualifies for durable fencing. The default wait is 40 seconds to
  clear the worker's 25-second idle long poll. Restoration uses
  `cluster_setup._launch_agent` and requires a new nonce registration plus
  writer lock. No OS kill or extra daemon is used.
- Files owned here: `jarvis/continuity/bootstrap.py`,
  `jarvis/continuity/cli.py`, `jarvis/continuity/local_lifecycle.py`,
  `tests/continuity/test_bootstrap.py`, and
  `tests/continuity/test_local_lifecycle.py`. Focused validation after the
  Team B hook integration: `tests.continuity.test_bootstrap
  tests.continuity.test_cli tests.continuity.test_local_lifecycle` — 34 tests
  passed. Live Dell cutover remains unrun; a test fixture is not live acceptance.
