"""Durable node credentials and at-most-once dispatch on the main PC."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .topology import DEVICE_ID

CAPABILITIES = {"desktop", "inventory", "grokbot", "files", "ask", "console"}
# What a new node gets unless more is asked for: work the main sends it. "ask" and
# "console" let a node drive the main itself, so they are opt-in per node.
NODE_DEFAULT = frozenset({"desktop", "inventory", "grokbot", "files"})
TERMINAL = {"done", "failed", "expired", "uncertain"}
# Job kinds that carry file chunks (transfer.CHUNK raw bytes, base64 in JSON). They may be
# larger than control messages. Their payload and result live only in the Broker's memory
# (the jobs row keeps state and deadline, with '{}' as the body), so a 2 GB transfer isn't
# rewritten through SQLite several times over. Dropped once the requester has the result.
BULK = frozenset({"transfer"})
MAX_PAYLOAD, MAX_BULK_PAYLOAD = 65536, 3_000_000
# A node checks in once per held poll (cluster.POLL_WAIT, 25 s) and not at all while it
# runs an action (up to 45 s), so "online" must outlast both, or a busy node looks gone.
ONLINE = 75
GRACE = 15  # seconds a running job may report past its deadline before it counts as uncertain


class Broker:
    def __init__(self, state):
        self.state = Path(state).resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.path = self.state / "cluster.sqlite3"
        self._bulk = {}  # job id -> {"payload", "encoded", "result"}; memory only, never persisted
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
                    capabilities TEXT NOT NULL, seen REAL, info TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, node TEXT NOT NULL, requester TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL, deadline REAL NOT NULL, result TEXT);
                CREATE INDEX IF NOT EXISTS cluster_pending ON jobs(node,state,created);
            """)
            try:  # older databases: an invite's credential works only until this time
                db.execute("ALTER TABLE nodes ADD COLUMN invite_expires REAL")
            except sqlite3.OperationalError:
                pass

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def owner_token(self):
        path = self.state / "cluster-owner.token"
        try:
            with path.open("x", encoding="ascii") as stream:
                path.chmod(0o600)
                stream.write(secrets.token_urlsafe(32))
        except FileExistsError:
            pass
        return path.read_text(encoding="ascii").strip()

    def enroll(self, node, capabilities=NODE_DEFAULT, invite_ttl=None):
        """A fresh credential for `node`. With `invite_ttl` (seconds) it is an invite's:
        good only until the node redeems it for its own token, and only for that long."""
        if not DEVICE_ID.fullmatch(node):
            raise ValueError("Invalid node ID.")
        caps = set(capabilities)
        if not caps or not caps <= CAPABILITIES:
            raise ValueError("Unknown or empty node capabilities.")
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("INSERT INTO nodes(id,token_hash,capabilities,invite_expires) VALUES(?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET token_hash=excluded.token_hash, "
                       "capabilities=excluded.capabilities, seen=NULL, info='{}', "
                       "invite_expires=excluded.invite_expires",
                       (node, hashlib.sha256(token.encode()).hexdigest(), json.dumps(sorted(caps)),
                        time.time() + invite_ttl if invite_ttl else None))
            # Rotating credentials never redelivers an action already dispatched.
            db.execute("UPDATE jobs SET state='uncertain' WHERE node=? AND state='running'", (node,))
        return token

    def set_capabilities(self, node, capabilities):
        """Change what an enrolled node may do without rotating its credential."""
        caps = set(capabilities)
        if not caps or not caps <= CAPABILITIES:
            raise ValueError("Unknown or empty node capabilities.")
        with self.db() as db:
            if not db.execute("UPDATE nodes SET capabilities=? WHERE id=?",
                              (json.dumps(sorted(caps)), node)).rowcount:
                raise LookupError(f"Node {node} is not enrolled.")
        return sorted(caps)

    def revoke(self, node):
        with self.db() as db:
            db.execute("DELETE FROM nodes WHERE id=?", (node,))
            db.execute("UPDATE jobs SET state=CASE WHEN state='running' THEN 'uncertain' "
                       "ELSE 'expired' END WHERE (node=? OR requester=?) AND state IN ('pending','running')", (node, node))

    def authenticate(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            return None
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            row = db.execute("SELECT id,capabilities,invite_expires FROM nodes WHERE token_hash=?",
                             (hashed,)).fetchone()
        if not row or (row["invite_expires"] is not None and row["invite_expires"] < time.time()):
            return None  # unknown, revoked, or an invite nobody redeemed in time
        return row["id"], set(json.loads(row["capabilities"]))

    def redeem(self, node, token=None):
        """Swap an invite's credential for the node's own, once: (token, first). The invite
        code stops working at that moment, so a copy of it left in chat or a file is worthless.
        A node may propose its own `token` (saved before asking): if our reply is lost it
        asks again with that token, and gets the same token instead of a lockout, with
        first=False so the caller repeats nothing (a node's token must not re-pair it)."""
        if token is not None and (not isinstance(token, str) or not 32 <= len(token) <= 128):
            raise ValueError("A proposed node credential must be 32-128 characters.")
        token = token or secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as db:
            if not db.execute("UPDATE nodes SET token_hash=?, invite_expires=NULL "
                              "WHERE id=? AND invite_expires IS NOT NULL AND invite_expires>=?",
                              (digest, node, time.time())).rowcount:
                row = db.execute("SELECT token_hash,invite_expires FROM nodes WHERE id=?", (node,)).fetchone()
                if not (row and row[1] is None and secrets.compare_digest(row[0], digest)):
                    raise PermissionError("This invite was already used or has expired.")
                return token, False
        return token, True

    def heartbeat(self, node, info):
        # Never accept agent-provided paths, shell commands, or credentials as config.
        safe = {k: str(info.get(k, ""))[:120] for k in ("hostname", "platform", "version", "code", "pulls")}
        with self.db() as db:
            db.execute("UPDATE nodes SET seen=?,info=? WHERE id=?", (time.time(), json.dumps(safe), node))

    def touch(self, node):
        """A node just talked to us (e.g. delivered a result): it is alive right now."""
        with self.db() as db:
            db.execute("UPDATE nodes SET seen=? WHERE id=?", (time.time(), node))

    def running(self, node):
        """Jobs this node has claimed and not yet finished."""
        with self.db() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE node=? AND state='running'",
                              (node,)).fetchone()[0]

    def busy_job(self, node, grace=15):
        """The job this node is executing right now, if still inside its deadline plus
        `grace` seconds (a hung job stops counting as busy). None when idle."""
        with self.db() as db:
            row = db.execute("SELECT id, kind, payload, deadline FROM jobs WHERE node=? AND "
                             "state='running' AND deadline>? ORDER BY created DESC LIMIT 1",
                             (node, time.time() - grace)).fetchone()
        if not row:
            return None
        try:
            action = json.loads(row["payload"]).get("action")
        except (ValueError, AttributeError):
            action = None
        return {"id": row["id"], "kind": row["kind"], "action": action or row["kind"],
                "deadline": row["deadline"]}

    def nodes(self):
        with self.db() as db:
            rows = db.execute("SELECT id,capabilities,seen,info FROM nodes ORDER BY id").fetchall()
        return [{"id": r["id"], "capabilities": json.loads(r["capabilities"]),
                 "seen": r["seen"], "online": bool(r["seen"] and time.time() - r["seen"] < ONLINE),
                 **json.loads(r["info"])} for r in rows]

    def delivery(self, node, hours=24):
        """How the last day's jobs to `node` ended: every one should be done or failed.
        expired = never picked up; uncertain = picked up, result never came back."""
        with self.db() as db:
            rows = db.execute("SELECT state, COUNT(*) FROM jobs WHERE node=? AND created>? "
                              "GROUP BY state", (node, time.time() - hours * 3600)).fetchall()
        return {state: count for state, count in rows}

    def enqueue(self, node, requester, kind, payload, timeout=60, job_id=None):
        job_id = job_id or uuid.uuid4().hex
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise ValueError("request_id must be a 32-character UUID hex string.")
        encoded = json.dumps(payload, sort_keys=True)
        if len(encoded.encode()) > (MAX_BULK_PAYLOAD if kind in BULK else MAX_PAYLOAD):
            raise ValueError("Cluster request is too large.")
        bulk = kind in BULK
        stored = "{}" if bulk else encoded
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if existing:
                same = (self._bulk.get(job_id, {}).get("encoded") == encoded) if bulk else existing["payload"] == encoded
                if not same or (existing["node"], existing["requester"], existing["kind"]) != (node, requester, kind):
                    raise ValueError("request_id already belongs to a different request.")
                return job_id
            db.execute("DELETE FROM jobs WHERE state IN ('done','failed','expired','uncertain') "
                       "AND created<?", (now - 7 * 86400,))
            # Bulk jobs nobody collected (expired, uncertain, requester gone): drop the bytes.
            marks = ",".join("?" * len(BULK))
            for (old,) in db.execute(f"SELECT id FROM jobs WHERE kind IN ({marks}) AND state IN "
                                     "('done','failed','expired','uncertain') AND created<?",
                                     (*sorted(BULK), now - 3600)).fetchall():
                self._bulk.pop(old, None)
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE node=? AND state IN ('pending','running')",
                               (node,)).fetchone()[0]
            if count >= 128:
                raise ValueError("This node's queue is full.")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,'pending',?,?,NULL)",
                       (job_id, node, requester, kind, stored, now, now + timeout))
            if bulk:
                self._bulk[job_id] = {"payload": payload, "encoded": encoded}
        return job_id

    def recover(self):
        """After a hub restart nobody waits for queued work any more: running jobs are
        uncertain and pending ones expire, so a node never runs a stale request (an
        "open <app>" or a voice ask) minutes after its requester was told it failed."""
        with self.db() as db:
            db.execute("UPDATE jobs SET state='uncertain' WHERE state='running'")
            db.execute("UPDATE jobs SET state='expired' WHERE state='pending'")

    def claim(self, node, kinds):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET state='expired' WHERE state='pending' AND deadline<?", (time.time(),))
            kinds = sorted(set(kinds))
            if not kinds:
                return None
            # Only the small columns: payloads are read for the one job handed out, not for
            # every job waiting in the queue on every poll.
            rows = db.execute("SELECT id, kind, requester, deadline FROM jobs WHERE node=? AND state='pending' "
                              f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY created",
                              (node, *kinds)).fetchall()
            row = None
            for r in rows:
                if r["kind"] in BULK and r["id"] not in self._bulk:
                    # The hub restarted since this was queued: its bytes are gone. Expire, never guess.
                    db.execute("UPDATE jobs SET state='expired' WHERE id=?", (r["id"],))
                    continue
                row = r
                break
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
            # "remaining" lets the node time the job on its own monotonic clock; the wall-clock
            # deadline is only compared across machines by old nodes.
            payload = (self._bulk[row["id"]]["payload"] if row["kind"] in BULK else json.loads(
                db.execute("SELECT payload FROM jobs WHERE id=?", (row["id"],)).fetchone()[0]))
            return {"id": row["id"], "kind": row["kind"], "payload": payload,
                    "requester": row["requester"], "deadline": row["deadline"],
                    "remaining": row["deadline"] - time.time()}

    def finish(self, job_id, node, result, failed=False):
        encoded = json.dumps(result)
        if len(encoded.encode()) > 4_000_000:
            raise ValueError("Cluster result is too large.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT kind, state, result FROM jobs WHERE id=? AND node=?",
                             (job_id, node)).fetchone()
            if not row:
                raise LookupError("Unknown job for this node.")
            target_state = "failed" if failed else "done"
            bulk = row["kind"] in BULK
            if row["state"] == target_state and (
                    self._bulk.get(job_id, {}).get("result") == encoded if bulk else row["result"] == encoded):
                return  # result acknowledgement was lost: safe to repeat the result only
            if row["state"] != "running":
                raise ValueError("Job is no longer running; it will not be executed again.")
            if bulk:  # the result stays in memory; the row only records that it arrived
                self._bulk.setdefault(job_id, {})["result"] = encoded
            db.execute("UPDATE jobs SET state=?,result=? WHERE id=?",
                       (target_state, "{}" if bulk else encoded, job_id))

    def compact(self, job_id):
        """Drop a finished bulk job's chunk data once its result has been read."""
        self._bulk.pop(job_id, None)
        with self.db() as db:
            db.execute("UPDATE jobs SET payload='{}', result=CASE WHEN result IS NULL THEN NULL "
                       "ELSE '{}' END WHERE id=? AND state IN ('done','failed','expired','uncertain')",
                       (job_id,))

    def get(self, job_id, requester=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or (requester is not None and requester != row["requester"]):
                raise LookupError("Unknown request.")
            state = row["state"]
            held = self._bulk.get(job_id) if row["kind"] in BULK else None
            # A running job gets GRACE past its deadline so a result landing just after the
            # requester gave up is still accepted instead of rejected as "no longer running".
            late = row["deadline"] + (GRACE if state == "running" else 0) < time.time()
            if state in ("pending", "running") and late:
                state = "expired" if state == "pending" else "uncertain"
                # Only if it is still what we read: a result that just landed wins.
                changed = db.execute("UPDATE jobs SET state=? WHERE id=? AND state=?",
                                     (state, job_id, row["state"])).rowcount
                if not changed:
                    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                    state = row["state"]
        return {"id": row["id"], "node": row["node"], "kind": row["kind"], "state": state,
                "result": json.loads(held["result"] if held and "result" in held else row["result"])
                if row["result"] is not None else None}
