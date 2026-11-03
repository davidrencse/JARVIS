"""Expiring, origin-bound, single-use approvals, persisted with the history audit."""

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time

from .history import KEEP_DAYS, History


def summary(command):
    intent, args = command["intent"], command["args"]
    if intent in ("agent_task", "browser_action"):
        what = args["text"]
    elif intent == "close_app":
        what = "close " + args["name"]
    elif intent == "terminal":
        what = f"run \"{args['command']}\" in the terminal"
    else:
        what = intent.replace("_", " ") + " " + str(next(iter(args.values()), ""))
    return re.sub(r"\s+", " ", f"{what} on the {command['device']}").strip()


GRANT_SECONDS = 600  # "allow on this site" lasts ten minutes
RECENT = 10
BARE_YES_DELAY = 3  # seconds before a bare spoken "yes" may approve a browser question


def notify(text):
    """Best-effort desktop nudge that David's OK is wanted. Never raises, never waits
    more than 3 s; silent off Linux or without notify-send (and under JARVIS_NO_NOTIFY)."""
    try:
        if os.environ.get("JARVIS_NO_NOTIFY"):
            return False
        from .platforms import detect
        exe = shutil.which("notify-send") if detect()["os"] == "linux" else None
        if not exe:
            return False
        subprocess.run([exe, "-a", "JARVIS", "-u", "critical", "JARVIS needs your OK",
                        str(text)[:300]], timeout=3, capture_output=True, check=False)
        return True
    except Exception:
        return False


def describe(item):
    """The structured fields the console shows for a pending item (see ConfirmQueue.listing)."""
    command = item.get("command") or {}
    args = command.get("args") if isinstance(command.get("args"), dict) else {}
    if command.get("intent") != "browser_action":
        return {"kind": "command", "expires_at": item.get("expires"), "can_grant": False}
    meta = args.get("browser") if isinstance(args.get("browser"), dict) else {}
    stakes = "low" if meta.get("stakes") == "low" else "high"
    site, verb = meta.get("site") or None, meta.get("verb")
    verb = verb if verb in ("click", "type", "press", "select", "dialog", "other") else "other"
    return {"kind": "browser", "site": site, "verb": verb, "target": meta.get("target") or None,
            "stakes": stakes, "expires_at": meta.get("expires_at") or item.get("expires"),
            "can_grant": stakes == "low" and bool(site) and bool(meta.get("named"))}


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
            for column in ("resolved REAL", "note TEXT"):  # added later: older files lack them
                try:
                    db.execute("ALTER TABLE confirmations ADD COLUMN " + column)
                except sqlite3.OperationalError:
                    pass
            db.execute("""CREATE TABLE IF NOT EXISTS site_grants(
                site TEXT NOT NULL, verb TEXT NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY(site, verb))""")

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
            # Handled ones are kept as long as the history (their audit rows go with it).
            db.execute("DELETE FROM confirmations WHERE status!='pending' AND created<?",
                       (now - KEEP_DAYS * 86400,))
            item = self._item(db.execute("SELECT * FROM confirmations WHERE number=?", (number,)).fetchone())
        from .events import emit
        emit(self.path.parent, "turn.confirm", item=item)
        return item

    def expire(self):
        now = time.time()
        with self.history.connect() as db:
            count = db.execute("UPDATE confirmations SET status='expired', resolved=expires "
                               "WHERE status='pending' AND expires<=?", (now,)).rowcount
            # A browser ask carries its own (shorter) deadline.
            for row in db.execute("SELECT number, command FROM confirmations WHERE status='pending'").fetchall():
                try:
                    args = json.loads(row["command"]).get("args") or {}
                    due = float((args.get("browser") or {}).get("expires_at") or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
                if due and due <= now:
                    count += db.execute("UPDATE confirmations SET status='expired', resolved=? "
                                        "WHERE number=? AND status='pending'", (now, row["number"])).rowcount
            return count

    def expire_item(self, id):
        """The waiting caller gave up: 'expired', not 'rejected', so the trace says why."""
        with self.history.connect() as db:
            db.execute("UPDATE confirmations SET status='expired', resolved=? WHERE number=? AND status='pending'",
                       (time.time(), int(str(id)[1:])))

    def pending(self, origin=None):
        self.expire()
        with self.history.connect() as db:
            rows = db.execute("SELECT * FROM confirmations WHERE status='pending'" +
                              (" AND origin=?" if origin is not None else "") + " ORDER BY number",
                              (origin,) if origin is not None else ()).fetchall()
        return [self._item(r) for r in rows]

    def _resolve(self, db, identifier, origin, bare_guard=False):
        if identifier is None:
            rows = db.execute("SELECT * FROM confirmations WHERE status='pending' AND origin=? AND expires>? ORDER BY number",
                              (origin, time.time())).fetchall()
            if len(rows) > 1:
                raise ValueError("Which one? " + " or ".join("C" + str(r["number"]) for r in rows))
            if not rows:
                raise ValueError("No pending confirmation for this device.")
            if bare_guard and time.time() - rows[0]["created"] < BARE_YES_DELAY and \
                    '"intent":"browser_action"' in rows[0]["command"]:
                # A bare "yes" may have been about something else; a browser ask just parked
                # needs its ID or the Confirm button.
                raise ValueError(f"That browser question just came up; say 'yes C{rows[0]['number']}' "
                                 "or press Confirm.")
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
        self.expire()  # an expired item can't be approved, whatever its deadline said
        with self.history.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._resolve(db, id_or_None, origin, bare_guard=True)
            digest = hashlib.sha256(row["command"].encode()).hexdigest()
            if not hmac.compare_digest(digest, row["hash"]) or (
                    hash is not None and not hmac.compare_digest(str(hash), digest)):
                raise ValueError("The command changed; request a new confirmation.")
            db.execute("UPDATE confirmations SET status='approved', resolved=? WHERE number=?",
                       (time.time(), row["number"]))
            self.history.record_confirmation(db, "C" + str(row["number"]), origin, "approve", digest)
            return json.loads(row["command"])

    def status(self, number):
        """pending, approved, rejected or expired; browser_gate polls this while it waits.
        Read-only: a locked database can't make the poll fail mid-wait (see expire_safely)."""
        with self.history.connect() as db:
            row = db.execute("SELECT status, expires, command FROM confirmations WHERE number=?",
                             (number,)).fetchone()
        if row is None:
            return None
        if row["status"] != "pending":
            return row["status"]
        now = time.time()
        try:
            due = float((((json.loads(row["command"]).get("args") or {}).get("browser") or {})
                         .get("expires_at")) or 0)
        except (ValueError, TypeError, AttributeError):
            due = 0
        return "expired" if row["expires"] <= now or (due and due <= now) else "pending"

    def expire_safely(self, tries=3):
        """expire() that rides out a briefly locked database."""
        for attempt in range(tries):
            try:
                return self.expire()
            except sqlite3.OperationalError:
                if attempt == tries - 1:
                    return 0
                time.sleep(0.1)

    def reject(self, id, origin=None):
        with self.history.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._resolve(db, id, origin)
            db.execute("UPDATE confirmations SET status='rejected', resolved=? WHERE number=?",
                       (time.time(), row["number"]))
            self.history.record_confirmation(db, "C" + str(row["number"]), row["origin"], "reject", row["hash"])


    # -- "allow on this site for 10 min" ----------------------------------------------------
    def grant(self, site, verb, seconds=GRANT_SECONDS):
        if not site or verb not in ("click", "type", "press", "select"):
            raise ValueError("A grant needs a site and a click, type, press or select.")
        with self.history.connect() as db:
            db.execute("INSERT OR REPLACE INTO site_grants(site,verb,expires) VALUES(?,?,?)",
                       (str(site).lower(), verb, time.time() + seconds))

    def has_grant(self, site, verb):
        with self.history.connect() as db:
            row = db.execute("SELECT 1 FROM site_grants WHERE site=? AND verb=? AND expires>?",
                             (str(site).lower(), verb, time.time())).fetchone()
        return row is not None

    def grants(self):
        now = time.time()
        with self.history.connect() as db:
            db.execute("DELETE FROM site_grants WHERE expires<=?", (now,))
            rows = db.execute("SELECT site, verb, expires FROM site_grants ORDER BY site, verb").fetchall()
        return [{"site": r["site"], "verb": r["verb"], "expires_at": r["expires"]} for r in rows]

    def revoke(self, site, verb):
        with self.history.connect() as db:
            return db.execute("DELETE FROM site_grants WHERE site=? AND verb=?",
                              (str(site).lower(), verb)).rowcount

    def grant_item(self, identifier, origin, hash=None):
        """Approve this item and allow its (site, verb) for GRANT_SECONDS. Only a low-stakes
        browser item with a known site and a named element; commit-like asks never."""
        mine = [i for i in self.pending(origin) if i["id"].upper() == str(identifier).upper()]
        info = describe(mine[0]) if mine else None
        if not info or not info["can_grant"]:
            raise ValueError("That one can't be allowed for a whole site; approve it once instead.")
        command = self.approve(identifier, origin, hash=hash)
        self.grant(info["site"], info["verb"])
        with self.history.connect() as db:
            db.execute("UPDATE confirmations SET note='granted' WHERE number=?", (int(str(identifier)[1:]),))
        return command, info

    def listing(self, origin=None):
        """What the console shows: pending items (id, summary, hash, ... plus describe()'s
        kind/site/verb/target/stakes/expires_at/can_grant), active grants, last RECENT outcomes."""
        pending = [{**i, **describe(i)} for i in self.pending(origin)]
        with self.history.connect() as db:
            rows = db.execute("SELECT number, summary, status, note, resolved, created FROM confirmations "
                              "WHERE status!='pending' ORDER BY COALESCE(resolved, created) DESC, number DESC "
                              "LIMIT ?", (RECENT,)).fetchall()
        recent = [{"id": "C" + str(r["number"]), "summary": r["summary"],
                   "outcome": "granted" if r["note"] == "granted" else r["status"],
                   "at": r["resolved"] or r["created"]} for r in rows]
        return {"pending": pending, "grants": self.grants(), "recent": recent}
