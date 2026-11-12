"""Single-writer MAIN authority: fleet mode, lease, epoch, transitions, admission consumption.

Prototype of MAIN-HANDOFF.md §3-§7 (H2). One process owns one SQLite file (WAL, FULL sync,
an exclusive OS lock on `<db>.writer`). Every mutation is one `BEGIN IMMEDIATE` transaction
with compare-and-set on the expected tuple.

Rules this enforces (each has a failing-then-passing test in tests/continuity):
- Zero or one MAIN. A lease is only granted by a transition, never self-claimed or renewed
  after it lapsed; recovery always advances the epoch.
- Lease validity lives only in this process's RAM on its monotonic clock. Opening the store
  is a new incarnation: every old lease is void, and nothing is granted until the longest
  validity ever handed out (plus a margin) has passed.
- Handover/recovery fences before granting: renewals stop, then either every eligible
  effect-bearing participant acknowledged the fence, or the old validity ran out (+margin).
  A candidate is granted not-ready; ordinary admission waits for a verified canary. A
  failure after the fence leaves no MAIN (never resurrects the old lease).
- Admission is consumed once per (target, operation, attempt) under the exact current
  tuple. A retry of the same admission returns the original record, also after a later
  handover; a different digest or admission id for that attempt is a conflict.
- Metadata only: ids, digests, numbers, booleans. No prompt, body, payload or result is
  accepted or stored (identifier shapes are validated; there is no free-text column).

Not here: transport, caller authentication, passkey verification, signatures, PC receipt
journals, executor guards. Callers pass identities they already authenticated.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1
LEASE_SECONDS = 15.0          # design §10.1: renew ~5 s, expire 15 s
RENEW_SECONDS = 5.0
SAFETY_MARGIN = 2.0           # added to every validity wait; measure before trusting
MAX_TICKET_SECONDS = 24 * 3600.0
TICKET_SKEW = 5.0             # issued_at may be this far in the future (clock skew)
DEDUP_SECONDS = 90 * 24 * 3600.0
# Admission ticket fields, in signing order. The one copy: gateway, runtime, executor and
# integration import it, and each keeps its own validation of the values.
TICKET_FIELDS = (
    "admission_id", "target", "operation_id", "attempt_id", "digest", "main",
    "incarnation", "epoch", "issued_at", "admit_until", "issuer_incarnation",
)
# Longest accepted ssh signature armor (a 4096-bit RSA signature fits). Every boundary that
# checks a ticket signature uses this one cap, so none passes what the gateway would refuse.
MAX_SIGNATURE = 1800

MODES = ("LEGACY_ACTIVE", "PREPARING_V2", "QUIESCED", "V2_ONLY_NO_MAIN", "V2_ACTIVE",
         "HANDOVER", "ROLLBACK_QUIESCED", "QUARANTINED")
# Cutover edges an operator may request with set_mode. V2_ACTIVE/HANDOVER/V2_ONLY_NO_MAIN
# between each other only change through grants, transitions and release.
MODE_EDGES = {
    "LEGACY_ACTIVE": {"PREPARING_V2"},
    "PREPARING_V2": {"QUIESCED", "LEGACY_ACTIVE"},
    "QUIESCED": {"V2_ONLY_NO_MAIN", "LEGACY_ACTIVE"},
    "V2_ONLY_NO_MAIN": {"ROLLBACK_QUIESCED"},
    "ROLLBACK_QUIESCED": {"LEGACY_ACTIVE"},
}
ACKED_MODES = {"V2_ONLY_NO_MAIN", "LEGACY_ACTIVE"}  # entering needs every eligible participant's ack
ACTIONS = ("make_main_now", "set_preferred_main", "recover")
PROFILES = ("control", "ai")
OPEN_STATES = ("REQUESTED", "VALIDATING", "DRAINING", "FENCING", "GRANTED_NOT_READY")
FENCED_STATES = ("FENCING", "GRANTED_NOT_READY")
FINAL_STATES = ("SUCCEEDED", "FAILED", "CANCELLED")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS authority(
  fleet_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL,
  mode TEXT NOT NULL, mode_version INTEGER NOT NULL,
  incarnation INTEGER NOT NULL, epoch INTEGER NOT NULL, policy_version INTEGER NOT NULL,
  holder TEXT, lease_id TEXT, lease_ready INTEGER NOT NULL DEFAULT 0,
  last_holder TEXT, preferred TEXT, transition_id TEXT,
  max_validity REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS participants(
  device_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('pc', 'server')), eligible INTEGER NOT NULL,
  mode_version INTEGER NOT NULL DEFAULT 0, fenced_epoch INTEGER NOT NULL DEFAULT -1,
  boot_id TEXT, acked_at REAL);
CREATE TABLE IF NOT EXISTS transitions(
  transition_id TEXT PRIMARY KEY, caller TEXT NOT NULL, client_key TEXT NOT NULL,
  request_digest TEXT NOT NULL, action TEXT NOT NULL, profile TEXT NOT NULL,
  source TEXT, candidate TEXT NOT NULL, expected_incarnation INTEGER NOT NULL,
  expected_epoch INTEGER NOT NULL, expected_policy INTEGER NOT NULL, state TEXT NOT NULL,
  control_ready INTEGER, ai_ready INTEGER, checkpoint TEXT,
  fenced_epoch INTEGER, outcome TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(caller, client_key));
CREATE TABLE IF NOT EXISTS admissions(
  admission_id TEXT PRIMARY KEY, target TEXT NOT NULL, operation_id TEXT NOT NULL,
  attempt_id TEXT NOT NULL, digest TEXT NOT NULL, main TEXT NOT NULL,
  incarnation INTEGER NOT NULL, epoch INTEGER NOT NULL, issued_at REAL NOT NULL,
  admit_until REAL NOT NULL, issuer_incarnation INTEGER NOT NULL, consumed_at REAL NOT NULL,
  UNIQUE(target, operation_id, attempt_id));
"""


class AuthorityError(ValueError):
    """A refusal with a stable `code`; `wait` is seconds until it may succeed, if known."""

    def __init__(self, code, message, wait=None):
        super().__init__(message)
        self.code = code
        self.wait = wait


def _ident(value, name):
    if not isinstance(value, str) or not _ID.match(value):
        raise AuthorityError("bad_request", f"{name} must be a short identifier")
    return value


def _digest(value):
    if not isinstance(value, str) or not _DIGEST.match(value):
        raise AuthorityError("bad_request", "digest must be sha256:<64 hex>")
    return value


def _int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuthorityError("bad_request", f"{name} must be a non-negative integer")
    return value


def _num(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise AuthorityError("bad_request", f"{name} must be a number")
    return float(value)


def _writer_lock(path):
    """Exclusive, nonblocking lock held for the store's lifetime: one writer per file."""
    handle = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AuthorityError("writer_busy", "another authority process owns this store")
    return handle


class AuthorityStore:
    """One authority incarnation. Create once per process start; `close()` on shutdown."""

    def __init__(self, path, fleet_id, authority_id, *, monotonic=time.monotonic, wall=time.time,
                 lease_seconds=LEASE_SECONDS, margin=SAFETY_MARGIN):
        self.path = Path(path)
        self.fleet_id = _ident(fleet_id, "fleet_id")
        self.authority_id = _ident(authority_id, "authority_id")
        self._mono, self._wall = monotonic, wall
        self.lease_seconds = float(lease_seconds)
        self.margin = float(margin)
        if not 0 < self.lease_seconds <= 300:
            raise AuthorityError("bad_request", "lease_seconds out of range")
        self._lock = threading.RLock()
        self._writer = _writer_lock(str(self.path) + ".writer")
        try:
            self._db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(_SCHEMA)
        except Exception:
            self._writer.close()
            raise
        # RAM-only validity (authority monotonic clock). Never persisted, never restored.
        self._lease = None            # {"lease_id", "holder", "expires"}
        self._validity_end = None     # last moment any grant this incarnation could be valid
        self._drain_since = None      # renewals stopped for the open transition
        self._start()

    # --- lifecycle ---------------------------------------------------------------------

    def _start(self):
        now_m, now_w = self._mono(), self._wall()
        with self._tx() as db:
            schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            row = db.execute("SELECT * FROM authority WHERE fleet_id=?", (self.fleet_id,)).fetchone()
            others = db.execute("SELECT COUNT(*) FROM authority WHERE fleet_id<>?", (self.fleet_id,)).fetchone()[0]
            if row is None and schema is None and not others:
                db.execute("INSERT INTO meta VALUES('schema', ?)", (str(SCHEMA_VERSION),))
                db.execute("INSERT INTO authority(fleet_id, authority_id, mode, mode_version, incarnation, epoch,"
                           " policy_version, max_validity, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (self.fleet_id, self.authority_id, "LEGACY_ACTIVE", 1, 1, 0, 1, 0.0, now_w))
                self._barrier_until = now_m
                return
            bad = (row is None or others or schema is None or schema[0] != str(SCHEMA_VERSION)
                   or row["authority_id"] != self.authority_id or row["mode"] not in MODES)
            if bad:
                if row is not None:
                    db.execute("UPDATE authority SET mode='QUARANTINED', holder=NULL, lease_id=NULL,"
                               " lease_ready=0, updated_at=? WHERE fleet_id=?", (now_w, self.fleet_id))
                self._quarantined = True
                self._barrier_until = float("inf")
                return
            # New incarnation: every earlier lease is void; wait out the longest validity ever issued.
            mode = "V2_ONLY_NO_MAIN" if row["mode"] in ("V2_ACTIVE", "HANDOVER") else row["mode"]
            db.execute("UPDATE authority SET incarnation=incarnation+1, epoch=epoch+1, mode=?,"
                       " last_holder=COALESCE(holder, last_holder), holder=NULL, lease_id=NULL, lease_ready=0,"
                       " transition_id=NULL, updated_at=? WHERE fleet_id=?", (mode, now_w, self.fleet_id))
            db.execute("UPDATE transitions SET state='FAILED', outcome='authority_restarted', updated_at=?"
                       " WHERE state IN (%s)" % ",".join("?" * len(OPEN_STATES)), (now_w, *OPEN_STATES))
            wait = row["max_validity"] + self.margin if row["max_validity"] > 0 else 0.0
            self._barrier_until = now_m + wait

    _quarantined = False

    def close(self):
        with self._lock:
            try:
                self._db.close()
            finally:
                self._writer.close()

    def _tx(self):
        store = self

        class _Tx:
            def __enter__(self):
                store._lock.acquire()
                try:
                    store._db.execute("BEGIN IMMEDIATE")
                except Exception:
                    store._lock.release()
                    raise
                return store._db

            def __exit__(self, kind, exc, tb):
                try:
                    store._db.execute("COMMIT" if kind is None else "ROLLBACK")
                finally:
                    store._lock.release()
                return False
        return _Tx()

    def _row(self, db):
        return db.execute("SELECT * FROM authority WHERE fleet_id=?", (self.fleet_id,)).fetchone()

    def _guard(self):
        if self._quarantined:
            raise AuthorityError("quarantined", "authority state is unrecognized; restore by an operator")

    # --- validity helpers ---------------------------------------------------------------

    def _lease_valid(self, row, now):
        lease = self._lease
        return bool(lease and row["lease_id"] and lease["lease_id"] == row["lease_id"]
                    and lease["holder"] == row["holder"] and now < lease["expires"])

    def _acked(self, db, row, also=()):
        """Every eligible participant (and each device in `also`) acknowledged that it refuses
        grants of the current epoch. Acks never shortcut the new-incarnation barrier."""
        epoch = row["epoch"]
        if db.execute("SELECT COUNT(*) FROM participants WHERE eligible=1 AND fenced_epoch<?",
                      (epoch,)).fetchone()[0]:
            return False
        for device in also:
            p = self._participant(db, device) if device else None
            if device and (p is None or p["fenced_epoch"] < epoch):
                return False
        return True

    def _quiet(self, db, row, now, acked):
        """Seconds until no earlier grant can still be honoured (0 = quiet now)."""
        waits = [self._barrier_until - now]
        if self._validity_end is not None and not acked:
            waits.append(self._validity_end + self.margin - now)
        return max(0.0, *waits)

    def _participant(self, db, device_id):
        return db.execute("SELECT * FROM participants WHERE device_id=?", (device_id,)).fetchone()

    # --- read ---------------------------------------------------------------------------

    def status(self):
        """RoleStatus: safe to show on any authenticated surface (no credentials, no bodies)."""
        with self._lock:
            row = self._row(self._db)
            now = self._mono()
            valid = self._lease_valid(row, now)
            tr = None
            if row["transition_id"]:
                t = self._db.execute("SELECT * FROM transitions WHERE transition_id=?",
                                     (row["transition_id"],)).fetchone()
                tr = _transition_view(t)
            return {"fleet_id": self.fleet_id, "authority_id": row["authority_id"], "mode": row["mode"],
                    "mode_version": row["mode_version"], "incarnation": row["incarnation"],
                    "epoch": row["epoch"], "policy_version": row["policy_version"],
                    "main": row["holder"] if valid else None, "lease_holder": row["holder"],
                    "lease_valid": valid, "main_ready": bool(valid and row["lease_ready"]),
                    "lease_remaining": round(self._lease["expires"] - now, 3) if valid else 0.0,
                    "last_main": row["last_holder"], "preferred": row["preferred"], "transition": tr,
                    "grant_barrier": round(max(0.0, self._barrier_until - now), 3)
                    if self._barrier_until != float("inf") else None}

    def transition(self, transition_id):
        with self._lock:
            t = self._db.execute("SELECT * FROM transitions WHERE transition_id=?",
                                 (_ident(transition_id, "transition_id"),)).fetchone()
            if t is None:
                raise AuthorityError("not_found", "no such transition")
            return _transition_view(t)

    def admission(self, admission_id):
        """Reconciliation lookup. Returns the consumed record or None; never consumes."""
        with self._lock:
            r = self._db.execute("SELECT * FROM admissions WHERE admission_id=?",
                                 (_ident(admission_id, "admission_id"),)).fetchone()
            return dict(r) if r else None

    # --- participants and cutover -----------------------------------------------------

    def register_participant(self, device_id, installation_id, kind, eligible=True):
        """Enroll (or re-identify) a device. Servers are never MAIN candidates. A new
        installation id resets its acknowledgements: a reinstalled machine must re-fence."""
        self._guard()
        _ident(device_id, "device_id"), _ident(installation_id, "installation_id")
        if kind not in ("pc", "server"):
            raise AuthorityError("bad_request", "kind must be pc or server")
        with self._tx() as db:
            old = self._participant(db, device_id)
            if old is None or old["installation_id"] != installation_id:
                db.execute("INSERT OR REPLACE INTO participants(device_id, installation_id, kind, eligible)"
                           " VALUES(?,?,?,?)", (device_id, installation_id, kind, 1 if eligible else 0))
            else:
                db.execute("UPDATE participants SET kind=?, eligible=? WHERE device_id=?",
                           (kind, 1 if eligible else 0, device_id))

    def acknowledge(self, device_id, installation_id, mode_version, fenced_epoch, boot_id=None):
        """A participant durably installed `mode_version` and refuses every grant whose epoch is
        <= `fenced_epoch` (it acks the epoch being fenced). Acks only move forward; a foreign
        installation or a version/epoch the authority never issued is refused."""
        self._guard()
        _int(mode_version, "mode_version"), _int(fenced_epoch, "fenced_epoch")
        if boot_id is not None:
            _ident(boot_id, "boot_id")
        with self._tx() as db:
            p = self._participant(db, _ident(device_id, "device_id"))
            if p is None or p["installation_id"] != installation_id:
                raise AuthorityError("unknown_participant", "device/installation not enrolled")
            row = self._row(db)
            if mode_version > row["mode_version"] + 1 or fenced_epoch > row["epoch"]:
                raise AuthorityError("ahead", "acknowledgement names a version the authority never issued")
            db.execute("UPDATE participants SET mode_version=MAX(mode_version, ?), fenced_epoch=MAX(fenced_epoch, ?),"
                       " boot_id=COALESCE(?, boot_id), acked_at=? WHERE device_id=?",
                       (mode_version, fenced_epoch, boot_id, self._wall(), device_id))

    def set_mode(self, mode, expected_mode_version):
        """Cutover step (MAIN-HANDOFF §5). Entering V2_ONLY_NO_MAIN or LEGACY_ACTIVE from a
        quiesced mode needs every eligible participant to have acked the new version first."""
        self._guard()
        _int(expected_mode_version, "expected_mode_version")
        with self._tx() as db:
            row = self._row(db)
            if row["mode_version"] != expected_mode_version:
                raise AuthorityError("stale", "mode version changed")
            if mode not in MODE_EDGES.get(row["mode"], ()):
                raise AuthorityError("bad_edge", f"{row['mode']} cannot move to {mode}")
            new_version = row["mode_version"] + 1
            if mode in ACKED_MODES and row["mode"] in ("QUIESCED", "ROLLBACK_QUIESCED"):
                behind = db.execute("SELECT device_id FROM participants WHERE eligible=1 AND mode_version<?",
                                    (new_version,)).fetchall()
                if behind:
                    raise AuthorityError("unacked", "not fenced: " + ", ".join(r[0] for r in behind))
            if mode == "ROLLBACK_QUIESCED":
                wait = self._quiet(db, row, self._mono(), self._acked(db, row, (row["last_holder"],)))
                if wait > 0:
                    raise AuthorityError("not_quiet", "earlier v2 grants may still be valid", wait)
            db.execute("UPDATE authority SET mode=?, mode_version=?, updated_at=? WHERE fleet_id=?",
                       (mode, new_version, self._wall(), self.fleet_id))
            return new_version

    # --- lease --------------------------------------------------------------------------

    def renew(self, holder, lease_id, incarnation, epoch):
        """MAIN heartbeat: compare-and-set on the full tuple. An expired lease is never
        revived; a fenced or draining one is refused. Returns seconds of validity granted
        from the authority's receipt of this call (caller anchors at its request start)."""
        self._guard()
        with self._tx() as db:
            row, now = self._row(db), self._mono()
            self._match(row, holder, lease_id, incarnation, epoch)
            if not self._lease_valid(row, now):
                raise AuthorityError("expired", "lease lapsed; only a recovery transition can grant again")
            if self._drain_since is not None:
                raise AuthorityError("draining", "a transition stopped renewals")
            self._lease["expires"] = now + self.lease_seconds
            self._validity_end = max(self._validity_end or 0.0, self._lease["expires"])
            return {"valid_for": self.lease_seconds, "renew_after": RENEW_SECONDS,
                    "ready": bool(row["lease_ready"])}

    def release(self, holder, lease_id, incarnation, epoch):
        """Planned stop (e.g. before suspend). MAIN gives up new-work authority now; the
        validity it already handed out still has to drain or be acked before a successor."""
        self._guard()
        with self._tx() as db:
            row = self._row(db)
            self._match(row, holder, lease_id, incarnation, epoch)
            if row["transition_id"]:
                raise AuthorityError("busy", "a transition is open; let it fence")
            self._lease = None
            db.execute("UPDATE authority SET holder=NULL, lease_id=NULL, lease_ready=0, last_holder=?,"
                       " mode=?, updated_at=? WHERE fleet_id=?",
                       (holder, "V2_ONLY_NO_MAIN", self._wall(), self.fleet_id))

    def _match(self, row, holder, lease_id, incarnation, epoch):
        if (row["holder"] is None or row["holder"] != holder or row["lease_id"] != lease_id
                or row["incarnation"] != incarnation or row["epoch"] != epoch):
            raise AuthorityError("stale", "not the current MAIN tuple")

    # --- transitions --------------------------------------------------------------------

    def request(self, caller, client_key, action, candidate, expected_incarnation, expected_epoch,
                expected_policy, profile="control"):
        """Open one transition (idempotent on caller+client_key). The caller must already
        hold verified `manage_main` proof; this store does not check passkeys."""
        self._guard()
        _ident(caller, "caller"), _ident(client_key, "client_key"), _ident(candidate, "candidate")
        for v, n in ((expected_incarnation, "expected_incarnation"), (expected_epoch, "expected_epoch"),
                     (expected_policy, "expected_policy")):
            _int(v, n)
        if action not in ACTIONS:
            raise AuthorityError("bad_request", "unknown action")
        if profile not in PROFILES:
            raise AuthorityError("bad_request", "unknown readiness profile")
        body = json.dumps([action, candidate, expected_incarnation, expected_epoch, expected_policy, profile])
        digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        with self._tx() as db:
            same = db.execute("SELECT * FROM transitions WHERE caller=? AND client_key=?",
                              (caller, client_key)).fetchone()
            if same is not None:
                if same["request_digest"] != digest:
                    raise AuthorityError("key_reused", "client key already used for a different request")
                return _transition_view(same)
            row, now = self._row(db), self._mono()
            if row["transition_id"]:
                raise AuthorityError("busy", "another transition is open", None)
            if (row["incarnation"], row["epoch"], row["policy_version"]) != (
                    expected_incarnation, expected_epoch, expected_policy):
                raise AuthorityError("stale", "authority moved on; refresh status")
            if row["mode"] not in ("V2_ACTIVE", "V2_ONLY_NO_MAIN"):
                raise AuthorityError("mode", f"no MAIN transitions in {row['mode']}")
            cand = self._participant(db, candidate)
            if cand is None or cand["kind"] != "pc" or not cand["eligible"]:
                raise AuthorityError("ineligible", "candidate is not an eligible enrolled PC")
            if cand["mode_version"] != row["mode_version"]:
                raise AuthorityError("ineligible", "candidate has not acknowledged the current mode")
            valid = self._lease_valid(row, now)
            if action == "recover":
                if valid:
                    raise AuthorityError("main_alive", "the current MAIN still holds a valid lease")
            else:
                if not valid:
                    raise AuthorityError("no_main", "no valid MAIN to hand over from; use recover")
                if candidate == row["holder"]:
                    raise AuthorityError("noop", "candidate already is MAIN")
            tid = "tr-" + secrets.token_hex(8)
            w = self._wall()
            db.execute("INSERT INTO transitions(transition_id, caller, client_key, request_digest, action, profile,"
                       " source, candidate, expected_incarnation, expected_epoch, expected_policy, state,"
                       " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (tid, caller, client_key, digest, action, profile, row["holder"] if valid else None,
                        candidate, expected_incarnation, expected_epoch, expected_policy, "REQUESTED", w, w))
            db.execute("UPDATE authority SET transition_id=?, updated_at=? WHERE fleet_id=?", (tid, w, self.fleet_id))
            return _transition_view(self._get(db, tid))

    def _get(self, db, tid):
        return db.execute("SELECT * FROM transitions WHERE transition_id=?", (_ident(tid, "transition_id"),)).fetchone()

    def _open(self, db, tid, states):
        t = self._get(db, tid)
        if t is None:
            raise AuthorityError("not_found", "no such transition")
        if t["state"] not in states:
            raise AuthorityError("bad_state", f"transition is {t['state']}")
        return t

    def readiness(self, transition_id, control_ready, ai_ready, checkpoint=None):
        """Candidate-reported readiness (booleans + opaque cursor id, never state contents)."""
        self._guard()
        if checkpoint is not None:
            _ident(checkpoint, "checkpoint")
        with self._tx() as db:
            self._open(db, transition_id, ("REQUESTED", "VALIDATING"))
            db.execute("UPDATE transitions SET state='VALIDATING', control_ready=?, ai_ready=?, checkpoint=?,"
                       " updated_at=? WHERE transition_id=?",
                       (int(bool(control_ready)), int(bool(ai_ready)), checkpoint, self._wall(), transition_id))
            return _transition_view(self._get(db, transition_id))

    def drain(self, transition_id):
        """Stop renewals and new admission for the current MAIN. Needs readiness first."""
        self._guard()
        with self._tx() as db:
            t = self._open(db, transition_id, ("REQUESTED", "VALIDATING"))
            if not t["control_ready"] or (t["profile"] == "ai" and not t["ai_ready"]):
                raise AuthorityError("not_ready", "candidate readiness does not meet the requested profile")
            self._drain_since = self._mono()
            db.execute("UPDATE authority SET mode=CASE WHEN mode='V2_ACTIVE' THEN 'HANDOVER' ELSE mode END,"
                       " updated_at=? WHERE fleet_id=?", (self._wall(), self.fleet_id))
            db.execute("UPDATE transitions SET state='DRAINING', updated_at=? WHERE transition_id=?",
                       (self._wall(), transition_id))
            return _transition_view(self._get(db, transition_id))

    def fence(self, transition_id):
        """Clear the old lease and advance the epoch once nothing older can still act: either
        every eligible participant and the previous MAIN acknowledged refusing the current
        epoch, or the old validity ran out plus the safety margin. Never before the
        new-incarnation barrier."""
        self._guard()
        with self._tx() as db:
            t = self._open(db, transition_id, ("DRAINING",))
            row, now = self._row(db), self._mono()
            prior = t["source"] or row["holder"] or row["last_holder"]
            acked = self._acked(db, row, (prior,))
            if self._lease_valid(row, now):
                if not acked:
                    raise AuthorityError("lease_live", "old lease still valid",
                                         self._lease["expires"] + self.margin - now)
                self._lease = None  # the old MAIN and every target confirmed they stopped
            wait = self._quiet(db, row, now, acked)
            if wait > 0:
                raise AuthorityError("not_quiet", "earlier grants may still be honoured", wait)
            self._lease = None
            db.execute("UPDATE authority SET epoch=epoch+1, last_holder=COALESCE(holder, last_holder), holder=NULL,"
                       " lease_id=NULL, lease_ready=0, mode='HANDOVER', updated_at=? WHERE fleet_id=?",
                       (self._wall(), self.fleet_id))
            row = self._row(db)
            db.execute("UPDATE transitions SET state='FENCING', fenced_epoch=?, updated_at=? WHERE transition_id=?",
                       (row["epoch"], self._wall(), transition_id))
            return _transition_view(self._get(db, transition_id))

    def grant(self, transition_id):
        """Give the candidate a not-ready lease under the fenced epoch: canary only, no admission."""
        self._guard()
        with self._tx() as db:
            t = self._open(db, transition_id, ("FENCING",))
            row, now = self._row(db), self._mono()
            if now < self._barrier_until:
                raise AuthorityError("not_quiet", "new incarnation barrier", self._barrier_until - now)
            lease_id = "ls-" + secrets.token_hex(8)
            db.execute("UPDATE authority SET holder=?, lease_id=?, lease_ready=0,"
                       " max_validity=MAX(max_validity, ?), updated_at=? WHERE fleet_id=?",
                       (t["candidate"], lease_id, self.lease_seconds, self._wall(), self.fleet_id))
            self._lease = {"lease_id": lease_id, "holder": t["candidate"], "expires": now + self.lease_seconds}
            self._validity_end = max(self._validity_end or 0.0, self._lease["expires"])
            self._drain_since = None  # the new holder renews normally; admission still waits for ready
            db.execute("UPDATE transitions SET state='GRANTED_NOT_READY', updated_at=? WHERE transition_id=?",
                       (self._wall(), transition_id))
            return {"holder": t["candidate"], "lease_id": lease_id, "incarnation": row["incarnation"],
                    "epoch": row["epoch"], "valid_for": self.lease_seconds, "ready": False}

    def complete(self, transition_id, canary_ok):
        """Verified canary activates admission; a failed one leaves no MAIN at a fresh epoch."""
        self._guard()
        with self._tx() as db:
            t = self._open(db, transition_id, ("GRANTED_NOT_READY",))
            row, now, w = self._row(db), self._mono(), self._wall()
            if canary_ok and self._lease_valid(row, now):
                pref = t["candidate"] if t["action"] == "set_preferred_main" else row["preferred"]
                db.execute("UPDATE authority SET lease_ready=1, mode='V2_ACTIVE', preferred=?, transition_id=NULL,"
                           " updated_at=? WHERE fleet_id=?", (pref, w, self.fleet_id))
                db.execute("UPDATE transitions SET state='SUCCEEDED', outcome='new_main', updated_at=?"
                           " WHERE transition_id=?", (w, transition_id))
            else:
                self._fail_after_fence(db, transition_id, "lease_lapsed" if canary_ok else "canary_failed")
            return _transition_view(self._get(db, transition_id))

    def cancel(self, transition_id):
        """Before the fence: old MAIN keeps (or resumes renewing) its lease. After: no MAIN."""
        self._guard()
        with self._tx() as db:
            t = self._open(db, transition_id, OPEN_STATES)
            w = self._wall()
            row = self._row(db)
            fenced_by_acks = t["state"] == "DRAINING" and db.execute(
                "SELECT COUNT(*) FROM participants WHERE fenced_epoch>=?", (row["epoch"],)).fetchone()[0]
            if t["state"] in FENCED_STATES or fenced_by_acks:
                # Targets already refuse this epoch: keeping the old MAIN would be a MAIN nobody obeys.
                self._fail_after_fence(db, transition_id,
                                       "cancelled_after_fence" if t["state"] in FENCED_STATES else "cancelled_after_acks")
            else:
                self._drain_since = None
                db.execute("UPDATE authority SET transition_id=NULL, mode=CASE WHEN mode='HANDOVER' THEN"
                           " 'V2_ACTIVE' ELSE mode END, updated_at=? WHERE fleet_id=?", (w, self.fleet_id))
                db.execute("UPDATE transitions SET state='CANCELLED', outcome='old_main_kept', updated_at=?"
                           " WHERE transition_id=?", (w, transition_id))
                # A drained MAIN whose lease lapsed meanwhile is simply gone; mode must say so.
                row = self._row(db)
                if row["holder"] and not self._lease_valid(row, self._mono()):
                    db.execute("UPDATE authority SET mode='V2_ONLY_NO_MAIN' WHERE fleet_id=?", (self.fleet_id,))
            return _transition_view(self._get(db, transition_id))

    def _fail_after_fence(self, db, transition_id, outcome):
        self._lease = None
        self._drain_since = None
        w = self._wall()
        db.execute("UPDATE authority SET holder=NULL, lease_id=NULL, lease_ready=0, epoch=epoch+1,"
                   " mode='V2_ONLY_NO_MAIN', transition_id=NULL, updated_at=? WHERE fleet_id=?", (w, self.fleet_id))
        db.execute("UPDATE transitions SET state='FAILED', outcome=?, updated_at=? WHERE transition_id=?",
                   (outcome, w, transition_id))

    # --- admission ----------------------------------------------------------------------

    def consume(self, *, admission_id, target, operation_id, attempt_id, digest, main, incarnation, epoch,
                issued_at, admit_until, issuer_incarnation):
        """Consume one new-operation admission under the exact current, ready MAIN tuple.
        Same admission again -> the original record (`already`: True), even after handover."""
        self._guard()
        for v, n in ((admission_id, "admission_id"), (target, "target"), (operation_id, "operation_id"),
                     (attempt_id, "attempt_id"), (main, "main")):
            _ident(v, n)
        _digest(digest)
        _int(incarnation, "incarnation"), _int(epoch, "epoch"), _int(issuer_incarnation, "issuer_incarnation")
        issued_at, admit_until = _num(issued_at, "issued_at"), _num(admit_until, "admit_until")
        with self._tx() as db:
            now_w, now_m = self._wall(), self._mono()
            # Expired or malformed tickets are refused before any lookup (design: afterGC).
            if admit_until <= issued_at or admit_until - issued_at > MAX_TICKET_SECONDS:
                raise AuthorityError("bad_ticket", "ticket lifetime invalid")
            if now_w >= admit_until:
                raise AuthorityError("ticket_expired", "ticket admission window is over")
            if issued_at > now_w + TICKET_SKEW:
                raise AuthorityError("bad_ticket", "ticket issued in the future")
            mine = db.execute("SELECT * FROM admissions WHERE admission_id=?", (admission_id,)).fetchone()
            same_attempt = db.execute("SELECT * FROM admissions WHERE target=? AND operation_id=? AND attempt_id=?",
                                      (target, operation_id, attempt_id)).fetchone()
            for prior in (mine, same_attempt):
                if prior is None:
                    continue
                fields = (prior["admission_id"], prior["target"], prior["operation_id"], prior["attempt_id"],
                          prior["digest"], prior["issued_at"], prior["admit_until"], prior["issuer_incarnation"])
                if fields != (admission_id, target, operation_id, attempt_id, digest, issued_at, admit_until,
                              issuer_incarnation):
                    raise AuthorityError("conflict", "this attempt was already admitted differently")
                return dict(prior, already=True)
            row = self._row(db)
            if row["mode"] != "V2_ACTIVE" or row["transition_id"] or self._drain_since is not None:
                raise AuthorityError("no_admission", f"no new work in {row['mode']}")
            if issuer_incarnation != row["incarnation"]:
                raise AuthorityError("stale", "ticket from an earlier authority incarnation")
            if row["holder"] != main or row["incarnation"] != incarnation or row["epoch"] != epoch:
                raise AuthorityError("stale", "not the current MAIN tuple")
            if not self._lease_valid(row, now_m) or not row["lease_ready"]:
                raise AuthorityError("no_admission", "MAIN lease not valid and ready")
            tp = self._participant(db, target)
            if tp is None or tp["mode_version"] != row["mode_version"]:
                raise AuthorityError("target_unfenced", "target has not acknowledged the current mode")
            if tp["fenced_epoch"] >= row["epoch"]:
                raise AuthorityError("stale", "target already fenced this epoch")
            db.execute("INSERT INTO admissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (admission_id, target, operation_id, attempt_id, digest, main, incarnation, epoch,
                        issued_at, admit_until, issuer_incarnation, now_w))
            rec = db.execute("SELECT * FROM admissions WHERE admission_id=?", (admission_id,)).fetchone()
            return dict(rec, already=False)

    def prune(self, keep_seconds=DEDUP_SECONDS):
        """Drop admission records whose ticket window closed more than `keep_seconds` ago."""
        with self._tx() as db:
            cur = db.execute("DELETE FROM admissions WHERE admit_until < ?", (self._wall() - keep_seconds,))
            return cur.rowcount


def _transition_view(t):
    if t is None:
        return None
    return {k: t[k] for k in ("transition_id", "action", "profile", "source", "candidate", "state",
                              "expected_incarnation", "expected_epoch", "fenced_epoch", "outcome",
                              "control_ready", "ai_ready", "checkpoint")}
