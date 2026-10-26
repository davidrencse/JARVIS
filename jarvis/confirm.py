"""Expiring, origin-bound, single-use approvals, persisted with the history audit."""

import hashlib
import hmac
import json
import re
import time

from .history import History


def summary(command):
    intent, args = command["intent"], command["args"]
    if intent == "agent_task":
        what = args["text"]
    elif intent == "close_app":
        what = "close " + args["name"]
    else:
        what = intent.replace("_", " ") + " " + str(next(iter(args.values()), ""))
    return re.sub(r"\s+", " ", f"{what} on the {command['device']}").strip()


def prompt(item):
    return (f"I need your OK to {item['summary']}. "
            f"Say 'Jarvis, yes' or press Confirm ({item['id']}).")


class ConfirmQueue:
    def __init__(self, directory):
        self.history = History(directory)
        self.path = self.history.path
        with self.history.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS confirmations(
                number INTEGER PRIMARY KEY AUTOINCREMENT, summary TEXT NOT NULL,
                command TEXT NOT NULL, hash TEXT NOT NULL, origin TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL)""")

    @staticmethod
    def _item(row):
        item = dict(row)
        item["id"] = "C" + str(item.pop("number"))
        item["command"] = json.loads(item["command"])
        return item

    def park(self, command, origin, channel):
        if not isinstance(origin, str) or not origin:
            raise ValueError("A confirmation needs an origin device.")
        raw = json.dumps(command, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest, now = hashlib.sha256(raw.encode()).hexdigest(), time.time()
        with self.history.connect() as db:
            number = db.execute("INSERT INTO confirmations(summary,command,hash,origin,created,expires,status) "
                                "VALUES(?,?,?,?,?,?, 'pending')",
                                (summary(command), raw, digest, origin, now,
                                 now + (60 if channel == "voice" else 600))).lastrowid
            item = self._item(db.execute("SELECT * FROM confirmations WHERE number=?", (number,)).fetchone())
        from .events import emit
        emit(self.path.parent, "turn.confirm", item=item)
        return item

    def expire(self):
        with self.history.connect() as db:
            return db.execute("UPDATE confirmations SET status='expired' WHERE status='pending' AND expires<=?",
                              (time.time(),)).rowcount

    def pending(self, origin=None):
        self.expire()
        with self.history.connect() as db:
            rows = db.execute("SELECT * FROM confirmations WHERE status='pending'" +
                              (" AND origin=?" if origin is not None else "") + " ORDER BY number",
                              (origin,) if origin is not None else ()).fetchall()
        return [self._item(r) for r in rows]

    def _resolve(self, db, identifier, origin):
        if identifier is None:
            rows = db.execute("SELECT * FROM confirmations WHERE status='pending' AND origin=? AND expires>? ORDER BY number",
                              (origin, time.time())).fetchall()
            if len(rows) > 1:
                raise ValueError("Which one? " + " or ".join("C" + str(r["number"]) for r in rows))
            if not rows:
                raise ValueError("No pending confirmation for this device.")
            return rows[0]
        if not re.fullmatch(r"[cC][1-9][0-9]*", str(identifier)):
            raise ValueError("Use a confirmation ID such as C7.")
        row = db.execute("SELECT * FROM confirmations WHERE number=?", (int(identifier[1:]),)).fetchone()
        if row is None or (origin is not None and row["origin"] != origin):
            raise ValueError("No pending confirmation for this device.")
        if row["status"] != "pending" or row["expires"] <= time.time():
            raise ValueError("That confirmation expired or was already handled; request it again.")
        return row

    def approve(self, id_or_None, origin, hash=None):
        if not origin:
            raise ValueError("A confirmation needs an origin device.")
        with self.history.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._resolve(db, id_or_None, origin)
            digest = hashlib.sha256(row["command"].encode()).hexdigest()
            if not hmac.compare_digest(digest, row["hash"]) or (
                    hash is not None and not hmac.compare_digest(str(hash), digest)):
                raise ValueError("The command changed; request a new confirmation.")
            db.execute("UPDATE confirmations SET status='approved' WHERE number=?", (row["number"],))
            self.history.record_confirmation(db, "C" + str(row["number"]), origin, "approve", digest)
            return json.loads(row["command"])

    def reject(self, id, origin=None):
        with self.history.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._resolve(db, id, origin)
            db.execute("UPDATE confirmations SET status='rejected' WHERE number=?", (row["number"],))
            self.history.record_confirmation(db, "C" + str(row["number"]), row["origin"], "reject", row["hash"])

