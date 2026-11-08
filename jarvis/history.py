"""Command history: every command JARVIS ran, and what happened (docs/jarvis-os-architecture.md, L6).

The memory heap keeps what was said; this keeps what was done: one row per command with
its device, intent, outcome state, verification level, latency, and one row per hop
(parse, gates, run, observe) with its time. It is the engine behind the H-checks:

- H1 recent_failure: the same command failed the same way a few minutes ago. David's own
  request gets a warning and runs anyway; an agent's request is blocked.
- H3 repeating: the same command keeps recurring from one origin (the Grok loop that
  opened Instagram every minute). Agents are blocked and David is told; David's own
  repeats are left alone.

History is evidence, not authority: it can warn, block or slow JARVIS down, never approve.
Arguments stay here, in SQLite on the main PC; nothing is written to the vault.
"""

import json
import re
import time
import uuid
from pathlib import Path

from .store import connect_database

AGENT_ORIGINS = frozenset({"agent", "system"})
FAILED = ("pre_effect_failure", "partial_effect", "uncertain", "unconfirmed")
H1_WINDOW = 10 * 60      # seconds a failure stays relevant
H3_WINDOW = 10 * 60      # seconds looked back for a repeating command
H3_REPEATS = 3           # this many earlier runs (plus this one) is a loop
KEEP_DAYS = 90


def command_key(command):
    """The identity of a command: same device, intent and args means the same command."""
    args = json.dumps(command["args"], sort_keys=True, separators=(",", ":"))
    return f"{command['device']}|{command['intent']}|{args}"


def error_class(text):
    """Failures compare by kind, not exact wording: lowercased, numbers and quoted names
    removed, first 80 characters."""
    text = re.sub(r"'[^']*'|\"[^\"]*\"|\d+", "#", str(text or "").lower())
    return re.sub(r"\s+", " ", text).strip()[:80]


class History:
    def __init__(self, directory):
        self.path = Path(directory).resolve() / "history.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS commands(
                  id TEXT PRIMARY KEY, request TEXT NOT NULL, at REAL NOT NULL,
                  origin TEXT, origin_class TEXT NOT NULL, text TEXT NOT NULL,
                  key TEXT NOT NULL, device TEXT NOT NULL, intent TEXT NOT NULL,
                  args TEXT NOT NULL, state TEXT NOT NULL, ok INTEGER NOT NULL,
                  result TEXT NOT NULL, error_class TEXT NOT NULL,
                  observed_ms INTEGER, total_ms INTEGER);
                CREATE INDEX IF NOT EXISTS commands_key ON commands(key, at);
                CREATE INDEX IF NOT EXISTS commands_at ON commands(at);
                CREATE TABLE IF NOT EXISTS hops(
                  command TEXT NOT NULL, name TEXT NOT NULL, ms INTEGER NOT NULL,
                  state TEXT NOT NULL, PRIMARY KEY(command, name));
                CREATE TABLE IF NOT EXISTS confirmation_audit(
                  id INTEGER PRIMARY KEY, confirmation TEXT NOT NULL, at REAL NOT NULL,
                  origin TEXT NOT NULL, action TEXT NOT NULL, hash TEXT NOT NULL);
            """)

    def connect(self):
        return connect_database(self.path)

    # -- writing --------------------------------------------------------------------------
    @staticmethod
    def record_confirmation(db, confirmation, origin, action, digest):
        """Use the queue's transaction: consumption and audit succeed together."""
        db.execute("INSERT INTO confirmation_audit(confirmation,at,origin,action,hash) VALUES(?,?,?,?,?)",
                   (confirmation, time.time(), origin, action, digest))

    def record(self, request, command, outcome, text="", origin=None, origin_class="owner",
               hops=None, total_ms=None, now=None):
        """One finished (or blocked) command. hops: [{"name", "ms", "state"}]. Returns its id."""
        now = time.time() if now is None else now
        cid = f"cmd:{time.strftime('%Y%m%d', time.localtime(now))}:{uuid.uuid4().hex[:8]}"
        state = outcome.get("state", "not_sent")
        ok = bool(outcome.get("ok"))
        with self.connect() as db:
            db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                cid, request, now, origin, origin_class, text[:500], command_key(command),
                command["device"], command["intent"],
                json.dumps(command["args"], sort_keys=True), state, int(ok),
                str(outcome.get("text", ""))[:500],
                "" if ok else error_class(outcome.get("text")),
                outcome.get("observed_ms"), total_ms))
            db.executemany("INSERT OR REPLACE INTO hops VALUES(?,?,?,?)",
                           [(cid, h["name"], int(h["ms"]), h.get("state", "done"))
                            for h in hops or ()])
            db.execute("DELETE FROM hops WHERE command IN (SELECT id FROM commands WHERE at<?)",
                       (now - KEEP_DAYS * 86400,))
            db.execute("DELETE FROM commands WHERE at<?", (now - KEEP_DAYS * 86400,))
            db.execute("DELETE FROM confirmation_audit WHERE at<?", (now - KEEP_DAYS * 86400,))
        return cid

    # -- the H-checks ---------------------------------------------------------------------
    def recent_failure(self, command, now=None, window=H1_WINDOW):
        """H1: the latest run of this exact command, if the executor reported a failure
        within `window` seconds and nothing ran since. {"at", "state", "result", "ago"} or None."""
        now = time.time() if now is None else now
        with self.connect() as db:
            row = db.execute("SELECT at, state, ok, result FROM commands WHERE key=? AND at>=? "
                             "AND state != 'not_sent' ORDER BY at DESC LIMIT 1",
                             (command_key(command), now - window)).fetchone()
        if row is None or row["ok"]:  # unconfirmed ran fine as far as anyone knows
            return None
        return {"at": row["at"], "state": row["state"], "result": row["result"],
                "ago": round(now - row["at"])}

    def repeats(self, command, origin_class, now=None, window=H3_WINDOW):
        """H3: how many times this command actually ran (blocked, not_sent rows excluded) from this origin class within `window`."""
        now = time.time() if now is None else now
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM commands WHERE key=? AND origin_class=? "
                              "AND at>=? AND state != 'not_sent'", (command_key(command), origin_class,
                                            now - window)).fetchone()[0]

    def check(self, command, origin_class="owner", now=None):
        """H1 + H3 for one command: {"block": bool, "warn": str|None, "alert": bool, "checks"}."""
        checks, warn, block, alert = [], None, False, False
        # The brain's own tool calls count as a possible loop too: it reads web pages.
        agent = origin_class in AGENT_ORIGINS or origin_class == "brain"
        failure = self.recent_failure(command, now)
        if failure:
            mins = round(failure["ago"] / 60)
            when = "just now" if mins < 1 else f"{mins} min ago"
            note = f"That failed {when} ({failure['result'] or failure['state']})"
            if agent:
                block = True
                checks.append({"name": "H1 not_known_failing", "pass": False, "why": note})
            else:
                warn = f"{note}; trying again."
                checks.append({"name": "H1 not_known_failing", "pass": True, "why": "warned"})
        else:
            checks.append({"name": "H1 not_known_failing", "pass": True, "why": ""})
        count = self.repeats(command, origin_class, now)
        if agent and count >= H3_REPEATS:
            block = alert = True
            checks.append({"name": "H3 not_repeating", "pass": False,
                           "why": f"ran {count} times in {H3_WINDOW // 60} min from {origin_class}"})
        else:
            checks.append({"name": "H3 not_repeating", "pass": True, "why": f"{count} recent"})
        return {"block": block, "warn": warn, "alert": alert, "checks": checks}

    # -- reading ----------------------------------------------------------------------------
    def recent(self, limit=20):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM commands ORDER BY at DESC LIMIT ?",
                              (max(1, min(int(limit), 500)),)).fetchall()
        return [dict(r) for r in rows]

    def hops(self, command_id):
        with self.connect() as db:
            rows = db.execute("SELECT name, ms, state FROM hops WHERE command=? ORDER BY rowid",
                              (command_id,)).fetchall()
        return [dict(r) for r in rows]

    def requests(self, limit=12):
        """The most recent requests, newest first, each with its commands and their hops:
        what the console's replay draws. [{request, at, text, origin, total_ms, commands}]"""
        limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            ids = [r["request"] for r in db.execute(
                "SELECT request, MAX(at) AS last FROM commands GROUP BY request "
                "ORDER BY last DESC LIMIT ?", (limit,))]
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            rows = db.execute(f"SELECT * FROM commands WHERE request IN ({marks}) "
                              "ORDER BY at, rowid", ids).fetchall()
            hops = {}
            for h in db.execute(f"SELECT command, name, ms, state FROM hops WHERE command IN "
                                f"(SELECT id FROM commands WHERE request IN ({marks})) "
                                "ORDER BY rowid", ids):
                hops.setdefault(h["command"], []).append(
                    {"name": h["name"], "ms": h["ms"], "state": h["state"]})
        out = {}
        for r in rows:
            req = out.setdefault(r["request"], {
                "request": r["request"], "at": r["at"], "text": r["text"],
                "origin": r["origin"], "total_ms": r["total_ms"], "commands": []})
            req["total_ms"] = max(req["total_ms"] or 0, r["total_ms"] or 0)
            req["commands"].append({
                "id": r["id"], "intent": r["intent"], "device": r["device"],
                "args": json.loads(r["args"]), "state": r["state"], "ok": bool(r["ok"]),
                "result": r["result"], "hops": hops.get(r["id"], [])})
        return [out[i] for i in ids if i in out]

    def stats(self, intent, device, limit=50):
        """H2 track record: runs, success rate and median total time for an intent on a device."""
        with self.connect() as db:
            rows = db.execute("SELECT ok, state, total_ms FROM commands WHERE intent=? AND "
                              "device=? AND state != 'not_sent' ORDER BY at DESC LIMIT ?",
                              (intent, device, limit)).fetchall()
        if not rows:
            return {"runs": 0, "success": None, "median_ms": None}
        good = sum(1 for r in rows if r["ok"] and r["state"] not in FAILED)
        times = sorted(r["total_ms"] for r in rows if r["total_ms"] is not None)
        return {"runs": len(rows), "success": round(good / len(rows), 3),
                "median_ms": times[len(times) // 2] if times else None}
