"""Target-side fence and effect admission (OPERATIONAL-CONTRACT.md, executor.py).

One installation, one journal. Fleet mode, leases, and admission consumption stay in
`AuthorityStore` — this file does not keep a second copy that can authorize work.
Legacy sinks run only until `fence()` durably closes them. After that, every ordinary
effect is validated and consumed online, then started at most once.

Not wired into `Remote.call` or `NodeWorker`. Not failover. See
docs/design/mobile-continuity/EXECUTOR-IMPLEMENTATION.md.
"""

from ..locking import transaction
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import threading
import time

from .authority import (
    MAX_SIGNATURE,
    MAX_TICKET_SECONDS,
    MODES,
    SCHEMA_VERSION,
    TICKET_FIELDS as _TICKET_FIELDS,
    TICKET_SKEW,
    AuthorityError,
    _digest,
    _ident,
    _int,
    _num,
    _writer_lock,
)
from .clock import boot_id as read_boot_id

EXECUTOR_SCHEMA = 1
_LEGACY_OPEN = frozenset(("LEGACY_ACTIVE", "PREPARING_V2"))
_EFFECTS = frozenset(("ordinary", "pure_compute", "interactive"))
_OPEN_STATES = frozenset(("PREPARED", "CONSUMED"))
_UNCERTAIN_STATES = frozenset(("ACCEPTED", "UNCERTAIN"))
_OP_REQUIRED = (
    "authority_id", "fleet_id", "main", "incarnation", "epoch", "mode_version",
    "policy_version", "target", "operation_id", "attempt_id", "digest", "effect_class",
    "issued_at", "admit_until", "issuer_incarnation", "admission_id", "signature",
)
_OP_OPTIONAL = frozenset(("session_id",))
_TOKEN_FIELDS = (
    "authority_id", "fleet_id", "main", "incarnation", "epoch", "mode_version",
    "policy_version", "target", "operation_id", "attempt_id", "digest", "effect_class",
    "issued_at", "admit_until", "issuer_incarnation", "admission_id",
)
_FENCE_FIELDS = (
    "installation_id", "device_id", "fleet_id", "authority_id", "authority_schema",
    "high_incarnation", "high_epoch", "high_mode_version", "fenced_incarnation",
    "fenced_epoch", "legacy_closed", "boot_id",
)
_RECEIPT_FIELDS = (
    "target", "operation_id", "attempt_id", "admission_id", "digest", "token_hash",
    "main", "authority_id", "incarnation", "epoch", "mode_version", "policy_version",
    "issued_at", "admit_until", "issuer_incarnation", "effect_class", "session_id",
    "state", "result_code", "consumed_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fence(
  installation_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, fleet_id TEXT NOT NULL,
  authority_id TEXT NOT NULL, authority_schema INTEGER NOT NULL,
  high_incarnation INTEGER NOT NULL, high_epoch INTEGER NOT NULL, high_mode_version INTEGER NOT NULL,
  fenced_incarnation INTEGER NOT NULL, fenced_epoch INTEGER NOT NULL,
  legacy_closed INTEGER NOT NULL, boot_id TEXT NOT NULL, record_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(
  target TEXT NOT NULL, operation_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
  admission_id TEXT NOT NULL, digest TEXT NOT NULL, token_hash TEXT NOT NULL,
  main TEXT NOT NULL, authority_id TEXT NOT NULL,
  incarnation INTEGER NOT NULL, epoch INTEGER NOT NULL, mode_version INTEGER NOT NULL,
  policy_version INTEGER NOT NULL, issued_at TEXT NOT NULL, admit_until TEXT NOT NULL,
  issuer_incarnation INTEGER NOT NULL, effect_class TEXT NOT NULL, session_id TEXT,
  state TEXT NOT NULL, result_code TEXT, consumed_at TEXT, record_hash TEXT NOT NULL,
  PRIMARY KEY(target, operation_id, attempt_id),
  UNIQUE(admission_id));
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL, record_hash TEXT NOT NULL);
"""


def _canon_num(value):
    return format(float(value), ".6f")


def _digest_row(fields, keys):
    body = {}
    for key in keys:
        value = fields[key]
        if key in ("issued_at", "admit_until", "consumed_at") and value is not None:
            value = _canon_num(value) if not isinstance(value, str) else value
        body[key] = value
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _effect_word(state):
    if state == "COMPLETED":
        return "completed"
    if state in _UNCERTAIN_STATES:
        return "uncertain"
    if state == "RELEASED":
        return "released"
    return "not_run"


class ExecutorGuard:
    """Durable local fence plus online admission before any v2 effect.

    `authority` is a client with `status`, `consume`, `acknowledge`, and `admission`.
    Tests may pass an `AuthorityStore`. Production must pass a client that has already
    authenticated this installation as the ticket's target. The store itself does not
    authenticate, and this guard does not add a second mode machine.
    """

    def __init__(self, path, *, device_id, installation_id, fleet_id, authority_id,
                 authority=None, verifier=None, boot=None, wall=None, managed=False):
        self.path = path
        self.device_id = _ident(device_id, "device_id")
        self.installation_id = _ident(installation_id, "installation_id")
        self.fleet_id = _ident(fleet_id, "fleet_id")
        self.authority_id = _ident(authority_id, "authority_id")
        self._authority = authority
        self._verifier = verifier
        self._boot = read_boot_id if boot is None else boot
        self._wall = wall or time.time
        self._managed = bool(managed)
        self._lock = threading.RLock()
        # Journal sections take ``_lock``. Authority calls and sinks release it.
        # ``_inflight`` counts sinks already running; ``fence`` waits for zero
        # before acknowledge. ``_active`` serializes one operation at a time.
        self._cv = threading.Condition(self._lock)
        self._inflight = 0
        self._active = set()
        self._tls = threading.local()
        self._writer = None
        self._db = None
        self._quarantined = False
        self._quarantine_reason = None
        self._legacy_closed = False
        self._rebooted = False
        self._high = (-1, -1)
        self._high_mode = 0
        self._fenced = (-1, -1)
        self._fence_boot = None
        if self._managed and not _exists(path):
            # A missing journal must not look like a fresh legacy install.
            self._quarantined = True
            self._legacy_closed = True
            self._quarantine_reason = "missing_journal"
            return
        if _exists(path):
            self._open_existing()

    def _enter(self):
        # Per-thread, so a sink blocks re-entry on this thread only. Another
        # thread may admit, fence, or read while a sink is running.
        if getattr(self._tls, "depth", 0):
            raise AuthorityError("busy", "executor guard cannot be re-entered from a sink")
        self._tls.depth = 1

    def _leave(self):
        self._tls.depth = 0

    @contextmanager
    def _unlocked(self):
        """Release the journal lock for one authority call or sink.

        The caller holds ``self._lock`` once. Other threads may fence, read, or
        admit until this block re-acquires it, including when the body raises.
        """
        self._lock.release()
        try:
            yield
        finally:
            self._lock.acquire()

    def _operation_busy(self, key):
        target, operation_id, _attempt = key
        return any(item[0] == target and item[1] == operation_id for item in self._active)

    def close(self):
        with self._lock:
            # Waiting on our own sink would deadlock. Same refusal as fence.
            if getattr(self._tls, "depth", 0):
                raise AuthorityError("busy", "executor guard cannot be re-entered from a sink")
            while self._inflight:
                self._cv.wait()
            if self._db is not None:
                self._db.close()
                self._db = None
            if self._writer is not None:
                self._writer.close()
                self._writer = None

    def local_view(self):
        """Local journal only. Not fleet authority, and not permission to run an effect.

        Does not wait for a running sink or an authority call.
        """
        with self._lock:
            present = self._db is not None or self._quarantined
            return {
                "journal": self._db is not None,
                "present": present,
                "quarantined": self._quarantined,
                "legacy_closed": self._legacy_closed,
                "rebooted": self._rebooted,
                "authority_id": self.authority_id,
                "authority_schema": SCHEMA_VERSION,
                "high_incarnation": self._high[0] if self._db is not None else None,
                "high_epoch": self._high[1] if self._db is not None else None,
                "high_mode_version": self._high_mode if self._db is not None else None,
                "fenced_incarnation": self._fenced[0] if self._db is not None else None,
                "fenced_epoch": self._fenced[1] if self._db is not None else None,
                "boot_id": self._fence_boot,
            }

    def legacy_effect(self, sink):
        """Today's dispatch. Refused once this installation has a durable v2 fence.

        The refusal is decided under the journal lock. The sink itself runs without
        that lock. ``fence`` does not acknowledge until this sink has returned.
        """
        if not callable(sink):
            raise AuthorityError("bad_request", "sink must be callable")
        with self._lock:
            self._enter()
            try:
                self._reject_closed_legacy()
                self._inflight += 1
                try:
                    with self._unlocked():
                        return sink()
                finally:
                    self._inflight -= 1
                    self._cv.notify_all()
            finally:
                self._leave()

    def fence(self, incarnation, epoch, mode_version, *, reopen_legacy=False):
        """Durably ack a mode version and the highest epoch this process refuses.

        The tuple is written locally before `acknowledge`, so a crash cannot leave the
        authority believing we stopped while this process still runs legacy. Sinks that
        have already started, including legacy sinks, finish before `acknowledge`.
        A refused acknowledge does not roll that back: rolling back would reopen legacy.
        `reopen_legacy` is the rollback protocol's local half and only applies when the
        authority status is already `LEGACY_ACTIVE` at a newer mode version.
        """
        incarnation, epoch = _int(incarnation, "incarnation"), _int(epoch, "epoch")
        mode_version = _int(mode_version, "mode_version")
        with self._lock:
            self._enter()
            try:
                return self._fence_locked(incarnation, epoch, mode_version, reopen_legacy)
            finally:
                self._leave()

    def _fence_locked(self, incarnation, epoch, mode_version, reopen_legacy):
        if self._quarantined:
            raise AuthorityError("quarantined", "executor journal is unusable")
        boot = self._current_boot()
        if boot is None:
            raise AuthorityError("no_boot_id", "fencing requires a boot id")
        with self._unlocked():
            status = self._status()
        if self._quarantined:
            raise AuthorityError("quarantined", "executor journal is unusable")
        boot = self._current_boot()
        if boot is None:
            raise AuthorityError("no_boot_id", "fencing requires a boot id")
        self._check_pinned(status)
        if status["mode"] not in MODES or status["mode"] == "QUARANTINED":
            raise AuthorityError("quarantined", "authority state is unrecognized")
        issued = (int(status["incarnation"]), int(status["epoch"]))
        requested = (incarnation, epoch)
        if requested > issued:
            raise AuthorityError("ahead", "fence names an epoch the authority never issued")
        if mode_version > int(status["mode_version"]) + 1:
            raise AuthorityError("ahead", "fence names a mode version the authority never issued")
        if issued < self._high or int(status["mode_version"]) < self._high_mode:
            raise AuthorityError("stale", "authority tuple is older than the durable high-water mark")
        close = status["mode"] not in _LEGACY_OPEN
        if reopen_legacy:
            if status["mode"] != "LEGACY_ACTIVE" or mode_version <= self._high_mode:
                raise AuthorityError("bad_edge", "legacy reopens only on a newer LEGACY_ACTIVE version")
            if not self._legacy_closed:
                raise AuthorityError("bad_edge", "legacy is already open")
            close = False
        elif self._legacy_closed:
            close = True
        high = issued if issued > self._high else self._high
        fenced = requested if requested > self._fenced else self._fenced
        high_mode = max(self._high_mode, mode_version)
        self._write_fence(high, high_mode, fenced, close, boot)
        # The durable tuple now refuses older effects. Acknowledge only after
        # sinks that already passed ACCEPTED (and legacy sinks) have returned.
        while self._inflight:
            self._cv.wait()
        recorded = self._fence_ack(True)
        try:
            with self._unlocked():
                self._authority.acknowledge(
                    self.device_id, self.installation_id, mode_version, fenced[1], boot_id=boot)
        except AuthorityError:
            raise
        except OSError:
            raise AuthorityError("unavailable", "authority unreachable") from None
        return recorded

    def accept(self, signed_operation, admission_ticket, sink, *, failpoint=None):
        """Validate, consume online, persist the receipt, and only then call `sink`.

        `sink` is zero-argument. The guard does not pass operation fields into it and
        does not keep its return value. `failpoint` only raises after a durable write;
        it never skips a check. Authority calls and the sink run without the journal
        lock. ACCEPTED is fsynced immediately before the sink, and only after a fresh
        check that the tuple is still above the fence, the session is not released,
        and the receipt is still CONSUMED.
        """
        if not callable(sink):
            raise AuthorityError("bad_request", "sink must be callable")
        with self._lock:
            self._enter()
            try:
                if self._quarantined:
                    raise AuthorityError("quarantined", "executor journal is unusable")
                op = _operation(signed_operation, admission_ticket)
                # One operation at a time. A second attempt waits, then sees the
                # finished receipt instead of starting another effect.
                key = (op["target"], op["operation_id"], op["attempt_id"])
                while self._operation_busy(key):
                    self._cv.wait()
                self._active.add(key)
                try:
                    return self._accept_locked(signed_operation, op, sink, failpoint)
                finally:
                    self._active.discard(key)
                    self._cv.notify_all()
            finally:
                self._leave()

    def _accept_locked(self, signed_operation, op, sink, failpoint):
        self._require_v2()
        self._verify(signed_operation)
        if op["target"] != self.device_id or op["authority_id"] != self.authority_id or op["fleet_id"] != self.fleet_id:
            raise AuthorityError("bad_request", "ticket is for a different installation")
        if op["effect_class"] == "interactive":
            op["session_id"] = op.get("session_id") or op["operation_id"]
            _ident(op["session_id"], "session_id")
        elif op.get("session_id"):
            raise AuthorityError("bad_request", "session_id is only valid for interactive work")
        existing = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
        if existing is not None:
            _same_attempt(existing, op)
            return self._finish_existing(existing, op, sink, failpoint)
        self._reject_blocked_attempt(op)
        self._reject_local_stale(op)
        if op["effect_class"] == "interactive" and self._session_state(op["session_id"]) == "released":
            raise AuthorityError("released", "interactive session was released")
        self._pin_expiry_if_needed(op)
        prior = self._lookup_admission(op["admission_id"])
        raced = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
        if raced is not None:
            _same_attempt(raced, op)
            return self._finish_existing(raced, op, sink, failpoint)
        self._reject_blocked_attempt(op)
        self._revalidate_open(op)
        if self._wall() >= op["admit_until"]:
            self._pin_expiry_if_needed(op)
        if prior is not None:
            _same_admission(prior, op)
            self._insert_receipt(op, "UNCERTAIN", "authority_consumed")
            raise AuthorityError("uncertain", "admission already consumed; effect was not restarted")
        self._insert_receipt(op, "PREPARED", None)
        self._crash(failpoint, "after_prepare")
        return self._drive(op, sink, failpoint, already=False)

    def reconcile(self, operation_id, attempt_id):
        """Local receipt only. Does not consume, does not call the sink, does not need Oracle.

        Does not wait for a running sink or an authority call.
        """
        with self._lock:
            self._enter()
            try:
                if self._quarantined:
                    raise AuthorityError("quarantined", "executor journal is unusable")
                row = self._receipt(self.device_id, _ident(operation_id, "operation_id"),
                                    _ident(attempt_id, "attempt_id"))
                if row is None:
                    raise AuthorityError("not_found", "no local receipt")
                return _public(row, already=True)
            finally:
                self._leave()

    def stop_interactive(self, session_id, reason):
        """Record that a session's input must not run or replay. Does not press keys or consume.

        Does not wait for a running sink or an authority call.
        """
        session_id, reason = _ident(session_id, "session_id"), _ident(reason, "reason")
        with self._lock:
            self._enter()
            try:
                if self._quarantined:
                    raise AuthorityError("quarantined", "executor journal is unusable")
                self._ensure_db()
                current = self._session_state(session_id)
                self._write_session(session_id, "released", reason)
                return {"session_id": session_id, "state": "released", "reason": reason,
                        "replay": False, "already": current == "released"}
            finally:
                self._leave()

    # --- lifecycle ----------------------------------------------------------------------

    def _open_existing(self):
        self._writer = _writer_lock(str(self.path) + ".writer")
        try:
            self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._load()
        except AuthorityError:
            self._quarantined = True
            self._legacy_closed = True
            if self._quarantine_reason is None:
                self._quarantine_reason = "unreadable"
        except Exception:
            self._quarantined = True
            self._legacy_closed = True
            self._quarantine_reason = self._quarantine_reason or "unreadable"

    def _load(self):
        schema = self._meta("schema")
        authority_schema = self._meta("authority_schema")
        if schema != str(EXECUTOR_SCHEMA) or authority_schema != str(SCHEMA_VERSION):
            self._quarantine_reason = "schema"
            raise AuthorityError("quarantined", "executor journal schema is not this build")
        if self._meta("quarantined") == "1":
            self._quarantine_reason = "quarantined"
            raise AuthorityError("quarantined", "executor journal was quarantined")
        row = self._db.execute("SELECT * FROM fence").fetchone()
        if row is not None:
            self._check_row_hash(row, _FENCE_FIELDS)
            if (row["installation_id"] != self.installation_id or row["device_id"] != self.device_id
                    or row["fleet_id"] != self.fleet_id or row["authority_id"] != self.authority_id
                    or int(row["authority_schema"]) != SCHEMA_VERSION):
                self._quarantine_reason = "pin"
                raise AuthorityError("quarantined", "executor journal is for a different installation")
            self._high = (int(row["high_incarnation"]), int(row["high_epoch"]))
            self._high_mode = int(row["high_mode_version"])
            self._fenced = (int(row["fenced_incarnation"]), int(row["fenced_epoch"]))
            self._legacy_closed = bool(row["legacy_closed"])
            self._fence_boot = row["boot_id"]
        for receipt in self._db.execute("SELECT * FROM receipts"):
            self._check_row_hash(receipt, _RECEIPT_FIELDS)
        for session in self._db.execute("SELECT * FROM sessions"):
            self._check_row_hash(session, ("session_id", "state", "reason"))
        current = self._current_boot()
        seen = self._meta("seen_boot")
        if current is not None and seen not in (None, current):
            self._rebooted = True
        if current is not None and seen != current:
            self._db.execute("INSERT INTO meta(key, value) VALUES('seen_boot', ?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (current,))
        if self._fence_boot and current and current != self._fence_boot:
            self._rebooted = True

    def _ensure_db(self):
        if self._db is not None:
            return
        if self._quarantined:
            raise AuthorityError("quarantined", "executor journal is unusable")
        self._writer = _writer_lock(str(self.path) + ".writer")
        self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta VALUES('schema', ?)", (str(EXECUTOR_SCHEMA),))
        self._db.execute("INSERT OR IGNORE INTO meta VALUES('authority_schema', ?)", (str(SCHEMA_VERSION),))

    def _meta(self, key):
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def _current_boot(self):
        value = self._boot() if callable(self._boot) else self._boot
        if value is None:
            return None
        return _ident(value, "boot_id")

    # --- fence record -------------------------------------------------------------------

    def _write_fence(self, high, high_mode, fenced, close, boot):
        self._ensure_db()
        fields = {
            "installation_id": self.installation_id, "device_id": self.device_id,
            "fleet_id": self.fleet_id, "authority_id": self.authority_id,
            "authority_schema": SCHEMA_VERSION, "high_incarnation": high[0], "high_epoch": high[1],
            "high_mode_version": high_mode, "fenced_incarnation": fenced[0], "fenced_epoch": fenced[1],
            "legacy_closed": 1 if close else 0, "boot_id": boot,
        }
        fields["record_hash"] = _digest_row(fields, _FENCE_FIELDS)
        with self._tx() as db:
            db.execute("INSERT INTO fence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(installation_id) DO UPDATE SET "
                       "high_incarnation=excluded.high_incarnation, high_epoch=excluded.high_epoch, "
                       "high_mode_version=excluded.high_mode_version, "
                       "fenced_incarnation=excluded.fenced_incarnation, fenced_epoch=excluded.fenced_epoch, "
                       "legacy_closed=excluded.legacy_closed, boot_id=excluded.boot_id, "
                       "record_hash=excluded.record_hash",
                       tuple(fields[k] for k in _FENCE_FIELDS) + (fields["record_hash"],))
        self._high, self._high_mode, self._fenced = high, high_mode, fenced
        self._legacy_closed, self._fence_boot = close, boot

    def _fence_ack(self, recorded):
        return {
            "device_id": self.device_id, "installation_id": self.installation_id,
            "authority_id": self.authority_id, "fleet_id": self.fleet_id,
            "high_incarnation": self._high[0], "high_epoch": self._high[1],
            "mode_version": self._high_mode, "fenced_incarnation": self._fenced[0],
            "fenced_epoch": self._fenced[1], "legacy_closed": self._legacy_closed,
            "boot_id": self._fence_boot, "authority_recorded": recorded,
        }

    # --- accept path --------------------------------------------------------------------

    def _require_v2(self):
        if self._quarantined:
            raise AuthorityError("quarantined", "executor journal is unusable")
        if not self._legacy_closed:
            raise AuthorityError("legacy", "v2 admission is closed until this installation is fenced")
        if self._authority is None:
            raise AuthorityError("no_authority", "no authority client")
        if self._verifier is None:
            raise AuthorityError("no_verifier", "no MAIN signature verifier")

    def _reject_closed_legacy(self):
        if self._quarantined:
            raise AuthorityError("quarantined", "executor journal is unusable")
        if self._legacy_closed:
            raise AuthorityError("fenced", "legacy dispatch is closed on this installation")

    def _verify(self, signed):
        try:
            ok = self._verifier(signed) is True
        except Exception:
            ok = False
        if not ok:
            raise AuthorityError("signature", "MAIN signature rejected")

    def _reject_local_stale(self, op):
        tup = (op["incarnation"], op["epoch"])
        if tup < self._high or tup <= self._fenced:
            raise AuthorityError("stale", "ticket is older than this installation's durable fence")

    def _revalidate_open(self, op):
        """Re-check fence, high-water, and session after the lock was released.

        The caller holds the lock and has not written the next receipt state yet.
        A fenced open receipt becomes REJECTED so the sink cannot start. Session
        release keeps the messages the locked path already used. No row yet uses
        the same stale refusal as the pre-network check.
        """
        if self._quarantined:
            raise AuthorityError("quarantined", "executor journal is unusable")
        row = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
        state = None if row is None else row["state"]
        if state not in (None, "PREPARED", "CONSUMED"):
            return row
        tup = (op["incarnation"], op["epoch"])
        if tup <= self._fenced or tup < self._high:
            if state in ("PREPARED", "CONSUMED") and tup <= self._fenced:
                self._update_receipt(op, "REJECTED", "stale", None)
                raise AuthorityError("stale", "this installation has fenced that epoch")
            if state in ("PREPARED", "CONSUMED") and tup < self._high:
                raise AuthorityError("stale", "authority tuple is older than the durable high-water mark")
            raise AuthorityError("stale", "ticket is older than this installation's durable fence")
        if op["effect_class"] == "interactive" and self._session_state(op.get("session_id")) == "released":
            if state == "PREPARED":
                self._update_receipt(op, "RELEASED", "released", None)
                raise AuthorityError("released", "interactive session was released")
            if state == "CONSUMED":
                raise AuthorityError("released", "interactive session was released before the effect")
            raise AuthorityError("released", "interactive session was released")
        return row

    def _reject_blocked_attempt(self, op):
        if self._db is None:
            return
        rows = self._db.execute("SELECT * FROM receipts WHERE target=? AND operation_id=?",
                                (op["target"], op["operation_id"])).fetchall()
        for row in rows:
            self._check_row_hash(row, _RECEIPT_FIELDS)
            if row["state"] in _OPEN_STATES or row["state"] in _UNCERTAIN_STATES:
                raise AuthorityError("in_progress", "an earlier attempt for this operation is unresolved")
            if row["state"] == "COMPLETED" and (op["effect_class"] != "pure_compute" or row["effect_class"] != "pure_compute"):
                raise AuthorityError("conflict", "only a finished pure-compute attempt may be retried")

    def _pin_expiry_if_needed(self, op):
        lifetime = op["admit_until"] - op["issued_at"]
        if lifetime <= 0 or lifetime > MAX_TICKET_SECONDS:
            raise AuthorityError("bad_ticket", "ticket lifetime invalid")
        if op["issued_at"] > self._wall() + TICKET_SKEW:
            raise AuthorityError("bad_ticket", "ticket issued in the future")
        if self._wall() >= op["admit_until"]:
            self._insert_receipt(op, "EXPIRED", "ticket_expired")
            raise AuthorityError("ticket_expired", "ticket admission window is over")

    def _finish_existing(self, row, op, sink, failpoint):
        state = row["state"]
        if state == "COMPLETED":
            return _public(row, already=True)
        if state in _UNCERTAIN_STATES:
            raise AuthorityError("uncertain", "effect outcome unknown")
        if state == "EXPIRED":
            raise AuthorityError("ticket_expired", "ticket admission window is over")
        if state == "REJECTED":
            raise AuthorityError(row["result_code"] or "stale", "admission was refused")
        if state == "RELEASED":
            raise AuthorityError("released", "reservation released without an effect")
        if state in _OPEN_STATES:
            return self._drive(op, sink, failpoint, already=True)
        self._mark_quarantine()
        raise AuthorityError("quarantined", "unrecognized receipt state")

    def _drive(self, op, sink, failpoint, already):
        """PREPARED or CONSUMED only. ACCEPTED is the last record written before `sink`.

        ``admission()`` is a historical read and does not authorize the sink. A PREPARED
        row still consumes the exact ticket when that lookup already found a record.
        CONSUMED work rechecks the current tuple and consumes the same ticket again
        before ACCEPTED. An identical consume may return the old receipt for
        reconciliation; a stale current status still refuses the effect. Revoked, stale,
        conflict, and bad_ticket become REJECTED. A transient error leaves CONSUMED, or
        PREPARED if this attempt never consumed, and does not call the sink. ACCEPTED
        is uncertain and is never retried.

        The status check, the consume, and the sink are not one distributed atomic
        effect with a revoke. A fence that is durable before ACCEPTED refuses the
        sink. A fence or revoke that lands after ACCEPTED can overlap the running
        sink; fence's acknowledge waits until that sink returns. Once ACCEPTED is
        written, the effect is not attempted again.
        """
        row = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
        if row["state"] == "PREPARED":
            if op["effect_class"] == "interactive" and self._session_state(op.get("session_id")) == "released":
                self._update_receipt(op, "RELEASED", "released", None)
                raise AuthorityError("released", "interactive session was released")
            prior = self._lookup_admission(op["admission_id"])
            row = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
            if row["state"] != "PREPARED":
                return self._finish_existing(row, op, sink, failpoint)
            self._revalidate_open(op)
            if prior is not None:
                try:
                    _same_admission(prior, op)
                except AuthorityError as exc:
                    self._on_consume_error(op, exc)
                    raise
            consumed = self._consume_authorized(op)
            row = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
            if row["state"] != "PREPARED":
                return self._finish_existing(row, op, sink, failpoint)
            self._update_receipt(op, "CONSUMED", None, consumed.get("consumed_at"))
            self._advance_high(op["incarnation"], op["epoch"], op["mode_version"])
            self._crash(failpoint, "after_consume")
            row = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
        if row["state"] != "CONSUMED":
            return self._finish_existing(row, op, sink, failpoint)
        if op["effect_class"] == "interactive" and self._session_state(op.get("session_id")) == "released":
            raise AuthorityError("released", "interactive session was released before the effect")
        # Re-check immediately before ACCEPTED. A consume that already returned, including
        # an exact historical duplicate, is not permission to run the sink.
        self._consume_authorized(op)
        row = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
        if row["state"] != "CONSUMED":
            return self._finish_existing(row, op, sink, failpoint)
        self._revalidate_open(op)
        row = self._receipt(op["target"], op["operation_id"], op["attempt_id"])
        if row["state"] != "CONSUMED":
            return self._finish_existing(row, op, sink, failpoint)
        # Fsync the accepted receipt before the effect. A crash here leaves ACCEPTED, which
        # is uncertain and is never fed to the sink again.
        consumed_at = row["consumed_at"]
        self._update_receipt(op, "ACCEPTED", None, consumed_at)
        self._crash(failpoint, "after_accept")
        self._inflight += 1
        try:
            with self._unlocked():
                try:
                    sink()
                    self._crash(failpoint, "during_effect")
                except AuthorityError:
                    raise
                except Exception:
                    raise AuthorityError("uncertain", "effect outcome unknown") from None
            self._update_receipt(op, "COMPLETED", "completed", consumed_at)
            return _public(self._receipt(op["target"], op["operation_id"], op["attempt_id"]), already=already)
        finally:
            self._inflight -= 1
            self._cv.notify_all()

    def _consume_authorized(self, op):
        """Current tuple, mode, and readiness, then an exact authenticated consume.

        Status is first. A stale tuple is refused here and is not sent to ``consume``.
        When the tuple is still current, ``consume`` rechecks membership. An identical
        ticket can return the stored receipt; ``_same_admission`` keeps that receipt
        bound to this attempt. ``admission()`` is not called.
        """
        self._status_for_ticket(op)
        try:
            with self._unlocked():
                consumed = self._authority.consume(
                    admission_id=op["admission_id"], target=op["target"], operation_id=op["operation_id"],
                    attempt_id=op["attempt_id"], digest=op["digest"], main=op["main"],
                    incarnation=op["incarnation"], epoch=op["epoch"], issued_at=op["issued_at"],
                    admit_until=op["admit_until"], issuer_incarnation=op["issuer_incarnation"])
        except AuthorityError as exc:
            self._on_consume_error(op, exc)
            raise
        except OSError:
            raise AuthorityError("unavailable", "authority unreachable") from None
        _same_admission(consumed, op)
        # Fence, stop, or a higher tuple may have landed while consume ran.
        self._revalidate_open(op)
        return consumed

    def _status_for_ticket(self, op):
        with self._unlocked():
            status = self._status()
        if self._quarantined:
            raise AuthorityError("quarantined", "executor journal is unusable")
        self._check_pinned(status)
        issued = (int(status["incarnation"]), int(status["epoch"]))
        if issued < self._high or int(status["mode_version"]) < self._high_mode:
            raise AuthorityError("stale", "authority tuple is older than the durable high-water mark")
        if issued > self._high or int(status["mode_version"]) > self._high_mode:
            self._advance_high(issued[0], issued[1], int(status["mode_version"]))
        tup = (op["incarnation"], op["epoch"])
        if (issued != tup or int(status["mode_version"]) != op["mode_version"]
                or int(status["policy_version"]) != op["policy_version"] or status["main"] != op["main"]
                or status["authority_id"] != op["authority_id"]):
            self._update_receipt(op, "REJECTED", "stale", None)
            raise AuthorityError("stale", "not the current MAIN tuple")
        if status.get("mode") != "V2_ACTIVE" or not status.get("main_ready") or not status.get("lease_valid"):
            raise AuthorityError("no_admission", "MAIN lease not valid and ready")
        if tup <= self._fenced:
            self._update_receipt(op, "REJECTED", "stale", None)
            raise AuthorityError("stale", "this installation has fenced that epoch")
        return status

    def _on_consume_error(self, op, exc):
        if exc.code in ("stale", "conflict", "bad_ticket", "revoked"):
            self._update_receipt(op, "REJECTED", exc.code, None)
        elif exc.code == "ticket_expired":
            self._update_receipt(op, "EXPIRED", "ticket_expired", None)
        # no_admission, target_unfenced, unavailable, quarantined: leave PREPARED or CONSUMED.

    def _advance_high(self, incarnation, epoch, mode_version):
        if self._fence_boot is None:
            raise AuthorityError("fenced", "high-water mark requires a durable fence")
        high = (incarnation, epoch)
        if high < self._high or mode_version < self._high_mode:
            return
        if high == self._high and mode_version == self._high_mode:
            return
        new_high = high if high > self._high else self._high
        # Keep the boot id that was acked. A later reboot must not rewrite it.
        self._write_fence(new_high, max(self._high_mode, mode_version), self._fenced,
                          self._legacy_closed, self._fence_boot)

    def _status(self):
        if self._authority is None:
            raise AuthorityError("no_authority", "no authority client")
        try:
            status = self._authority.status()
        except AuthorityError:
            raise
        except OSError:
            raise AuthorityError("unavailable", "authority unreachable") from None
        if not isinstance(status, dict):
            raise AuthorityError("unavailable", "authority status unreadable")
        try:
            status["incarnation"] = _int(status["incarnation"], "incarnation")
            status["epoch"] = _int(status["epoch"], "epoch")
            status["mode_version"] = _int(status["mode_version"], "mode_version")
            status["policy_version"] = _int(status["policy_version"], "policy_version")
            status["authority_id"] = _ident(status["authority_id"], "authority_id")
            status["mode"] = status["mode"]
            if status["main"] is not None:
                status["main"] = _ident(status["main"], "main")
        except (KeyError, AuthorityError):
            raise AuthorityError("unavailable", "authority status unreadable") from None
        return status

    def _check_pinned(self, status):
        if status["authority_id"] != self.authority_id:
            raise AuthorityError("authority_mismatch", "status is from a different authority")

    def _lookup_admission(self, admission_id):
        try:
            with self._unlocked():
                return self._authority.admission(admission_id)
        except AuthorityError:
            raise
        except OSError:
            raise AuthorityError("unavailable", "authority unreachable") from None

    def _crash(self, failpoint, point):
        if failpoint == point:
            raise AuthorityError("crash", "injected crash before the effect commit")

    # --- receipt rows -------------------------------------------------------------------

    def _receipt(self, target, operation_id, attempt_id):
        if self._db is None:
            return None
        row = self._db.execute("SELECT * FROM receipts WHERE target=? AND operation_id=? AND attempt_id=?",
                               (target, operation_id, attempt_id)).fetchone()
        if row is not None:
            self._check_row_hash(row, _RECEIPT_FIELDS)
        return row

    def _insert_receipt(self, op, state, result_code):
        self._ensure_db()
        fields = _receipt_fields(op, state, result_code, None)
        fields["record_hash"] = _digest_row(fields, _RECEIPT_FIELDS)
        try:
            with self._tx() as db:
                db.execute("INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           tuple(fields[k] for k in _RECEIPT_FIELDS) + (fields["record_hash"],))
        except sqlite3.IntegrityError:
            raise AuthorityError("conflict", "admission identity already used") from None

    def _update_receipt(self, op, state, result_code, consumed_at):
        fields = _receipt_fields(op, state, result_code, consumed_at)
        fields["record_hash"] = _digest_row(fields, _RECEIPT_FIELDS)
        with self._tx() as db:
            db.execute("UPDATE receipts SET state=?, result_code=?, consumed_at=?, record_hash=? "
                       "WHERE target=? AND operation_id=? AND attempt_id=?",
                       (state, result_code, fields["consumed_at"], fields["record_hash"],
                        op["target"], op["operation_id"], op["attempt_id"]))

    def _session_state(self, session_id):
        if self._db is None or not session_id:
            return None
        row = self._db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            return None
        self._check_row_hash(row, ("session_id", "state", "reason"))
        return row["state"]

    def _write_session(self, session_id, state, reason):
        fields = {"session_id": session_id, "state": state, "reason": reason}
        digest = _digest_row(fields, ("session_id", "state", "reason"))
        with self._tx() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                       "state=excluded.state, reason=excluded.reason, record_hash=excluded.record_hash",
                       (session_id, state, reason, digest))

    def _check_row_hash(self, row, keys):
        fields = {key: row[key] for key in keys}
        if row["record_hash"] != _digest_row(fields, keys):
            self._mark_quarantine()
            raise AuthorityError("quarantined", "executor journal failed its integrity check")

    def _mark_quarantine(self):
        self._quarantined = True
        self._legacy_closed = True
        self._quarantine_reason = "integrity"
        if self._db is not None:
            try:
                self._db.execute("INSERT INTO meta(key, value) VALUES('quarantined', '1') "
                                 "ON CONFLICT(key) DO UPDATE SET value='1'")
            except sqlite3.Error:
                pass

    def _tx(self):
        return transaction(self._db)


def _exists(path):
    try:
        return path.exists()
    except AttributeError:
        import os
        return os.path.exists(path)


def _only_fields(data, required, optional, name):
    if not isinstance(data, dict):
        raise AuthorityError("bad_request", name + " must be an object")
    unknown = set(data) - set(required) - set(optional)
    if unknown or any(key not in data for key in required):
        raise AuthorityError("bad_request", name + " fields do not match the admission contract")
    return data


def _operation(signed, ticket):
    signed = _only_fields(signed, _OP_REQUIRED, _OP_OPTIONAL, "signed operation")
    ticket = _only_fields(ticket, _TICKET_FIELDS, (), "admission ticket")
    signature = signed["signature"]
    if not isinstance(signature, str) or not 1 <= len(signature) <= MAX_SIGNATURE:
        raise AuthorityError("bad_request", "signature missing")
    if signed["effect_class"] not in _EFFECTS:
        raise AuthorityError("bad_request", "unknown effect class")
    op = {
        "authority_id": _ident(signed["authority_id"], "authority_id"),
        "fleet_id": _ident(signed["fleet_id"], "fleet_id"),
        "main": _ident(signed["main"], "main"),
        "target": _ident(signed["target"], "target"),
        "operation_id": _ident(signed["operation_id"], "operation_id"),
        "attempt_id": _ident(signed["attempt_id"], "attempt_id"),
        "admission_id": _ident(signed["admission_id"], "admission_id"),
        "digest": _digest(signed["digest"]),
        "effect_class": signed["effect_class"],
        "incarnation": _int(signed["incarnation"], "incarnation"),
        "epoch": _int(signed["epoch"], "epoch"),
        "mode_version": _int(signed["mode_version"], "mode_version"),
        "policy_version": _int(signed["policy_version"], "policy_version"),
        "issuer_incarnation": _int(signed["issuer_incarnation"], "issuer_incarnation"),
        "issued_at": _num(signed["issued_at"], "issued_at"),
        "admit_until": _num(signed["admit_until"], "admit_until"),
    }
    if "session_id" in signed and signed["session_id"] is not None:
        op["session_id"] = _ident(signed["session_id"], "session_id")
    for key in _TICKET_FIELDS:
        left = op[key]
        right = ticket[key]
        if key in ("issued_at", "admit_until"):
            right = _num(right, key)
            if abs(left - right) > 1e-6:
                raise AuthorityError("bad_request", "ticket does not match the signed operation")
        elif key in ("incarnation", "epoch", "issuer_incarnation"):
            if left != _int(right, key):
                raise AuthorityError("bad_request", "ticket does not match the signed operation")
        elif key == "digest":
            if left != _digest(right):
                raise AuthorityError("bad_request", "ticket does not match the signed operation")
        elif left != _ident(right, key):
            raise AuthorityError("bad_request", "ticket does not match the signed operation")
    op["token_hash"] = _digest_row(op, _TOKEN_FIELDS)
    return op


def _same_attempt(row, op):
    if row["token_hash"] != op["token_hash"] or row["digest"] != op["digest"]:
        raise AuthorityError("conflict", "this attempt was already admitted differently")
    if (row["session_id"] or None) != op.get("session_id"):
        raise AuthorityError("conflict", "this attempt was already admitted differently")
    if row["issued_at"] != _canon_num(op["issued_at"]) or row["admit_until"] != _canon_num(op["admit_until"]):
        raise AuthorityError("conflict", "original issuance and expiry cannot be refreshed")
    if int(row["incarnation"]) != op["incarnation"] or int(row["epoch"]) != op["epoch"]:
        raise AuthorityError("conflict", "this attempt was already admitted differently")


def _same_admission(prior, op):
    if prior is None:
        raise AuthorityError("unavailable", "authority admission unreadable")
    try:
        same = (
            prior["admission_id"] == op["admission_id"] and prior["target"] == op["target"]
            and prior["operation_id"] == op["operation_id"] and prior["attempt_id"] == op["attempt_id"]
            and prior["digest"] == op["digest"] and prior["main"] == op["main"]
            and int(prior["incarnation"]) == op["incarnation"] and int(prior["epoch"]) == op["epoch"]
            and int(prior["issuer_incarnation"]) == op["issuer_incarnation"]
            and abs(float(prior["issued_at"]) - op["issued_at"]) < 1e-6
            and abs(float(prior["admit_until"]) - op["admit_until"]) < 1e-6
        )
    except (KeyError, TypeError, ValueError):
        same = False
    if not same:
        raise AuthorityError("conflict", "this attempt was already admitted differently")


def _receipt_fields(op, state, result_code, consumed_at):
    return {
        "target": op["target"], "operation_id": op["operation_id"], "attempt_id": op["attempt_id"],
        "admission_id": op["admission_id"], "digest": op["digest"], "token_hash": op["token_hash"],
        "main": op["main"], "authority_id": op["authority_id"], "incarnation": op["incarnation"],
        "epoch": op["epoch"], "mode_version": op["mode_version"], "policy_version": op["policy_version"],
        "issued_at": _canon_num(op["issued_at"]), "admit_until": _canon_num(op["admit_until"]),
        "issuer_incarnation": op["issuer_incarnation"], "effect_class": op["effect_class"],
        "session_id": op.get("session_id"), "state": state, "result_code": result_code,
        "consumed_at": None if consumed_at is None else _canon_num(consumed_at),
    }


def _public(row, already):
    return {
        "target": row["target"], "operation_id": row["operation_id"], "attempt_id": row["attempt_id"],
        "admission_id": row["admission_id"], "digest": row["digest"], "token_hash": row["token_hash"],
        "main": row["main"], "authority_id": row["authority_id"],
        "incarnation": int(row["incarnation"]), "epoch": int(row["epoch"]),
        "mode_version": int(row["mode_version"]), "issued_at": row["issued_at"],
        "admit_until": row["admit_until"], "effect_class": row["effect_class"],
        "state": row["state"], "result_code": row["result_code"],
        "effect": _effect_word(row["state"]), "already": already,
    }
