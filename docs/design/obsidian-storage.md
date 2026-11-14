# Obsidian storage

*How the second brain captures SQLite into the vault, what is and is not a cutover, and how brainsync moves a snapshot between PCs.*

Code: `jarvis/second_brain.py`. Fleet vault status is [fleet-api.md](fleet-api.md). MAIN handoff of that snapshot is [main-handoff.md](main-handoff.md). Recall and memory behavior for operators is [architecture-guide.md](../architecture-guide.md).

## Stages

| Stage | What it is |
| --- | --- |
| C0 | SQLite is the source. `vault.read` is read-only. This is today for every caller except opt-in `vault.recall` |
| C1 | Exporter and rebuild. Implemented and rehearsed. Not a cutover |
| C2 | Transactional outbox and drainer. Memory, history, and tasks write through. Plans, runs, and knowledge use periodic file sync. Implemented. Not a cutover |
| C3 | Rebuild plus write and read cutover. The only cutover. Not done |
| C4 | Retention after C3. Not done |

SQLite stays the source until C3. Do not paint C2 as "the vault is primary". `vault.write` is never read and never changed by capture. It stays false. It is not the capture switch.

The implemented layout is `<vault>/reference/jarvis-records/`, immutable revision notes. That tree is excluded from ordinary vault indexing. An earlier proposal of a `jarvis/` day-segment layout is not what shipped.

## Capture

`second_brain.enabled` must be boolean `true`. The default is off. The string `"true"` does not enable it. When it is on, capture is attempted synchronously at startup before workers. An offline vault still arms capture. Publication waits for the gate. The background drainer starts only when enabled. Manual CLI commands do not require `second_brain.enabled`. The drainer does.

Drainer: `interval_s` 30, `batch` 100, maximum 500, `max_backoff_s` 900. It is not a node. An unreadable role fails closed. `vault.enabled` must not be literal `false`. A sentinel `.obsidian/` or the archive index must exist. There is no lease or epoch fence on the drainer. Only the current MAIN's drainer writes archive revisions.

`drain.ok` means the vault is reachable, not busy, and there was no database error. It does not mean the queue is empty.

Status words: `disabled`, `blocked`, `unverified`, `held`, `pending`, `error`, `current`, and `partial`. `status` is `canonical: true` only when the opt-in is on, state is `current`, every acknowledged head opened, there is no gap, and there is no damaged head. `jarvis archive verify` stays `canonical: false`.

CLI: `jarvis archive status|sync|drain|validate|verify|restore|overflow`. `sync` is a dry run unless `--apply`. `--apply` of database stores is refused while capture is on (`capture-active-use-drain`). `--files` stays allowed. `drain --limit` is clamped to 500. `drain --rescan` exists. `overflow 0` turns the chunk policy off.

## What a revision is

Chunk cap `MAX_OVERFLOW_BYTES` is 64 MiB. `max_record_bytes` is 4 MiB. An oversize record is reported and never truncated. A transaction over the cap raises `ArchiveCaptureError`.

A held oversize row is marked `state=superseded`, `held_reason=superseded-oversize` when a later pending revision of the same record fits. The old payload stays. The row is excluded from pending, held, and remaining. A dry run does not mark it. Secret holds and conflict holds are never superseded this way.

`published_readable` opens every non-tombstone head through `_load`, including gaps, duplicates, and plain notes over 64 KiB. A hash mismatch or a damaged chunk sets `archive_chunk_damaged` at least 1 and state `error`. A gap or a duplicate sets `archive_revision_conflict` at least 1 and state `conflict`. Neither is `current`. `publication_sound` is false unless revisions are `1..n`, including when the newest is a tombstone. An intact gap is not auto-repaired. `latest()` raises on a gap.

Lines cite `vault:archive:<store>/<kind>/<key>`.

Recall retains at most 8 lines and 4000 characters (defaults 4 and 900). On a backlog, a gap, or a damaged head, recall returns nothing: no older note and no SQLite fallback, when the queue is readable. A conflict or an unreadable head returns no lines.

Canonical read is opt-in `second_brain.canonical_read` literal `true`, and only when `vault.enabled` and `vault.recall` are not literal `false`. Only literal `false` closes those gates.

## Tombstone and restore

A tombstone is explicit owner intent. It blocks resurrection. There is no revive API. Source changes while a record is tombstoned are not archived. That is an open C1 limit, not a silent merge.

Restore does not restore authority. Tasks come back interrupted. Plans come back blocked. The restore target must be new or empty and must not overlap state or the vault.

User edits to revision notes are never overwritten (`human_edited`). An edited payload voids restore (`payload-hash-mismatch`). Secret-like records are held, not silently redacted, unless `allow_secret_like` is set.

`History.record` prune and `Store.prune` are suspended while capture is on. Older processes that lack the hook only get an eventual latest-state reconcile through `archive_guard` (`origin reconcile`). Intermediate states are lost. A row deleted before reconcile is `source_gone`. Exact per-mutation capture requires restarting every long-lived process. Cross-store state can disagree. This is not a fleet checkpoint.

The archive loader is only `SecondBrain._load` / `_assemble`. Do not add a second reader that skips the gap and damage checks.

Locations, `camera.json`, the decisions table, run `*.log` files, and `tasks.spec` redaction remain owner decisions. They are not implied by this capture.

## Brainsync

`brainsync.enabled` must be boolean `true`. The string `"true"` does not enable it. Leave it false until a private remote exists. WS1 calls `brainsync.handoff(old_main, new_main, config, base)` only after the old MAIN is quiesced and fenced, and refuses activation unless `ok` is true. `transport=` and `between_reads=` are keyword-only. Failures are `{ok:false, status, error}`, not exceptions. `old_main` and `new_main` must be keys of `config["devices"]`. Other devices return `not_participant`.

`status()` is read-only. `sync()` runs only on the current MAIN. CLI: `jarvis brain sync|status`. Hidden export, receive, and freeze are omitted from `--help`. `--state` must be `<config parent>/.jarvis`.

`brainsync.repo` defaults to `<base>/.jarvis/brain-git`. `remote` is optional. `user:password@` is refused and is omitted from status. `remote_allowlist` entries are exact paths `vault/<rel>` or `state/memory.sqlite3`. An empty allowlist is vault notes only. `.git`, `.obsidian`, and `.jarvis` cannot be allowlisted. The repo must not be the JARVIS checkout, `base/.git`, the vault, a directory containing the vault, or a linked worktree.

Git runs via subprocess with `shell` false. No force-push, no autostash, no destructive reset, no `+` refspec. A non-fast-forward push is preserved. Both histories stay. `refs/heads/brainsync/<device>` is published. `brainsync-local/<device>` is private and never pushed. A sanitized orphan ref is the only one offered to a remote when history contains a disallowed blob.

Materialize is exclusive-create. A vault file is never overwritten or deleted. The same path with different bytes becomes `conflict-<sha256><suffix>`. Additive history can recreate a note the user deleted.

Memory for handoff is `sqlite3.backup()` from a read-only connection, never a raw copy. The bundle is `JARVIS_BRAIN1`: a JSON header, a git bundle, and memory bytes. Caps: 16 MiB per file, 512 MiB per bundle, 100000 files. Export and freeze request bodies are capped at 4096 bytes.

The handoff marker is `<base>/.jarvis/brainsync-handoff.json`, mode 0444, written only after the peer accepts. Later sync and export return `handoff_readonly`. The marker does not fence vault writers, the memory writer, or cluster roles.

The new MAIN installs memory only when the source snapshot is unchanged across two backups. A conflicting nonempty destination stays. The incoming file is written as `memory.sqlite3.conflict-<sha256>` and the result is `memory_conflict`. An empty destination is copied aside as `memory.sqlite3.prior-<hash>` and then replaced. Identical bytes are a no-op.

Quiesce is two identical backup snapshots. A pull refuses a local `between_reads` hook (`source_not_quiesced`). Two matching snapshots are not proof the fence held. An offline source is `source_offline`. The destination is unchanged. Do not call that copy latest. Success status is `handed_off`.

`SshBundleTransport` uses `deploy._remote` with fixed commands: `python -m jarvis brain receive|export|freeze`. Export is base64 because `_remote` decodes stdout as UTF-8. Pull-mode peers (transport other than ssh) return `source_offline`. When the peer has a POSIX absolute `jarvis_dir`, the adapter runs `<jarvis_dir>/.venv/bin/python -m jarvis --config <jarvis_dir>/jarvis.config.json --state <jarvis_dir>/.jarvis brain <cmd>` with `shlex.quote`. An unsafe `jarvis_dir`, or a Windows `os` / `platform` / `system` hint, is `peer_path_unsafe`. No `jarvis_dir` falls back to bare `python -m jarvis`. Set `jarvis_dir` before enabling. Export refuses `omitted_notes` (no bundle, no pending file). Import refuses `materialize_incomplete` (memory not installed, source not frozen). Freeze refuses `source_changed` (no marker).

No live SSH, Dell, GitHub, or Windows handoff has been run. Use a dedicated private repo of the vault snapshot only. Do not put `memory.sqlite3` on GitHub.

Recommendation that stays: brainsync is off until that remote exists. Handoff of the brain is not a cluster role change and not C3.
