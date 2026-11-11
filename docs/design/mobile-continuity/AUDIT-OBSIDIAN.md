# Independent audit — Obsidian second brain

Status: fake-data audit only. No production code, config, vault, live DB, service, or node was changed.
Repro: `/tmp/jarvis-obsidian-audit.py` (temp directories only).

Live vault parity, the C1 claim of 831 revisions / 569-row compare, and the parent health checks were not re-opened here. Treat `/tmp/jarvis-independent-live-audit.json` as parent evidence, not as rebuild proof. The parent full suite (3334 tests, one BrokenPipe) was not re-run.

## Commands

```sh
JARVIS_NO_MODELS=1 .venv/bin/python /tmp/jarvis-obsidian-audit.py
JARVIS_NO_MODELS=1 .venv/bin/python -m unittest \
  tests.data.test_second_brain tests.data.test_second_brain_queue \
  tests.runtime.test_second_brain_runtime tests.runtime.test_archive_cli \
  tests.runtime.test_archive_history_faults -q
```

Targeted suite: **145 tests, 2.278s, OK** (49 engine, 32 queue, 41 runtime, 21 CLI, 2 history-fault). Those tests do not cover the four failures below. `test_local_drain_success_feeds_current` locks in an empty drain as `current`. `test_held_oversize_released_with_larger_bound` passes only because the test raises `max_record_bytes`; the drainer never does.

## Findings

### 1. High — arming capture skips existing rows, then `current` is a false claim

`jarvis/second_brain_queue.py:273` (`configure` builds the three stores and installs triggers) and `:168` (`_enqueue` only loads a key that a trigger just touched). No scan of existing `items`, `commands`, or `tasks`. `jarvis/second_brain_runtime.py:424` then refuses `archive sync --apply` whenever capture is enabled. `Drainer.cycle` at `:205` sets `last_success` on every `ok` drain, including `acked == 0`. `_verdict` at `:375` reports `current` from that local timestamp.

Verified fake: one memory row written before `configure`. Pending stayed 0. `sync --apply` returned exit 1 `capture-active-use-drain`. Drain acked 0 and the vault had no note for that id. One drainer cycle still reported `state=current`, `capture_complete=true`. A second row written after arming was published (`acked=1`). So post-activation capture works, and pre-activation rows are dropped out of both the queue and the only bulk tool that could still archive them.

This is the window between the C1 snapshot and process restart, and every new state dir the drainer arms. Not a C3 cutover item: C2 already claims these three stores.

Minimal fix: at the end of `configure`, enqueue the current payload of every existing row whose sha is not in `archive_head` (a later mutation gets a higher `seq`, so order holds; an unchanged C1 note acks as `unchanged`). Do not set `current` from a zero-ack drain unless that pass has finished.

Regression: the `baseline-gap-false-current` check in the repro script.

### 2. High — a legal memory body wedges the outbox, and a later small edit never publishes

`jarvis/memory.py:225` accepts a body of up to 2_000_000 characters. `jarvis/second_brain.py:46` rejects a payload over 4 MiB. `second_brain_queue.py:190` stores that payload and marks the row `held` / `oversize`. `_HEAD_SQL` at `:73` only publishes a key's minimum `seq`, so a newer pending revision waits behind the held head. `release_held` (`:383`) puts the same bytes back, and the drainer calls `drain` without a higher cap (`second_brain_runtime.py:194`). There is no CLI for `release_held`.

Verified fake: `Memory.add` of 1_400_000 `€` (under the memory cap, over 4 MiB UTF-8) held the head. An in-transaction shrink to `small now` queued a pending revision and did not clear the hold. Default `drain` acked 0. `release_held(reason="oversize")` plus another default drain held it again. The vault never received the record. Final queue: pending 1, held 1, reason `oversize`.

Minimal fix: if the head is `held` for `oversize` and a newer pending payload of that key is under the cap, drop the unpublishable head without writing it and publish the newer one. Until that exists, `capture` should raise `ArchiveCaptureError` when the canonical payload exceeds the cap so the personal write rolls back instead of inserting a head that the default drainer can never ack.

Regression: the `oversize-head-stalls-later-revision` check.

### 3. High — strict restore dies on `events.seq` when two task ledgers share a vault

`jarvis/store.py:64` makes `events.seq` a global integer primary key. `jarvis/second_brain.py:1173` inserts that seq on restore. `restore` at `:1090` aborts the whole staging directory on any error. Record ids are `store/kind/key` with no installation id (`second_brain.py:433`).

Verified fake: two temp state dirs, each with its own task (distinct uuid, both events start at seq 1) and the same memory id `note:shared` with different bodies. Both `sync` calls wrote 2 notes. `latest("memory/memory-item/note:shared")` kept only `from-install-b` (the second sync appends a revision; the first body is not a separate record). Strict `restore` returned `ok=false`, left no target, and failed with `IntegrityError: UNIQUE constraint failed: events.seq`. Staging had applied one memory record and one task before the second task's events collided.

Single-ledger restore in the same run succeeded (control below). Installation identity is still unimplemented and is not filed here as a new feature. Namespacing the record key would not fix this: the two tasks already had different ids, and both payloads still contain `seq: 1`.

Minimal fix: on restore, when `events.seq` is already used, insert with a fresh seq and keep the archived seq in the event detail. One ledger's seqs stay exact because they do not collide. Do not fail the other stores.

Regression: the `cross-install-seq-and-id` check.

### 4. Medium — `validate()` accepts a tombstone whose `-t` suffix was removed, then strict restore fails

`_scan` (`second_brain.py:410`) takes the tombstone bit only from the filename. `validate` at `:1059` checks the revision number and the payload hash prefix, not `jarvis_tombstone`. `restore` at `:1129` skips a record only when that filename bit is set. A real tombstone payload (`:1004`) has `fields.text` and no item columns.

Verified fake: sync a note, `tombstone()`, rename the note from `*-t.md` to `*.md` (hash prefix unchanged). `validate()` returned `ok=true`, `error_count=0`, 2 revisions. Strict restore returned `ok=false` with `ProgrammingError: You did not supply a value for binding parameter :id` and created no target. The revision 1 body is not restored, because only the latest note is applied and that note no longer counts as a tombstone.

A correct `-t` name is skipped (covered by the existing engine tests, which passed). This is the disagreeing name. It does not resurrect the old body; it makes a green validate point at an unrestorable archive.

Minimal fix: if `jarvis_tombstone` and the `-t` suffix disagree, `validate` records `tombstone-flag-disagrees-with-filename` and restore treats the note as a bad record for that stem (still strict). Do not apply the tombstone JSON as an item.

Regression: the `tombstone-filename-not-checked` check.

### 5. Low — docs still say the engine is unwired and C2 was never enabled

`docs/design/mobile-continuity/OBSIDIAN-STORAGE-IMPLEMENTATION.md:4` says no live write hook, config key, CLI, migration, or real-vault run exists. `jarvis/second_brain.py:3` says the module is not wired into the daemon and that callers only opt in through `sync`. `docs/design/mobile-continuity/OBSIDIAN-RUNTIME.md:3` says `not enabled`, and `:89` says no real enablement or restart has occurred. `IMPLEMENTATION-PROGRESS.md` and `jarvis/daemon.py:367` contradict that: the drainer starts from the voice daemon, the CLI exists, and activation was recorded. The canonical-store sentence in the module docstring is still accurate (C3 is not cut over). The "not wired / not enabled" sentences are not.

Minimal fix: replace those status lines with "C2 write-through is on for memory, history, and tasks; plans/runs/knowledge stay backfill-only; C3 read cutover is not done."

No behavior test. The check is the contradiction with `daemon.py:367`.

## Verified fake (not filed)

One temp state dir, capture on, from the `controls` check:

- Capture failure inside the memory transaction rolled the title change back (title stayed `T`).
- Three revisions published in order; revision 1 still had empty meta after two later meta updates.
- Ack raised `OSError` after the note was written. Retry acked `unchanged=1`, `published=0`, and did not add a fifth revision (revisions stayed 1–4, the fourth being that crashed publish).
- A raw sqlite update with no `capture()` call was reconciled (`reconciled=1`).
- A `sk-` + 30 `a` note was held. Those bytes were not in any vault file. A payload with a trailing space was held as `spool-corrupt`.
- `reference/jarvis-records` as a symlink: sync returned `ok=false`, `unsafe-path: jarvis-records is a link or not a directory`.
- Two history commands 400 days apart both remained while capture was on.
- Direct `sync` of plans and runs wrote 2 file notes (the queue does not). Restore of that single ledger: `ok=true`, the running task came back `interrupted`, the running plan came back `blocked` with `restored_from_state=running`, and the mode-0755 run file came back `0644` with no execute bit.
- Two `start` calls shared one thread (`refs=2`). One `stop` left it alive. The second joined it and cleared `running()`.

## Known gaps, not new bugs

Plans, runs, and knowledge are still snapshot-only. `sync --apply` is refused while capture is on, so the CLI cannot refresh them; direct `SecondBrain.sync` still can. Deletion is not propagated and there is no revive API. There is no install id, lease fence, or C3 read path. Same-id memory collapse across two state dirs was reproduced (`note:shared` kept the second body only) and is that missing install id, not a separate defect.

## Not verified

- Live vault notes versus the live memory/history/task rows. Not opened.
- Parent `/v1/status`, `/api/fleet`, `/api/fleet/vault` figures. Not re-fetched.
- Kill -9 during the no-hardlink write fallback. The ack crash above is an in-process exception after a successful publish, not a torn file.
- A second machine actually calling `sync` into the real vault. Finding 3 is two temp dirs and a temp vault.
