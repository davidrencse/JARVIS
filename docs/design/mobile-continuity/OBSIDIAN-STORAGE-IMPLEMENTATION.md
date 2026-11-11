# Obsidian second-brain storage engine (implementation note)

Code: `jarvis/second_brain.py`. Tests: `tests/data/test_second_brain.py`.
Status 2026-11-11: **archive with C2 write-through; not a canonical cutover (C3).** The engine,
the write-through outbox (`second_brain_queue.py`), the drainer/CLI (`second_brain_runtime.py`),
the `second_brain` config key and a real-vault C1 backfill exist. Memory/history/tasks are captured
transactionally; plans/runs/knowledge get a scoped periodic file sync. SQLite/JSON state stays
authoritative: C3 (vault as canonical read source, edits/deletions/activation) is not done.

## Owner requirement and split

Personal facts, conversations, command/task history and knowledge content belong in the Obsidian
vault. Credentials, tokens, locks, live approvals/grants, execution receipts and rebuildable
indexes stay PC-private operational state (never Oracle, never the vault).
A vault export alone is not a cutover; the existing DBs remain the migration source and
compatibility store until rebuild plus write/read integration are proven.

## Storage model

Area: `<vault>/reference/jarvis-records/` (reserved, additive). Layout:

```
reference/jarvis-records/jarvis-records-index.md        created once, never rewritten
reference/jarvis-records/manifests/jarvis-sync-<utc>-<run8>.md   one per sync that wrote something
reference/jarvis-records/<store>/<kind>/<2hex>/jr-<kind>-<slug24>-<id12>-r0003-<payload12>[-t].md
```

* **Identity** = `store/kind/key` (e.g. `memory/memory-item/<id>`). `id12` = sha256(identity)[:12];
  basename is unique vault-wide (vault rule), lowercase-kebab, <= ~95 chars, vault-relative path
  budget 200 chars (Windows).
* **Immutable revisions.** Source unchanged -> nothing written. Changed -> `r(N+1)` with
  `jarvis_prev` = previous basename. Existing notes are never opened for writing, so human edits
  to any revision are never overwritten. Created exclusively, mode 0600, fsynced, directory
  fsynced (POSIX): temp file + `os.link`; where hard links fail, the final name is created with
  `O_EXCL` instead of `rename` (rename can replace on POSIX). A crash in that fallback can leave a
  damaged note at a revision name; validate flags it and the next sync appends a repair revision.
  Writers of one record take an `O_EXCL` claim file (`.jr-tmp-lock-<stem>`, broken after 300 s) and
  refuse if a revision >= theirs already exists, because the payload hash is in the filename and
  `os.link` alone only stops identical content. A busy claim is a retryable failure.
* **"Unchanged" is verified.** Filename digest match is not enough: sync reads the newest note and
  requires payload hash, frontmatter payload sha and revision to agree. Otherwise it appends a
  repair revision (`report["repaired"]`) and never edits the damaged one. Cost: one note read per
  record per sync.
* **Revision conflicts** (duplicate or missing numbers, e.g. from Obsidian-sync forks or hand
  deletion) mark the record `conflict`: sync refuses to append (failure), `latest()` raises,
  `restore` errors for it (strict aborts, non-strict skips). Owner resolves by hand.
* **Note format** follows the vault `CLAUDE.md`: YAML `title/type/tags/created/updated` (+
  `jarvis_*` fields), a wikilink to `[[jarvis-records-index]]` and the previous revision. Body =
  capped preview in a code fence (so user text can't create phantom links/tags) + the
  authoritative typed payload (one canonical JSON line, between sentinel comments).
  `jarvis_payload_sha256` covers that line; `jarvis_body_sha256` covers the body.
  Human edits outside the payload only show as `human_edited` in `validate()`; an edited payload
  voids restore for that revision (`payload-hash-mismatch`).
* **Lossless typed payload**: all row columns, source ids, timestamps, relationships.
  Memory links are grouped per `src` (`memory-links`), command hops nested in the command,
  task events nested in the task. BLOBs would be `{"$b64":...}` (none are exported).
  Files (plans/runs/knowledge) carry relpath, size, sha256, utf-8 text or base64.
  File mtime/mode are not preserved.
* **Bounds**: rows streamed in `fetchmany(500)` from the snapshot; one record in RAM at a time
  (the archive index holds filenames only). `max_record_bytes` (4 MiB) per payload; larger
  records/files are *reported* in `skipped` as `oversize`, never truncated, and make
  `complete` false. Chunked large-file capture is not built.
* **Path safety**: components are generated/validated, each directory is `lstat`-checked (symlink,
  junction, non-dir rejected) before use; notes are read with `O_NOFOLLOW`; scan skips links;
  file keys are re-validated (`safe_relpath`) on export and on restore. Residual race: a link
  swapped between check and use by a local attacker is not defended.
* **Secret hold**: records whose strings match token/private-key shapes are not written
  (`skipped: secret-like`); the owner can release specific ids via `allow_secret_like`.
  Heuristic, not a guarantee.

## What is captured

| Source | Captured | Excluded (and why) |
|---|---|---|
| `memory.sqlite3` | `items`, `links` | `vectors` (rebuildable), `items_fts` (index) |
| `history.sqlite3` | `commands` + `hops` | `confirmations`, `confirmation_audit`, `site_grants` (live authority/receipts); `decisions` (decider telemetry, not reviewed as personal) |
| `tasks.sqlite3` | `tasks` + `events` | - |
| `plans/plan-*.json` | whole file | anything else |
| `runs/<task>/` | top-level `.md .json .jsonl .txt` | `*.log`, subfolders |
| `knowledge/` | `raw/ wiki/ checks/ bots/` files, `engine.json`, `log.jsonl` | `.lock`, other paths |
| everything else in state | - | `inventory(state)` classifies each top-level entry as captured / excluded-(credential\|operational\|derived\|media\|biometric\|unreviewed-personal) / **unclassified**. `sync` reports `unclassified_state` and is never `complete` while any exist. `locations.json` and `camera.json` need an owner decision. Orphan `hops`/`events` (parent pruned) are not exported. |

Not covered at all: vault notes themselves (already canonical), `cluster.sqlite3`, voice, media,
browser profiles, other machines' state (only the local state dir is read).

## Consistency semantics

Each DB store is read from a `sqlite3.Connection.backup()` snapshot (source opened `mode=ro`),
so one store is transactionally consistent; the live file is never copied raw. Stores are
snapshotted one after another, so **cross-store state can disagree** (task in tasks.sqlite3 whose
command is not yet in history). This is not a fleet checkpoint; `report["scope"]` and each store's
`snapshot_at` say so. Files in plans/runs/knowledge are read individually (each file atomic, the
set is not).

## Amendment, deletion, tombstone

* Amendment = new revision. Pruning in the source (90-day retention, `forget`) is retention, not
  deletion: the vault keeps the last revision; `report["absent_from_source"]` counts them.
* `tombstone(record_id, reason)` appends a `-t` revision (owner intent only). `sync` **never**
  resurrects a tombstoned record, even if the source changed: it counts it in
  `tombstoned_skipped` and lists the id in `tombstoned_source_changed`. `restore` skips tombstoned
  records. Source changes made while tombstoned are therefore NOT archived (open blocker C1).
* **Not built:** revive/un-tombstone API (C1), deletion propagation vault -> source, vault-side
  redaction/GDPR erase (immutability means erase requires manual file deletion plus index awareness). Do not
  claim canonical deletion semantics yet.

## Failure behaviour

`sync()` returns a report and does not raise for per-record trouble. Vault unavailable ->
`ok False, error "vault-unavailable..."` before any read. A write failure is listed in
`failures`, source data is untouched (read-only snapshot), rerun is idempotent and finishes the
rest. Crashed writers leave only `.jr-tmp-*` (ignored; `sweep_stale_tmp()` removes old ones).
Reports contain ids, counts and reasons, never bodies.

## Restore

`restore(target_dir)` builds `memory/history/tasks.sqlite3`, `plans/`, `runs/`, `knowledge/` from
the notes alone, in a sibling staging dir renamed into place on success. Target must not exist
or be empty; nothing existing is overwritten; strict mode (default) aborts with no target on any bad
record. Restore chooses the newest revision per record and verifies it (payload hash, frontmatter
revision vs filename, identity, contiguous revision chain); it does not deep-verify older
revisions, so run `validate()` first for a full check. Embeddings/FTS are rebuilt by triggers/recompute, not restored; credentials, grants,
receipts are not recreated. **Restored work is inert:** queued/running tasks become `interrupted`
(with a `restored` event), running plans become `blocked`. Archive sync of records is not
execution approval and never resurrects a grant.

## Public API

```
SecondBrain(vault_root, max_record_bytes=4MiB, clock=utcnow)
  .check_vault() -> {ok, reason}
  .sync(state_dir, stores=None, knowledge_dir=None, dry_run=False, allow_secret_like=()) -> report
  .latest(record_id) -> payload dict | None
  .validate(deep=True) -> {ok, records, revisions, errors, human_edited, stale_tmp, ...}
  .tombstone(record_id, reason) -> {ok, status}
  .restore(target_dir, stores=None, strict=True) -> {ok, restored, errors, nonterminal_tasks, ...}
  .sweep_stale_tmp(older_than_s=3600)
inventory(state_dir); safe_relpath(store, rel); looks_secret(obj)
```
Stores: `memory history tasks plans runs knowledge`. Nothing runs on import.

## Remaining blockers before this can be called canonical

1. Live write hooks: Memory.add / History.record / task transitions call `sync`-like incremental
   archive (needs a per-record entry point and a retry queue; today only full-scan `sync`).
   Full scan cost is O(records) per run; fine for current sizes, needs a cursor later.
2. Read path: `jarvis/vault.py:55` `UNINDEXED = ("archive/", "templates/", "attachments/")` does
   not exclude `reference/jarvis-records/`, so once real records land the vault search cache would
   index every revision and its JSON payload line (ranking noise, cache growth, and old revisions
   appearing as separate hits). Parent decides: add the area to `UNINDEXED` plus a dedicated
   archive reader (`SecondBrain.latest`), or index only latest revisions. Source of truth during
   dual-run must also be decided. (vault.py is outside this task's ownership; not changed.)
3. Config: opt-in key, archive-area write policy in `vault` settings (separate from
   `vault.write=false`, which must stay for interactive tools), CLI (`jarvis archive sync|validate|restore`).
4. Owner decisions: `locations.json`, `camera.json`, `decisions` table, run `*.log`, `tasks.spec`
   redaction (specs may embed secrets; heuristic scan only).
5. Large-file chunking (oversize files are skipped and reported).
6. Real-data rehearsal: sync to a scratch vault copy, `validate`, `restore` to a scratch dir, diff
   typed rows; only then touch the real vault (parent does the first real sync after review).
7. Windows: hard-link fallback and path budget are untested on NTFS.
8. P6 replication: the vault may be synced between machines by Obsidian tooling; two machines
   archiving the same record concurrently can fork revision numbers (`r0003` twice with different
   payloads, producing a "revision-gap-or-duplicate" validate error). Only the MAIN writes archive
   revisions; replication must not elect writers or bypass handoff.
9. C1: revive API for tombstoned records (source changes while tombstoned are not archived), plus
   tombstone/erase policy and retention vs. immutability: owner decision.


## Parent integration and real rehearsal — 2026-11-11

Parent excluded `reference/jarvis-records/` from ordinary vault indexing and bumped its index
version, with a stale-index regression. 51 combined engine/index-boundary tests pass. The
release validation diagnostic `code-release-failed.json` is classified operational.

Private temporary-vault rehearsal:831 revisions, no skips/failures, validate/restore passed;
569 rebuilt memory/command/task rows exactly equal their archived typed rows. Temporary
personal copies were removed. The real configured MAIN then passed a complete dry-run and
additively backfilled831 revisions into `/home/jdoe/Documents/obsidian-vault`. Full validation
and scratch restore from that real vault passed. Existing notes/databases were preserved,
`vault.write` remains false; no restart occurred. This is C1 only, not live canonical cutover.

Remaining implemented policy: tombstones block all automatic resurrection; no revive API.
Coverage excludes operational state, media/biometrics, provider credentials and unreviewed
personal-domain files as listed above. Complete means the declared archive scope, not every
byte on a PC or across nodes. New personal mutations still require C2 transactional integration.
