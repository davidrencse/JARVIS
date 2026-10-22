"""Durable task and event ledger; every execution is claimed atomically."""

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "tasks.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(
                  id TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY, task_id TEXT NOT NULL, at TEXT NOT NULL,
                  kind TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS single_running_task
                  ON tasks(state) WHERE state='running';
                CREATE INDEX IF NOT EXISTS tasks_created_at ON tasks(created_at);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, spec):
        task_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)",
                       (task_id, json.dumps(spec), "queued", now(), now()))
            db.execute("INSERT INTO events(task_id,at,kind,detail) VALUES(?,?,?,?)",
                       (task_id, now(), "queued", "{}"))
        return self.get(task_id)

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("Unknown task ID.")
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        return result

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,state,created_at,updated_at FROM tasks ORDER BY created_at DESC")]

    def overview(self, limit=14):
        """Bounded recent tasks and state totals from one database snapshot."""
        limit = max(1, min(int(limit), 200))
        with self.connect() as db:
            db.execute("BEGIN")
            states = {row["state"]: row["n"] for row in db.execute(
                "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")}
            recent = []
            for row in db.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                                  (limit,)):
                item = dict(row)
                spec = json.loads(item.pop("spec"))
                recent.append({**item, "objective": spec.get("objective", "")[:160],
                               **{key: spec.get(key) for key in
                                  ("provider", "power", "role", "mode")}})
        return {"recent": recent, "states": states}

    def transition(self, task_id, expected, state, detail=None):
        if state == "running":  # who owns the claim, so a crashed owner can be recognised
            detail = {**(detail or {}), "pid": os.getpid()}
        for attempt in (1, 2):
            try:
                with self.connect() as db:
                    changed = db.execute(
                        "UPDATE tasks SET state=?,updated_at=? WHERE id=? AND state=?",
                        (state, now(), task_id, expected)).rowcount
                    if changed != 1:
                        raise ValueError(f"Task must be {expected}; it may already have been "
                                         "claimed.")
                    db.execute("INSERT INTO events(task_id,at,kind,detail) VALUES(?,?,?,?)",
                               (task_id, now(), state, json.dumps(detail or {})))
                return
            except sqlite3.IntegrityError:
                if attempt == 1 and self.reap_stale():
                    continue  # a dead process held the single running slot; it's free now
                raise ValueError("Another task is running in this ledger. Wait for it to "
                                 "finish.") from None

    def reap_stale(self, legacy_after=timedelta(hours=2)):
        """Fail 'running' tasks whose owning process is gone (crash, kill, power loss).

        Without this one dead run locks the ledger forever. Claims made before pids were
        recorded are only reaped once older than any run could legitimately last."""
        from .daemon import _alive
        reaped = []
        with self.connect() as db:
            for row in db.execute("SELECT id, updated_at FROM tasks WHERE state='running'"
                                  ).fetchall():
                event = db.execute("SELECT detail FROM events WHERE task_id=? AND kind='running' "
                                   "ORDER BY seq DESC LIMIT 1", (row["id"],)).fetchone()
                pid = json.loads(event["detail"]).get("pid") if event else None
                if pid is not None:
                    stale = not _alive(int(pid))
                else:
                    stale = (datetime.fromisoformat(row["updated_at"]) + legacy_after
                             < datetime.now(timezone.utc))
                if stale:
                    db.execute("UPDATE tasks SET state='failed', updated_at=? WHERE id=? "
                               "AND state='running'", (now(), row["id"]))
                    db.execute("INSERT INTO events(task_id,at,kind,detail) VALUES(?,?,?,?)",
                               (row["id"], now(), "failed", json.dumps(
                                   {"error_type": "OwnerDied",
                                    "note": "The process running this task exited without "
                                            "finishing it."})))
                    reaped.append(row["id"])
        return reaped

    def events(self, task_id):
        self.get(task_id)
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY seq", (task_id,))]
