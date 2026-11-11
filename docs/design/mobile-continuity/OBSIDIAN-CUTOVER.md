---
title: Obsidian-canonical personal state — cutover design and integration slices
status: design-draft-pending-review
authorization: owner-requirement-2026-11-11 (storage work only; approval gates in DESIGN.md unchanged)
updated: 2026-11-11
scope: documentation only; no production code, vault write, DB read/migration or config change was made
---

# Obsidian-canonical personal state

Companion to DESIGN.md §12 (amended), IMPLEMENTATION-PROGRESS.md, P1-BOUNDARIES.md.
Owner requirement (2026-11-11): *"please ensure all data and memory are in the obsidian second
brain utilize that feature"*. This file turns that into a concrete cutover. Everything under
"Design" and "Slices" is **future**; "Today" describes the repository as inspected.

## 0. Status — read this first

| Stage | Meaning | Canonical store of personal records | State |
|---|---|---|---|
| C0 | Today | SQLite (`memory.sqlite3`, `history.sqlite3`, `tasks.sqlite3`). Vault is read-only recall (`vault.write=false`). | **Current** |
| C1 | Exporter + rebuild tool (`jarvis/second_brain.py`) | Still SQLite. Vault notes are a **mirror**. | Implemented, real backfill/rebuild verified 2026-11-11; **not a cutover** |
| C2 | In-transaction outbox + drainer, write-through | SQLite mutation plus transactional pending journal; only published, validated vault revisions have vault durability. Transitional, not fully vault-primary. | Designed here |
| C3 | Rebuild + write/read integration proven (§9 gate) | Vault. SQLite = derived index + operational spool. | Designed here; **cutover is declared only here** |
| C4 | Retention/prune and corruption recovery lean on the vault | Vault | After C3 + owner approval |

Hard statements for every report, UI string and commit message:

- An exporter, a mirror, or a one-shot migration is **not** the cutover. Cutover = C3 gate passed.
- Existing databases stay as compatibility and migration source until C3 passes **and** owner
  approval. No historical data is deleted. Pre-cutover copies are `*.pre-obsidian-cutover`.
- "Remembered/saved" wording to the owner must not claim vault durability before the ledger says
  archived (§4.3).
- No claim that SQLite and the vault are atomic with each other. They are not (§4).

## 1. Data classification (what goes where)

**Principle:** personal facts, conversations, command and task history and content are canonical
in the vault. Credentials, tokens, locks, live authority, execution receipts and rebuildable
indexes are PC operational state. Oracle never receives personal bodies (DESIGN.md §13.1).

### 1.1 Canonical in the vault (owner's personal records)

| Domain | Source today | Archived fields | Excluded / derived on rebuild |
|---|---|---|---|
| Memory items | `memory.sqlite3` `items` | `id, kind, title, body, tags, source, importance(at creation), created_at`, meta keys `was, retired_at, by, replaces`, state (active/retired kind) | `digest` (recomputed: sha256(body[+id for events]); retired rows `digest-id`), `items_fts`, `meta.usage` and per-call counters, `meta.seen` (derived) |
| Memory links | `links` | `src, dst, rel, created_at` as wikilinks | none |
| Conversation | `items` kinds `message`, `reply` | text, channel, id, timestamp | |
| Command history | `history.sqlite3` `commands` | `id, request, at(epoch), origin, origin_class, text, device, intent, args (after §6 redaction), state, ok, result, observed_ms, total_ms` | `key` (recomputed by `command_key`), `error_class` (recomputed), timing hops may be archived as audit evidence; never execution authority |
| Task content/history | `tasks.sqlite3` `tasks`+`events` | personal task specs, objectives and sanitized progress from creation onward; terminal outcomes and timeline without `pid` | live lane claims, `pid`, process/permission authority; rebuild never resumes execution |
| Consolidation summaries, plans, results, cases | `items` kinds `consolidation, plan, result, case` | as Memory items | |
| Knowledge wiki pages | `knowledge.py` wiki `.md` | pages (later slice K, §10) | raw blobs, `engine.json`, bot state, `log.jsonl`, `checks/` |

### 1.2 Operational — stay in PC databases/files, never vault bodies, never Oracle

`confirmations`, `site_grants`, `confirmation_audit` (live authority and receipts),
non-terminal execution claims (personal task content remains in scope), `*.lock` files, `vault-writes.jsonl` (write receipts), cluster/pairing/
node link state, credentials, tokens, provider sessions, `jarvis.config.json`, decide.py shadow
decisions (derived training signal), `archive_outbox`, `archive_ledger`, `archive_meta`.

### 1.3 Derived / rebuildable (never canonical, safe to drop and rebuild)

`items_fts`, `vectors` (embeddings), `vault.sqlite3` (search cache over the vault), recall caches,
`archive_ledger` (rebuildable from a scan of `jarvis/` frontmatter).

### 1.4 Invariants

1. Syncing or rebuilding personal records is **not** live execution approval. No archive note,
   rebuild or replica creates, extends or resurrects a confirmation, site grant, lease,
   capability or lock. Rebuilt command history is evidence only (history.py contract).
2. Archive notes are *data, not instructions*. They carry `jarvis_trust: generated`.
3. The archive writer never writes outside its namespace root and never modifies a note it does
   not own (§5).
4. Only the single current MAIN's drainer writes the archive (§8). Nodes never do.
5. Oracle persists no archive body. A sync provider for the vault, if any, is a separate owner
   decision (§7), not an Oracle role.

## 2. Today: mutation entry points, context, failure semantics

Inventory by read-only scout + direct reads (line numbers as of 2026-11-11 working tree; verify
before editing). All stores use `store.connect_database` (`sqlite3.connect(timeout=10)`,
`with db:` commit/rollback).

| Store | Entry point | Constructed with | Notes |
|---|---|---|---|
| Memory | `Memory.add` memory.py:~215, `_link`, `link`, `retire`, `absorb`, `note_correction`, `merge_meta`, `index_vectors`, `prune_vectors` | `Memory(directory, semantic=False)` | **no config**. Callers: assistant.py (message/reply, 28/180/685/700/710/796/806), ui.py:459, orchestrator.py:204/320, consolidate.py:224-232, MemoryTools `remember`/`profile`/`profile_forget`, `__main__.py:826-857` |
| History | `History.record`, static `record_confirmation` | `History(directory)`; built in `pipeline._history` (cached per state), assistant.py:746, **and inside every `ConfirmQueue(directory)`** (confirm.py:75) | **no config**; ≥11 construction sites |
| Tasks | `Store.add`, `transition`, `reap_stale`, `prune` | `Store(directory, lanes)` | prune runs at construction once a day |
| Vault | `Vault._save/_create/_audit`, `Tasks.add/update` (Obsidian task lines) | `vault_for(config, state)` per process | gate `vault.write` (live), `READ_ONLY=("archive/","templates/","attachments/")`, `protected`; **only constructor path with config** |
| Knowledge | `KnowledgeBase.ingest/resolve/merge/check/ask`, `_save` | `knowledge_for(config, state)` (default root `state/knowledge`) | own files; not in scope of this slice set |
| Lifecycle | daemon.py:327-329 builds Store/Memory (not on nodes), :364 `warm_vectors` + `consolidate.start`; ui.py:158-160, 189-191, 219-223; `__main__.py:476,742-743,585,609` | | consolidate guarded by `enabled(config)` (`not is_node`) + `exclusive_lock(consolidate.lock)` |

**Constructor context.** Available: directory only (Memory/History/Store/ConfirmQueue);
`semantic`, `lanes` precomputed by callers. Config exists only in daemon/ui/CLI/`vault_for`.
*Consequence:* archive enablement, vault location and role must **not** be constructor
parameters. The in-transaction hook is gated by a flag stored in the DB (`archive_meta`), and the
vault/role/config is resolved only by the drainer, which runs where config exists (§4.2).

**Failure semantics today.**
- `Memory.add` returns the existing row on a (kind,digest) hit and does **not** write; retries 8× on id clash then `RuntimeError`.
- `Memory.absorb` and consolidate use several short transactions: not atomic across items; summary written last.
- `History.record` has no try/except, but `pipeline._record` and callers treat it as non-fatal ("Recording never fails the turn"): a raised exception silently loses the history row. History prunes rows older than `KEEP_DAYS=90` on **every** `record()`.
- `Memory.retire` never deletes: it changes `kind` (`forgotten/replaced/closed`) and keeps the body. **There is no erase today** — "forgotten" is only excluded from recall.
- `Vault._save`: file written before `_audit` and `_index`; crash in between leaves note without audit line.

**Not inventoried (gaps to close in slice S0 before relying on this table):** `clipsync`,
`dropzone`, `transfer`, `decide.py` DB path, `voice` transcripts, `phone.Store` (pairing: operational), whether nodes write `history.sqlite3` rows of their own (`daemon.py:118` constructs `ConfirmQueue` on a node).

## 3. Vault layout, record format, and ownership

**Implemented C1 format takes precedence over the proposed layout below:** the tested storage
engine writes immutable revision notes under `reference/jarvis-records/<store>/<kind>/<2hex>/`
with `jr-…-rNNNN-<payload12>.md` names and a `jarvis-records-index.md` map. It does not rewrite
human notes or append daily segments. `validate()` detects conflicts; user edits are preserved,
not yet ingested as active Memory records. Secret-like records are held, not silently redacted.
C2 must build on this tested format or explicitly migrate it; the alternative `jarvis/` paths
and day segments below describe earlier proposals, not deployed behavior.


New namespace root `second_brain.root` (default `jarvis/`) — **not** `archive/` (the vault's
`archive/` is a read-only junk folder). Vault `CLAUDE.md` rules apply: lowercase-kebab filenames,
unique basenames vault-wide, YAML `title/type/tags/created/updated`, `[[wikilinks]]`, absolute
dates, additive only.

```
jarvis/_index.md                                 owner-approved sentinel + map (type: index)
jarvis/memory/<kind>/jarvis-<kind>-YYYYMMDD-<hex6>.md       one note per non-event memory item
jarvis/conversations/YYYY/MM/jarvis-conversation-YYYY-MM-DD.md   append-only day segment
jarvis/commands/YYYY/MM/jarvis-commands-YYYY-MM-DD.md            append-only day segment
jarvis/tasks/jarvis-task-<uuid>.md                                terminal tasks
jarvis/state/jarvis-state-<id>-r<N>.md                            state events for protected notes (§5.3)
```

- **Knowledge-class records** (facts, preferences, lessons, loops, profile, plan, result,
  consolidation, case): one topic = one file, per vault golden rule 1.
- **Event-class records** (message, reply, command, terminal task timeline): append-only day
  segments. One file per message/command would flood the vault; per-day rewrites would be
  O(n²) and sync-hostile. Blocks are delimited `<!-- jarvis:rec id=<id> sha=<sha256> -->` …
  `<!-- /jarvis:rec -->`. Append is `O_APPEND` + `fsync`; a torn tail block (no closing marker)
  is ignored and re-appended on recovery (idempotent by id). Roll to `-part-2` past 1 MB.
- Frontmatter **fits the existing schema without extending it**: `type: note` (knowledge),
  `reference` (transcripts/commands), `index` (`_index`); extra keys are allowed by the schema
  (`jarvis_id, jarvis_kind, jarvis_rev, jarvis_state, jarvis_writer (device uuid),
  jarvis_epoch, jarvis_sha, jarvis_trust, jarvis_tags, jarvis_meta`). Memory tags are kept in
  `jarvis_tags` (their charset differs from vault tag vocabulary); `tags:` uses
  `[jarvis-archive, jarvis-<kind>]`.
- The owner authorized additive personal archival storage. Creating a confined namespace and
  sentinel is routine implementation within that request; no separate approval is inferred.
  Existing hand-tuned map files need a backup before any additive update. Final namespace and
  codec must match the tested S1 implementation; paths shown here remain proposed.
- The exact serializer/parser belongs to `jarvis/second_brain.py` (other lead). This document
  fixes semantics: ids, revisions, ownership, ordering, redaction, tombstones.
- **Vault recall must skip the namespace** (or dedupe by `jarvis_id`): otherwise `vault.recall`
  and `Memory.context` return the same record twice, and generated reply text (which can quote
  untrusted web text) is re-injected as if it were owner-authored vault prose.

## 4. Write path: outbox first, honest durability

### 4.1 Choice and why

Vault-first-write would block the request path on filesystem/sync availability and still not be
atomic with the DB index. Pure async export loses records on a crash. Chosen: **transactional
outbox in the same SQLite file as the mutation + at-least-once idempotent drainer**, with an
inline best-effort drain right after commit (normal case is effectively write-through).

- Outbox rows bind an immutable revision, operation, sequence and payload digest to the exact
  personal mutation. Preserve the revision body in the same transaction (directly in the
  outbox or a referenced immutable revision table); IDs alone cannot preserve overwritten or
  deleted content. This temporary private spool stays on PCs and drains only after verified
  durable publication. Coalesce only explicitly derived updates, never history events.
- Archive payload excludes credentials and live authority. Completed personal evidence and
  active task objectives may be archived, but rebuild never reactivates execution.
- The outbox is a **spool, not canonical**. It is PC operational state.
- The ledger `archive_ledger(record_id, rev, path, sha, state, archived_at)` records the last
  fsynced vault write per record. A record is "archived" iff ledger sha equals the note sha.
- Per-DB tables (`archive_meta`, `archive_outbox`, `archive_ledger`) live in `memory.sqlite3`,
  `history.sqlite3`, `tasks.sqlite3` respectively: same-DB is the only place the enqueue can be
  atomic with the mutation. SQLite offers no cross-file atomic commit that we rely on.

### 4.2 Hook and lifecycle

- `second_brain.enqueue(db, domain, op, record_id)` is called **inside** the existing
  transaction of each mutation (`Memory.add` only when `INSERT` actually changed a row;
  `retire`, `_link`, metadata mutations; `History.record`; task creation and personal state transitions). It reads
  `archive_meta.enabled` from the same DB, so no constructor/config change is needed at the ≥20
  construction sites. When disabled it is a no-op and behaviour is unchanged.
- When archive mode is enabled, enqueue failure rolls back the enclosing personal mutation.
  Backfill is a recovery audit, not a substitute for this atomicity. A completed OS effect
  cannot be rolled back: failure to record its history must surface a recording/durability
  fault and must never cause the effect to run again. Existing callers that swallow history
  errors need a visible fault path before canonical cutover can be declared.
- **Drainer** `second_brain.start(config, state, log)` mirrors `consolidate.start`: background
  thread started next to `consolidate.start` in `daemon.py` (~364) and `ui.py` (~189), guarded by
  `not is_node(config)` (later: "current MAIN under valid lease", §8) and
  `exclusive_lock(<state>/archive.lock)` so the daemon and console never drain concurrently.
- The drainer, not the stores, resolves config, vault path (`vault_path(settings)` per-OS), device
  id and archive policy (§5.1). Windows path `C:\Users\jdoe\Documents\Vault` and Linux
  `~/Documents/obsidian-vault` are resolved there only.
- Prune coupling: with the archive enabled, `History.record`'s 90-day prune, `Store.prune` and
  `decide` pruning must not delete a row whose ledger state is not `archived`.

### 4.3 Failure semantics (explicit)

| Failure | Behaviour | Durability claim |
|---|---|---|
| Crash after DB commit, before vault write | Outbox row survives; drainer writes later | Record is **DB-spooled, not yet canonical** |
| Crash after note fsync, before ledger commit | Startup recovery sees note with matching sha → ledger completed; no duplicate | Canonical |
| Torn append (day segment) | Validate framed length/digest; incomplete suffix stays quarantined; publish a fresh immutable segment/revision, never append inside a torn frame | Canonical only after a valid published revision |
| Vault offline / sentinel missing / read-only | Drainer pauses, outbox grows, UI shows `archive_lag` (count, oldest age); requests unaffected | DB-spooled only; never silently "saved to Obsidian" |
| Disk full | Same as offline; alert at threshold; **never drop** outbox rows | Spooled |
| User edited the note | §5.2 | Vault wins |
| Two processes drain | `archive.lock` serialises; loser backs off | — |

Vault reachability probe (before any write): root exists **and** sentinel `jarvis/_index.md`
exists (guards an unmounted bind mount creating a stray vault) **and** namespace dir writable
with `no symlink` components. Missing → offline mode.

## 5. Archive write policy (separate from interactive vault editing)

The owner authorized personal archival writes, not broad vault editing. `vault.write` stays
`false` and keeps gating `VaultTools.write`/`Tasks.*` (interactive surface). A distinct archive
policy (`second_brain.enabled`, default false) governs only the drainer.

### 5.1 Rules

- Only paths under `second_brain.root`, only `.md`, never a symlink, never `..`, never
  `READ_ONLY` or `protected`. Creation uses the existing no-overwrite primitive
  (`Vault._create`: temp file + `os.link`, `O_EXCL` fallback); append uses `O_APPEND`.
- An existing note is rewritten **only** if its on-disk sha256 equals `archive_ledger.sha`
  (archive-owned and unedited) and via `atomic_write(..., fsync=True)`.
- Every archive write is audited to the PC-local `vault-writes.jsonl` (channel `archive`).
- Archive writes never touch `calendar.md`, `MEMORY.md`, `daily/`, `tasks/` or any non-namespace note.

### 5.2 User edits to generated notes (Obsidian is canonical)

Reconcile scan (mtime/size → sha) at startup and every drain cycle:
- **sha ≠ ledger sha ⇒ user edit.** The note is now *protected*: the archive never rewrites it.
  The edited frontmatter/body is ingested into the derived DB (`meta.edited_in_vault=true`,
  vectors for the id dropped and re-embedded). Vault wins over any DB state.
- **New unknown note under the namespace** with a valid `jarvis_id` schema ⇒ imported as
  `source="vault"`, importance capped 0.5; text is data, never changes config/permissions.
  Conflict-copy filenames from sync tools and duplicate `jarvis_id` files are never chosen
  silently: both are surfaced as `archive_conflict` and ignored until resolved.
- **File missing** ⇒ unresolved absence, not proof of intentional deletion. Mount loss,
  partial sync and moves can look identical. Suspend overwrite/backfill of that identity and
  reconcile manifests, moves and explicit owner deletion evidence. Only an explicit durable
  tombstone retires it; never infer mass deletion from an empty or incomplete replica.

### 5.3 State changes on protected notes

Later DB-side changes (retire, replace, link) for a protected note are written as new
append-only sibling notes `jarvis/state/jarvis-state-<id>-r<N>.md` (`jarvis_target`,
`jarvis_state`, `jarvis_writer`, `jarvis_epoch`); rebuild applies them in `N` order. The user's
note is never altered.

## 6. Deleted, forgotten, tombstoned, erased

Four different things; current code supports only the first.

| Concept | Meaning | Vault | DB / derived |
|---|---|---|---|
| **Retire** (exists: `retire`) | Excluded from recall; history kept | `jarvis_state: retired` + `retired_at/by/was` (rewrite if unedited, else §5.3 state note) | kind `forgotten/replaced/closed` as today |
| **Tombstone** | Record intentionally gone; must not come back | Archive-owned stub note keeps `jarvis_id`, `jarvis_state: tombstoned`, `tombstoned_at`, reason class; body empty. Explicit delete emits a durable vault tombstone revision; missing file alone is unresolved, not a deletion. Local ledger alone cannot prevent resurrection after rebuild | Row retired; ledger blocks backfill/rebuild resurrection |
| **Erase (privacy deletion)** — *new, not implemented* | Owner demands content removal | Body replaced by stub in place (owner-confirmed via confirm queue; overrides edit protection, audited) | body blanked, FTS entry removed (`items_fts` rebuild), `vectors` row deleted, `vault.sqlite3` row dropped, `vacuum` of freed pages |
| **Expire** | Retention prune of hot DB window | Never deletes the note | Allowed only when ledger says archived |

Limits stated honestly: erase cannot purge copies in sync-provider version history, OS file
history, backups, or provider logs of a model that already saw the text. Resurrection from an old
DB backup is prevented only if the ledger/tombstone stubs survive: restoring a pre-erase DB runs
`archive reconcile` which applies vault tombstones before serving recall. Highest-sensitivity
content should use the §6.1 no-archive exclusion rather than erase-after-the-fact.

### 6.1 Secret exclusion versus lossless personal data

- Aim for complete personal content coverage. Each archived revision declares whether it is
  lossless or redacted/excluded; a redacted snapshot cannot reproduce the original DB.
- **Never archived (stay PC-only, flagged):** records tagged `no-archive` or `meta.archive=false`; any record in a denylist kind (credentials, provider/session files are not memory items at all).
- **Redaction pass before render** (best-effort, not a guarantee): high-confidence secret shapes (PEM blocks, JWT-like, vendor key prefixes, `tskey-`, bearer/basic auth headers, `password|token|secret=` pairs, long high-entropy tokens next to key words) in `body`, `request`, `text`, `result` and `args` values are replaced by `[redacted:<class>:<sha8>]` in the **vault copy only**; the DB keeps the original and `meta.archive_redacted=n`. After a rebuild the placeholder is all that remains — the documented, counted, owner-visible loss. Such records block any claim of exact original-state rebuild and cannot justify deleting their original DB source without a separate intentional loss policy. `archive status` lists redaction counts, not values.
- `args` for commands that carry typed text/URLs with query strings are scrubbed through the same pass plus URL query/fragment stripping; unknown intents default to redacting values of keys matching the secret-key list.
- history.py's current docstring ("arguments stay in SQLite; nothing is written to the vault") must be revised when slice S2 lands, to point at this redaction rule.
- Task specs and replies may embed secrets typed by voice; the same pass applies. No scanner removes the need for the owner to not dictate secrets.

## 7. Privacy, encryption, sync retention

- The vault is plaintext Markdown on disk. Disk-level encryption (LUKS/BitLocker) is a PC
  property to confirm, not something JARVIS provides. File mode of archive files: owner-only
  (`0600`) on Linux where the vault policy allows.
- Inventory the existing vault transport without reading credentials or changing its sync
  configuration. The owner explicitly requested this configured Obsidian vault; additive
  storage is authorized. Existing vault sync may carry notes outside the PCs; report that
  fact and do not claim local-only transport without evidence. Do not enable a new provider
  or change encryption/retention settings as part of archival integration. Provider-specific
  encryption/retention claims require primary-document verification.
- Oracle: still receives no archive bodies, no vault, no outbox. The gateway/Oracle path has no
  route to the vault. Test: grep gateway modules for the archive root and ledger names.
- Sync providers and backups retain prior versions; erase/tombstone guarantees stop at the local
  vault (§6).

## 8. PC transfer and single-writer barrier

- Exactly one MAIN writes the archive. The drainer checks local role today
  (`not is_node(config)`); P2+ replaces this with "holds the current valid lease/epoch". Archive
  writes stamp `jarvis_writer` (device uuid) and `jarvis_epoch` for audit only; **these stamps are
  never authority** — only the Oracle lease/epoch is.
- Vault replication between PCs (P6) is a **transport**. It elects no writer, resets no epoch and
  cannot bypass MAIN handover. The standby's vault replica is read-only for the archive writer.
- **Planned handover:** old MAIN freezes new personal writes → drains outbox to zero → writes
  `jarvis/state/jarvis-barrier-<transition_id>.md` (last ledger seq + digest of ordered
  `(id, rev, sha)`) → candidate proves its replica contains that barrier and a recomputed digest
  match (not "recent mtime") → candidate rebuilds derived indexes → authority epoch advances.
  Obsidian sync of the barrier is evidence of state, not approval.
- **Unplanned loss:** candidate rebuilds from whatever its vault replica holds; the dead MAIN's
  un-drained outbox is **unavailable until that PC returns**, and the UI labels the gap
  (DESIGN.md §12.2). A returning old MAIN is demoted first; its stale-epoch outbox is imported
  through reconcile as late records (original ids/created times; namespace by stable source-installation identity and detect all
  conflicting IDs/revisions; random suffixes reduce collision probability but do not prove uniqueness), flagged for owner review, never drained blindly.
- Settings and operational DBs do not move with the vault; the standby re-creates grants/
  consents locally (DESIGN.md §12.1).

## 9. Startup recovery, rebuild, cutover gate

**Startup order (MAIN, archive enabled):** open DBs → probe vault (offline ⇒ run degraded, no
writes) → acquire `archive.lock` → complete ledger for notes already written → reconcile scan
(user edits/new/missing) → resume drain.

**Corrupt or missing DB:** `PRAGMA integrity_check`; failure ⇒ keep the file as
`<db>.corrupt-<ts>`, rebuild into a temp DB from the vault, swap in, then re-embed vectors. If the
vault is also unreachable: start with **no memory and a visible banner**; never create an empty
DB that later wins over the vault. Operational authority tables (confirmations, grants, leases) come back
empty by design. Historical timing evidence may be restored without authority.

**Rebuild** `rebuild(vault_root, out_dir, window)` parses `jarvis/` to fresh `memory`/`history`/
`tasks` DBs: ids and timestamps verbatim, `digest/key/error_class` recomputed, commands/tasks
loaded for the 90-day hot window (vault keeps all), conversation events and memory items in full.
Never reads or writes operational tables.

**Cutover gate (C3), all required, evidence recorded in IMPLEMENTATION-PROGRESS.md:**
1. Round-trip: fake DB → drain → rebuild → equal id sets, digests, `created_at/at`, link sets, retired states; documented lossy fields only.
2. Write/read integration: with the **live** read path switched to a rebuilt DB, recall (BM25+vector), H1/H3 checks, `Memory.search`, console history and task views match the pre-rebuild fixtures.
3. Fault matrix passes (§4.3), including vault offline, crash between each pair of steps, torn append, user edit/delete/move, conflict copy, duplicate `jarvis_id`.
4. Redaction fixtures: planted fake secrets never appear in the vault copy; counts reported.
5. Zero archive content in Oracle paths; zero operational tables in the vault; rebuild creates no confirmation/grant rows.
6. Backfill of an owner copy (not live) completed with counts matching; owner approval; pre-cutover `*.pre-obsidian-cutover` copies kept.

## 10. Smallest next integration slices

Each slice is independently shippable and leaves the suite green with the archive **disabled**.
No generic framework; no new daemon (drainer is an in-process thread like `consolidate.start`).

| # | Slice | Files | Acceptance |
|---|---|---|---|
| S1 | Record codec + archive writer (other lead) | `jarvis/second_brain.py`, `tests/second_brain/` | Round-trip fake Memory/History/Store; writes only under root; refuses symlink/`..`/non-owned note; torn-append recovery; sha edit detection; redaction fixtures; Windows path forms |
| S2 | Outbox in same txn | `jarvis/memory.py`, `jarvis/history.py`, `jarvis/store.py` (schema + `enqueue` calls); **not** assistant/chatgpt/cluster/inference/knowledge | Disabled ⇒ behaviour/tests unchanged. Enabled ⇒ outbox row commits/rolls back with the mutation (raise after insert ⇒ no orphan); dedupe `add` ⇒ no row; enqueue failure rolls back the personal mutation, while any already executed OS effect is never retried; prune skips un-archived rows |
| S3 | Drainer + lifecycle + status | `second_brain.start`, call sites at `daemon.py` ~364 and `ui.py` ~189, `archive_lag` in console status | Runs only when `not is_node`; `archive.lock` exclusion; crash-between-steps idempotent; offline vault ⇒ lag shown, requests unaffected; sentinel guard |
| S4 | Reconcile + vault skip | `second_brain.py`, `vault.py` (skip namespace in recall/refresh index or dedupe) | Validated user edit ingested; explicit delete ⇒ durable tombstone, missing replica file ⇒ quarantine; conflict copy surfaced; no double recall |
| S5 | `archive status\|backfill\|verify\|rebuild` CLI | `jarvis/__main__.py` | Dry-run default; verify counts; rebuild only to a temp dir on fakes; real backfill is a parent-run, owner-approved step |
| S6 | Retire/tombstone/erase | `memory.py`, `second_brain.py`, confirm-queue gate | Erase blanks body, FTS, vectors, `vault.sqlite3`; stub in vault; resurrection blocked after DB-backup restore on fakes |
| S7 | Terminal tasks + knowledge wiki pages | `store.py`, `second_brain.py`; `knowledge.py` later (currently owned elsewhere) | Personal task objectives/progress from creation, terminal outcomes; wiki page export without authority/bot state |
| S8 | Handover barrier hooks | with P6 `personal_state` | Barrier note + digest check; stale-epoch outbox imported as late records |

Do not do before the gate: remove SQLite reads, set `vault.write=true`, enable the archive on
real state, move vault under Oracle, or prune any DB row.

## 11. Implementation defaults and remaining evidence

The owner's request authorizes additive records in the configured second brain. Proceed without
re-asking about namespace creation or routine archival policy. Preserve existing hot DB retention
settings and original sources; no new pruning or irreversible privacy erase in the initial slice.
Use the documented conservative redaction/exclusion policy and report its rebuild limitations.
No existing sync provider/configuration is changed. Transport discovery, full mutation coverage,
real archive counts, crash/rebuild tests and live-read integration remain evidence requirements,
not requests to silently assume success. Final codec/namespace must be reconciled with S1.
