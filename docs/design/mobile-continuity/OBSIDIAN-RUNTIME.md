---
title: Obsidian second brain, C2 runtime (drainer, status, archive CLI)
status: implemented; C2 write-through wired (drainer in the voice daemon, `jarvis archive` CLI); C3 read cutover not done
updated: 2026-11-11
scope: jarvis/second_brain_runtime.py and its wiring. No real vault, live DB, config, service or node was touched.
---

# C2 runtime

Stage: **C2 transitional**. SQLite is still the source; `jarvis/second_brain_queue.py` (other lead)
owns the outbox and publish. Read/rebuild cutover (**C3**) is not done. Nothing here says
"saved to Obsidian"; status reports counts and a state word only.

## Pieces

| Piece | Where |
|---|---|
| Gates, drainer thread, status, CLI | `jarvis/second_brain_runtime.py` |
| Start next to `consolidate.start` (voice daemon, main only) | `jarvis/daemon.py` |
| Start/stop with the console, `archive` block in `/api/overview` | `jarvis/ui.py` |
| `jarvis archive status\|sync\|drain\|validate\|verify\|restore\|overflow` | `jarvis/__main__.py` |
| Remember station lists the two modules | `jarvis/sysview.py` |

## Queue API assumed (agreed with parent)

- `configure(state_dir, enabled=True)` idempotent
- `drain(state_dir, vault_root, limit=100)` returns a dict; success needs `ok` not false and no `failed`/`errors`.
  Accepted counts: `acked|published|archived|drained|written`, `remaining|pending`, `held`, `failed|errors`, `busy`.
- `status(state_dir)` returns numeric dict: `pending|archive_pending|queued`, `held`, `errors|failed`,
  `oldest_age_s|oldest_pending_age_s`, `last_success|last_drained|last_archived_at`.
  Strings, ids, lists and nested objects are dropped, never forwarded.
- Missing module = `queue-unavailable`, backoff, nothing written. Parent: if the queue uses other key names, add them to the tuples at the top of the runtime module.

## Background drainer

Opt-in: `second_brain.enabled: true` (literal true; default off). Optional `interval_s` (30),
`batch` (100, max 500), `max_backoff_s` (900). Re-checked every cycle; any failure writes nothing:

1. `second_brain.enabled` true. 2. Not a cluster node (`topology.is_node`); an unreadable role fails closed.
3. `vault.enabled` not false. 4. Existing per-OS `vault.path` resolver gives a directory.
5. Sentinel present (`.obsidian/` or the archive index note) so an unmounted mount point is never filled.

Permanent failures (disabled, node, vault off/unconfigured) start no thread or end it. Transient ones
(vault not mounted yet) keep the thread and back off `interval * 2^failures`, capped. A busy drain lock is
neither success nor failure. Only exception class names are kept. One thread per state dir,
reference-counted, joined on stop and via `atexit`. `vault.write` is never read or changed.

**No lease/epoch fence exists.** "Configured main only" is a today-check, not a single-writer guarantee
across failover.

## Status

`second_brain_runtime.status()`: read-only; opens no queue unless a store DB already exists; creates no
files or threads. `state` values: `disabled`, `blocked`, `unverified`, `held`, `pending`, `error`,
`current`. `current` needs zero pending/held/errors/failures, a recorded successful drain, and a
bounded read-back of published heads (`archive_chunk_checked` / `archive_chunk_damaged`). A chunked
note, or any note up to 64 KiB, is opened through the archive loader. One damaged head is `error`,
not `current`. A larger plain note with no chunk file is not opened. Always `canonical: false`,
`lease_fenced: false`, `stage: c2-transitional`. The console overview gets this block (5 s TTL); no
record bodies, ids or paths.

## CLI

- `status`: as above.
- `sync`: dry run unless `--apply`. Output: counts, skip reasons (`secret-like`, `oversize`, `other`).
  `oversize: N bytes` counts as `oversize`. Sync, verify and file sync use each ledger's overflow
  policy. `--apply` of the database stores is refused while capture is on (`capture-active-use-drain`).
  `--files` stays allowed and is what the drainer runs for plans/runs/knowledge.
- `drain [--limit N] [--rescan]`: one queue drain, limit clamped to 500. `--rescan` seeds every
  current record; the drain then keeps a matching head only after the note reads back. A damaged
  chunk is republished as the next revision and is not overwritten.
- `overflow [--mib N]`: show the chunk policy, or set it. `0` turns it off. No vault.
- `validate`: archive integrity counts; reads an unmarked vault without writing. Still lists a
  damaged older revision after a repair.
- `verify`: `validate` plus read-only parity. Not a cutover.
- `restore --target DIR`: DIR must be new or empty and not overlap the state dir or the vault.
- Exit 1 on any failure or refusal. Nodes are refused by the existing proxy-node gate.
- Manual commands do not require `second_brain.enabled` (operator action); the background drainer does.

## Parent steps (not done here)

1. After the queue module lands: set `second_brain.enabled: true` in config (back up as `.pre-second-brain`).
2. Restart JARVIS (python change; web assets unaffected).
3. Full suite. `tests/runtime/test_second_brain_runtime.py` (35) and `tests/runtime/test_archive_cli.py` (20) use fakes only.
4. C3 (reads/rebuild cutover), lease fence and retention remain open.


## Parent review corrections

Capture configuration is now attempted synchronously at startup before the worker begins and
before Console live requests; it is independent of model warm-up. A temporarily offline vault
still arms capture, while no vault publication occurs until the gate passes. Permanent disabled/
node gates do not arm it. Queue implementation must also cover stores created after activation.

Drain results require explicit `ok: true`; absent held/error counters cannot prove current
status. Nonfinite/negative counters are rejected. Concurrent starts publish one started worker
under the registry lock; a stopping live worker blocks a replacement until it exits.

Parent targeted runtime/CLI tests:59 OK, including four added regression cases. These remain
fake-backed until actual queue integration passes. (That was true at the time; C2 was enabled later, see IMPLEMENTATION-PROGRESS.md. New since: `sync --files`, `verify`, `drain --rescan`, periodic file sync, `partial` state.)


Real queue contract integration completed by parent: `capture_complete`, `ok`, ISO last_success,
unreconciled/source_gone guards and pending/held/error counts now influence safe status. Two
real-queue integration tests cover offline capture, ordered drain, status and restore. Bulk
snapshot publication is refused during active capture; source DB publication uses drain.
