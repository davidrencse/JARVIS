"""Write-through outbox for the second-brain archive (C2): every personal mutation commits an
immutable full-revision payload in the SAME SQLite transaction; `drain()` later publishes those
exact revisions to the Obsidian vault and acknowledges each one only after it was written,
fsynced and read back.

Public API (typed reports, see each docstring; callers must not treat a missing key as success):
    configure(state_dir, enabled=True) -> report     install/flag the outbox on memory/history/tasks
    status(state_dir)                  -> report     read-only counts, no bodies, no vault access
    drain(state_dir, vault_root, limit=100, ...)     bounded, cross-process locked publish
    release_held(state_dir, record_id=None, reason=None) -> int

How capture works
- Per database (memory.sqlite3, history.sqlite3, tasks.sqlite3) `configure` adds `archive_*` tables
  and AFTER INSERT/UPDATE/DELETE triggers that note which record a statement touched
  (`archive_guard`). Hooks in memory.py/history.py/store.py call `capture(db)` last inside their
  transaction: it builds the full payload from the rows as committed by that transaction and
  INSERTs it into `archive_outbox`. If that insert fails the exception propagates, the
  transaction rolls back and the personal mutation never happened (callers see the error).
- A writer that does not know about the hook (an older process, a raw sqlite client) still fires
  the triggers: its guard rows survive the commit and the next `drain()` reconciles them into
  outbox events from the then-current rows (intermediate states of that write are lost; the
  event is marked origin 'reconcile' and counted). Exact per-mutation capture therefore needs
  every long-lived JARVIS process restarted after activation.
- `configure(enabled=True)` also writes a marker (`archive-capture.json`) in the state dir. A
  store database created later (first command ever, restored state) is adopted by the store
  constructor (`adopt`), so a store first created after activation has no capture gap.
- Disabled (no configure / enabled=False) is the legacy behaviour: no tables, no hooks run.

Not claimed: a cross-store atomic checkpoint (each store has its own queue), fleet/lease fencing,
or that the vault is already the read path (C3). Retention pruning of captured stores is
suspended while capture is enabled so no pending record can disappear (see history/store).
"""

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import second_brain as sb
from .locking import LockBusy, exclusive_lock

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
    "key TEXT NOT NULL, PRIMARY KEY(store, kind, key))",
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
_HEAD_SQL = ("SELECT o.seq, o.store, o.kind, o.key FROM archive_outbox o WHERE o.state='pending' "
             "AND o.seq=(SELECT MIN(p.seq) FROM archive_outbox p WHERE p.store=o.store "
             "AND p.kind=o.kind AND p.key=o.key) ORDER BY o.seq LIMIT ?")


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
            body = " ".join(f"INSERT OR IGNORE INTO archive_guard VALUES('{store}','{kind}',{r}.{col});"
                            for r in refs)
            out.append(f"CREATE TRIGGER IF NOT EXISTS archive_{table}_{op.lower()} AFTER {op} ON {table} "
                       f"WHEN (SELECT value FROM archive_meta WHERE key='enabled')='1' BEGIN {body} END")
    return out


def _has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


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
    db.execute("INSERT OR REPLACE INTO archive_meta VALUES('enabled', '1')")   # last: triggers go live


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


def _enqueue(db, store, kind, key, origin, verify=None, hold_unverified=False):
    """Queue the record's current revision unless it equals the newest queued/published one.
    Returns 1 if an event was added, 0 if nothing changed, -1 if a matching head could not be
    read back and the caller must keep its backlog row (vault not available to verify)."""
    state, reason = "pending", None
    try:
        obj = _build(db, store, kind, key)
        text = sb._canonical(obj) if obj else None
    except (ValueError, TypeError):   # NaN/inf/unserialisable value: held, never dropped
        obj, text, state, reason = True, '{"unserializable":true}', "held", "unserializable"
    if obj is not True and obj and sb.excluded(obj):   # owner flagged it PC-only: counted, never queued
        db.execute("INSERT INTO archive_meta VALUES('excluded','1') ON CONFLICT(key) DO UPDATE "
                   "SET value=CAST(value AS INTEGER)+1")
        return 0
    if not obj:
        db.execute("INSERT INTO archive_meta VALUES('source_gone','1') ON CONFLICT(key) DO UPDATE "
                   "SET value=CAST(value AS INTEGER)+1")
        return 0
    digest = sb._sha(text)
    size = len(text.encode("utf-8"))
    last = db.execute("SELECT payload_sha FROM archive_outbox WHERE store=? AND kind=? AND key=? "
                      "ORDER BY seq DESC LIMIT 1", (store, kind, key)).fetchone()
    if last is None:
        last = db.execute("SELECT payload_sha FROM archive_head WHERE store=? AND kind=? AND key=?",
                          (store, kind, key)).fetchone()
    if last is not None and last[0] == digest:
        if _same_payload_queued(db, store, kind, key, digest):
            return 0
        if verify is not None:
            try:
                sound = verify(store, kind, key, digest)
            except (OSError, sb.SecondBrainError):
                sound = False
            if sound:
                return 0
            # head hash matches but the note did not read back: queue a repair revision
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


def _capture_guard(db, origin):
    guards = db.execute("SELECT store,kind,key FROM archive_guard ORDER BY rowid").fetchall()
    added = sum(_enqueue(db, g[0], g[1], g[2], origin) for g in guards)
    if guards:
        db.execute("DELETE FROM archive_guard")
    return added


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


def _backfill(db, limit=BACKFILL_BATCH, verify=None):
    """One bounded batch of the baseline backlog -> outbox events (origin 'baseline'). Newest
    state wins and is appended AFTER anything the triggers already queued, so ordering per record
    holds. When `verify(store, kind, key, sha)` is set, a head whose hash matches is acked as
    unchanged only if that call reads the note back; a damaged note is queued for a new revision.
    Without `verify`, a matching head is left in the backlog (return counts only rows consumed).
    Returns (events_added, rows_consumed). Caller owns the transaction."""
    rows = db.execute("SELECT store,kind,key FROM archive_backfill ORDER BY rowid LIMIT ?", (limit,)).fetchall()
    added = consumed = 0
    for store, kind, key in rows:
        n = _enqueue(db, store, kind, key, "baseline", verify=verify, hold_unverified=True)
        if n < 0:
            continue
        db.execute("DELETE FROM archive_backfill WHERE store=? AND kind=? AND key=?", (store, kind, key))
        added += n
        consumed += 1
    return added, consumed


def capture(db):
    """Queue the revisions touched by this transaction. Call as the LAST statement inside the
    `with connect() as db:` block of a personal mutation. No-op when capture is off. Any failure
    raises ArchiveCaptureError so the surrounding transaction rolls back. Returns events added."""
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


@contextmanager
def _tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


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
            "errors": 0, "unreconciled": 0, "bytes": 0, "oldest_age_s": None, "last_success": None,
            "source_gone": 0, "held_reasons": {}, "error_samples": [], "baseline_seeded": False,
            "baseline_pending": 0, "rows": 0, "heads": 0, "last_drain": None, "excluded": 0,
            "overflow_max_bytes": 0, "held_oversize_bytes": 0}
    if not info["exists"]:
        return info
    conn = _open(path, readonly=True)
    try:
        if not _has_table(conn, "archive_meta"):
            return info
        meta = {r[0]: r[1] for r in conn.execute("SELECT key,value FROM archive_meta")}
        info.update(configured=True, enabled=meta.get("enabled") == "1",
                    last_success=meta.get("last_success_at"), source_gone=int(meta.get("source_gone", 0)),
                    excluded=int(meta.get("excluded", 0)))
        row = conn.execute("SELECT SUM(state='pending'), SUM(state='held'), "
                           "SUM(last_error IS NOT NULL), COALESCE(SUM(bytes),0), MIN(captured_at) "
                           "FROM archive_outbox").fetchone()
        info.update(pending=row[0] or 0, held=row[1] or 0, errors=row[2] or 0, bytes=row[3],
                    oldest_age_s=_age(row[4]))
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
    "pending", "held", "errors", "unreconciled", "bytes", "oldest_age_s" (int|None),
    "last_success" (ISO|None: last acknowledged publish), "last_drain" (ISO|None: last drain that
    reached the vault and finished without error, even if the queue was empty), "source_gone",
    "baseline_seeded" (every enabled store had its pre-existing rows queued), "baseline_pending"
    (rows still waiting to be queued), "rows" (records the sources hold: coverage), "heads"
    (records with a verified published revision: NOT parity, see SecondBrain.parity),
    "held_reasons", "error_samples", "stores": {...}}.
    ok is False only if a database could not be read (then "error" says which)."""
    state = Path(state_dir).expanduser()
    out = {"ok": True, "enabled": False, "configured": False, "capture_complete": False,
           "marker": (state / MARKER).exists(), "pending": 0, "held": 0, "errors": 0, "unreconciled": 0,
           "bytes": 0, "oldest_age_s": None, "last_success": None, "last_drain": None, "source_gone": 0,
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
        for k in ("pending", "held", "errors", "unreconciled", "bytes", "source_gone", "baseline_pending",
                  "rows", "heads", "excluded"):
            out[k] += info[k]
        for k in ("overflow_max_bytes", "held_oversize_bytes"):
            out[k] = max(out[k], info.get(k, 0))
        for reason, n in info["held_reasons"].items():
            out["held_reasons"][reason] = out["held_reasons"].get(reason, 0) + n
        out["error_samples"] += info["error_samples"][:2]
        ages = [a for a in (out["oldest_age_s"], info["oldest_age_s"]) if a is not None]
        out["oldest_age_s"] = max(ages) if ages else None
        out["last_success"] = max(filter(None, (out["last_success"], info["last_success"])), default=None)
        out["last_drain"] = max(filter(None, (out["last_drain"], info["last_drain"])), default=None)
    stores = out["stores"].values()
    out["baseline_seeded"] = all(s.get("baseline_seeded") for s in stores)
    out["enabled"] = any(s.get("enabled") for s in stores)
    out["configured"] = all(s.get("configured") for s in stores)
    out["capture_complete"] = all(s.get("configured") and s.get("enabled") for s in stores)
    return out


def rescan(state_dir):
    """Re-queue every current record of every capturing store through the bounded baseline
    backlog (e.g. after rows changed while capture was off, or to re-verify publication).
    This only seeds. The next drain that can read the vault drops a matching head only after
    SecondBrain.publication_sound reads the note back through _load; a damaged chunk stays
    immutable and is republished as the next revision. Returns {"ok", "seeded": {store: rows}}."""
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
    removed from the queue only afterwards. Held or failed events stay queued, block newer
    events of the same record (order is preserved) and are never dropped. dry_run writes nothing
    (not even the vault) and does not need the lock.

    Returns {"ok": bool, "busy": bool, "vault_ok": bool, "dry_run": bool, "acked": int,
    "published": int, "unchanged": int, "held": int, "failed": int, "reconciled": int,
    "remaining": int (pending+held left in enabled stores), "held_ids": [<=20 ids],
    "error_samples": [<=5 short strings], "error"?: str, "elapsed_ms": int, "stores": {...}}.
    ok means: vault reachable, not busy, no database error. It does NOT mean the queue is empty:
    check remaining/held/failed."""
    t0 = time.monotonic()
    rep = {"ok": True, "busy": False, "vault_ok": False, "dry_run": bool(dry_run), "acked": 0,
           "published": 0, "unchanged": 0, "held": 0, "failed": 0, "reconciled": 0, "baselined": 0,
           "released_oversize": 0, "chunked": 0, "repaired": 0, "baseline_pending": 0, "remaining": 0, "held_ids": [], "error_samples": [], "stores": {}, "elapsed_ms": 0}
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
                        while batches * BACKFILL_BATCH < limit:   # bounded and resumable per drain
                            with _tx(conn):
                                added, seen = _backfill(
                                    conn, min(BACKFILL_BATCH, limit - batches * BACKFILL_BATCH), verify=verify)
                            rep["baselined"] += seen
                            batches += 1
                            if seen == 0:
                                break
                    # recovery: held only for size, now fits the bound or the owner's overflow policy
                    if (cap := max(brain.max_record_bytes, brains[store].max_overflow_bytes)):
                        with _tx(conn):
                            rep["released_oversize"] += conn.execute(
                                "UPDATE archive_outbox SET state='pending', held_reason=NULL "
                                "WHERE state='held' AND held_reason='oversize' AND bytes<=?", (cap,)).rowcount
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
            except sqlite3.Error as exc:
                rep.update(ok=False)
                info["error"] = _short(exc)
            if info["enabled"]:
                rep["baseline_pending"] += info["baseline_pending"]
                rep["remaining"] += info["pending"] + info["held"] + info["baseline_pending"] + info["unreconciled"]
        if not dry_run and rep["ok"] and vault["ok"]:   # a finished drain that reached the vault
            for store, conn in conns.items():
                if rep["stores"][store]["enabled"] and "error" not in rep["stores"][store]:
                    with _tx(conn):
                        conn.execute("INSERT OR REPLACE INTO archive_meta VALUES('last_drain_at', ?)", (_stamp(),))
    finally:
        for conn in conns.values():
            conn.close()
