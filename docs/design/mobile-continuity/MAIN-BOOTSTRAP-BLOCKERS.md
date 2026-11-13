# MAIN production bootstrap: current blockers

The adapter work is green in fake-transport tests. **Production `cluster main`
remains unavailable until the startup/controller composition below is complete.**
No gateway/Dell/Oracle contact, real cutover, service restart, repository commit,
or `jarvis.config.json` edit was performed by this implementation.

## Implemented

- **CLI process bridge:** owner-authenticated, numeric-loopback-only
  `/v1/continuity/main` and `/v1/continuity/recover` routes on the existing hub.
  The client does not hold the fleet lock while waiting on the hub. An unbound
  coordinator refuses rather than recursing or rewriting the role.
- **Participant channel:** signed `continuity` long-poll jobs retain code matching,
  deadlines and replay protection. Each receiver checks its own authenticated
  gateway client, transition, tuple, phase, coordinator and role. Candidate
  readiness is signed on the candidate, not by the source. Drain/release, durable
  executor fences, real grant acceptance and snapshots are implemented. Mutating
  RPCs require the exact runtime/guard/issuer binding that fences legacy effects.
- **Scoped brain transfer:** every staging/chunk/import/export/freeze operation
  pins one transition/incarnation/epoch and rechecks live authority. Only generated
  bundle names inside the configured non-Git staging roots are allowed. Integrity,
  SQLite, source-freeze and memory-conflict checks remain. Ordinary admission is
  not bypassed. Memory never goes to GitHub. Broker bulk RPC bodies stay in memory;
  the existing private node outbox still supports result delivery.
- **Manual initial lease:** `ManualRecovery` plus `continuity recover`/`lease`
  drives owner proofs, candidate-local readiness, durable fencing, not-ready grant,
  acceptance and a fresh installation-signed canary. False/unverifiable canaries
  clear the grant; live MAIN is never forced. The CLI has no lease id. It refuses
  when the hub's recovery coordinator is unbound.
- **Signed repointing:** candidate offers bind installation, endpoint, code key,
  target credential and grant. Targets require a live not-ready grant, exact
  previous-epoch local fence and a fresh signed nonce/key response from the actual
  serving successor. They recheck authority after probing before private pointer/
  credential publication. Partial/corrupt state fails closed. Runtime clients,
  CLI entry and tunnel workers use a verified in-memory role/endpoint overlay;
  raw config is untouched. Offer retries do not rotate credentials.
- **Guarded lifecycle:** existing role autostart/unit and tunnel helpers perform
  explicit switching. Successor start requires free role locks and a released
  old tunnel port. A private, real not-ready grant may cross that single process
  boundary, is proven by gateway renewal and is consumed once. It cannot restore
  a ready lease after a later crash. Every OS action in tests is patched.
  `startup_ready` defaults to unavailable: lifecycle calls refuse with
  `startup_incomplete` before actions when no complete controller is supplied.
- **Explicit return brain:** `continuity return-prepare` requires reviewed
  incoming-memory/current-backup/freeze hashes and free local role locks. It
  validates SQLite, privately preserves all old database/WAL/SHM and bundle bytes,
  and leaves the freeze active. Intent precedes moves; interrupted work refuses
  for review. Normal receive/conflict logic is unchanged. Only the approved
  incoming memory and matching newer live return grant permit `activate_return`,
  which archives the original freeze. `return-refreeze` preserves incoming memory
  and restores freeze/read-only protection after a failed return canary.
- **Renewal:** Hub/NodeWorker lifecycle hooks renew only an already accepted,
  bound lease. They stop with that runtime, tolerate gateway errors, and never
  request a lease, elect, recover or promote.
- **Two-way adapter drill:** Zenbook→Dell→Zenbook exercises actual signatures,
  gateway checks, scoped storage, endpoint attestations, lifecycle helpers,
  qualified grant consumption, explicit return approval/activation and canary.
  Wire and OS/process boundaries are fakes. Readiness refusal, ordinary-job
  fencing, unsigned/stale/replayed instructions, memory-conflict refusal,
  canary/refreeze rollback, busy role locks and lease expiry are covered.

## Exact remaining production work

1. **Complete startup/controller factory.** Valid local operator config must
   construct real verifier/guard/issuer/readiness, endpoint, recovery and handoff
   adapters. Stage without fencing legacy dispatch; bind only at the explicit
   cutover boundary. Consume a qualified successor grant before ordinary effects.
   Missing config/adapters and gateway outages must not create a partially bound
   managed runtime. Renewal hooks do not construct this missing binding.
2. **Deferred switching and transport pivot.** Compose offers, scoped return
   activation and lifecycle helpers. Nodes must ACK callback results before role
   switches; the old hub must finish the HTTP response before its unit changes.
   Do not terminate the process carrying an unacknowledged callback/coordinator.
   `startup_ready` currently has no production provider and stays unavailable.
3. **Reachable successor route.** The Dell's existing forward tunnel reaches the
   Zenbook and its SSH key explicitly forbids reverse forwarding
   (`permitlisten=127.0.0.1:1`). No restriction was widened. A separately prepared
   pinned HTTPS route can be carried by the signed offer; the current forward
   link alone cannot provide the opposite connection.
4. **Distributed owner-proof path for return.** The adapter drill's real owner
   port signs in the Zenbook fixture. It does not implement a distributed owner
   signer on the real PCs. Do not copy the Zenbook owner key or replace OwnerProof
   with installation signatures. A correctly authorized owner-proof path must
   survive the Dell becoming MAIN.
5. **CLI role publication and composed drill.** `cluster_cli.set_main` still reads
   the raw role under the fleet lock; a successor can have only a signed overlay.
   Reconcile publication intent/race checks with that verified effective role.
   Then exercise the shipped startup/controller and CLI, not just the explicitly
   composed adapters, through the two-way drill and failure cases.

## Operator sequence and current boundary

The order remains: status → PREPARING_V2 → QUIESCED → fence legacy writers and
`ack --next-version` on every eligible PC → V2_ONLY_NO_MAIN → current-version
acknowledgements → initial Zenbook recover/lease → MAIN Dell → MAIN Zenbook.
Use each status response's mode_version, and each PC's own operator file/key.
`ack` asserts fencing; it does not perform the missing runtime cutover itself.
`mode V2_ACTIVE` cannot grant a lease.

`recover --hub-config <local-config> --state <local-state> --request-id <stable-id>`
exists but refuses on an unbound real hub. The hash-bound return preparation and
refreeze commands are documented in `docs/handbook/continuity.md`.

**This is not yet a working production cutover sequence.** The fake adapter drill
explicitly supplies startup capability and process boundaries. It does not prove
that the missing production composition exists. `auto_recover` stays off; there
is no automatic failover or promotion.

## Verification

Final focused adapter/CLI/autostart run: **85 tests, OK**. Full-suite evidence for
this continuation is recorded below after the final run. Prior milestone:
4,088 tests OK, 7 skipped, 3 expected failures. New tests in this continuation are
`test_repoint_lifecycle.py` and `test_roundtrip_adapters.py`; earlier callback tests
remain in `test_participant_channel.py` and `test_bootstrap_adapters.py`.

Final continuation verification (2026-11-13): the required environment-stripped
full-suite command completed with **4,194 tests in 625.085s: OK (skipped=7,
expected failures=2)**. Log: `/tmp/jarvis-handoff-final-verified.log`.
The focused handoff/CLI/autostart run passed 85 tests; the final narrower
lifecycle/autostart run passed 24 tests, and the continuity package rerun passed
420 tests with 2 expected failures.

Concurrent workspace hardening required compatible test headers and backend Host
forwarding in the existing process-drill proxy. Signed bodies and production Host
checks were preserved. The history duplicate-route check now counts AST keys
instead of relying on stale imported function line offsets; its exact-one-route
assertion remains. New shared-workspace utility modules were added to the system
view inventory. No clipboard behavior was changed by this implementation.
`git diff --check` is clean. These results establish the adapter milestone, not
the missing production composition or a live-machine cutover.
