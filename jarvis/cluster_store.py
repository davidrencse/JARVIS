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
# larger than control messages, and their bodies are dropped once the requester has the
# result, so a sent file doesn't linger in cluster.sqlite3 for a week.
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

    def redeem(self, node):
        """Swap an invite's credential for the node's own, once. The invite code stops
        working at that moment, so a copy of it left in chat or a file is worthless."""
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            if not db.execute("UPDATE nodes SET token_hash=?, invite_expires=NULL "
                              "WHERE id=? AND invite_expires IS NOT NULL AND invite_expires>=?",
                              (hashlib.sha256(token.encode()).hexdigest(), node, time.time())).rowcount:
                raise PermissionError("This invite was already used or has expired.")
        return token

    def heartbeat(self, node, info):
        # Never accept agent-provided paths, shell commands, or credentials as config.
        safe = {k: str(info.get(k, ""))[:120] for k in ("hostname", "platform", "version", "code")}
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
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if existing:
                if (existing["node"], existing["requester"], existing["kind"], existing["payload"]) != (
                        node, requester, kind, encoded):
                    raise ValueError("request_id already belongs to a different request.")
                return job_id
            db.execute("DELETE FROM jobs WHERE state IN ('done','failed','expired','uncertain') "
                       "AND created<?", (now - 7 * 86400,))
            # Bulk jobs nobody collected (expired, uncertain, requester gone): drop the bytes.
            db.execute(f"UPDATE jobs SET payload='{{}}', result=NULL WHERE kind IN "
                       f"({','.join('?' * len(BULK))}) AND state IN "
                       "('done','failed','expired','uncertain') AND created<? AND payload!='{}'",
                       (*sorted(BULK), now - 3600))
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE node=? AND state IN ('pending','running')",
                               (node,)).fetchone()[0]
            if count >= 128:
                raise ValueError("This node's queue is full.")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,'pending',?,?,NULL)",
                       (job_id, node, requester, kind, encoded, now, now + timeout))
        return job_id

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE jobs SET state='uncertain' WHERE state='running'")

    def claim(self, node, kinds):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET state='expired' WHERE state='pending' AND deadline<?", (time.time(),))
            rows = db.execute("SELECT * FROM jobs WHERE node=? AND state='pending' ORDER BY created", (node,))
            row = next((r for r in rows if r["kind"] in kinds), None)
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (row["id"],))
            # "remaining" lets the node time the job on its own monotonic clock; the wall-clock
            # deadline is only compared across machines by old nodes.
            return {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]),
                    "requester": row["requester"], "deadline": row["deadline"],
                    "remaining": row["deadline"] - time.time()}

    def finish(self, job_id, node, result, failed=False):
        encoded = json.dumps(result)
        if len(encoded.encode()) > 4_000_000:
            raise ValueError("Cluster result is too large.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND node=?", (job_id, node)).fetchone()
            if not row:
                raise LookupError("Unknown job for this node.")
            target_state = "failed" if failed else "done"
            if row["state"] == target_state and row["result"] == encoded:
                return  # result acknowledgement was lost: safe to repeat the result only
            if row["state"] != "running":
                raise ValueError("Job is no longer running; it will not be executed again.")
            db.execute("UPDATE jobs SET state=?,result=? WHERE id=?", (target_state, encoded, job_id))

    def compact(self, job_id):
        """Drop a finished bulk job's chunk data once its result has been read."""
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
            # A running job gets GRACE past its deadline so a result landing just after the
            # requester gave up is still accepted instead of rejected as "no longer running".
            late = row["deadline"] + (GRACE if state == "running" else 0) < time.time()
            if state in ("pending", "running") and late:
                state = "expired" if state == "pending" else "uncertain"
                db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))
        return {"id": row["id"], "node": row["node"], "kind": row["kind"], "state": state,
                "result": json.loads(row["result"]) if row["result"] is not None else None}
