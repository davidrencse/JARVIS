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
  effect-bearing participant acknowledged the fence (and at least one eligible
  participant exists), or the old validity ran out (+margin). An ineligible ack is
  not a quorum and cannot drop a live lease. A draining source may release its own
  lease without closing the transition or dropping that validity margin. Any other
  open stage still refuses release. Cutover (`set_mode`) refuses while a transition
  is open, and fence refuses a mode that is not part of the transfer. A candidate
  is granted not-ready; a repeat of that grant returns the same lease and does not
  extend it. Ordinary admission waits for a verified canary. A failure after the
  fence leaves no MAIN (never resurrects the old lease).
- A transition is compare-and-set on the incarnation, epoch, and policy it requested.
  Fence rechecks that tuple before it advances the epoch. Grant and complete recheck
  the fenced epoch. A moved tuple is `stale` and writes nothing.
- Admission is consumed once per (target, operation, attempt) under the exact current
  tuple. A retry of the same admission returns the original record, also after a later
  handover; a different digest or admission id for that attempt is a conflict.
- Metadata only: ids, digests, numbers, booleans. No prompt, body, payload or result is
  accepted or stored (identifier shapes are validated; there is no free-text column).
- Revocation is durable and metadata-only. disable_participant sets eligible false for
  one installation and keeps the participant row and its fence highwater. It does not
  drop a lease, an admission, or a transition. The current holder and any open
  transition source or candidate are refused. Replaying register_participant on that
  same installation does not clear the revocation.

Not here: transport, caller authentication, passkey verification, signatures, PC receipt
journals, executor guards. Callers pass identities they already authenticated.
"""

from ..locking import transaction
import errno
import hashlib
import json
import math
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


def _finite_non_negative(value, name):
    """Reject bools, non-numbers, NaN, infinities, and negatives before any write."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuthorityError("bad_request", f"{name} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise AuthorityError("bad_request", f"{name} must be a finite non-negative number")
    return number


def _bool(value, name):
    if not isinstance(value, bool):
        raise AuthorityError("bad_request", f"{name} must be true or false")
    return value


def _writer_lock(path):
    """Exclusive, nonblocking lock held for the store's lifetime: one writer per file.

    The lock file is `<realpath(database)>.writer`. A symlink and its target therefore
    contend for one lock, instead of each creating a lock beside its own path. The
    lock path itself must be a regular file; following a symlink there would lock
    some other inode. An empty file gets one byte so a Windows byte-range lock has
    something to cover.
    """
    lock_path = os.path.realpath(path) + ".writer"
    if os.path.islink(lock_path):
        raise AuthorityError("bad_request", "writer lock must be a regular file")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EISDIR):
            raise AuthorityError("bad_request", "writer lock must be a regular file") from None
        raise AuthorityError("writer_busy", "another authority process owns this store") from None
    handle = os.fdopen(fd, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
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
        if not 0 < self.lease_seconds <= 300:
            raise AuthorityError("bad_request", "lease_seconds out of range")
        # Before the writer lock and the database, so a bad margin cannot hold the file.
        self.margin = _finite_non_negative(margin, "margin")
        self._lock = threading.RLock()
        self._writer = _writer_lock(self.path)
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
            if row["mode"] == "QUARANTINED":
                # Stored quarantine is a latch. The new-incarnation path must not
                # clear it, bump the epoch, or rewrite membership evidence.
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
        return transaction(self._db, self._lock)

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

    def _same_tuple(self, row, incarnation, epoch, policy):
        """Refuse before a write when this is not the tuple the caller bound."""
        if (row["incarnation"], row["epoch"], row["policy_version"]) != (incarnation, epoch, policy):
            raise AuthorityError("stale", "authority moved on; refresh status")

    def _expect_tuple(self, row, transition, epoch):
        """Bind a step to the transition's incarnation and policy.

        ``epoch`` is ``expected_epoch`` until fence commits, and ``fenced_epoch`` after.
        """
        self._same_tuple(row, transition["expected_incarnation"], epoch, transition["expected_policy"])

    def _require_transfer_mode(self, row, allowed):
        if row["mode"] not in allowed:
            raise AuthorityError("mode", f"no MAIN transitions in {row['mode']}")

    def _fence_quorum(self, db, row, prior):
        """Shared by fence and cancel.

        Every eligible participant, plus the previous holder when one is named, must
        have acked this epoch. No eligible participant means there is no quorum:
        an ineligible row with a high ``fenced_epoch`` must not retire a live MAIN.
        """
        if db.execute("SELECT COUNT(*) FROM participants WHERE eligible=1").fetchone()[0] < 1:
            return False
        return self._acked(db, row, (prior,))

    # --- read ---------------------------------------------------------------------------

    def status(self):
        """RoleStatus: safe to show on any authenticated surface (no credentials, no bodies)."""
        with self._lock:
            row = self._row(self._db)
            if row is None:
                raise AuthorityError("quarantined",
                                     "authority state is unrecognized; restore by an operator")
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

    def transition_caller(self, transition_id):
        """Who opened a transition. The gateway uses it to keep self-recovery
        steps to transitions a device opened for itself."""
        with self._lock:
            t = self._db.execute("SELECT caller FROM transitions WHERE transition_id=?",
                                 (_ident(transition_id, "transition_id"),)).fetchone()
            if t is None:
                raise AuthorityError("not_found", "no such transition")
            return t["caller"]

    def admission(self, admission_id):
        """Reconciliation lookup. Returns the consumed record or None; never consumes.

        Historical row only. It does not check the current MAIN tuple, mode, readiness,
        or whether that main or target is still eligible. Callers that need effect
        permission use ``consume``, which does.
        """
        with self._lock:
            r = self._db.execute("SELECT * FROM admissions WHERE admission_id=?",
                                 (_ident(admission_id, "admission_id"),)).fetchone()
            return dict(r) if r else None

    # --- participants and cutover -----------------------------------------------------

    def register_participant(self, device_id, installation_id, kind, eligible=True, *, reenable=False):
        """Enroll a device, or refresh an enrollment that is already stored.

        Servers are never MAIN candidates. WS5 gateway enroll calls
        ``register_participant(device_id, installation_id, kind, True)`` and does not
        pass ``reenable``. That replay must not undo ``disable_participant``.

        - No row yet: insert this installation with the requested eligibility.
        - New installation id: the owner enrolled a different installation. Reset
          ``mode_version`` and ``fenced_epoch`` (the machine must re-fence) and store
          the requested eligibility. Refused, with no write, when this device is the
          current holder or an open transition source or candidate.
        - Same installation: update ``kind`` only. A different kind is a membership
          change and is refused, with no write, for the current holder or an open
          transition source or candidate. The same kind is idempotent and is allowed
          for those members. ``eligible=True`` does not make a revoked installation
          eligible again, and ``eligible=False`` does not revoke.
          Pass ``reenable=True`` together with ``eligible=True`` to mark that same
          installation eligible again without moving the fence highwater. ``reenable``
          is refused for the current holder and for an open transition endpoint.
          ``eligible`` must be a boolean. A non-boolean is ``bad_request`` and
          writes nothing.
        """
        self._guard()
        _ident(device_id, "device_id"), _ident(installation_id, "installation_id")
        if kind not in ("pc", "server"):
            raise AuthorityError("bad_request", "kind must be pc or server")
        if not isinstance(reenable, bool):
            raise AuthorityError("bad_request", "reenable must be true or false")
        if not isinstance(eligible, bool):
            raise AuthorityError("bad_request", "eligible must be true or false")
        if reenable and not eligible:
            raise AuthorityError("bad_request", "reenable requires eligible=True")
        with self._tx() as db:
            if reenable:
                old = self._participant(db, device_id)
                if old is not None and old["installation_id"] == installation_id:
                    self._refuse_live_member(db, device_id)
                    db.execute("UPDATE participants SET kind=?, eligible=1 WHERE device_id=? AND installation_id=?",
                               (kind, device_id, installation_id))
                    return
            self.apply_participant_registration(db, device_id, installation_id, kind, eligible)

    def apply_participant_registration(self, db, device_id, installation_id, kind, eligible=True):
        """Participant write on the caller's already-open transaction.

        Does not BEGIN, COMMIT, or reenable. `register_participant` runs this
        inside its own transaction. Gateway enrollment runs it on the same
        transaction as the directory row. Calling `register_participant` from
        that transaction would commit the participant first.
        """
        _ident(device_id, "device_id")
        _ident(installation_id, "installation_id")
        if kind not in ("pc", "server"):
            raise AuthorityError("bad_request", "kind must be pc or server")
        _bool(eligible, "eligible")
        self._guard()
        want = 1 if eligible else 0
        old = self._participant(db, device_id)
        if old is None:
            self._refuse_live_member(db, device_id)
            db.execute("INSERT INTO participants(device_id, installation_id, kind, eligible)"
                       " VALUES(?,?,?,?)", (device_id, installation_id, kind, want))
            return
        if old["installation_id"] != installation_id:
            self._refuse_live_member(db, device_id)
            db.execute("INSERT OR REPLACE INTO participants(device_id, installation_id, kind, eligible)"
                       " VALUES(?,?,?,?)", (device_id, installation_id, kind, want))
            return
        if old["kind"] != kind:
            self._refuse_live_member(db, device_id)
        db.execute("UPDATE participants SET kind=? WHERE device_id=? AND installation_id=?",
                   (kind, device_id, installation_id))

    def disable_participant(self, device_id, installation_id):
        """Durably revoke one enrolled installation. Idempotent for that pair.

        Compare-and-swap on ``installation_id``. A different installation raises
        ``installation_mismatch`` and writes nothing. An unknown device raises
        ``unknown_participant``. The current MAIN holder (authority holder or the
        live lease holder) raises ``active_holder``. An open transition source or
        candidate raises ``transition_member``. Those refusals write nothing.

        Success keeps the participant row and its ``mode_version`` and
        ``fenced_epoch`` so a later enrollment still sees the fence highwater, and
        sets ``eligible`` false. The lease, epoch, admissions, and transition rows
        are left as they are. A second call with the same installation returns the
        same metadata with ``already`` true.

        Returned keys: ``device_id``, ``installation_id``, ``kind``, ``eligible``
        (false), ``mode_version``, ``fenced_epoch``, ``disabled`` (true), ``already``.
        """
        self._guard()
        _ident(device_id, "device_id"), _ident(installation_id, "installation_id")
        with self._tx() as db:
            current = self._participant(db, device_id)
            if current is None:
                raise AuthorityError("unknown_participant", "device is not enrolled")
            if current["installation_id"] != installation_id:
                raise AuthorityError("installation_mismatch",
                                     "installation does not match the enrolled participant")
            self._refuse_live_member(db, device_id)
            already = not current["eligible"]
            if not already:
                updated = db.execute("UPDATE participants SET eligible=0"
                                     " WHERE device_id=? AND installation_id=?",
                                     (device_id, installation_id)).rowcount
                if updated != 1:
                    raise AuthorityError("bad_state", "participant installation changed")
                current = self._participant(db, device_id)
            return _disabled_view(current, already)

    def _refuse_live_member(self, db, device_id):
        """The current holder and open transition endpoints cannot change membership."""
        row = self._row(db)
        lease_holder = self._lease["holder"] if self._lease else None
        if device_id == row["holder"] or device_id == lease_holder:
            raise AuthorityError("active_holder", "cannot change membership of the current MAIN holder")
        hit = db.execute("SELECT 1 FROM transitions WHERE state IN (%s) AND (source=? OR candidate=?)"
                         % ",".join("?" * len(OPEN_STATES)),
                         (*OPEN_STATES, device_id, device_id)).fetchone()
        if hit is not None:
            raise AuthorityError("transition_member",
                                 "cannot change membership of an open transition source or candidate")

    def _refuse_disabled(self, db, device_id, message):
        """Revoke when this device's current row is ineligible. A missing row is not revoked."""
        participant = self._participant(db, device_id)
        if participant is not None and not participant["eligible"]:
            raise AuthorityError("revoked", message)

    def acknowledge(self, device_id, installation_id, mode_version, fenced_epoch, boot_id=None):
        """A participant durably installed `mode_version` and refuses every grant whose epoch is
        <= `fenced_epoch` (it acks the epoch being fenced). Acks only move forward; a foreign
        installation or a version/epoch the authority never issued is refused. A disabled
        installation is refused with ``revoked`` and is not recorded.

        The next mode version may be acked only while the fleet is already in QUIESCED or
        ROLLBACK_QUIESCED, which is the cutover waiting for that ack. From any other mode,
        a version ahead of the one this authority has issued is ``ahead`` and is not stored.
        """
        self._guard()
        _int(mode_version, "mode_version"), _int(fenced_epoch, "fenced_epoch")
        if boot_id is not None:
            _ident(boot_id, "boot_id")
        with self._tx() as db:
            device_id = _ident(device_id, "device_id")
            p = self._participant(db, device_id)
            if p is None or p["installation_id"] != installation_id:
                raise AuthorityError("unknown_participant", "device/installation not enrolled")
            if not p["eligible"]:
                raise AuthorityError("revoked", "disabled installation cannot acknowledge")
            row = self._row(db)
            ceiling = row["mode_version"] + (1 if row["mode"] in ("QUIESCED", "ROLLBACK_QUIESCED") else 0)
            if mode_version > ceiling or fenced_epoch > row["epoch"]:
                raise AuthorityError("ahead", "acknowledgement names a version the authority never issued")
            db.execute("UPDATE participants SET mode_version=MAX(mode_version, ?), fenced_epoch=MAX(fenced_epoch, ?),"
                       " boot_id=COALESCE(?, boot_id), acked_at=? WHERE device_id=? AND installation_id=?",
                       (mode_version, fenced_epoch, boot_id, self._wall(), device_id, installation_id))

    def set_mode(self, mode, expected_mode_version):
        """Cutover step (MAIN-HANDOFF §5). Entering V2_ONLY_NO_MAIN or LEGACY_ACTIVE from a
        quiesced mode needs every eligible participant to have acked the new version first.

        An open transition owns the mode until it finishes. ``set_mode`` then raises
        ``busy`` and leaves the row unchanged, so rollback cannot overwrite a handover
        that is about to grant a MAIN.
        """
        self._guard()
        _int(expected_mode_version, "expected_mode_version")
        with self._tx() as db:
            row = self._row(db)
            if row["mode_version"] != expected_mode_version:
                raise AuthorityError("stale", "mode version changed")
            if row["transition_id"]:
                raise AuthorityError("busy", "a transition is open")
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
            updated = db.execute("UPDATE authority SET mode=?, mode_version=?, updated_at=? "
                                 "WHERE fleet_id=? AND mode_version=? AND transition_id IS NULL",
                                 (mode, new_version, self._wall(), self.fleet_id, expected_mode_version)).rowcount
            if updated != 1:
                raise AuthorityError("busy", "a transition is open")
            return new_version

    # --- lease --------------------------------------------------------------------------

    def renew(self, holder, lease_id, incarnation, epoch):
        """MAIN heartbeat: compare-and-set on the full tuple. An expired lease is never
        revived; a fenced or draining one is refused. Returns seconds of validity granted
        from the authority's receipt of this call (caller anchors at its request start)."""
        self._guard()
        incarnation, epoch = _int(incarnation, "incarnation"), _int(epoch, "epoch")
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
        validity it already handed out still has to drain or be acked before a successor.

        An open transition still refuses, except the matching source while that transition
        is DRAINING. That release drops only the live lease. The transition, the mode, and
        the old validity margin stay so the fence cannot succeed early.
        """
        self._guard()
        incarnation, epoch = _int(incarnation, "incarnation"), _int(epoch, "epoch")
        with self._tx() as db:
            row = self._row(db)
            self._match(row, holder, lease_id, incarnation, epoch)
            if row["transition_id"]:
                current = self._get(db, row["transition_id"])
                if current is None or current["state"] != "DRAINING" or current["source"] != holder:
                    raise AuthorityError("busy", "a transition is open; let it fence")
                db.execute("UPDATE authority SET holder=NULL, lease_id=NULL, lease_ready=0, last_holder=?,"
                           " updated_at=? WHERE fleet_id=?",
                           (holder, self._wall(), self.fleet_id))
                self._lease = None
                return
            db.execute("UPDATE authority SET holder=NULL, lease_id=NULL, lease_ready=0, last_holder=?,"
                       " mode=?, updated_at=? WHERE fleet_id=?",
                       (holder, "V2_ONLY_NO_MAIN", self._wall(), self.fleet_id))
            self._lease = None

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
            self._same_tuple(row, expected_incarnation, expected_epoch, expected_policy)
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
            updated = db.execute("UPDATE authority SET transition_id=?, updated_at=? "
                                 "WHERE fleet_id=? AND transition_id IS NULL", (tid, w, self.fleet_id)).rowcount
            if updated != 1:
                raise AuthorityError("busy", "another transition is open")
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
        control_ready, ai_ready = _bool(control_ready, "control_ready"), _bool(ai_ready, "ai_ready")
        if checkpoint is not None:
            _ident(checkpoint, "checkpoint")
        with self._tx() as db:
            t = self._open(db, transition_id, ("REQUESTED", "VALIDATING"))
            self._expect_tuple(self._row(db), t, t["expected_epoch"])
            db.execute("UPDATE transitions SET state='VALIDATING', control_ready=?, ai_ready=?, checkpoint=?,"
                       " updated_at=? WHERE transition_id=?",
                       (1 if control_ready else 0, 1 if ai_ready else 0, checkpoint, self._wall(), transition_id))
            return _transition_view(self._get(db, transition_id))

    def drain(self, transition_id):
        """Stop renewals and new admission for the current MAIN. Needs readiness first."""
        self._guard()
        with self._tx() as db:
            t = self._open(db, transition_id, ("REQUESTED", "VALIDATING"))
            row = self._row(db)
            self._expect_tuple(row, t, t["expected_epoch"])
            self._require_transfer_mode(row, ("V2_ACTIVE", "V2_ONLY_NO_MAIN", "HANDOVER"))
            if not t["control_ready"] or (t["profile"] == "ai" and not t["ai_ready"]):
                raise AuthorityError("not_ready", "candidate readiness does not meet the requested profile")
            w = self._wall()
            db.execute("UPDATE authority SET mode=CASE WHEN mode='V2_ACTIVE' THEN 'HANDOVER' ELSE mode END,"
                       " updated_at=? WHERE fleet_id=?", (w, self.fleet_id))
            db.execute("UPDATE transitions SET state='DRAINING', updated_at=? WHERE transition_id=?",
                       (w, transition_id))
            self._drain_since = self._mono()
            return _transition_view(self._get(db, transition_id))

    def fence(self, transition_id):
        """Clear the old lease and advance the epoch once nothing older can still act.

        The fast path is the same quorum ``cancel`` uses: every eligible participant
        and the previous MAIN have acked this epoch, and at least one eligible
        participant exists. Otherwise the old validity must run out, plus the safety
        margin. Never before the new-incarnation barrier. A retry once the transition
        is already FENCING returns that result and does not advance the epoch again.
        """
        self._guard()
        with self._tx() as db:
            t = self._get(db, transition_id)
            if t is None:
                raise AuthorityError("not_found", "no such transition")
            row, now = self._row(db), self._mono()
            if t["state"] == "FENCING":
                self._expect_tuple(row, t, t["fenced_epoch"])
                self._require_transfer_mode(row, ("HANDOVER",))
                if row["holder"] is not None or row["lease_id"] is not None:
                    raise AuthorityError("bad_state", "fence left a lease behind")
                return _transition_view(t)
            if t["state"] != "DRAINING":
                raise AuthorityError("bad_state", f"transition is {t['state']}")
            self._expect_tuple(row, t, t["expected_epoch"])
            self._require_transfer_mode(row, ("V2_ACTIVE", "V2_ONLY_NO_MAIN", "HANDOVER"))
            prior = t["source"] or row["holder"] or row["last_holder"]
            quorum = self._fence_quorum(db, row, prior)
            if self._lease_valid(row, now) and not quorum:
                raise AuthorityError("lease_live", "old lease still valid",
                                     self._lease["expires"] + self.margin - now)
            wait = self._quiet(db, row, now, quorum)
            if wait > 0:
                raise AuthorityError("not_quiet", "earlier grants may still be honoured", wait)
            w = self._wall()
            updated = db.execute(
                "UPDATE authority SET epoch=epoch+1, last_holder=COALESCE(holder, last_holder), holder=NULL,"
                " lease_id=NULL, lease_ready=0, mode='HANDOVER', updated_at=? "
                "WHERE fleet_id=? AND incarnation=? AND epoch=? AND policy_version=?",
                (w, self.fleet_id, row["incarnation"], row["epoch"], row["policy_version"])).rowcount
            if updated != 1:
                raise AuthorityError("stale", "authority moved on; refresh status")
            row = self._row(db)
            db.execute("UPDATE transitions SET state='FENCING', fenced_epoch=?, updated_at=? WHERE transition_id=?",
                       (row["epoch"], w, transition_id))
            self._lease = None
            return _transition_view(self._get(db, transition_id))

    def _lease_view(self, row, now, valid_for):
        return {"holder": self._lease["holder"], "lease_id": self._lease["lease_id"],
                "incarnation": row["incarnation"], "epoch": row["epoch"],
                "valid_for": valid_for, "ready": False}

    def grant(self, transition_id):
        """Give the candidate a not-ready lease under the fenced epoch: canary only, no admission.

        A repeat while the transition is still GRANTED_NOT_READY returns that same lease
        and the time it still has left. It does not mint a second id or extend the expiry.
        The candidate must still be an eligible PC on the first grant.
        """
        self._guard()
        with self._tx() as db:
            t = self._get(db, transition_id)
            if t is None:
                raise AuthorityError("not_found", "no such transition")
            row, now = self._row(db), self._mono()
            if t["state"] == "GRANTED_NOT_READY":
                self._expect_tuple(row, t, t["fenced_epoch"])
                self._require_transfer_mode(row, ("HANDOVER",))
                if row["holder"] != t["candidate"] or not row["lease_id"]:
                    raise AuthorityError("bad_state", "grant does not match this transition")
                if not self._lease_valid(row, now):
                    raise AuthorityError("expired", "lease lapsed; only a recovery transition can grant again")
                return self._lease_view(row, now, self._lease["expires"] - now)
            if t["state"] != "FENCING":
                raise AuthorityError("bad_state", f"transition is {t['state']}")
            self._expect_tuple(row, t, t["fenced_epoch"])
            self._require_transfer_mode(row, ("HANDOVER",))
            if now < self._barrier_until:
                raise AuthorityError("not_quiet", "new incarnation barrier", self._barrier_until - now)
            cand = self._participant(db, t["candidate"])
            if cand is None or cand["kind"] != "pc" or not cand["eligible"]:
                raise AuthorityError("ineligible", "candidate is not an eligible enrolled PC")
            lease_id = "ls-" + secrets.token_hex(8)
            w = self._wall()
            updated = db.execute(
                "UPDATE authority SET holder=?, lease_id=?, lease_ready=0,"
                " max_validity=MAX(max_validity, ?), updated_at=? "
                "WHERE fleet_id=? AND holder IS NULL AND lease_id IS NULL AND incarnation=? AND epoch=?",
                (t["candidate"], lease_id, self.lease_seconds, w, self.fleet_id,
                 row["incarnation"], row["epoch"])).rowcount
            if updated != 1:
                raise AuthorityError("bad_state", "fence does not match the authority tuple")
            db.execute("UPDATE transitions SET state='GRANTED_NOT_READY', updated_at=? WHERE transition_id=?",
                       (w, transition_id))
            self._lease = {"lease_id": lease_id, "holder": t["candidate"], "expires": now + self.lease_seconds}
            self._validity_end = max(self._validity_end or 0.0, self._lease["expires"])
            self._drain_since = None  # the new holder renews normally; admission still waits for ready
            return self._lease_view(row, now, self.lease_seconds)

    def complete(self, transition_id, canary_ok):
        """Verified canary activates admission; a failed one leaves no MAIN at a fresh epoch.

        A tuple that no longer matches the fenced grant is ``stale`` and is not activated
        and not failed. Failing it would advance the epoch again on a record this step
        does not own.
        """
        self._guard()
        _bool(canary_ok, "canary_ok")
        with self._tx() as db:
            t = self._open(db, transition_id, ("GRANTED_NOT_READY",))
            row, now, w = self._row(db), self._mono(), self._wall()
            self._expect_tuple(row, t, t["fenced_epoch"])
            self._require_transfer_mode(row, ("HANDOVER",))
            if row["holder"] != t["candidate"] or not row["lease_id"]:
                raise AuthorityError("bad_state", "grant does not match this transition")
            if canary_ok and self._lease_valid(row, now):
                pref = t["candidate"] if t["action"] == "set_preferred_main" else row["preferred"]
                updated = db.execute(
                    "UPDATE authority SET lease_ready=1, mode='V2_ACTIVE', preferred=?, transition_id=NULL,"
                    " updated_at=? WHERE fleet_id=? AND incarnation=? AND epoch=? AND holder=?",
                    (pref, w, self.fleet_id, row["incarnation"], row["epoch"], t["candidate"])).rowcount
                if updated != 1:
                    raise AuthorityError("stale", "authority moved on; refresh status")
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
            # DRAINING is fenced-by-acks only on fence()'s quorum. A partial set leaves
            # the old MAIN in place; targets that already acked refuse its tickets in
            # consume ("target already fenced this epoch"). Ineligible acks do not count.
            # FENCING and GRANTED_NOT_READY still fail unconditionally: fence() already
            # advanced the epoch and cleared the old lease, so there is nothing to restore.
            fenced_by_acks = False
            if t["state"] == "DRAINING":
                prior = t["source"] or row["holder"] or row["last_holder"]
                fenced_by_acks = self._fence_quorum(db, row, prior)
            if t["state"] in FENCED_STATES or fenced_by_acks:
                # Full quorum, or fence() already ran: keeping the old MAIN would
                # be a MAIN nobody obeys.
                self._fail_after_fence(db, transition_id,
                                       "cancelled_after_fence" if t["state"] in FENCED_STATES else "cancelled_after_acks")
            elif t["state"] == "DRAINING" and row["holder"] is None:
                # Source released during drain. There is no lease to restore, and reporting
                # V2_ACTIVE would claim a MAIN the store no longer has.
                self._fail_after_fence(db, transition_id, "cancelled_after_release")
            else:
                db.execute("UPDATE authority SET transition_id=NULL, mode=CASE WHEN mode='HANDOVER' THEN"
                           " 'V2_ACTIVE' ELSE mode END, updated_at=? WHERE fleet_id=?", (w, self.fleet_id))
                db.execute("UPDATE transitions SET state='CANCELLED', outcome='old_main_kept', updated_at=?"
                           " WHERE transition_id=?", (w, transition_id))
                # A drained MAIN whose lease lapsed meanwhile is simply gone; mode must say so.
                row = self._row(db)
                if row["holder"] and not self._lease_valid(row, self._mono()):
                    db.execute("UPDATE authority SET mode='V2_ONLY_NO_MAIN' WHERE fleet_id=?", (self.fleet_id,))
                self._drain_since = None
            return _transition_view(self._get(db, transition_id))

    def _fail_after_fence(self, db, transition_id, outcome):
        w = self._wall()
        db.execute("UPDATE authority SET holder=NULL, lease_id=NULL, lease_ready=0, epoch=epoch+1,"
                   " mode='V2_ONLY_NO_MAIN', transition_id=NULL, updated_at=? WHERE fleet_id=?", (w, self.fleet_id))
        db.execute("UPDATE transitions SET state='FAILED', outcome=?, updated_at=? WHERE transition_id=?",
                   (outcome, w, transition_id))
        self._lease = None
        self._drain_since = None

    # --- admission ----------------------------------------------------------------------

    def consume(self, *, admission_id, target, operation_id, attempt_id, digest, main, incarnation, epoch,
                issued_at, admit_until, issuer_incarnation):
        """Consume one new-operation admission under the exact current, ready MAIN tuple.

        An identical ticket, including ``main``, ``incarnation``, and ``epoch``, returns
        the original record (``already`` true) even after handover. That replay is
        reconciliation, not permission to run the effect. Any changed ticket field,
        including those three, is ``conflict``. Before the identical record is returned,
        the named main and target are checked against current enrollment; a disabled
        installation is ``revoked`` and the stored row is left in place. ``admission``
        stays a historical read and does not apply that check. A disabled installation
        named as main or target on a new consume is ``revoked`` and is not stored."""
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
            presented = (admission_id, target, operation_id, attempt_id, digest, main, incarnation, epoch,
                         issued_at, admit_until, issuer_incarnation)
            for prior in (mine, same_attempt):
                if prior is None:
                    continue
                fields = (prior["admission_id"], prior["target"], prior["operation_id"], prior["attempt_id"],
                          prior["digest"], prior["main"], prior["incarnation"], prior["epoch"],
                          prior["issued_at"], prior["admit_until"], prior["issuer_incarnation"])
                if fields != presented:
                    raise AuthorityError("conflict", "this attempt was already admitted differently")
                # Current enrollment, not the historical row. Identical replay stays readable
                # through admission(); a revoked main or target cannot reuse this receipt.
                self._refuse_disabled(db, main, "disabled installation cannot be used as MAIN")
                self._refuse_disabled(db, target, "disabled participant cannot be an admission target")
                return dict(prior, already=True)
            row = self._row(db)
            if row["mode"] != "V2_ACTIVE" or row["transition_id"] or self._drain_since is not None:
                raise AuthorityError("no_admission", f"no new work in {row['mode']}")
            if issuer_incarnation != row["incarnation"]:
                raise AuthorityError("stale", "ticket from an earlier authority incarnation")
            self._refuse_disabled(db, main, "disabled installation cannot be used as MAIN")
            if row["holder"] != main or row["incarnation"] != incarnation or row["epoch"] != epoch:
                raise AuthorityError("stale", "not the current MAIN tuple")
            if not self._lease_valid(row, now_m) or not row["lease_ready"]:
                raise AuthorityError("no_admission", "MAIN lease not valid and ready")
            self._refuse_disabled(db, target, "disabled participant cannot be an admission target")
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
        """Drop admission records whose ticket window closed more than `keep_seconds` ago.

        Refuses while quarantined. A non-finite or negative window refuses before
        any delete. The gateway does not call this.
        """
        self._guard()
        keep = _finite_non_negative(keep_seconds, "keep_seconds")
        with self._tx() as db:
            cur = db.execute("DELETE FROM admissions WHERE admit_until < ?", (self._wall() - keep,))
            return cur.rowcount


def _disabled_view(row, already):
    return {"device_id": row["device_id"], "installation_id": row["installation_id"], "kind": row["kind"],
            "eligible": False, "mode_version": row["mode_version"], "fenced_epoch": row["fenced_epoch"],
            "disabled": True, "already": bool(already)}


def _transition_view(t):
    if t is None:
        return None
    return {k: t[k] for k in ("transition_id", "action", "profile", "source", "candidate", "state",
                              "expected_incarnation", "expected_epoch", "fenced_epoch", "outcome",
                              "control_ready", "ai_ready", "checkpoint")}
