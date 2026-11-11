# Obsidian write-through (C2): transactional outbox

Status: implemented and fake-tested; **not activated**. `vault.write` remains false; only the separate `second_brain.enabled` policy enables capture. Code: `jarvis/second_brain_queue.py`, hooks in `jarvis/memory.py`,
`jarvis/history.py`, `jarvis/store.py`, per-record `SecondBrain.publish` in `jarvis/second_brain.py`.
Tests: `tests/data/test_second_brain_queue.py`.

## Guarantee (and its limits)

Once capture is enabled for a store database and a process uses the hooked code:

- Every personal mutation (memory add/link/merge_meta/retire/absorb, history record, task
  add/transition/reap_stale) commits **its full immutable revision payload in the same SQLite
  transaction** as the change (`archive_outbox`). If queuing fails, the exception
  (`ArchiveCaptureError`) propagates, the transaction rolls back, and the personal mutation did not
  happen. Callers see the error; no side effect has to be re-run.
- `drain()` publishes **exactly the queued revision**, in order per record, through
  `SecondBrain.publish` (validation, secret/size/tombstone/conflict checks, write + fsync, then a
  read-back of the full payload). Only then is the event deleted (together with an
  `archive_head` row). A crash between the note write and the ack leaves the event queued; the
  retry finds the identical newest revision (`unchanged`) and acks — no duplicate revision.
- Held (secret-like, oversize, tombstoned, conflict, invalid, corrupt spool) and failed events
  stay queued; a held/failed head blocks later events of the *same record* only.
  Nothing is dropped or truncated.
- Restored archive state is inert: queued/running tasks come back `interrupted`, no capture
  tables or marker are created in the restored directory.

Not guaranteed:

- **Older/running processes.** A writer without the hook (a process started before this code, or
  a raw sqlite client) still fires the database triggers: its touched record ids survive in
  `archive_guard` and the next `drain()` captures the *then-current* row as an event with origin
  `reconcile` (counted in the report). Intermediate states of such writes are lost, and a record
  deleted before reconciliation is counted as `source_gone`, not archived. **Exact per-mutation
  capture requires restarting every long-lived JARVIS process after activation.** Until then
  activation only promises "latest state, eventually".
- No cross-store atomic checkpoint (memory, history, tasks queue independently).
- No fleet/lease fencing (single MAIN legacy check only); concurrent drains on one machine are
  serialized by `<state>/archive.lock`.
- The vault is not yet the read path (C3); SQLite remains the source of truth.

## Schema (in each store's own database)

`archive_meta(key,value)` (`enabled`, `schema`, `store`, `last_success_at`, `source_gone`),
`archive_outbox(seq,store,kind,key,origin,payload,payload_sha,bytes,captured_at,attempts,state,held_reason,last_error)`,
`archive_guard(store,kind,key)`, `archive_head(store,kind,key,payload_sha,published_at)`, plus
AFTER INSERT/UPDATE/DELETE triggers on items/links, commands/hops, tasks/events that fill
`archive_guard` (only while `enabled='1'`; `INSERT OR IGNORE` and no-op updates stay silent, so a
deduplicated add makes no false event). `capture()` skips a revision equal to the newest queued or
published payload (idempotent writes).

## Activation

1. `configure(state_dir)` — before requests are served, vault reachability not required.
   Creates missing store databases, installs the structures, writes `archive-capture.json`
   (so a store first created *later* — e.g. `History` on the first command — is adopted by its
   constructor: no capture gap). Returns `ok=False` if any store failed.
2. Restart long-lived processes (daemon, console, voice) so they run the hooked code.
3. `drain()` on a schedule/at start; `status()` for health.

Rollback: `configure(state_dir, enabled=False)` flips `enabled=0` (hooks/triggers go inert, queued
events remain visible in `status`; re-enabling re-seeds every existing row into the baseline backlog in the same transaction, so rows written while it was off are archived by the next drains).

## Retention is suspended while capture is on

`History.record` skips the commands/hops prune and `Store.prune` returns 0, so a record cannot be
pruned before it is archived. (`confirmation_audit` pruning is unchanged: operational, not
archived.) No irreversible deletion was added; the suspension lifts with C3 retention design.

## API

```
configure(state_dir, enabled=True) -> {ok, enabled, marker, stores:{memory|history|tasks:{configured,enabled,created,error?}}, errors, error?}
status(state_dir) -> {ok, enabled, configured, capture_complete, marker, pending, held, errors, unreconciled, bytes,
                      oldest_age_s, last_success, source_gone, held_reasons, error_samples, stores:{...}, error?}
drain(state_dir, vault_root, limit=100, *, allow_secret_like=(), dry_run=False, max_record_bytes=None)
      -> {ok, busy, vault_ok, dry_run, acked, published, unchanged, held, failed, reconciled, remaining,
          held_ids, error_samples, elapsed_ms, stores, error?}
release_held(state_dir, record_id=None, reason=None) -> int
SecondBrain.publish(payload, allow_secret_like=False, dry_run=False)
      -> {ok, status: written|unchanged|would-write|held|retry, record, revision, name, bytes, reason}
```

`drain.ok` means "vault reachable, not busy, no database error" — **not** that the queue is empty;
check `remaining`/`held`/`failed`. Reports contain record ids and counts, never bodies.
`dry_run` opens databases read-only and writes nothing (it counts the next head per record only).

## Remaining blockers for canonical status

C3 read path / retention policy; fleet-wide single-writer lease fence; a revive API for tombstoned
records; cross-store checkpoint; restart-required activation of existing processes; plans, runs and
knowledge files are still backfill-only (`sync`), not write-through.


## Parent integration review

Actual queue/runtime integration passes: configure before request handling; capture while vault
is offline; ordered publish and acknowledgement; status accepts the queue's ISO last-success
stamp and refuses `current` when capture is incomplete, status read failed or legacy guard
rows remain. Cached-package import mocks were fixed by a canonical importlib queue loader.

Bulk CLI `sync --apply` refuses while capture is enabled or queued work remains, preventing
snapshots from overtaking ordered pending revisions. Use `drain` for the captured database
stores. File-backed plans/runs/knowledge remain backfill-only; do not claim their live capture.

Pipeline recording errors now produce an explicit durability warning while preserving the
already executed action outcomes; recording failure never requests an effect replay. Two
regressions cover actual enqueue rollback and the visible response. Targeted pipeline set:67 OK.

Activation requires loading hooked code in all long-lived processes. Until restart, triggers
provide eventual latest-state reconciliation only, not exact intermediate revision capture.
Credentials/authority remain operational state; no C3 canonical read/rebuild cutover is claimed.
