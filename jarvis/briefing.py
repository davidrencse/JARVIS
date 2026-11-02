"""Spoken briefings straight from the vault: no model, no network, no plan usage.

"Jarvis, brief me", "what's on today?", "what do I have tomorrow?", "what's due?" and
"what's overdue?" are answered from calendar.md, the task agenda and today's daily
note in milliseconds, like the clock answers in instant.py. The ChatGPT brain is not
asked, so these keep working when the plan's usage limit is reached or the network
is down. Anything not clearly one of these returns None and goes to the brain.
"""

import re
from datetime import date, datetime, timedelta

_LEAD = r"^\W*(?:hey\s+|ok(?:ay)?\s+)?(?:jarvis\W+)?(?:so\s+|um\s+|uh\s+|please\s+|can\s+you\s+)?"
_TAIL = r"(?:\s+please)?\W*$"
BRIEF = re.compile(_LEAD + r"(?:(?:give\s+me\s+)?(?:my\s+|the\s+|a\s+)?(?:morning\s+|daily\s+)?"
                   r"brief(?:ing)?(?:\s+me)?|brief\s+me|good\s+morning|catch\s+me\s+up|"
                   r"what(?:'s|s|\s+is)\s+(?:my\s+)?day\s+look(?:ing)?\s+like)" + _TAIL, re.I)
DAY = re.compile(_LEAD + r"(?:what(?:'s|s|\s+is)\s+(?:on\s+)?(?:my\s+)?(?:schedule|agenda|calendar|plate)?"
                 r"\s*(?:on\s+|for\s+)?(today|tomorrow|tonight)|what\s+do\s+i\s+have\s+(?:on\s+)?"
                 r"(today|tomorrow|tonight)|what(?:'s|s|\s+is)\s+on\s+(today|tomorrow|tonight)|"
                 r"what(?:'s|s|\s+is)\s+(?:on\s+)?my\s+(?:schedule|agenda|calendar|plate))"
                 r"(?:\s+(?:today|tomorrow))?" + _TAIL, re.I)
DUE = re.compile(_LEAD + r"(?:what(?:'s|s|\s+is)\s+(due|overdue)(?:\s+(?:this\s+week|today|soon))?|"
                 r"what\s+(?:am\s+i|is)\s+(?:behind|late)\s+on|any(?:thing)?\s+overdue|"
                 r"am\s+i\s+(?:behind|late)\s+on\s+anything|is\s+anything\s+overdue|"
                 r"what\s+deadlines\s+do\s+i\s+have(?:\s+(?:this\s+week|coming\s+up|soon))?|"
                 r"what\s+are\s+my\s+deadlines|when\s+are\s+my\s+deadlines)" + _TAIL, re.I)


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _clock(hhmm):
    hour, minute = map(int, hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    hour = hour % 12 or 12
    return f"{hour}{f':{minute:02d}' if minute else ''} {suffix}"


def _day(text):
    """A date from ISO text, or None when it is missing or not a real date."""
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def _when(day, today):
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if day - today < timedelta(days=7):
        return f"{day:%A}"
    return f"{day:%A}, {day:%B} {_ordinal(day.day)}"


def _event(text):
    """'14:00–15:50 OS — Room 101 Main Hall' → 'OS at 2 PM'."""
    match = re.match(r"^(\d{1,2}:\d{2})(?:\s*[–-]\s*\d{1,2}:\d{2})?\s+(.*)$", text)
    if match:
        return f"{match.group(2).split(' — ')[0].strip()} at {_clock(match.group(1))}"
    match = re.match(r"^(?:\(all[- ]day[^)]*\)|all[- ]day)\s*[—-]?\s*(.*)$", text, re.I)
    return (match.group(1) if match else text).split(" — ")[0].strip()


def _deadline(item, today):
    text = re.sub(r"^(?:\(all[- ]day[^)]*\)|all[- ]day|\d{1,2}:\d{2}(?:\s*[–-]\s*\d{1,2}:\d{2})?)"
                  r"\s*[—-]?\s*", "",
                  item["text"]).split(" — ")[0]
    # "Connect Assignment Ch 7 due (tasks board, CourseSite)" -> "Connect Assignment Ch 7"
    text = re.sub(r"\s*\([^)]*\)", "", text)
    text = re.sub(r"\s+due\Z", "", text.strip(), flags=re.I).strip()
    return f"{text} {_when(item['date'], today)}"


def _say(title, limit=60):
    """A task title fit to speak: its first clause, no prose due date, whole words."""
    text = re.split(r"[;.]\s|\s—\s(?=[a-z]|due\b)", title, maxsplit=1)[0]
    text = re.sub(r"\s*[—-]?\s*(?:was\s+)?due\b.*$", "", text, flags=re.I).strip() or title
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0]
    return text.rstrip(" ,;:—-")


def _join(items):
    items = [i for i in items if i]
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + ", and " + items[-1]


def _distinct(tasks):
    """One mention per piece of work, in order. "Operating Systems Assignment 2 was due
    2026-10-13; see [[operating-systems]]" in an audit note and "Assignment 2" in that
    course note are the same task: same date, it points at the other's note, and it
    contains the other's spoken title. The fuller wording is kept."""
    from .tasks import _key

    def covers(a, b):
        if a is b or (a["due"] or a["scheduled"]) != (b["due"] or b["scheduled"]):
            return False
        link = b["path"].removesuffix(".md")
        refers = (f"[[{link}" in a["raw"] or f"[[{link.rsplit('/', 1)[-1]}" in a["raw"]
                  or f" {_key(b['note'])} " in f" {_key(a['raw'])} ")
        return refers and f" {_key(_say(b['title']))} " in f" {_key(a['raw'])} "

    kept = []
    for task in tasks:
        if any(covers(k, task) for k in kept):
            continue
        for i, k in enumerate(kept):
            if covers(task, k):
                kept[i] = task
                break
        else:
            kept.append(task)
    return kept


def _priority(text):
    """'Atlas (carried over from Thu): finish the research, then DM Sam' → 'Atlas'."""
    from .vault import plain
    text = re.sub(r"\s*\([^)]*\)", "", plain(text))
    return _say(re.split(r":\s", text, maxsplit=1)[0], 50)


def priorities_line(vault, today, back=3, limit=3):
    """Open priorities from today's daily note or, before today's is written, the ones
    still unchecked in the latest note of the last few days ("carried over from Friday")."""
    for ago in range(back + 1):
        day = today - timedelta(days=ago)
        path = vault.daily_path(day)
        if path.is_file():
            break
    else:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    items = [_priority(m) for m in re.findall(r"^\s*\d+\.\s+- \[[ /]\]\s+(\S.*)$", text, re.M)]
    items = [i for i in items if i]
    if not items:
        return ""
    more = f", plus {len(items) - limit} more" if len(items) > limit else ""
    lead = "Today's priorities" if day == today else f"Carried over from {_when(day, today) if ago > 1 else 'yesterday'}"
    return f"{lead}: {_join(items[:limit])}{more}."


def day_line(vault, day, today, limit=5):
    events = [_event(i["text"]) for i in vault.calendar_items(today, days=(day - today).days)
              if i["kind"] == "event" and i["date"] == day]
    if not events:
        return f"Nothing on your calendar {_when(day, today)}."
    more = f", plus {len(events) - limit} more" if len(events) > limit else ""
    return f"{_when(day, today).capitalize()} you have {_join(events[:limit])}{more}."


def tasks_line(tasks, today, limit=2):
    agenda = tasks.agenda(today)
    parts, overdue = [], _distinct(agenda["overdue"])
    if overdue:
        names = _join(_say(t["title"]) for t in overdue[:limit])
        parts.append(f"{len(overdue)} overdue task{'s' if len(overdue) != 1 else ''}: {names}.")
    due_today = [t for t in agenda["today"] if t["due"] == today.isoformat() or t["scheduled"]]
    if due_today:
        parts.append(f"Due today: {_join(_say(t['title']) for t in due_today[:3])}.")
    soon = [t for t in agenda["upcoming"] if _day(t["due"] or t["scheduled"])][:limit]
    if soon:
        parts.append("Coming up: " + _join(f"{_say(t['title'], 50)} {_when(_day(t['due'] or t['scheduled']), today)}"
                                           for t in soon) + ".")
    return " ".join(parts)


def deadlines_line(vault, today, limit=2):
    items = [i for i in vault.calendar_items(today) if i["kind"] == "deadline"][:limit]
    return ("Next deadline" + ("s" if len(items) > 1 else "") + ": "
            + _join(_deadline(i, today) for i in items) + ".") if items else ""


LOOP_DAYS = 14  # open loops older than this are left to the brain's recall, not the briefing


# Loops are written about David ("David still hasn't sent..."); the briefing talks to him.
_VERB = {"hasn't": "haven't", "has not": "have not", "has": "have", "is": "are",
         "isn't": "aren't", "was": "were", "wasn't": "weren't", "needs": "need",
         "wants": "want", "plans": "plan", "owes": "owe", "says": "say", "said": "said",
         "promised": "promised", "asked": "asked", "agreed": "agreed"}
_YOU = [(re.compile(r"\bDavid's\b"), lambda m: "your"),
        (re.compile(r"\bDavid (still |also )?(" + "|".join(sorted(map(re.escape, _VERB), key=len, reverse=True))
                    + r")\b"), lambda m: f"you {m.group(1) or ''}{_VERB[m.group(2)]}"),
        (re.compile(r"\b(said|promised|agreed|planned|thought) (he|that he)\b"),
         lambda m: f"{m.group(1)} {'that you' if m.group(2) == 'that he' else 'you'}"),
        (re.compile(r"\bDavid\b"), lambda m: "you")]


def _to_you(text):
    for pattern, repl in _YOU:
        text = pattern.sub(repl, text)
    return text


def loops_line(memory, limit=2, days=LOOP_DAYS):
    """Memory's open loops (consolidate.py: what someone said would happen and hasn't, a
    question left hanging), newest first: "Still open: ...". "" when there are none, or
    the memory can't be read: the briefing never fails over it."""
    if memory is None:
        return ""
    try:
        from datetime import timezone
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        loops = memory.search(kind="loop", since=since, limit=limit + 1)
    except Exception:
        return ""
    said = [_say(_to_you(str(i.get("preview") or i.get("title") or "").rstrip(".…")), 90)
            for i in loops[:limit]]
    said = [s for s in said if s]
    if not said:
        return ""
    more = f", plus {len(loops) - limit} more" if len(loops) > limit else ""
    return f"Still open: {_join(said)}{more}."


def answer(text, vault, tasks, now=None, memory=None):
    """A spoken answer from the vault (and memory's open loops for the briefing), or None
    when the question isn't one of these."""
    if vault is None:
        return None
    now = now or datetime.now()
    today = now.date() if isinstance(now, datetime) else now
    text = text.replace("’", "'").strip()
    if BRIEF.match(text):
        greeting = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening"
        parts = [f"{greeting}, David. It's {today:%A}, {today:%B} {_ordinal(today.day)}.",
                 day_line(vault, today, today), priorities_line(vault, today),
                 tasks_line(tasks, today), deadlines_line(vault, today), loops_line(memory)]
        return " ".join(p for p in parts if p)
    if (match := DAY.match(text)):
        word = next((g for g in match.groups() if g), "today").lower()
        day = today + timedelta(days=1) if word == "tomorrow" else today
        line = day_line(vault, day, today)
        if day == today:
            line = " ".join(p for p in (line, priorities_line(vault, today), tasks_line(tasks, today)) if p)
        return line
    if (match := DUE.match(text)):
        if (match.group(1) and match.group(1).lower() == "overdue") or re.search(r"behind|late|overdue", text, re.I):
            overdue = _distinct(tasks.agenda(today)["overdue"])
            if not overdue:
                return "Nothing is overdue."
            def was(t):
                due = _day(t["due"])
                return f"{_say(t['title'])}, due {due:%B} {_ordinal(due.day)}" if due else _say(t["title"])
            return f"{len(overdue)} overdue: " + _join(was(t) for t in overdue[:4]) + "."
        parts = [tasks_line(tasks, today), deadlines_line(vault, today, limit=3)]
        return " ".join(p for p in parts if p) or "Nothing is due in the next week."
    return None
