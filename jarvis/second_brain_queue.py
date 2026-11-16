"""Write-through outbox for the second-brain archive (C2): every personal mutation commits an
immutable full-revision payload in the SAME SQLite transaction; `drain()` later publishes those
exact revisions to the Obsidian vault and acknowledges each one only after it was written,
fsynced and read back.

Public API (typed reports, see each docstring; callers must not treat a missing key as success):
    configure(state_dir, enabled=True) -> report     install/flag the outbox on memory/history/tasks
    status(state_dir)                  -> report     read-only counts, no bodies, no vault access
    drain(state_dir, vault_root, limit=100, ...)     bounded, cross-process locked publish
    release_held(state_dir, record_id=None, reason=None) -> int
    acknowledge_absent(state_dir, record_id) -> int   clear one missing-source key; no tombstone

How capture works
- Per database (memory.sqlite3, history.sqlite3, tasks.sqlite3) `configure` adds `archive_*` tables
  and AFTER INSERT/UPDATE/DELETE triggers that note which record a statement touched
  (`archive_guard`). Hooks in memory.py/history.py/store.py call `enter(db)` before those writes
  and `capture(db)` last inside their transaction. `enter` marks guards that already existed.
  Capture builds the full payload from the rows of this transaction and INSERTs it into
  `archive_outbox`. If that insert fails the exception propagates, the transaction rolls back
  and the personal mutation never happened (callers see the error). A pre-existing guard is
  reconciled instead, so it cannot roll an unrelated write back.
- A writer that does not know about the hook (an older process, a raw sqlite client) still fires
  the triggers: its guard rows survive the commit and the next `drain()` or hooked `capture()`
  reconciles them into outbox events from the then-current rows (intermediate states of that
  write are lost; the event is marked origin 'reconcile' and counted). A missing row that already
  has a published head or a spool revision becomes one `archive_absent` key. A scratch row with
  neither is dropped and does not latch. Exact per-mutation capture therefore needs every
  long-lived JARVIS process restarted after activation.
- `configure(enabled=True)` also writes a marker (`archive-capture.json`) in the state dir. A
  store database created later (first command ever, restored state) is adopted by the store
  constructor (`adopt`), so a store first created after activation has no capture gap.
- Disabled (no configure / enabled=False) is the legacy behaviour: no tables, no hooks run.

Not claimed: a cross-store atomic checkpoint (each store has its own queue), fleet/lease fencing,
or that every reader has cut over to the vault. `vault.recall` can opt in
(`second_brain.canonical_read`); every other caller still reads SQLite. Retention pruning of
captured stores is suspended while capture is enabled so no pending record can disappear
(see history/store). A held `oversize` row stops blocking once a later pending revision of
the same record fits: the row stays, state `superseded`, reason `superseded-oversize`,
payload unchanged. Secret and conflict holds are not superseded. A transaction that is
itself over the cap still raises ArchiveCaptureError. A guard left by an earlier raw write
of a different key does not: it is reconciled, and an over-cap payload of that key is held.
"""

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import second_brain as sb
from .locking import LockBusy, exclusive_lock
from .locking import transaction as _tx

STORE_FILES = {"memory": "memory.sqlite3", "history": "history.sqlite3", "tasks": "tasks.sqlite3"}
MARKER = "archive-capture.json"
LOCK = "archive.lock"
MAX_RECORD_BYTES = sb.MAX_RECORD_BYTES   # 16 MiB: above Memory's 2M-char body cap even at 4 bytes/char
MAX_LIMIT = 1000
SCHEMA_VERSION = "1"

# (table, key column, store, kind): which rows belong to which archive record
_WATCH = {
    "memory": (("items", "id", "memory-item"), ("links", "src", "memory-links")),
    "history": (("commands", "id", "history-command"), ("hops", "command", "history-command")),
    "tasks": (("tasks", "id", "task"), ("events", "task_id", "task")),
}
_DDL = (
    "CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS archive_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, "
    "store TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, origin TEXT NOT NULL, "
    "payload TEXT NOT NULL, payload_sha TEXT NOT NULL, bytes INTEGER NOT NULL, "
    "captured_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
    "state TEXT NOT NULL DEFAULT 'pending', held_reason TEXT, last_error TEXT)",
    "CREATE INDEX IF NOT EXISTS archive_outbox_rec ON archive_outbox(store, kind, key, seq)",
    "CREATE TABLE IF NOT EXISTS archive_guard(store TEXT NOT NULL, kind TEXT NOT NULL, "
    "key TEXT NOT NULL, touched INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(store, kind, key))",
    # One row per source key that disappeared after a published head or a spool revision.
    # Scratch deletes (neither of those) are not inserted. The vault note is not removed here.
    "CREATE TABLE IF NOT EXISTS archive_absent(store TEXT NOT NULL, kind TEXT NOT NULL, "
    "key TEXT NOT NULL, since TEXT NOT NULL, PRIMARY KEY(store, kind, key))",
    "CREATE TABLE IF NOT EXISTS archive_head(store TEXT NOT NULL, kind TEXT NOT NULL, "
    "key TEXT NOT NULL, payload_sha TEXT NOT NULL, published_at TEXT NOT NULL, "
    "PRIMARY KEY(store, kind, key))",
    # baseline: records that existed before capture went live. Filled in the SAME transaction that
    # turns the triggers on (no gap), emptied by bounded drain batches (resumable, never one big scan).
    "CREATE TABLE IF NOT EXISTS archive_backfill(store TEXT NOT NULL, kind TEXT NOT NULL, "
    "key TEXT NOT NULL, PRIMARY KEY(store, kind, key))",
)
# parent tables that enumerate every record of a store (children are captured with their parent)
_SEED = {
    "memory": (("items", "id", "memory-item"), ("links", "src", "memory-links")),
    "history": (("commands", "id", "history-command"),),
    "tasks": (("tasks", "id", "task"),),
}
BACKFILL_BATCH = 200
# Superseded rows keep their payload but are not a head and do not block a later revision.
_HEAD_SQL = ("SELECT o.seq, o.store, o.kind, o.key FROM archive_outbox o WHERE o.state='pending' "
             "AND o.seq=(SELECT MIN(p.seq) FROM archive_outbox p WHERE p.store=o.store "
             "AND p.kind=o.kind AND p.key=o.key AND p.state IN ('pending', 'held')) "
             "ORDER BY o.seq LIMIT ?")


class ArchiveCaptureError(RuntimeError):
    """The revision could not be queued. Raised inside the mutation's transaction, so the
    personal change is rolled back rather than left uncaptured."""


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- schema ------------------------------------------------------------------------------------
def _triggers(store):
    out = []
    for table, col, kind in _WATCH[store]:
        for op, refs in (("INSERT", ("NEW",)), ("UPDATE", ("NEW", "OLD")), ("DELETE", ("OLD",))):
            body = " ".join(
                f"INSERT INTO archive_guard(store,kind,key,touched) VALUES('{store}','{kind}',{r}.{col},1) "
                f"ON CONFLICT(store,kind,key) DO UPDATE SET touched=1;"
                for r in refs)
            out.append(f"CREATE TRIGGER IF NOT EXISTS archive_{table}_{op.lower()} AFTER {op} ON {table} "
                       f"WHEN (SELECT value FROM archive_meta WHERE key='enabled')='1' BEGIN {body} END")
    return out


_has_table = sb.has_table


def _meta(db, key, default=None):
    row = db.execute("SELECT value FROM archive_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _overflow(db):
    """Owner overflow policy stored in this ledger (archive_meta 'overflow_max_bytes'): payloads up
    to this size may be published as chunked notes. 0 (default) = off. Clamped to the hard cap."""
    try:
        value = int(_meta(db, "overflow_max_bytes", "0") or 0)
    except (TypeError, ValueError, sqlite3.Error):
        return 0
    return max(0, min(value, sb.MAX_OVERFLOW_BYTES))


def _seed_baseline(db, store):
    """Queue EVERY existing record of the store for the drainer. Idempotent (a record already in the
    backlog is not duplicated; an already-published identical one is acked as 'unchanged' later).
    Caller owns the transaction. Returns rows seeded."""
    n = 0
    for table, col, kind in _SEED[store]:
        n += db.execute(f"INSERT OR IGNORE INTO archive_backfill SELECT DISTINCT '{store}','{kind}',{col} "
                        f"FROM {table} WHERE {col} IS NOT NULL").rowcount
    db.execute("INSERT OR REPLACE INTO archive_meta VALUES('baseline_seeded_at', ?)", (_stamp(),))
    return n


def _install(db, store):
    """Idempotent. Caller owns the transaction (DDL is transactional in SQLite).

    Existing rows are seeded into the baseline backlog in this same transaction as the triggers
    going live, whenever capture was off or never seeded: nothing written before activation (or
    while it was off) is left snapshot-only."""
    for table, _, _ in _WATCH[store]:
        if not _has_table(db, table):
            raise ArchiveCaptureError(f"{store}: base table {table} missing")
    for stmt in _DDL + tuple(_triggers(store)):
        db.execute(stmt)
    if _meta(db, "enabled") != "1" or _meta(db, "baseline_seeded_at") is None:
        _seed_baseline(db, store)
    db.execute("INSERT OR REPLACE INTO archive_meta VALUES('schema', ?)", (SCHEMA_VERSION,))
    db.execute("INSERT OR REPLACE INTO archive_meta VALUES('store', ?)", (store,))
    _ensure_capture_schema(db)
    db.execute("INSERT OR REPLACE INTO archive_meta VALUES('enabled', '1')")   # last: triggers go live


def _ensure_capture_schema(db):
    """Add transaction-entry provenance and per-key absence. Idempotent.

    Existing ledgers keep their rows. Triggers are replaced once so a watched write sets
    `touched`; `enter` clears that flag on guards that were already committed. Absence rows
    are created here and never by a read-only status call."""
    if not _has_table(db, "archive_guard"):
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(archive_guard)")}
    if "touched" not in columns:
        db.execute("ALTER TABLE archive_guard ADD COLUMN touched INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE TABLE IF NOT EXISTS archive_absent(store TEXT NOT NULL, kind TEXT NOT NULL, "
               "key TEXT NOT NULL, since TEXT NOT NULL, PRIMARY KEY(store, kind, key))")
    store = _meta(db, "store")
    if store not in _WATCH or _meta(db, "guard_provenance") == "1":
        return
    for table, _, _ in _WATCH[store]:
        for op in ("insert", "update", "delete"):
            db.execute(f"DROP TRIGGER IF EXISTS archive_{table}_{op}")
    for stmt in _triggers(store):
        db.execute(stmt)
    db.execute("INSERT OR REPLACE INTO archive_meta VALUES('guard_provenance', '1')")


def _sibling_overflow(directory, store):
    """Highest overflow policy on the other ledgers. A recreated store has lost its own row;
    adopting this keeps status (which publishes the max) and the next drain on the same cap."""
    best, root = 0, Path(directory)
    for other, fname in STORE_FILES.items():
        if other == store:
            continue
        path = root / fname
        try:
            if not path.is_file():
                continue
            conn = _open(path, readonly=True)
        except (OSError, sqlite3.Error):
            continue
        try:
            if _has_table(conn, "archive_meta"):
                best = max(best, _overflow(conn))
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return best


def adopt(db, directory, store):
    """Store constructors call this after creating their schema. If capture is active for this
    state dir (marker present) and this database has no outbox yet, install it now. A sibling
    ledger's overflow policy is copied so the new file does not drain at 0 while status shows
    the owner's cap."""
    if not os.path.exists(os.path.join(str(directory), MARKER)) or _has_table(db, "archive_meta"):
        return
    try:
        if not db.in_transaction:
            db.execute("BEGIN IMMEDIATE")
        _install(db, store)
        sibling = _sibling_overflow(directory, store)
        if sibling and _overflow(db) == 0:
            db.execute("INSERT OR REPLACE INTO archive_meta VALUES('overflow_max_bytes', ?)",
                       (str(sibling),))
    except sqlite3.Error as exc:
        raise ArchiveCaptureError(f"{store}: could not adopt write-through capture: {exc}") from exc


def enabled(db):
    """True when this database's capture is on. Cheap; False (never raises) if not configured."""
    try:
        row = db.execute("SELECT value FROM archive_meta WHERE key='enabled'").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise
    return row is not None and row[0] == "1"


# -- capture (producer side) ---------------------------------------------------------------------
def _build(db, store, kind, key):
    """The record's full payload from the rows visible to `db` (children included), or None if
    the record no longer exists. Same shapes as SecondBrain.sync."""
    if kind == "memory-item":
        r = db.execute("SELECT * FROM items WHERE id=?", (key,)).fetchone()
        return r and sb.build_payload("memory", kind, key, "memory.sqlite3#items", sb._row(r))
    if kind == "memory-links":
        rows = db.execute("SELECT src,dst,rel,created_at FROM links WHERE src=? ORDER BY src,dst,rel",
                          (key,)).fetchall()
        return rows and sb.build_payload("memory", kind, key, "memory.sqlite3#links",
                                         {"src": key, "links": [sb._row(r) for r in rows]})
    if kind == "history-command":
        r = db.execute("SELECT * FROM commands WHERE id=?", (key,)).fetchone()
        if r is None:
            return None
        hops = [sb._row(h) for h in db.execute(
            "SELECT command,name,ms,state FROM hops WHERE command=? ORDER BY name", (key,))]
        return sb.build_payload("history", kind, key, "history.sqlite3#commands", sb._row(r), {"hops": hops})
    if kind == "task":
        r = db.execute("SELECT * FROM tasks WHERE id=?", (key,)).fetchone()
        if r is None:
            return None
        events = [sb._row(e) for e in db.execute(
            "SELECT seq,task_id,at,kind,detail FROM events WHERE task_id=? ORDER BY seq", (key,))]
        return sb.build_payload("tasks", kind, key, "tasks.sqlite3#tasks", sb._row(r), {"events": events})
    raise ArchiveCaptureError(f"unknown record kind {kind!r}")


def _last_digest(db, store, kind, key):
    """Sha of the newest queued/held revision of a key, else of its published head, else None."""
    last = db.execute("SELECT payload_sha FROM archive_outbox WHERE store=? AND kind=? AND key=? "
                      "AND state IN ('pending', 'held') ORDER BY seq DESC LIMIT 1",
                      (store, kind, key)).fetchone()
    if last is None:
        last = db.execute("SELECT payload_sha FROM archive_head WHERE store=? AND kind=? AND key=?",
                          (store, kind, key)).fetchone()
    return last[0] if last is not None else None


def _link_source_excluded(db, src):
    """True when the memory item a link record hangs off is flagged PC-only (`no-archive`)."""
    try:
        item = _build(db, "memory", "memory-item", src)
    except (ValueError, TypeError):
        return False
    return bool(item) and sb.excluded(item)


def _enqueue(db, store, kind, key, origin, verify=None, hold_unverified=False, withhold=None):
    """Queue the record's current revision unless it equals the newest queued/published one.
    Returns 1 if an event was added, 0 if nothing changed, -1 if a matching head could not be
    read back and the caller must keep its backlog row (vault not available to verify)."""
    state, reason = "pending", None
    try:
        obj = _build(db, store, kind, key)
        text = sb._canonical(obj) if obj else None
    except (ValueError, TypeError):   # NaN/inf/unserialisable value: held, never dropped
        obj, text, state, reason = True, '{"unserializable":true}', "held", "unserializable"
    if (obj is not True and obj and sb.excluded(obj)) or \
            (kind == "memory-links" and _link_source_excluded(db, key)):
        # owner flagged it PC-only (a link follows its source item): counted, never queued
        db.execute("INSERT INTO archive_meta VALUES('excluded','1') ON CONFLICT(key) DO UPDATE "
                   "SET value=CAST(value AS INTEGER)+1")
        return 0
    if not obj:
        _note_missing_source(db, store, kind, key)
        return 0
    digest = sb._sha(text)
    size = len(text.encode("utf-8"))
    # A superseded oversize row still holds the old bytes. It is not the current revision.
    last = _last_digest(db, store, kind, key)
    if last is not None and last == digest:
        if _same_payload_queued(db, store, kind, key, digest):
            return 0
        if verify is not None:
            try:
                sound = verify(store, kind, key, digest)
            except (OSError, sb.SecondBrainError):
                sound = False
            if sound:
                return 0
            # An intact gap, or a tombstone, is not a repair. A damaged head is.
            try:
                if withhold is not None and withhold(store, kind, key, digest):
                    return 0
            except (OSError, sb.SecondBrainError):
                pass
        elif hold_unverified and _head_matches(db, store, kind, key, digest):
            return -1  # do not drop the backlog row until a drain can open the note
        else:
            return 0   # idempotent write: no false revision
    if state == "pending" and size > MAX_RECORD_BYTES and size > _overflow(db):
        if origin == "txn":   # still inside the user's transaction: refuse explicitly, change nothing
            raise ArchiveCaptureError(
                f"{store}/{kind}: record is {size} bytes, over the {MAX_RECORD_BYTES}-byte archive bound "
                "and the overflow policy; the change was not made")
        state, reason = "held", "oversize"   # already committed (older writer/baseline): kept, visible
    db.execute("INSERT INTO archive_outbox(store,kind,key,origin,payload,payload_sha,bytes,captured_at,"
               "state,held_reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (store, kind, key, origin, text, digest, size, _stamp(), state, reason))
    return 1


def _capture_origin(origin, touched):
    """Rollback applies only to a key this transaction changed.

    `enter` sets `touched` to 0 on guards that were already committed. A raw guard from
    another key stays 0 and is reconciled, including an over-cap payload, which is held.
    Guessing the set from rows written later is not provenance: the flag is the entry mark."""
    if origin == "txn" and not touched:
        return "reconcile"
    return origin


def _capture_guard(db, origin):
    _ensure_capture_schema(db)
    guards = db.execute("SELECT store,kind,key,touched FROM archive_guard ORDER BY rowid").fetchall()
    added = 0
    for guard in guards:
        added += _enqueue(db, guard["store"], guard["kind"], guard["key"],
                          _capture_origin(origin, guard["touched"]))
    if guards:
        db.execute("DELETE FROM archive_guard")
    return added


def _has_revision(db, store, kind, key):
    """True when this key was published or a spool revision was committed. A baseline
    key that was never queued is not evidence, and neither is a scratch insert."""
    if db.execute("SELECT 1 FROM archive_head WHERE store=? AND kind=? AND key=?",
                  (store, kind, key)).fetchone():
        return True
    return db.execute("SELECT 1 FROM archive_outbox WHERE store=? AND kind=? AND key=?",
                      (store, kind, key)).fetchone() is not None


def _note_missing_source(db, store, kind, key):
    """Inventory policy for a source row that is gone.

    Scratch (no head, no spool): leave no latch. Otherwise one identifiable absent row.
    Does not delete spool bytes, write a tombstone, or remove a vault note."""
    if not _has_revision(db, store, kind, key):
        return
    _ensure_capture_schema(db)
    db.execute("INSERT INTO archive_absent(store,kind,key,since) VALUES(?,?,?,?) "
               "ON CONFLICT(store,kind,key) DO NOTHING",
               (store, kind, key, _stamp()))


def _current_digest(db, store, kind, key):
    """Sha of the source row as it is now, or None when the row is gone or unreadable."""
    try:
        obj = _build(db, store, kind, key)
        text = sb._canonical(obj) if obj else None
    except (ValueError, TypeError):
        return None
    return sb._sha(text) if text else None


def _clear_absent_if_current(db, store, kind, key, digest):
    """Drop the absence only when the source row is back and equals this verified payload."""
    if not _has_table(db, "archive_absent"):
        return False
    if _current_digest(db, store, kind, key) != digest:
        return False
    return db.execute("DELETE FROM archive_absent WHERE store=? AND kind=? AND key=?",
                      (store, kind, key)).rowcount > 0


def _heal_absent(db, verify):
    """Clear absences whose source matches a published head `verify` can read back.

    A recreated row with different bytes stays absent until that revision is acknowledged.
    `verify` is None, or false, when the vault cannot be checked: nothing is cleared."""
    if verify is None or not _has_table(db, "archive_absent"):
        return 0
    cleared = 0
    rows = db.execute("SELECT store,kind,key FROM archive_absent").fetchall()
    for store, kind, key in rows:
        digest = _current_digest(db, store, kind, key)
        if digest is None:
            continue
        head = db.execute("SELECT payload_sha FROM archive_head WHERE store=? AND kind=? AND key=?",
                          (store, kind, key)).fetchone()
        if head is None or head[0] != digest:
            continue
        # A committed spool revision still has to publish. Matching the old head is not
        # the end of that key, and ack decides once the new payload is the one read back.
        if db.execute("SELECT 1 FROM archive_outbox WHERE store=? AND kind=? AND key=? "
                      "AND state IN ('pending','held')", (store, kind, key)).fetchone():
            continue
        try:
            sound = verify(store, kind, key, digest)
        except (OSError, sb.SecondBrainError):
            sound = False
        if sound and _clear_absent_if_current(db, store, kind, key, digest):
            cleared += 1
    return cleared


def _source_gone(conn):
    """Unresolved absent keys, plus a legacy anonymous counter if an older process left one.

    New absences are not added to that counter. The counter has no keys, so heal and
    acknowledge do not clear it."""
    keys = 0
    if _has_table(conn, "archive_absent"):
        keys = conn.execute("SELECT COUNT(*) FROM archive_absent").fetchone()[0]
    legacy = 0
    try:
        legacy = int(_meta(conn, "source_gone", "0") or 0)
    except (TypeError, ValueError):
        legacy = 0
    return keys + max(0, legacy)


def _absent_ids(conn, limit=20):
    if not _has_table(conn, "archive_absent"):
        return []
    return [f"{row[0]}/{row[1]}/{row[2]}" for row in conn.execute(
        "SELECT store,kind,key FROM archive_absent ORDER BY store,kind,key LIMIT ?", (limit,))]


def _split_record_id(record_id):
    parts = str(record_id).split("/", 2)
    if len(parts) != 3 or parts[0] not in STORE_FILES or not parts[1] or not parts[2]:
        raise ValueError("record id must be store/kind/key")
    return parts


def _head_matches(db, store, kind, key, digest):
    row = db.execute("SELECT payload_sha FROM archive_head WHERE store=? AND kind=? AND key=?",
                     (store, kind, key)).fetchone()
    return row is not None and row[0] == digest


def _same_payload_queued(db, store, kind, key, digest):
    """An outbox row already carries this payload. A second repair event would only repeat it."""
    return db.execute(
        "SELECT 1 FROM archive_outbox WHERE store=? AND kind=? AND key=? AND payload_sha=? "
        "AND state IN ('pending', 'held')",
        (store, kind, key, digest)).fetchone() is not None


def _backfill(db, limit=BACKFILL_BATCH, verify=None, withhold=None):
    """One bounded batch of the baseline backlog -> outbox events (origin 'baseline'). Newest
    state wins and is appended AFTER anything the triggers already queued, so ordering per record
    holds. When `verify(store, kind, key, sha)` is set, a head whose hash matches is acked as
    unchanged only if that call reads the note back; a damaged note is queued for a new revision.
    `withhold` true means the gap or tombstone stays fail-closed: no repair row is queued.
    Without `verify`, a matching head is left in the backlog (return counts only rows consumed).
    Returns (events_added, rows_consumed). Caller owns the transaction."""
    rows = db.execute("SELECT store,kind,key FROM archive_backfill ORDER BY rowid LIMIT ?", (limit,)).fetchall()
    added = consumed = 0
    for store, kind, key in rows:
        n = _enqueue(db, store, kind, key, "baseline", verify=verify, hold_unverified=True,
                     withhold=withhold)
        if n < 0:
            continue
        db.execute("DELETE FROM archive_backfill WHERE store=? AND kind=? AND key=?", (store, kind, key))
        added += n
        consumed += 1
    return added, consumed


def _memo(check, cache):
    """`check` with answers already read (no vault access); a miss falls back to the real call."""
    if check is None:
        return None

    def run(store, kind, key, digest):
        hit = cache.get((store, kind, key, digest))
        return hit if hit is not None else check(store, kind, key, digest)
    return run


def _guard(check, *args):
    try:
        return bool(check(*args))
    except (OSError, sb.SecondBrainError):
        return False


def prefetch_backfill(db, limit, verify, withhold):
    """Read the vault for the next backfill batch BEFORE the write transaction opens, so a slow
    note read never holds BEGIN IMMEDIATE (which blocks memory/history writers up to the sqlite
    timeout). Returns (verify, withhold) to pass to `_backfill` inside the transaction."""
    sound_by, held_by = {}, {}
    if verify is not None:
        rows = db.execute("SELECT store,kind,key FROM archive_backfill ORDER BY rowid LIMIT ?",
                          (limit,)).fetchall()
        for store, kind, key in rows:
            digest = _current_digest(db, store, kind, key)
            if digest is None or _last_digest(db, store, kind, key) != digest:
                continue   # _enqueue will not ask about this one
            ident = (store, kind, key, digest)
            sound_by[ident] = _guard(verify, *ident)
            if not sound_by[ident] and withhold is not None:
                held_by[ident] = _guard(withhold, *ident)
    return _memo(verify, sound_by), _memo(withhold, held_by)


def prefetch_heal(db, verify):
    """The vault reads `_heal_absent` will make, done before its write transaction opens."""
    sound_by = {}
    if verify is not None and _has_table(db, "archive_absent"):
        for store, kind, key in db.execute("SELECT store,kind,key FROM archive_absent").fetchall():
            digest = _current_digest(db, store, kind, key)
            if digest is not None and _last_digest(db, store, kind, key) == digest:
                sound_by[(store, kind, key, digest)] = _guard(verify, store, kind, key, digest)
    return _memo(verify, sound_by)


def enter(db):
    """Call once at the start of a hooked transaction, before any watched write.

    Guards already in the table are pre-existing raw evidence (`touched` 0). Triggers then
    set `touched` on keys this transaction changes. No-op when capture is off. Clearing the
    flag after the writes would hide that distinction, so this cannot run from `capture`."""
    if not enabled(db):
        return
    _ensure_capture_schema(db)
    db.execute("UPDATE archive_guard SET touched=0")


def capture(db):
    """Queue the revisions touched by this transaction. Call as the LAST statement inside the
    `with connect() as db:` block of a personal mutation, after `enter`. No-op when capture is
    off. Any failure raises ArchiveCaptureError so the surrounding transaction rolls back.
    Returns events added. A pre-existing guard is reconciled and does not raise for size."""
    if not enabled(db):
        return 0
    try:
        return _capture_guard(db, "txn")
    except ArchiveCaptureError:
        raise
    except sqlite3.Error as exc:
        raise ArchiveCaptureError(f"could not queue the revision: {type(exc).__name__}: {exc}"[:200]) from exc


# -- connections -----------------------------------------------------------------------------------
def _open(path, readonly=False):
    if readonly:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10,
                               isolation_level=None)
    else:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _short(exc):
    return f"{type(exc).__name__}: {exc}"[:120]


# -- configure -------------------------------------------------------------------------------------
def configure(state_dir, enabled=True):
    """Turn write-through capture on (creating any missing store database) or off.

    Returns {"ok": bool, "enabled": bool, "marker": bool, "stores": {name: {"configured": bool,
    "enabled": bool, "created": bool, "error"?: str}}, "errors": int}. ok is False if ANY store
    could not be configured; capture is then incomplete and callers must say so. Enabling is
    idempotent. Disabling keeps queued events (status shows them) and removes the marker.
    Touches only the outbox structures and the marker, never general config or personal rows."""
    state = Path(state_dir).expanduser()
    report = {"ok": True, "enabled": bool(enabled), "marker": False, "stores": {}, "errors": 0}
    marker = state / MARKER
    if enabled:
        try:
            state.mkdir(parents=True, exist_ok=True)
            tmp = state / f".{MARKER}.{uuid.uuid4().hex[:8]}"
            tmp.write_text(json.dumps({"schema": 1, "enabled": True, "configured_at": _stamp()}))
            os.replace(tmp, marker)   # first: a store created from here on is adopted by its constructor
            report["marker"] = True
        except OSError as exc:
            report.update(ok=False, errors=1, error=_short(exc))
            return report
        from .history import History
        from .memory import Memory
        from .store import Store
        makers = {"memory": lambda: Memory(state), "history": lambda: History(state),
                  "tasks": lambda: Store(state, prune=False)}
    for store, fname in STORE_FILES.items():
        info = report["stores"][store] = {"configured": False, "enabled": False, "created": False}
        path = state / fname
        try:
            if enabled:
                info["created"] = not path.exists()
                makers[store]()
                conn = _open(path)
                try:
                    with _tx(conn):
                        _install(conn, store)
                finally:
                    conn.close()
                info.update(configured=True, enabled=True)
            elif path.exists():
                conn = _open(path)
                try:
                    if _has_table(conn, "archive_meta"):
                        with _tx(conn):
                            conn.execute("INSERT OR REPLACE INTO archive_meta VALUES('enabled','0')")
                        info["configured"] = True
                finally:
                    conn.close()
        except (sqlite3.Error, OSError, ArchiveCaptureError) as exc:
            info["error"] = _short(exc)
            report["ok"] = False
            report["errors"] += 1
    if not enabled:
        try:
            marker.unlink(missing_ok=True)
        except OSError as exc:
            report.update(ok=False, error=_short(exc))
            report["errors"] += 1
    else:
        report["ok"] = report["ok"] and all(s["configured"] for s in report["stores"].values())
    return report


# -- status ----------------------------------------------------------------------------------------
def _age(stamp):
    try:
        return max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()))
    except (TypeError, ValueError):
        return None


def _store_status(path):
    info = {"exists": path.exists(), "configured": False, "enabled": False, "pending": 0, "held": 0,
            "superseded": 0, "errors": 0, "unreconciled": 0, "bytes": 0, "oldest_age_s": None,
            "last_success": None, "source_gone": 0, "source_gone_ids": [], "held_reasons": {}, "error_samples": [],
            "baseline_seeded": False, "baseline_pending": 0, "rows": 0, "heads": 0, "last_drain": None,
            "excluded": 0, "overflow_max_bytes": 0, "held_oversize_bytes": 0}
    if not info["exists"]:
        return info
    conn = _open(path, readonly=True)
    try:
        if not _has_table(conn, "archive_meta"):
            return info
        meta = {r[0]: r[1] for r in conn.execute("SELECT key,value FROM archive_meta")}
        info.update(configured=True, enabled=meta.get("enabled") == "1",
                    last_success=meta.get("last_success_at"), excluded=int(meta.get("excluded", 0)))
        info["source_gone"] = _source_gone(conn)
        info["source_gone_ids"] = _absent_ids(conn)
        row = conn.execute("SELECT SUM(state='pending'), SUM(state='held'), SUM(state='superseded'), "
                           "SUM(last_error IS NOT NULL), COALESCE(SUM(bytes),0), MIN(captured_at) "
                           "FROM archive_outbox").fetchone()
        info.update(pending=row[0] or 0, held=row[1] or 0, superseded=row[2] or 0,
                    errors=row[3] or 0, bytes=row[4], oldest_age_s=_age(row[5]))
        info["unreconciled"] = conn.execute("SELECT COUNT(*) FROM archive_guard").fetchone()[0]
        info["baseline_seeded"] = meta.get("baseline_seeded_at") is not None
        info["last_drain"] = meta.get("last_drain_at")
        info["baseline_pending"] = conn.execute("SELECT COUNT(*) FROM archive_backfill").fetchone()[0]
        info["heads"] = conn.execute("SELECT COUNT(*) FROM archive_head").fetchone()[0]
        info["overflow_max_bytes"] = _overflow(conn)
        info["held_oversize_bytes"] = conn.execute(   # largest held-for-size event: what the policy must cover
            "SELECT COALESCE(MAX(bytes),0) FROM archive_outbox WHERE state='held' AND held_reason='oversize'"
        ).fetchone()[0]
        store = meta.get("store")
        if store in _SEED:   # records the source holds now (coverage), not what the archive holds (parity)
            info["rows"] = sum(conn.execute(f"SELECT COUNT(DISTINCT {col}) FROM {table}").fetchone()[0]
                               for table, col, _ in _SEED[store])
        info["held_reasons"] = {r[0] or "unknown": r[1] for r in conn.execute(
            "SELECT held_reason, COUNT(*) FROM archive_outbox WHERE state='held' GROUP BY held_reason")}
        info["error_samples"] = [r[0][:120] for r in conn.execute(
            "SELECT DISTINCT last_error FROM archive_outbox WHERE last_error IS NOT NULL LIMIT 3")]
    finally:
        conn.close()
    return info


def status(state_dir):
    """Read-only snapshot (never writes, never touches the vault, no record bodies).

    Returns {"ok": bool, "enabled": bool (any store capturing), "configured": bool (all three
    stores have an outbox), "capture_complete": bool (all three configured AND enabled), "marker",
    "pending", "held", "superseded" (oversize rows kept, not blocking), "errors", "unreconciled",
    "bytes", "oldest_age_s" (int|None),
    "last_success" (ISO|None: last acknowledged publish), "last_drain" (ISO|None: last drain that
    reached the vault and finished without error, even if the queue was empty), "source_gone"
    (absent keys, plus a legacy anonymous counter), "source_gone_ids" (up to 20 store/kind/key ids,
    no bodies),
    "baseline_seeded" (every enabled store had its pre-existing rows queued), "baseline_pending"
    (rows still waiting to be queued), "rows" (records the sources hold: coverage), "heads"
    (records with a verified published revision: NOT parity, see SecondBrain.parity),
    "held_reasons", "error_samples", "stores": {...}}.
    ok is False only if a database could not be read (then "error" says which)."""
    state = Path(state_dir).expanduser()
    out = {"ok": True, "enabled": False, "configured": False, "capture_complete": False,
           "marker": (state / MARKER).exists(), "pending": 0, "held": 0, "superseded": 0,
           "errors": 0, "unreconciled": 0,
           "bytes": 0, "oldest_age_s": None, "last_success": None, "last_drain": None, "source_gone": 0,
           "source_gone_ids": [],
           "baseline_seeded": False, "baseline_pending": 0, "rows": 0, "heads": 0,
           "excluded": 0, "held_reasons": {}, "error_samples": [], "stores": {},
           "overflow_max_bytes": 0, "held_oversize_bytes": 0}
    for store, fname in STORE_FILES.items():
        try:
            info = out["stores"][store] = _store_status(state / fname)
        except (sqlite3.Error, OSError) as exc:
            out["stores"][store] = {"exists": True, "configured": False, "enabled": False, "error": _short(exc)}
            out.update(ok=False, error=f"{store}: {_short(exc)}")
            continue
        for k in ("pending", "held", "superseded", "errors", "unreconciled", "bytes", "source_gone",
                  "baseline_pending", "rows", "heads", "excluded"):
            out[k] += info[k]
        for k in ("overflow_max_bytes", "held_oversize_bytes"):
            out[k] = max(out[k], info.get(k, 0))
        for reason, n in info["held_reasons"].items():
            out["held_reasons"][reason] = out["held_reasons"].get(reason, 0) + n
        out["error_samples"] += info["error_samples"][:2]
        out["source_gone_ids"].extend(info.get("source_gone_ids") or [])
        ages = [a for a in (out["oldest_age_s"], info["oldest_age_s"]) if a is not None]
        out["oldest_age_s"] = max(ages) if ages else None
        out["last_success"] = max(filter(None, (out["last_success"], info["last_success"])), default=None)
        out["last_drain"] = max(filter(None, (out["last_drain"], info["last_drain"])), default=None)
    stores = out["stores"].values()
    out["baseline_seeded"] = all(s.get("baseline_seeded") for s in stores)
    out["enabled"] = any(s.get("enabled") for s in stores)
    out["configured"] = all(s.get("configured") for s in stores)
    out["capture_complete"] = all(s.get("configured") and s.get("enabled") for s in stores)
    out["source_gone_ids"] = out["source_gone_ids"][:20]
    return out


def rescan(state_dir):
    """Re-queue every current record of every capturing store through the bounded baseline
    backlog (e.g. after rows changed while capture was off, or to re-verify publication).
    This only seeds. The next drain that can read the vault drops a matching head only after
    SecondBrain.publication_sound reads the newest note back through _load. A revision gap does
    not make that head sound and is not filled in. A damaged chunk stays immutable and is
    republished as the next revision. Returns {"ok", "seeded": {store: rows}}."""
    state, out = Path(state_dir).expanduser(), {"ok": True, "seeded": {}}
    for store, fname in STORE_FILES.items():
        path = state / fname
        if not path.exists():
            continue
        try:
            conn = _open(path)
            try:
                if _has_table(conn, "archive_meta") and enabled(conn):
                    with _tx(conn):
                        out["seeded"][store] = _seed_baseline(conn, store)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            out.update(ok=False, error=f"{store}: {_short(exc)}")
    return out


# -- consumer side -----------------------------------------------------------------------------------
def set_overflow(state_dir, max_bytes):
    """Explicit owner operation: allow (or, with 0, stop) chunked publication of records larger
    than MAX_RECORD_BYTES and at most `max_bytes` (<= second_brain.MAX_OVERFLOW_BYTES). Stored in
    every configured ledger, so capture (whether a mutation may queue such a revision instead of
    rolling back) and drain (whether it may publish it) read one policy. Held 'oversize' events
    within the limit go back to pending on the next drain; nothing is published here.
    Returns {"ok", "overflow_max_bytes", "stores": {store: bool}, "error"?}."""
    try:
        value = int(max_bytes)
    except (TypeError, ValueError):
        raise ValueError("overflow limit must be a whole number of bytes") from None
    if value and not MAX_RECORD_BYTES < value <= sb.MAX_OVERFLOW_BYTES:
        raise ValueError(f"overflow limit must be 0 or in ({MAX_RECORD_BYTES}, {sb.MAX_OVERFLOW_BYTES}] bytes")
    state = Path(state_dir).expanduser()
    out = {"ok": True, "overflow_max_bytes": value, "stores": {}}
    for store, fname in STORE_FILES.items():
        path = state / fname
        out["stores"][store] = False
        if not path.exists():
            continue
        try:
            conn = _open(path)
            try:
                if _has_table(conn, "archive_meta"):
                    with _tx(conn):
                        conn.execute("INSERT OR REPLACE INTO archive_meta VALUES('overflow_max_bytes', ?)",
                                     (str(value),))
                    out["stores"][store] = True
            finally:
                conn.close()
        except sqlite3.Error as exc:
            out.update(ok=False, error=f"{store}: {_short(exc)}")
    return out


def overflow_policy(state_dir):
    """Read-only {store: bytes} for ledgers that exist. Missing or unreadable stores are omitted.
    0 means chunked publication is off for that store. Never writes."""
    state, out = Path(state_dir).expanduser(), {}
    for store, fname in STORE_FILES.items():
        path = state / fname
        try:
            if not path.is_file() or path.stat().st_size == 0:
                continue
            conn = _open(path, readonly=True)
        except (OSError, sqlite3.Error):
            continue
        try:
            if _has_table(conn, "archive_meta"):
                out[store] = _overflow(conn)
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return out


def published_heads(state_dir):
    """Read-only (store, kind, key, payload_sha) for every acknowledged head. Skips a missing,
    empty or unreadable ledger. Never writes. The vault is not opened here."""
    state, out = Path(state_dir).expanduser(), []
    for store, fname in STORE_FILES.items():
        path = state / fname
        try:
            if not path.is_file() or path.stat().st_size == 0:
                continue
            conn = _open(path, readonly=True)
        except (OSError, sqlite3.Error):
            continue
        try:
            if not _has_table(conn, "archive_head"):
                continue
            out.extend(tuple(row) for row in conn.execute(
                "SELECT store, kind, key, payload_sha FROM archive_head"))
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return out


def release_held(state_dir, record_id=None, reason=None):
    """Move held events back to pending (owner decision: e.g. after removing a secret from the
    source or raising the size bound). Filter by record id ('store/kind/key') and/or reason.
    Returns how many were released. Never publishes anything itself."""
    state, total = Path(state_dir).expanduser(), 0
    for store, fname in STORE_FILES.items():
        path = state / fname
        if not path.exists():
            continue
        if record_id and not str(record_id).startswith(store + "/"):
            continue
        conn = _open(path)
        try:
            if not _has_table(conn, "archive_meta"):
                continue
            sql, args = "UPDATE archive_outbox SET state='pending', held_reason=NULL WHERE state='held'", []
            if record_id:
                sql += " AND store||'/'||kind||'/'||key=?"
                args.append(record_id)
            if reason:
                sql += " AND held_reason=?"
                args.append(reason)
            with _tx(conn):
                total += conn.execute(sql, args).rowcount
        finally:
            conn.close()
    return total


def acknowledge_absent(state_dir, record_id):
    """Clear one missing-source key after the owner accepts that the source is gone.

    Does not write a tombstone, delete a vault note, or drop spool bytes. Returns how
    many rows were removed (0 or 1). A recreated source is not cleared here: drain
    clears it only after the published head reads back."""
    store, kind, key = _split_record_id(record_id)
    path = Path(state_dir).expanduser() / STORE_FILES[store]
    if not path.exists():
        return 0
    conn = _open(path)
    try:
        if not _has_table(conn, "archive_absent"):
            return 0
        with _tx(conn):
            return conn.execute("DELETE FROM archive_absent WHERE store=? AND kind=? AND key=?",
                                (store, kind, key)).rowcount
    finally:
        conn.close()


def _supersede_oversize(conn, cap):
    """A held oversize row stops being the head once a later pending revision of the same
    record fits under `cap`. The row and its payload stay, as state superseded. Secret,
    conflict, and every other hold are left blocking. Returns how many rows changed."""
    return conn.execute(
        "UPDATE archive_outbox AS held SET state='superseded', held_reason='superseded-oversize', "
        "last_error=NULL WHERE held.state='held' AND held.held_reason='oversize' AND EXISTS ("
        "SELECT 1 FROM archive_outbox AS newer WHERE newer.store=held.store AND newer.kind=held.kind "
        "AND newer.key=held.key AND newer.seq>held.seq AND newer.state='pending' AND newer.bytes<=?)",
        (cap,)).rowcount


def _hold(conn, seq, reason):
    with _tx(conn):
        conn.execute("UPDATE archive_outbox SET state='held', held_reason=?, last_error=NULL WHERE seq=?",
                     (reason, seq))


def _ack(conn, row, result):
    """Remove the event and remember what was published, atomically."""
    with _tx(conn):
        conn.execute("DELETE FROM archive_outbox WHERE seq=?", (row["seq"],))
        conn.execute("INSERT OR REPLACE INTO archive_head VALUES(?,?,?,?,?)",
                     (row["store"], row["kind"], row["key"], row["payload_sha"], _stamp()))
        conn.execute("INSERT OR REPLACE INTO archive_meta VALUES('last_success_at', ?)", (_stamp(),))
        # The note just read back. Clear the absence only if the source row is that payload.
        # A delete whose spool was already committed stays absent: the body reached the vault
        # and the source is still gone.
        _clear_absent_if_current(conn, row["store"], row["kind"], row["key"], row["payload_sha"])


def _retry(conn, seq, why):
    with _tx(conn):
        conn.execute("UPDATE archive_outbox SET attempts=attempts+1, last_error=? WHERE seq=?",
                     (str(why)[:120], seq))


def _process(conn, head, brain, allow, dry_run, rep):
    """Publish one head event. Returns the record id if it must be skipped for the rest of this drain."""
    rid = f"{head['store']}/{head['kind']}/{head['key']}"
    row = conn.execute("SELECT seq,store,kind,key,payload,payload_sha FROM archive_outbox WHERE seq=?",
                       (head["seq"],)).fetchone()
    if row is None:
        return None
    try:
        obj = json.loads(row["payload"])
        sound = sb._sha(row["payload"]) == row["payload_sha"] and sb._canonical(obj) == row["payload"]
    except (ValueError, TypeError):
        sound = False
    if not sound:
        rep["held"] += 1
        rep["held_ids"].append(rid)
        if not dry_run:
            _hold(conn, row["seq"], "spool-corrupt")
        return rid
    result = brain.publish(obj, allow_secret_like=rid in allow, dry_run=dry_run)
    status = result["status"]
    if status in ("written", "unchanged"):
        if not dry_run:
            _ack(conn, row, result)
        rep["acked"] += 0 if dry_run else 1
        rep["published" if status == "written" else "unchanged"] += 1
        rep["chunked"] += 1 if result.get("chunked") and status == "written" else 0
        rep["repaired"] += 1 if result.get("repaired") and status == "written" else 0
        return None
    if status == "would-write":
        rep["published"] += 1
        return rid
    if status == "held":
        rep["held"] += 1
        rep["held_ids"].append(rid)
        if not dry_run:
            _hold(conn, row["seq"], result["reason"] or "held")
        return rid
    rep["failed"] += 1
    if len(rep["error_samples"]) < 5:
        rep["error_samples"].append(result["reason"][:120])
    if not dry_run:
        _retry(conn, row["seq"], result["reason"])
    return rid


def _drain_store(conn, store, brain, quota, allow, dry_run, rep, skip):
    used = 0
    while used < quota:
        want = quota - used + len(skip)
        heads = [h for h in conn.execute(_HEAD_SQL, (want,)).fetchall()
                 if f"{h['store']}/{h['kind']}/{h['key']}" not in skip]
        if not heads:
            break
        for head in heads[:quota - used]:
            blocked = _process(conn, head, brain, allow, dry_run, rep)
            used += 1
            if blocked:
                skip.add(blocked)
        if dry_run:
            break   # heads only: nothing is acked, so the same heads would repeat
    return used


def drain(state_dir, vault_root, limit=100, *, allow_secret_like=(), dry_run=False,
          max_record_bytes=None):
    """Publish queued revisions, oldest first, at most `limit` (1..1000) events per call.

    One drain at a time per state dir (flock on <state>/archive.lock; a second caller gets
    busy=True and changes nothing). Each event is the exact payload captured at commit time,
    published through SecondBrain.publish (conflict/secret/size checks, fsync, read-back) and
    removed from the queue only afterwards. Held or failed events stay queued and block newer
    events of the same record (order is preserved). A held oversize row is the exception: when
    a later pending revision of that record fits, the old row becomes superseded and keeps its
    payload. It is not deleted. Secret and conflict holds are never superseded. dry_run writes
    nothing (not the vault, not the outbox) and does not need the lock.

    Returns {"ok": bool, "busy": bool, "vault_ok": bool, "dry_run": bool, "acked": int,
    "published": int, "unchanged": int, "held": int, "failed": int, "reconciled": int,
    "superseded": int (oversize rows marked this call), "source_gone": int (absent keys left
    in enabled stores), "remaining": int (pending+held+baseline+unreconciled+source_gone in
    enabled stores; superseded is not remaining), "held_ids": [<=20 ids],
    "error_samples": [<=5 short strings], "error"?: str, "elapsed_ms": int, "stores": {...}}.
    ok means: vault reachable, not busy, no database error. It does NOT mean the queue is empty:
    check remaining/held/failed."""
    t0 = time.monotonic()
    rep = {"ok": True, "busy": False, "vault_ok": False, "dry_run": bool(dry_run), "acked": 0,
           "published": 0, "unchanged": 0, "held": 0, "failed": 0, "reconciled": 0, "baselined": 0,
           "released_oversize": 0, "superseded": 0, "chunked": 0, "repaired": 0,
           "baseline_pending": 0, "source_gone": 0, "remaining": 0, "held_ids": [], "error_samples": [],
           "stores": {}, "elapsed_ms": 0}
    state = Path(state_dir).expanduser()
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = 100
    try:
        if dry_run:
            _drain_locked(state, vault_root, limit, allow_secret_like, True, max_record_bytes, rep)
        else:
            with exclusive_lock(state / LOCK, timeout=0):
                _drain_locked(state, vault_root, limit, allow_secret_like, False, max_record_bytes, rep)
    except LockBusy:
        rep.update(ok=False, busy=True, error="busy: another drain is running")
    except (OSError, sb.SecondBrainError) as exc:
        rep.update(ok=False, error=_short(exc))
    rep["held_ids"] = rep["held_ids"][:20]
    rep["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return rep


def _drain_locked(state, vault_root, limit, allow_secret_like, dry_run, max_record_bytes, rep):
    brain = sb.SecondBrain(vault_root, max_record_bytes=max_record_bytes or MAX_RECORD_BYTES)
    brains = {}   # per store: each ledger carries its own overflow policy
    vault = brain.check_vault()
    rep["vault_ok"] = vault["ok"]
    if not vault["ok"]:
        rep.update(ok=False, error=vault["reason"][:120])
    allow = set(allow_secret_like or ())
    if allow and not dry_run:   # owner named these records: lift their secret-like hold
        for rid in allow:
            release_held(state, record_id=rid, reason="secret-like")
    conns = {}
    try:
        for store, fname in STORE_FILES.items():
            path = state / fname
            info = rep["stores"][store] = {"configured": False, "enabled": False, "pending": 0, "held": 0}
            if not path.exists():
                continue
            try:
                conn = _open(path, readonly=dry_run)
                if not _has_table(conn, "archive_meta"):
                    conn.close()
                    continue
                conns[store] = conn
                brains[store] = sb.SecondBrain(vault_root, max_record_bytes=brain.max_record_bytes,
                                               max_overflow_bytes=_overflow(conn))
                info["configured"] = True
                info["enabled"] = enabled(conn)
                if info["enabled"] and not dry_run:   # capture-side: needs no vault
                    with _tx(conn):
                        if _meta(conn, "baseline_seeded_at") is None:   # activated by an older build
                            _seed_baseline(conn, store)
                        rep["reconciled"] += _capture_guard(conn, "reconcile")
                    # A matching head is dropped only after the note reads back. While the vault
                    # cannot be opened, leave the backlog so a later drain can still repair it.
                    if vault["ok"]:
                        batches = 0
                        verify = brains[store].publication_sound
                        withhold = brains[store].repair_withheld
                        while batches * BACKFILL_BATCH < limit:   # bounded and resumable per drain
                            n = min(BACKFILL_BATCH, limit - batches * BACKFILL_BATCH)
                            v, w = prefetch_backfill(conn, n, verify, withhold)   # vault reads: no lock held
                            with _tx(conn):
                                added, seen = _backfill(conn, n, verify=v, withhold=w)
                            rep["baselined"] += seen
                            batches += 1
                            if seen == 0:
                                break
                        healer = prefetch_heal(conn, verify)
                        with _tx(conn):
                            _heal_absent(conn, healer)
                    # recovery: held only for size, now fits the bound or the owner's overflow policy.
                    # What still does not fit stops blocking only when a later pending revision does.
                    if (cap := max(brain.max_record_bytes, brains[store].max_overflow_bytes)):
                        with _tx(conn):
                            rep["released_oversize"] += conn.execute(
                                "UPDATE archive_outbox SET state='pending', held_reason=NULL "
                                "WHERE state='held' AND held_reason='oversize' AND bytes<=?", (cap,)).rowcount
                        with _tx(conn):
                            rep["superseded"] += _supersede_oversize(conn, cap)
            except sqlite3.Error as exc:
                rep.update(ok=False)
                info["error"] = _short(exc)
                rep["error_samples"].append(f"{store}: {_short(exc)}"[:120])
        if vault["ok"] and conns:
            active = [s for s in conns if rep["stores"][s]["enabled"]]
            budget, skip = limit, {s: set() for s in active}
            for quota in (max(1, limit // 3), limit):   # fair first pass, then whatever is left
                for store in active:
                    if budget <= 0:
                        break
                    try:
                        budget -= _drain_store(conns[store], store, brains[store], min(quota, budget), allow,
                                               dry_run, rep, skip[store])
                    except sqlite3.Error as exc:
                        rep.update(ok=False)
                        rep["stores"][store]["error"] = _short(exc)
                        rep["error_samples"].append(f"{store}: {_short(exc)}"[:120])
                        skip[store] = set()
                        active.remove(store)
                        break
        for store, conn in conns.items():
            info = rep["stores"][store]
            try:
                row = conn.execute("SELECT SUM(state='pending'), SUM(state='held') FROM archive_outbox").fetchone()
                info["pending"], info["held"] = row[0] or 0, row[1] or 0
                info["baseline_pending"] = conn.execute("SELECT COUNT(*) FROM archive_backfill").fetchone()[0]
                info["unreconciled"] = conn.execute("SELECT COUNT(*) FROM archive_guard").fetchone()[0]
                info["source_gone"] = _source_gone(conn)
            except sqlite3.Error as exc:
                rep.update(ok=False)
                info["error"] = _short(exc)
            if info["enabled"]:
                rep["baseline_pending"] += info["baseline_pending"]
                rep["source_gone"] += info.get("source_gone", 0)
                rep["remaining"] += (info["pending"] + info["held"] + info["baseline_pending"]
                                     + info["unreconciled"] + info.get("source_gone", 0))
        if not dry_run and rep["ok"] and vault["ok"]:   # a finished drain that reached the vault
            for store, conn in conns.items():
                if rep["stores"][store]["enabled"] and "error" not in rep["stores"][store]:
                    with _tx(conn):
                        conn.execute("INSERT OR REPLACE INTO archive_meta VALUES('last_drain_at', ?)", (_stamp(),))
    finally:
        for conn in conns.values():
            conn.close()
