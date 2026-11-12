# Executor implementation — target fence and one effect

`jarvis/continuity/executor.py` is the target-side fence. Fleet mode, leases, and admission consumption stay in `AuthorityStore`. The journal is a local fence plus receipts. It cannot authorize work by itself.

This is an in-process module with temp-store tests. The repo has no verified provisioned gateway. Oracle and Dell readiness are unverified. Nothing here is live operational failover. `Remote.call`, `NodeWorker`, `Hub.dispatch`, and `execute_local` are still unfenced. `jarvis/continuity/__init__.py` still describes the package as an unwired prototype; the parent updates that docstring when the package is imported by the running app.

Tests inject `AuthorityStore` directly. That store trusts its caller. Production must inject an authenticated target client. A missing client is a deployment blocker (`no_authority` / `no_verifier`), and the guard has no flag that skips the signature or the consume.

## Facade

`OPERATIONAL-CONTRACT.md` names four methods. The implementation keeps those names and adds the arguments the fence needs. `integration.py` `PEER_FACADES["executor"]` lists the four names. `legacy_effect` and `local_view` are extra and local.

```python
class ExecutorGuard:
    def __init__(self, path, *, device_id, installation_id, fleet_id, authority_id,
                 authority=None, verifier=None, boot=None, wall=None, managed=False): ...
    def close(self): ...
    def local_view(self) -> dict: ...          # journal only
    def legacy_effect(self, sink): ...          # today's dispatch, until a durable fence
    def fence(self, incarnation, epoch, mode_version, *, reopen_legacy=False) -> dict: ...
    def accept(self, signed_operation, admission_ticket, sink, *, failpoint=None) -> dict: ...
    def reconcile(self, operation_id, attempt_id) -> dict: ...
    def stop_interactive(self, session_id, reason) -> dict: ...
```

`accept` takes a zero-argument `sink` and calls it only after the `ACCEPTED` receipt is fsynced. The caller does not run the effect after `accept` returns. The sink receives no operation fields. Its return value is discarded. `failpoint` is test-only and raises only after a durable write (`after_prepare`, `after_consume`, `after_accept`, `during_effect`).

`fence(..., reopen_legacy=True)` is the local half of rollback. It applies only when authority `status()` is already `LEGACY_ACTIVE` at a strictly newer `mode_version`. Any other use is `bad_edge`.

### Authority client

`authority` must provide:

| Method | Call the guard makes |
| --- | --- |
| `status()` | No arguments. Needs `authority_id`, `mode`, `main`, `incarnation`, `epoch`, `mode_version`, `policy_version`, `main_ready`, `lease_valid`. |
| `consume(...)` | Keyword arguments of the store ticket: `admission_id`, `target`, `operation_id`, `attempt_id`, `digest`, `main`, `incarnation`, `epoch`, `issued_at`, `admit_until`, `issuer_incarnation`. |
| `acknowledge(device_id, installation_id, mode_version, fenced_epoch, boot_id=)` | Same shape as `AuthorityStore.acknowledge`. |
| `admission(admission_id)` | `None` when unknown. Otherwise the store record fields `consume` returns, including `issued_at`, `admit_until`, and `issuer_incarnation`. |

`status()` and `admission()` errors: `AuthorityError` passes through; `OSError` becomes `unavailable` with a fixed message. The guard does not catch `GatewayError`. An HTTP adapter translates gateway failures into `AuthorityError` or `OSError` and keeps personal text out of the message.

`AuthorityStore.consume` does not take `mode_version`, `policy_version`, or a signature. The guard checks those on `status()` before `consume`. The gap between those two calls is one round trip. The store remains the admission writer.

### Signed operation and ticket

Required operation keys: `authority_id`, `fleet_id`, `main`, `incarnation`, `epoch`, `mode_version`, `policy_version`, `target`, `operation_id`, `attempt_id`, `digest`, `effect_class`, `issued_at`, `admit_until`, `issuer_incarnation`, `admission_id`, `signature`.

Optional: `session_id`, and only for `effect_class="interactive"` (default is `operation_id`).

The ticket is exactly the `consume` keyword set. Every ticket field must match the operation. Unknown keys are `bad_request`. The error does not name them.

`effect_class` is `ordinary`, `pure_compute`, or `interactive`.

`signature` is a string of 1..512 characters. The verifier must return exactly `True`. Any other return, or any exception, is `signature` with a fixed message. The signature is not stored. The journal keeps `token_hash`, the sha256 of the canonical token fields.

Identifiers follow the authority rule `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`. Digests are `sha256:` plus 64 hex digits. Lifetime must be inside `(0, MAX_TICKET_SECONDS]` (24h). Issuance more than `TICKET_SKEW` (5s) ahead of the wall clock is `bad_ticket` and is not stored.

### Return records

`fence` returns `device_id`, `installation_id`, `authority_id`, `fleet_id`, `high_incarnation`, `high_epoch`, `mode_version`, `fenced_incarnation`, `fenced_epoch`, `legacy_closed`, `boot_id`, `authority_recorded`.

`accept` / `reconcile` return `target`, `operation_id`, `attempt_id`, `admission_id`, `digest`, `token_hash`, `main`, `authority_id`, `incarnation`, `epoch`, `mode_version`, `issued_at`, `admit_until`, `effect_class`, `state`, `result_code`, `effect`, `already`.

`effect` is `completed`, `uncertain`, `released`, or `not_run`. `already` means this `(target, operation_id, attempt_id)` was already in the journal. A resume that starts the sink for the first time still returns `already=True`. Callers read `state` and `effect`. They do not start a second sink.

`stop_interactive` returns `session_id`, `state="released"`, `reason`, `replay=False`, `already`.

`local_view` is the journal: `quarantined`, `legacy_closed`, `rebooted`, the high-water tuple, the fenced pair, `boot_id`. `rebooted` is informational. It is not permission to admit offline.

## Ordering

Legacy sinks run while the journal is missing (unmanaged) or while the last durable mode is `LEGACY_ACTIVE` or `PREPARING_V2`. `QUIESCED` and every later mode close legacy inside `fence()`, and the close is written before `acknowledge`. A failed acknowledge does not roll the local fence back. Rolling it back would reopen legacy.

The guard does not poll fleet mode. Legacy stays open until some caller invokes `fence()`. A status read must not close legacy: a spoofed status would then be able to kill today's dispatch.

Tuple order is lexicographic `(incarnation, epoch)`, the same order as `clock.py`. `acknowledge` can send only the integer `fenced_epoch` (the store takes a MAX). The local journal stores the pair and refuses `tuple <= fenced pair`. The current store bumps epoch when an incarnation restarts, so the unpaired integer matches today. A later store that reset epoch below a previously acknowledged integer would disagree with this journal.

`managed=True` and a missing file quarantine the process. The path is not created. `fence()` cannot rebuild a wiped managed journal. First enrollment is an unmanaged `fence()` that creates the file. Later process starts use `managed=True` and the stable installation path.

### `accept` sequence

1. Under the lock, quarantine is checked before the ticket is parsed.
2. Parse, require a closed legacy fence, a client, and a verifier. Verify the signature.
3. Same attempt: identical token returns the prior record. A different digest, token, issuance, expiry, or session is `conflict`.
4. Another attempt of the same operation is `in_progress` while a row is `PREPARED`, `CONSUMED`, `ACCEPTED`, or `UNCERTAIN`. After `COMPLETED`, a new attempt is allowed only when both rows are `pure_compute`.
5. A tuple below the high-water mark, or at or below the fenced pair, is `stale` with no network call.
6. A well-formed window that the wall clock says is over is pinned `EXPIRED` and is not consumed. Refreshing that expiry later is `conflict`. A malformed lifetime is `bad_ticket` and is not stored.
7. `admission(id)` already present, and this journal has no row: insert `UNCERTAIN` and do not run.
8. Insert `PREPARED`.
9. Online `status()`. A regressed authority tuple does not lower the watermark and does not consume. A newer status tuple is persisted from `status()`, never from the ticket. Mismatch of main, incarnation, epoch, mode version, policy version, or authority id becomes `REJECTED` / `stale`. Mode other than `V2_ACTIVE`, or `main_ready` / `lease_valid` false, is `no_admission` and leaves `PREPARED`.
10. `consume`. Then `CONSUMED`.
11. Fsync `ACCEPTED`, then call `sink()`, then `COMPLETED`.

A sink that calls back into the guard raises `busy`. The depth flag stays set until the outer call finishes.

`reconcile` reads the local row only. It works while Oracle is down. An admission that exists only on the authority stays invisible until a later `accept` calls `admission()`.

`stop_interactive` writes a session release. It does not consume, does not insert a receipt, and does not press keys. The OS key release stays in the Remote or control sink. If the journal is quarantined, `stop_interactive` raises, and that OS sink still releases input.

### Crash and dedup

| Durable state | Resume |
| --- | --- |
| No row | Nothing ran. A later valid ticket may consume. |
| `PREPARED` | Consume has not happened. Resume continues at status and consume. After a handover the resume is `stale` and consume count stays 0. |
| `CONSUMED` | Authority has the admission. The effect has not started. Resume may call the sink once. |
| `ACCEPTED` or `UNCERTAIN` | The effect may have started. Never call the sink again. `reconcile` reports `uncertain`. |
| `COMPLETED` | Identical retry returns `already=True` and does not run. |
| `EXPIRED` / `REJECTED` / `RELEASED` | The original refusal stands. |

A sink exception becomes `uncertain` with a fixed message. The exception text is not attached.

## Journal

Separate SQLite file from the authority store. WAL, `synchronous=FULL`, exclusive `<path>.writer` lock, `BEGIN IMMEDIATE`. Schema id `1`. `meta.authority_schema` must equal `authority.SCHEMA_VERSION`.

Tables: `meta`, one `fence` row (high tuple, high mode version, fenced pair, `legacy_closed`, boot id at ack, `record_hash`), `receipts` keyed by `(target, operation_id, attempt_id)` with `UNIQUE(admission_id)`, and `sessions`. Times are stored as `.6f` text so the hash survives SQLite `REAL`.

The row hash detects torn writes and corruption. A person who can rewrite the row and the hash together is outside what the hash covers. Hash mismatch, schema mismatch, a foreign installation pin, or an unknown receipt state quarantines the journal. Quarantine does not fall back to legacy.

Allowlisted fields only. Prompt, body, payload, path, and signature bytes are rejected or dropped before any insert. Verifier, sink, and `OSError` text are not propagated (`from None` on the OS path).

## Codes

Raised as `AuthorityError` from `authority.py`: `legacy`, `fenced`, `quarantined`, `stale`, `conflict`, `in_progress`, `ticket_expired`, `bad_ticket`, `bad_request`, `no_verifier`, `no_authority`, `no_admission`, `unavailable`, `uncertain`, `released`, `crash`, `ahead`, `bad_edge`, `signature`, `authority_mismatch`, `writer_busy`, `not_found`, `busy`, `no_boot_id`.

Store codes `stale`, `conflict`, `bad_ticket`, and `ticket_expired` from `consume` pin the receipt (`REJECTED` or `EXPIRED`). `no_admission`, `target_unfenced`, `unavailable`, and `quarantined` from `consume` leave `PREPARED` so a later retry can proceed. The guard itself uses `no_admission` when status is not a ready `V2_ACTIVE` lease, before `consume`.

## Sink hooks (unedited)

Fencing `Remote.call` is not sufficient. These call sites still run effects with no continuity guard. The future hook passes a digest into the guard and leaves `arg`, payload, text, and paths out of both the journal and the authority database. The parent coordinates the edit. This owner does not change these files.

`jarvis/remote.py` `Remote.call` (line 211):

- line 230: cluster local `run_here`
- line 235: node `Client /v1/control` when this process is not MAIN
- line 239: `run_here` when the target is MAIN
- line 241: `dispatch` to another node
- line 250: SSH fallback after `NodeOffline`
- line 257: non-cluster local `run_here`
- lines 263–264: non-cluster SSH and tunnel

Also unguarded, so a `Remote.call` hook alone still misses them:

- `jarvis/cluster.py` `execute_local` line 437, which calls `run_here` at line 441
- `jarvis/cluster.py` `Hub.dispatch` local return at line 618
- `jarvis/cluster.py` `NodeWorker.once` calls `self.executor(...)` at line 1395
- `jarvis/grokbot.py` line 885 calls `run_here`
- `jarvis/transfer.py` and `jarvis/locate.py` define their own `run_here`

`NodeWorker._untrusted` writes `cluster-jobs-seen.json` at `cluster.py` lines 1429–1439 after `verify_job` and before `self.executor`. That file is a separate one-hour dedup. It marks the job seen before the effect starts. A continuity refusal or an uncertain receipt must not be merged into that file. A future hook sits after `verify_job` and before the executor call, and a job that the guard refuses before the effect starts has to be removable from that seen file. Otherwise the node reports the job as already run and the effect never starts.

## Incompatibilities with the other modules

These are interface facts as of this tree. Closing them by dropping the sink, skipping the signature, or accepting `control` / `ai` inside `ExecutorGuard` would weaken the fence. The adapter belongs in the integrator's file.

| Other module | What it does | What the guard requires |
| --- | --- | --- |
| Contract sketch, `OPERATIONAL-CONTRACT.md` lines 184–188 | `accept(signed_operation, admission_ticket)` | `accept(..., sink)`. The effect starts inside the guard. |
| `integration.py` `LocalExecutor` line 523 | Second SQLite journal (`seen` + `receipts`). `IsolatedFleet` lines 929–932 construct it with `require_signature=False`. | One journal here. No signature-skip flag. Tests that want a verifier pass a callable that returns `True`. |
| `LocalExecutor.accept` line 590 | `(identity, operation, ticket)`. Consumes, then sets `run_count=1` with no sink. `operation["crash"] is True` forces uncertain. | `(signed_operation, admission_ticket, sink)`. Crash injection is `failpoint` after a durable write. |
| `LocalExecutor.fence` line 575 | Deletes `mode_version` and stores max `(incarnation, epoch)` only. | Persists `mode_version`. Refuses an epoch or mode version `status()` has not issued. |
| `LocalExecutor.stop_interactive` line 582 | Inserts a receipt keyed `interactive-<session_id>`. | Session row only. No consume, no receipt. |
| `IsolatedFleet.ticket` / relay | `effect_class` `control` and `ai`. | `ordinary`, `pure_compute`, `interactive`. Anything else is `bad_request` and is not stored. |
| `runtime.py` `_EFFECTS` line 36 and `require_authority` | `control`, `ordinary`, `ai`. `admission_decision` maps store `already=True` to reconcile. | Effect classes differ. Guard `already` means the attempt row existed. A `CONSUMED` resume still runs the sink once. |
| `runtime.py` `StoreAdapter.consume` line 229 | `consume(ticket)` one dict. `acknowledge(...)` matches the store. | `consume` is keyword-only, matching `AuthorityStore.consume`. `StoreAdapter` is not a drop-in until a wrapper unpacks the dict. |
| `gateway.py` `AuthorityClient` lines 1001 and 1013 | `acknowledge(mode_version, fenced_epoch, boot_id=None)` binds device identity at construction. `consume(ticket, ticket_signature)`. | `acknowledge(device, installation, mode_version, fenced_epoch, boot_id=)`. `consume` keywords, no signature argument. The adapter verifies the MAIN signature at the gateway (already done in `ContinuityGateway.consume`) and drops the signature before the journal. |
| `ContinuityGateway.consume` line 649 | HTTP caller is the target. `principal.device_id` must equal `ticket.target`. Signature is checked with the MAIN public key. | The executor consumes as that target. A MAIN-only consume burns the admission without this journal running (`UNCERTAIN` on the later lookup). |
| `gateway.py` `_project_admission` | Ticket fields plus `consumed_at` and `already`. | `_same_admission` requires those ticket fields, including issuance and expiry. A projection that drops them fails closed as `conflict`. |
| `integration.py` `INCOMPATIBILITIES` id `peers_absent` | Text still says `executor.py` is not in the tree. `test_operational_e2e.py` asserts that id. | The file is in the tree. Their list and test are stale. This owner does not edit them. |
| `integration.py` `no_signatures` | "A default executor refuses." | Still true for this guard: `verifier is None` raises `no_verifier`. |

`IsolatedFleet` builds `PcRuntime` for Dell with `clock=None` (online only). That matches this guard: every `accept` validates online. The wall clock only pre-refuses expiry. It is not a grant. `SAFETY_MARGIN` in the authority store is still the unmeasured placeholder. Windows and macOS have no suspend-aware cache in this module.

`integration.py` end-to-end cutover currently fails inside that package (`reject_personal` on the loopback acknowledge path, and production-blocked cases). Those failures are independent of this journal. Patching them here would mix ownership.

## Tests

`.venv/bin/python -m unittest tests.continuity.test_executor`

26 tests, isolated run after the quarantine-before-parse change: `Ran 26 tests` / `OK`. They use `tests/continuity/_fleet.py` (`Clock`, `open_store`, `cutover`, `promote`, `ticket`) and an in-process store. They do not open a gateway, enroll a live key, or touch `.jarvis`.

| Test | What it holds |
| --- | --- |
| `test_default_legacy_runs_and_v2_ticket_does_not` | Unfenced legacy runs the sink. A v2 ticket does not run and does not consume. |
| `test_managed_missing_journal_does_not_become_legacy` | Missing managed file is `quarantined` before parse. The path is not created. |
| `test_preparing_ack_keeps_legacy_quiesced_closes_it` | `PREPARING_V2` keeps legacy. `QUIESCED` closes it. |
| `test_v2_fence_closes_legacy_and_survives_reboot` | Closed legacy and the high-water mark survive a new boot id. |
| `test_fence_is_monotonic_and_cannot_invent_an_epoch` | Cannot ack an epoch or mode version the authority has not issued. |
| `test_unenrolled_fence_stays_closed` | Local fence remains closed when `acknowledge` returns `unknown_participant`. |
| `test_second_writer_is_refused` | Second process on the same journal is `writer_busy`. |
| `test_legacy_reopens_only_after_authority_rollback` | Reopen only from a newer `LEGACY_ACTIVE`. V2 accept then refuses. |
| `test_consume_happens_before_one_effect_and_dedup_keeps_original_expiry` | One consume, one effect. Identical retry does not run. Expiry stays original. |
| `test_conflict_digest_expiry_and_ordinary_retry_do_not_run` | Digest change, expiry refresh, and a second ordinary attempt conflict. |
| `test_expired_ticket_is_pinned_and_not_consumed` | Well-formed expired window is pinned `EXPIRED`. |
| `test_stale_epoch_is_refused_without_authority_and_without_effect` | Stale epoch needs no network. A live epoch while the client is down does not run. |
| `test_forged_higher_epoch_does_not_move_the_high_water_mark` | Epoch 99 does not move the watermark and does not consume. |
| `test_tuple_mismatch_and_not_ready_do_not_run` | Mode or policy mismatch is `stale` with no consume. |
| `test_not_ready_grant_admits_nothing` | `HANDOVER` or `lease_ready=0` is `no_admission`. |
| `test_signature_verifier_and_personal_fields_fail_closed` | Verifier and unknown keys fail closed. Sentinels are absent from the temp directory after a successful accept. |
| `test_fencing_the_live_epoch_stops_this_target` | Fencing the live epoch makes this target and `store.consume` refuse. |
| `test_handover_rejects_old_epoch_and_runs_the_new_one_once` | Old epoch rejected. New epoch runs once. |
| `test_pure_compute_retry_needs_a_new_attempt_after_completion` | New attempt after `COMPLETED` only for `pure_compute`. |
| `test_consumed_elsewhere_is_not_replayed` | Authority consumed, no local row: `UNCERTAIN`, no replay. |
| `test_interactive_stop_releases_without_replay` | Stop does not consume, including while the authority is down. Second stop is `already=True`. |
| `test_crash_windows_do_not_double_run` | `after_prepare`, `after_consume`, `after_accept`, `during_effect`. |
| `test_sink_exception_is_uncertain_and_not_replayed` | Sink `RuntimeError` is `uncertain` and does not run again. |
| `test_tamper_and_schema_quarantine_do_not_fall_back_to_legacy` | Hash or schema damage quarantines. Legacy does not run. |
| `test_journal_bound_to_another_installation_is_quarantined` | Foreign installation pin quarantines. |
| `test_status_regression_does_not_authorize_work` | A scripted epoch regression does not consume or run. |

`python -m unittest discover -s tests/continuity` also loads gateway, runtime, and integration tests. Those modules fail for their own reasons. A red discover run is not an executor regression. Re-run this module alone.

## Limits

- No live Oracle, Dell, or Luna session was exercised. No enrollment key, passkey, or gateway origin was verified.
- Default cluster behavior is unchanged until a caller uses this guard and `fence()` reaches `QUIESCED` or later.
- The high-water mark is not a cache grant. Dell still validates online on every accept.
- Interactive stop records the release. It does not inject key-up.
- The journal hash is not a privilege boundary.
- `cluster-jobs-seen.json` can still mark a node job seen before any continuity decision.
- Fellow tests that construct `LocalExecutor` do not exercise this class. Wiring them onto `ExecutorGuard` without the adapter above will fail closed, which is the intended result until the parent integrates them.
