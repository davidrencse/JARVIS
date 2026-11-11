# Final archive repairs: records over the 16 MiB note bound

Date: 2026-11-11. Scope: `jarvis/second_brain.py`, `jarvis/second_brain_queue.py`,
`jarvis/second_brain_runtime.py`, `tests/data/test_second_brain_overflow.py`.
Fixes AUDIT-CONSOLIDATION-BACKEND finding "pre-existing oversize head blocks the record".

## The bug

A record committed before capture was enabled (a baseline event) whose canonical payload is over
`MAX_RECORD_BYTES` (16 MiB) was queued as `held/oversize`. The head query picks the lowest seq per
record, held rows included, so that head blocked every later revision of the record forever. The
drain only released a held oversize row when it fitted the bound, which it never did. A mutation
of that record inside a user transaction also rolled back with `ArchiveCaptureError`.

## The fix: chunked overflow publication behind an explicit owner policy

The 16 MiB bound did not change. Overflow is off by default and is turned on per ledger by an explicit
operation:

- `second_brain_queue.set_overflow(state_dir, max_bytes)` stores `archive_meta.overflow_max_bytes`
  in every configured ledger.
- `max_bytes` must be 0 (off) or in the range (16 MiB, `MAX_OVERFLOW_BYTES` = 64 MiB]. Any other
  value raises `ValueError`.
- Calling it publishes nothing. On the next drain, held `oversize` rows within the limit go back to
  `pending` and are counted in `released_oversize`.
- With the policy on, a transaction mutation of a record within the limit is queued instead of
  rolled back. A record over the limit still rolls back, so the limit stays honest.

### On-disk format (immutable, validated)

Each record over the bound is stored as one manifest note plus several chunk files.

**Chunk files**
- Name: `jrc-<sha(record_id)[:12]>-r<rev>-<idx:04d>-<sha12>.jrc`, in the same folder as the note.
- Each chunk is at most `CHUNK_BYTES` (4 MiB).
- Chunks belong to one revision; they are never shared between revisions.
- They are written first, through the same exclusive tmp + `os.link` path used for notes.
- If a chunk already exists:
  - with the same bytes, it is reused;
  - with different bytes, publication stops with `SecondBrainError("chunk-corrupt …")`, and an
    owner must resolve it.

**Manifest note**
- The normal `jr-…-rNNNN-<payload12>.md` name.
- Its payload block holds
  `{"format":"jarvis-record-chunks/1","bytes","sha256","chunks":[{name,bytes,sha256}]}`.
- Frontmatter adds `jarvis_payload_chunked: true`, `jarvis_payload_bytes` and
  `jarvis_manifest_sha256`.
- `jarvis_payload_sha256` is still the hash of the **full** payload, so the filename, the queue ack
  and parity keep their meaning.
- The note itself stays small (tests check that it is under 64 KiB).

**One reader.** All vault reads go through `SecondBrain._load(path)`. It is the single payload
codec and validator for publish-verify, `latest`, `validate`, `restore`, parity, identity-clash,
note-intact and tombstone-supersede checks. For chunked notes it checks, in this order:

1. the manifest hash;
2. the format;
3. the size bounds (0 < bytes ≤ 64 MiB, chunk count = ceil(bytes / CHUNK_BYTES));
4. per chunk:
   - the name pattern;
   - that the chunk's record and revision match the note;
   - index order and no duplicates;
   - the SHA prefix in the name;
   - an exact size read through `O_NOFOLLOW`;
   - the chunk hash;
5. the hash of the whole payload.

Any failure raises `ValueError("chunked-payload: …")`. Notes written in the old format parse as
before (`chunked: False`). Tombstoning reads only the metadata, so damaged chunks can't block a
deletion.

### Ledger truth, crash behaviour and secrets

- **Ack after verify.** The queue acks an event only after `publish()` returns
  `written`/`unchanged`. `publish()` writes every chunk, then the note, then reads the note back
  through `_load`, which re-hashes every chunk.
- **Crash mid-way.**
  - The event stays `pending`.
  - Chunks already on disk can't be mistaken for a note, because nothing names them until the note
    exists.
  - A retry reuses identical chunks, so recovery is idempotent.
- **Secrets.** `looks_secret` runs on the full object before chunking. A huge record that looks
  secret is held as `secret-like`, and reports carry only ids and counts. The owner's
  `allow_secret_like` still works.
- **Memory.**
  - Publication builds the full payload once, the same as every other record.
  - Reading holds at most one chunk plus the assembled payload, bounded by 64 MiB.
  - No vault-wide copy is made.

### Status and CLI (fields added, none renamed)

- `queue.status()` adds `overflow_max_bytes` and `held_oversize_bytes` (the largest held oversize
  event).
- `runtime.status()` adds `archive_overflow_max_bytes` and `archive_held_oversize_bytes`. Both are
  integers.
- `drain()` report adds `chunked`.
- `runtime.cli` action `overflow`:
  - With no `mib`, it shows the policy.
  - With `mib=N`, it sets the policy. This is the explicit operation.
  - Errors: `overflow-limit-out-of-range` and `overflow-not-saved`.
  - It needs no vault.

## Evidence

`tests/data/test_second_brain_overflow.py` is the permanent repro. Most tests shrink the bound to
4 KiB with 1 KiB chunks so they run fast. Two tests run at the real 16 MiB bound: the 20 MiB
round trip, and a flipped chunk at that size.

| Requirement | Test |
|---|---|
| Policy off: head held and visible, never dropped; transaction write rolls back | `test_policy_off_keeps_head_held_and_visible_never_dropped` |
| Held head released; transition while huge; every revision published in order, original intact | `test_policy_publishes_held_head_then_later_revisions_in_order` |
| Raw rewrite of the immutable task spec ("shrink") → held `identity-collision`, original never overwritten | same test |
| Secret in a huge payload: held, no leak in reports or status; owner allow publishes | `test_secret_in_huge_payload_is_held_without_leaking` |
| Crash after a partial chunk set: not acked, retry idempotent | `test_crash_after_some_chunks_is_not_acked_and_retry_reuses_them` |
| Foreign bytes at a chunk name: fails closed, stays pending | `test_foreign_bytes_at_a_chunk_name_fail_closed` |
| Truncated chunk: `latest`/`validate`/`restore` fail loudly, parity shows the damage, policy-gated repair writes a new revision | `test_truncated_chunk_breaks_read_validate_restore_then_repairs` |
| Duplicate, reordered, foreign-revision and over-cap manifests (rehashed forgeries); unhashed edit | `test_duplicate_reordered_foreign_and_unhashed_manifests_are_rejected` |
| Rebuild from the vault alone; old-format notes alongside chunked ones | `test_restore_from_vault_only_and_old_notes_coexist` |
| Read-only parity and numeric status, no payload text | `test_parity_and_runtime_status_are_numeric_and_read_only` |
| Policy bounds and CLI | `test_set_overflow_bounds_and_cli` |
| Real size: 20 MiB baseline task, 16 MiB bound, 4 MiB chunks, transition while huge, restore | `test_twenty_mib_baseline_task_is_published_and_restored_losslessly` |
| Flipped chunk: status is `error`, rescan/drain appends a revision, old bytes stay | `test_corrupt_chunk_is_not_current_and_rescan_appends_a_revision` |
| Truncated chunk: same repair, the short file is not grown | `test_truncated_chunk_repairs_without_growing_the_old_file` |
| Crash before the note: not acked, retry reuses the chunk | `test_crash_before_the_note_is_not_acked_and_retry_reuses_chunks` |
| Recreated ledger copies the sibling overflow cap | `test_recreated_ledger_keeps_the_owner_overflow_policy` |
| Full size flipped chunk, policy carried on dry-run sync, repair via drain | `test_fullsize_corrupt_chunk_is_not_current_and_rescan_repairs` |
| `oversize: N bytes` counts as `oversize` in the sync view | `test_oversize_reason_is_counted_as_oversize` |

Archive suites (`tests.data.test_second_brain_overflow` 17, `tests.data.test_second_brain` 49,
`tests.data.test_second_brain_queue` 33, `tests.data.test_second_brain_storage` 28,
`tests.runtime.test_second_brain_runtime` 41, `tests.runtime.test_archive_cli` 22,
`tests.runtime.test_archive_history_faults` 2): 192 tests, OK. The parent runs the full suite.

`/tmp/jarvis-consolidation-backend-audit.py` (`archive_controls_and_held_head`) still shows the
held head, because it never turns the policy on. That is the intended default. The repro's raw
`UPDATE tasks SET spec` is an identity change, not a legal shrink.

## Remaining limits (honest)

- **CLI.** `jarvis archive overflow` and `--mib` are registered in `jarvis/__main__.py`. With no
  `--mib` the command shows the policy; with `--mib` it sets it. It writes ledger metadata only.
- **Size limit.** Records over 64 MiB stay held as `oversize`: visible, never dropped, never
  published. Transaction writes over the policy still roll back.
- **Repair.** `rescan` only seeds the backlog. The next drain that can read the vault drops a
  matching head only after `publication_sound` reads the newest note through `_load`. A damaged
  chunk is not rewritten. The drain appends the next revision (new chunk names) and acks only
  after that note reads back. `jarvis archive drain --rescan` is the command while capture is on.
  `jarvis archive sync --apply` stays refused then (`capture-active-use-drain`). Dry-run sync,
  verify, and file sync carry each ledger's overflow policy, so an in-policy record is not an
  `oversize` skip. A recreated ledger copies the highest sibling `overflow_max_bytes`.
- **Status.** `current` requires that read-back. Status opens every published head whose note is
  at most 64 KiB, or that has a `.jrc` beside it, through the same loader. One failure makes
  `state` `error` and `archive_chunk_damaged` at least 1. A plain note larger than 64 KiB with no
  chunk file is not opened by status.
- **Evidence.** The damaged revision stays on disk. `validate` still reports it after the repair.
  `current` means the acknowledged head reads back, not that every older revision is intact.
- **Orphan chunks.** Chunks left by a crash with no note naming them stay on disk until the next
  attempt reuses them. There is no garbage collector (append-only vault policy).
- **Memory.** The writer builds and hashes the full payload in memory (≤ 64 MiB). This is not a
  streaming writer.
- **Not implemented.** Lease fencing, the Oracle handoff and the C3 canonical read are not
  implemented by this change. Tombstones are not resurrected. A revision conflict or an identity
  collision is not turned into a chunk repair. `no-archive` records stay counted and unqueued.

## Safe activation

1. Run `jarvis archive status` and read `archive_held_oversize_bytes` and `archive_chunk_damaged`.
2. If the held value is over 16 MiB and you want it archived, run `jarvis archive overflow --mib 32`
   (any value up to 64). This writes only ledger metadata.
3. The next scheduled drain publishes the chunked revision and every revision queued behind it.
   `jarvis archive validate` confirms the result. `state: current` also means the published heads
   that status opens read back.
4. A damaged chunk is repaired with `jarvis archive drain --rescan` while capture stays on. That
   appends a revision. It does not replace the damaged file. `validate` will still list the old
   revision.
5. Turn the policy off with `--mib 0`. Notes already published stay readable.
