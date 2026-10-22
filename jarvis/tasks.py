"""Tasks that live in the Obsidian vault, in the Obsidian Tasks plugin's format.

A task is a Markdown checkbox line in any vault note, the same line Obsidian shows and
the Tasks plugin queries:

    - [ ] Pick a flight controller ⏫ 🔁 every week ➕ 2026-10-22 ⏳ 2026-10-23 📅 2026-10-24

So there is one task list, not two: tick a box in Obsidian and JARVIS sees it on the next
read; tell JARVIS "I finished assignment 2" and the box is ticked in the note, with the
✅ date the plugin would add. Dataview reads the same emoji fields, so the daily note's
"Due" query sees JARVIS's tasks too. Tasks with no home note go to `tasks.md` at the
vault root, which also holds live Tasks-plugin queries (overdue, today, upcoming).

What counts: checkboxes in every indexed note (not archive/ or templates/). Daily notes
hold routine checklists (teeth, gym, Anki), so from them only today's Priorities and
lines with a date or priority marker count. Notes that are mostly a checklist (study
plans, objective lists) are summarized as progress instead of hundreds of cards.

Editing is one line at a time and verified: the line must still read exactly as it did
when parsed, or the edit is refused. Status changes, due dates and priorities are the
only edits; text is never rewritten and nothing is deleted. Writes are audited with the
vault's other writes in .jarvis/vault-writes.jsonl.
"""

import calendar
import hashlib
import json
import re
import threading
from datetime import date, datetime, timedelta

from .vault import CONFLICT, HEADING, _raw, plain, split_frontmatter

STATUS = {" ": "todo", "x": "done", "X": "done", "/": "in_progress", "-": "cancelled"}
OPEN = ("todo", "in_progress")
PRIORITY = {"🔺": "highest", "⏫": "high", "🔼": "medium", "🔽": "low", "⏬": "lowest"}
EMOJI = {v: k for k, v in PRIORITY.items()}
RANK = {"highest": 0, "high": 1, "medium": 2, None: 3, "low": 4, "lowest": 5}
DATES = {"📅": "due", "📆": "due", "🗓": "due", "⏳": "scheduled", "⌛": "scheduled",
         "🛫": "start", "➕": "created", "✅": "done", "❌": "cancelled"}
SIGNS = "".join(PRIORITY) + "".join(DATES) + "🔁🆔⛔🏁"
LINE = re.compile(r"^(?P<indent>[ \t]*)(?:\d+[.)][ \t]+)?(?P<bullet>[-*+]|\d+[.)])[ \t]+"
                  r"\[(?P<status>[^\]])\][ \t]+(?P<body>.*?)[ \t]*$")
DATE_FIELD = re.compile("(" + "|".join(DATES) + r")️?\s*(\d{4}-\d{2}-\d{2})")
PRIO_FIELD = re.compile("(" + "|".join(PRIORITY) + r")️?")
RECUR_FIELD = re.compile(r"🔁️?\s*([^" + SIGNS + r"]+?)\s*(?=[" + SIGNS + r"]|\^[\w-]+\s*$|$)")
OTHER_FIELD = re.compile(r"(?:🆔|⛔|🏁)️?\s*[\w,-]*")
INLINE = re.compile(r"\[(due|scheduled|start|created|completion|priority)::\s*([^\]]+)\]")
BLOCK_ID = re.compile(r"\s\^[\w-]+\s*$")
TAGS = re.compile(r"(?<![\w&])#([A-Za-z][\w/-]*)")
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
PROSE_DUE = re.compile(r"\bdue(?:\s+(?:on|by))?\s+(\d{4}-\d{2}-\d{2}|(?:[A-Z][a-z]{2,8})\.?\s+"
                       r"\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)", re.I)
DATE_IN = re.compile(r"\d{4}-\d{2}-\d{2}")
CHECKLIST_SIZE = 15  # a note with this many checkboxes is a checklist, shown as progress
INBOX, INBOX_SECTION = "tasks.md", "Inbox"
TASK_SECTIONS = ("Tasks", "Next step", "Next steps", "To do", "Todo", "Action items")


def parse_date(text, today=None, roll=True):
    """YYYY-MM-DD from ISO dates and the ways people say them: today, tomorrow, friday,
    next week, in 3 days, +2w, Oct 27, October 27 2026. None for empty; ValueError if
    unreadable. roll: a yearless date long past means next year (said, not written)."""
    today = today or date.today()
    text = re.sub(r"\s+", " ", str(text or "").strip().lower().rstrip("."))
    if not text or text in ("none", "no", "-"):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).isoformat()
    simple = {"today": 0, "tonight": 0, "tomorrow": 1, "tmrw": 1, "day after tomorrow": 2,
              "next week": 7}
    if text in simple:
        return (today + timedelta(days=simple[text])).isoformat()
    match = re.fullmatch(r"(?:in )?\+?(\d+) ?(d|day|days|w|wk|week|weeks)(?: from now)?", text)
    if match:
        days = int(match.group(1)) * (7 if match.group(2).startswith("w") else 1)
        return (today + timedelta(days=days)).isoformat()
    days = [d.lower() for d in calendar.day_name]
    match = re.fullmatch(r"(?:(this|next|on) )?(" + "|".join(days + [d[:3] for d in days]) + r")", text)
    if match:
        target = [d[:3] for d in days].index(match.group(2)[:3])
        ahead = (target - today.weekday()) % 7 or 7
        if match.group(1) == "next" and ahead < 7:
            ahead += 7 if today.weekday() >= target else 0
        return (today + timedelta(days=ahead)).isoformat()
    match = re.fullmatch(r"([a-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?(?:,? (\d{4}))?", text)
    if match and match.group(1) in MONTHS:
        year = int(match.group(3) or today.year)
        when = date(year, MONTHS[match.group(1)], int(match.group(2)))
        if roll and not match.group(3) and when < today - timedelta(days=60):
            when = date(year + 1, when.month, when.day)  # "Jan 5" said in October
        return when.isoformat()
    raise ValueError(f"Can't read the date {text!r}; use YYYY-MM-DD, today, tomorrow or a weekday.")


def parse_line(line, written=None):
    """The task on one Markdown line, or None. Fields follow the Tasks plugin.
    written: when the note was written, the year for a prose "due June 17"."""
    match = LINE.match(line.rstrip("\r\n"))
    if not match:
        return None
    body = match.group("body")
    task = {"status": STATUS.get(match.group("status"), "other"), "mark": match.group("status"),
            "indent": len(match.group("indent").expandtabs(4)), "priority": None, "recur": None,
            **{k: None for k in ("due", "scheduled", "start", "created", "done", "cancelled")}}
    for sign, value in DATE_FIELD.findall(body):
        task[DATES[sign]] = value
    if (prio := PRIO_FIELD.search(body)):
        task["priority"] = PRIORITY[prio.group(1)]
    if (recur := RECUR_FIELD.search(body)):
        task["recur"] = recur.group(1).strip()
    for key, value in INLINE.findall(body):  # Dataview inline fields
        value = value.strip()
        if key == "priority":
            task["priority"] = task["priority"] or (value.lower() if value.lower() in RANK else None)
        else:
            task[{"completion": "done"}.get(key, key)] = task[{"completion": "done"}.get(key, key)] or value[:10]
    text = RECUR_FIELD.sub("", body)
    for pattern in (DATE_FIELD, PRIO_FIELD, OTHER_FIELD, INLINE, BLOCK_ID):
        text = pattern.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if not task["due"] and (prose := PROSE_DUE.search(text)):
        try:  # "Assignment 2 — due October 13, 2026": the vault's older habit
            task["due"] = parse_date(prose.group(1).replace(",", ""), written, roll=False)
            task["due_from_text"] = True
        except ValueError:
            pass
    task["text"], task["title"], task["tags"] = text, plain(text), TAGS.findall(text)
    return task


def _key(text):
    return re.sub(r"[^\w]+", " ", plain(text).lower()).strip()


def _tid(path, text, nth):
    return hashlib.sha1(f"{path}\0{_key(text)}\0{nth}".encode("utf-8")).hexdigest()[:8]


def next_dates(task, today):
    """Dates for the next occurrence of a recurring task, the way the Tasks plugin
    advances them. ValueError for rules JARVIS can't follow."""
    rule = (task.get("recur") or "").lower().strip()
    match = re.fullmatch(r"every (?:(\d+) )?(day|week|month|year)s?( when done)?", rule)
    if not match:
        raise ValueError(f"JARVIS can't advance '🔁 {task['recur']}'. Tick it in Obsidian so "
                         "the Tasks plugin makes the next one.")
    count, unit = int(match.group(1) or 1), match.group(2)
    fields = [k for k in ("start", "scheduled", "due") if task.get(k)]
    if not fields:
        raise ValueError("A recurring task needs a due, scheduled or start date to repeat from.")
    # Repeat from the due date (else scheduled, else start), or from today "when done";
    # every date on the task moves by the same amount.
    anchor = date.fromisoformat(task[fields[-1]])
    base = today if match.group(3) else anchor

    def shift(day):
        if unit in ("day", "week"):
            return day + timedelta(days=count * (7 if unit == "week" else 1))
        months = count * (12 if unit == "year" else 1)
        year, month = divmod(day.month - 1 + months, 12)
        year, month = day.year + year, month + 1
        return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))

    delta = shift(base) - anchor
    return {k: (date.fromisoformat(task[k]) + delta).isoformat() for k in fields}


class Tasks:
    """Every task in the vault, read on demand (only changed notes are re-parsed)."""

    def __init__(self, vault):
        self.vault = vault
        self._cache, self._lock = {}, threading.Lock()

    # -- reading -------------------------------------------------------------------------
    def _note_tasks(self, rel, full, today, today_rel):
        stat = full.stat()
        key = (stat.st_mtime, stat.st_size, today if rel.startswith("daily/") else None)
        cached = self._cache.get(rel)
        if cached and cached[0] == key:
            return cached[1]
        text = full.read_text(encoding="utf-8", errors="replace")
        fields, _ = split_frontmatter(text)
        fields = fields or {}
        title = str(fields.get("title") or full.stem)
        daily, is_today = rel.startswith("daily/"), rel == today_rel
        written = DATE_IN.search(str(fields.get("created") or ""))
        written = date.fromisoformat(written.group(0)) if written else today
        closed = str(fields.get("status", "")).lower() in ("completed", "archived", "done")
        found, section, fenced, seen = [], "", False, {}
        for number, line in enumerate(text.splitlines(), 1):
            if re.match(r"^\s*(```|~~~)", line):
                fenced = not fenced
                continue
            if fenced:
                continue
            if (heading := HEADING.match(line)):
                section = heading.group(2).strip()
                continue
            task = parse_line(line, written)
            if not task:
                continue
            # In daily notes only real markers count: "due September 29" pasted into a
            # week of daily notes is history, not seven tasks.
            marked = (task["due"] and not task.get("due_from_text")) or task["scheduled"] \
                or task["start"] or task["priority"]
            if daily and not marked and not (is_today and section.lower() == "priorities"):
                continue  # routine daily checklists aren't tasks to track
            nth = seen[_key(task["text"])] = seen.get(_key(task["text"]), -1) + 1
            found.append({"id": _tid(rel, task["text"], nth), "path": rel, "line": number,
                          "raw": line, "section": section, "note": title,
                          "note_type": str(fields.get("type", "")), "today_note": is_today,
                          "note_closed": closed, **task})
        self._cache[rel] = (key, found)
        return found

    def all(self, today=None):
        today = today or date.today()
        today_rel = self.vault.daily_path(today).relative_to(self.vault.root).as_posix()
        with self._lock:
            out, live = [], set()
            for rel, full in self.vault.markdown():
                live.add(rel)
                try:
                    out.extend(self._note_tasks(rel, full, today, today_rel))
                except OSError:
                    continue
            for gone in set(self._cache) - live:
                self._cache.pop(gone, None)
        return out

    def get(self, ref, today=None, open_only=False):
        """A task by id, or by its words when they pick out exactly one task."""
        ref = str(ref or "").strip()
        tasks = [t for t in self.all(today) if not open_only or t["status"] in OPEN]
        for task in tasks:
            if task["id"] == ref.lower():
                return task
        words = set(_key(ref).split())
        if not words:
            raise ValueError("Say which task.")
        scored = []
        for task in tasks:
            have = set(_key(task["text"]).split())
            overlap = len(words & have) / len(words)
            if overlap >= 0.6:
                scored.append((overlap + (0.5 if task["status"] in OPEN else 0)
                               + (0.2 if _key(ref) in _key(task["text"]) else 0), task))
        scored.sort(key=lambda s: -s[0])
        if not scored:
            raise ValueError(f"No task matches {ref!r}.")
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.15:
            options = "; ".join(f"{t['id']} {t['text'][:60]} ({t['path']})" for _, t in scored[:4])
            raise ValueError(f"Several tasks match {ref!r}: {options}. Use the id.")
        return scored[0][1]

    def agenda(self, today=None, upcoming_days=7, query=""):
        """Open tasks bucketed by when they matter, checklists as progress, recent wins."""
        today = today or date.today()
        horizon = (today + timedelta(days=upcoming_days)).isoformat()
        now, week_ago = today.isoformat(), (today - timedelta(days=7)).isoformat()
        tasks, words = self.all(today), _key(query).split()
        if words:
            tasks = [t for t in tasks if all(w in _key(t["text"] + " " + t["note"]) for w in words)]
        per_note = {}
        for task in tasks:
            per_note.setdefault(task["path"], []).append(task)
        checklist = {p for p, ts in per_note.items() if not p.startswith("daily/") and (
            len(ts) >= CHECKLIST_SIZE or ts[0]["note_type"] == "study-plan")}
        buckets = {k: [] for k in ("overdue", "today", "in_progress", "upcoming", "later",
                                   "anytime", "done")}
        for task in tasks:
            if task["status"] in OPEN and task["note_closed"]:
                continue  # leftovers in a finished course or project
            if task["status"] in OPEN:
                when = task["due"] or task["scheduled"]
                if task["due"] and task["due"] < now:
                    buckets["overdue"].append(task)
                elif (task["due"] == now or (task["scheduled"] and task["scheduled"] <= now)
                      or (task["today_note"] and not when)):
                    buckets["today"].append(task)
                elif task["status"] == "in_progress":
                    buckets["in_progress"].append(task)
                elif when and when <= horizon:
                    buckets["upcoming"].append(task)
                elif when:
                    buckets["later"].append(task)
                elif (task["path"] not in checklist and not task["path"].startswith("daily/")
                      and not (task["start"] and task["start"] > now)):
                    buckets["anytime"].append(task)
            elif task["status"] == "done" and (task["done"] or "") >= week_ago:
                buckets["done"].append(task)
        order = lambda t: (t["due"] or t["scheduled"] or "9999", RANK.get(t["priority"], 3), t["path"], t["line"])
        for name, items in buckets.items():
            items.sort(key=order if name != "anytime" else
                       lambda t: (RANK.get(t["priority"], 3), t["path"], t["line"]))
        buckets["done"].sort(key=lambda t: t["done"] or "", reverse=True)
        lists = []
        for path in sorted(checklist):
            items = [t for t in per_note[path] if t["status"] != "cancelled"]
            left = [t for t in items if t["status"] in OPEN]
            lists.append({"path": path, "note": items[0]["note"] if items else path,
                          "total": len(items), "done": len(items) - len(left),
                          "next": left[0]["title"] if left else None})
        counts = {k: len(v) for k, v in buckets.items()}
        return {"date": now, **buckets, "checklists": lists, "counts": counts,
                "open": sum(1 for t in tasks if t["status"] in OPEN)}

    def summary(self, today=None, limit=5):
        """A few lines for the brain's world brief."""
        agenda = self.agenda(today)
        lines = []
        for name, label in (("overdue", "Overdue tasks"), ("today", "Tasks for today"),
                            ("upcoming", "Due this week")):
            items = agenda[name]
            if items:
                shown = "; ".join(f"{t['title'][:80]}" + (f" (due {t['due']})" if t["due"]
                                  and name != "today" else "") + f" [{t['note']}]"
                                  for t in items[:limit])
                more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
                lines.append(f"{label}: {shown}{more}.")
        return lines

    # -- writing -------------------------------------------------------------------------
    def _inbox(self, when):
        full = self.vault.root / INBOX
        if full.exists():
            return INBOX
        day = f"{when:%Y-%m-%d}"
        query = lambda filters: "```tasks\n" + filters + "\nshort mode\n```"
        text = (f"---\ntitle: Tasks\ntype: index\ntags: [index, meta]\ncreated: {day}\n"
                f"updated: {day}\n---\n\n# Tasks\n\nEvery open task in the vault, live from the "
                "Tasks plugin. Tasks belong in their project or course note; ones with no "
                "home go in Inbox below. JARVIS reads and ticks the same lines.\n\n"
                "## Overdue\n\n" + query("not done\ndue before today") + "\n\n"
                "## Today\n\n" + query("not done\nhappens today") + "\n\n"
                "## Next 7 days\n\n" + query("not done\ndue after today\ndue before in 8 days")
                + "\n\n## In progress\n\n" + query("status.type is IN_PROGRESS") + "\n\n"
                f"## {INBOX_SECTION}\n\n")
        full, rel = self.vault._check_writable(INBOX)
        self.vault._save(full, rel, text, before=None)
        self.vault._audit("tasks+create", rel, text, "jarvis")
        return rel

    def add(self, text, due=None, scheduled=None, priority=None, recur=None, note=None,
            section=None, channel="jarvis", when=None):
        """A new open task, in `note` (its Tasks/Next step section) or the tasks.md Inbox.
        Returns the existing task instead when an open one already says the same."""
        when = when or datetime.now()
        today = when.date() if isinstance(when, datetime) else when
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        text = re.sub(r"^(?:[-*+]\s+)?(?:\[.\]\s+)?", "", text)
        for pattern in (DATE_FIELD, PRIO_FIELD, OTHER_FIELD):
            text = pattern.sub("", text).strip()
        if not text:
            raise ValueError("What's the task?")
        if len(text) > 300:
            raise ValueError("Keep a task to one line (300 characters).")
        priority = (priority or "").strip().lower() or None
        if priority and priority not in EMOJI:
            raise ValueError("priority must be highest, high, medium, low or lowest.")
        due, scheduled = parse_date(due, today), parse_date(scheduled, today)
        if recur and not re.fullmatch(r"every [\w ,]+", recur.strip().lower()):
            raise ValueError("Repeat rules look like 'every week' or 'every 2 days'.")
        for task in self.all(today):
            if task["status"] in OPEN and _key(task["text"]) == _key(text):
                return {**task, "existing": True}
        if note:
            rel = self.vault.resolve(note)
            if not rel:
                raise ValueError(f"No note matches {note!r}.")
            if not section:
                body = _raw(self.vault.root / rel)
                heads = [h.group(2).strip() for h in map(HEADING.match, body.splitlines()) if h]
                section = next((h for want in TASK_SECTIONS for h in heads
                                if h.lower() == want.lower()), "Tasks")
        else:
            rel, section = self._inbox(when), section or INBOX_SECTION
        line = "- [ ] " + text + "".join(
            [f" {EMOJI[priority]}" if priority else "", f" 🔁 {recur.strip()}" if recur else "",
             f" ➕ {today.isoformat()}", f" ⏳ {scheduled}" if scheduled else "",
             f" 📅 {due}" if due else ""])
        self.vault.append(rel, line, section, channel=channel, when=when)
        made = [t for t in self.all(today) if t["path"] == rel and _key(t["text"]) == _key(text)]
        return {**(made[-1] if made else {"path": rel, "text": text}), "existing": False}

    def update(self, ref, action, value=None, channel="jarvis", when=None):
        """done | reopen | start | cancel | due DATE | scheduled DATE | priority LEVEL."""
        when = when or datetime.now()
        today = when.date() if isinstance(when, datetime) else when
        task = self.get(ref, today, open_only=action in ("done", "start", "cancel"))
        full, rel = self.vault._check_writable(task["path"])
        before = _raw(full)
        if CONFLICT.search(before):
            raise RuntimeError(f"{rel} has unresolved git merge conflicts; fix those first.")
        lines = before.splitlines(keepends=True)
        index = task["line"] - 1
        if index >= len(lines) or lines[index].rstrip("\r\n") != task["raw"].rstrip("\r\n"):
            raise RuntimeError(f"That line in {rel} changed since JARVIS read it; try again.")
        old = lines[index]
        ending = old[len(old.rstrip("\r\n")):]
        line, added = old.rstrip("\r\n"), None
        day = today.isoformat()

        def mark(line, char):
            match = LINE.match(line)
            return line[:match.start("status")] + char + line[match.end("status"):]

        def put(line, sign, value):
            line = re.sub("(?:" + "|".join(k for k, v in DATES.items() if v == DATES[sign])
                          + r")️?\s*\d{4}-\d{2}-\d{2}", "", line).rstrip()
            if value is None:
                return line
            block = BLOCK_ID.search(line)
            tail = block.group(0) if block else ""
            return line[:len(line) - len(tail)].rstrip() + f" {sign} {value}" + tail

        if action == "done":
            if task["recur"]:
                fresh = put(mark(line, " "), "✅", None)
                for field, value in next_dates(task, today).items():
                    fresh = put(fresh, {"due": "📅", "scheduled": "⏳", "start": "🛫"}[field], value)
                added = fresh
            line = put(mark(line, "x"), "✅", day)
        elif action == "reopen":
            line = put(put(mark(line, " "), "✅", None), "❌", None)
        elif action == "start":
            line = mark(line, "/")
        elif action == "cancel":
            line = put(mark(line, "-"), "❌", day)
        elif action in ("due", "scheduled"):
            line = put(line, "📅" if action == "due" else "⏳", parse_date(value, today))
        elif action == "priority":
            level = (value or "").strip().lower() or None
            if level and level not in EMOJI:
                raise ValueError("priority must be highest, high, medium, low, lowest or empty.")
            line = PRIO_FIELD.sub("", line)
            line = re.sub(r"  +", " ", line).rstrip()
            if level:
                match = LINE.match(line)
                body = match.group("body")
                cut = re.search("[" + SIGNS + "]", body)
                at = match.start("body") + (cut.start() if cut else len(body))
                line = (line[:at].rstrip() + f" {EMOJI[level]} " + line[at:].lstrip()).rstrip()
        else:
            raise ValueError("action must be done, reopen, start, cancel, due, scheduled or priority.")
        lines[index] = line + ending
        if added is not None:  # the Tasks plugin puts the next occurrence above the done one
            lines.insert(index, added + ending)
        text = self.vault._touch("".join(lines), day)
        self.vault._save(full, rel, text, before)
        self.vault._audit(f"task.{action}", rel, line, channel)
        result = {"id": task["id"], "path": rel, "action": action, "line": line.strip()}
        if added is not None:
            result["next"] = added.strip()
        return result


# -- tools for the brain and MCP ---------------------------------------------------------
TASK_INSTRUCTIONS = (
    "Your sandbox being read-only only means no shell or direct file edits: the task_* and "
    "vault_* tools are how you write, and JARVIS runs them for you, so never tell David "
    "you can't add or change something because you are read-only. "
    "Tasks live in his vault as checkbox lines (Obsidian Tasks format). task_list shows "
    "them (view agenda for what matters now). task_add makes one: pass due/priority/note "
    "only if he said them, dates as YYYY-MM-DD worked out from the current time, and the "
    "note (project or course) it belongs to if obvious, else leave note empty for the "
    "inbox. task_update marks done/start/cancel/reopen or changes due/priority; pass the "
    "task id from task_list when you have it. 'Remind me to X' means a task with a due "
    "date, not an alarm.")


def _schema(**props):
    return {"type": "object", "required": list(props), "additionalProperties": False,
            "properties": props}


S = {"type": "string"}


def _brief(task):
    keep = ("id", "title", "status", "due", "scheduled", "priority", "recur", "note", "path", "section")
    return {k: task[k] for k in keep if task.get(k)}


class TaskTools:
    def __init__(self, tasks, write=True, channel="jarvis"):
        self.tasks, self.channel = tasks, channel
        self.write = write and tasks.vault.writable

    def specs(self):
        specs = [{"type": "function", "name": "task_list",
                  "description": "David's tasks from the vault. view: agenda (overdue, today, "
                                 "in progress, this week, anytime, checklist progress), open, "
                                 "or done (finished in the last 7 days). query filters by words "
                                 "(\"\" for all).",
                  "inputSchema": _schema(view=S, query=S)}]
        if self.write:
            specs += [
                {"type": "function", "name": "task_add",
                 "description": "Add a task. due: YYYY-MM-DD or \"\". priority: highest, high, "
                                "medium, low, lowest or \"\". note: the project/course note it "
                                "belongs to, or \"\" for the tasks.md inbox.",
                 "inputSchema": _schema(text=S, due=S, priority=S, note=S)},
                {"type": "function", "name": "task_update",
                 "description": "Change a task. task: its id (preferred) or words that identify "
                                "it. action: done, start, cancel, reopen, due, priority. value: "
                                "the new date or priority for due/priority, else \"\".",
                 "inputSchema": _schema(task=S, action=S, value=S)}]
        return specs

    def call(self, name, args):
        try:
            if name == "task_list":
                view = str(args.get("view") or "agenda")
                agenda = self.tasks.agenda(query=str(args.get("query") or ""))
                if view == "agenda":
                    out = {k: [_brief(t) for t in agenda[k][:15]] for k in
                           ("overdue", "today", "in_progress", "upcoming", "later", "anytime")
                           if agenda[k]}
                    out["checklists"] = [{k: c[k] for k in ("note", "done", "total", "next")}
                                         for c in agenda["checklists"]]
                    out["today_is"] = agenda["date"]
                elif view == "done":
                    out = [_brief(t) for t in agenda["done"]]
                else:
                    out = [_brief(t) for k in ("overdue", "today", "in_progress", "upcoming",
                                               "later", "anytime") for t in agenda[k]][:60]
                return True, json.dumps(out, ensure_ascii=False)
            if name == "task_add" and self.write:
                made = self.tasks.add(str(args.get("text", "")), args.get("due") or None,
                                      priority=args.get("priority") or None,
                                      note=args.get("note") or None, channel=self.channel)
                return True, json.dumps({**_brief(made), "already_existed": made.get("existing")},
                                        ensure_ascii=False)
            if name == "task_update" and self.write:
                return True, json.dumps(self.tasks.update(
                    str(args.get("task", "")), str(args.get("action", "")),
                    args.get("value") or None, channel=self.channel), ensure_ascii=False)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"
        return False, f"Unknown tool {name}."


_OPEN_TASKS, _OPEN_LOCK = {}, threading.Lock()


def tasks_for(vault):
    """One Tasks reader per vault per process, so its parse cache is shared."""
    if vault is None:
        return None
    with _OPEN_LOCK:
        return _OPEN_TASKS.setdefault(id(vault), Tasks(vault))
