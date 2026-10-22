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
                 r"what\s+are\s+my\s+deadlines|when\s+are\s+my\s+deadlines)" + _TAIL, re.I)


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _clock(hhmm):
    hour, minute = map(int, hhmm.split(":"))
    suffix = "AM" if hour < 12 else "PM"
    hour = hour % 12 or 12
    return f"{hour}{f':{minute:02d}' if minute else ''} {suffix}"


def _when(day, today):
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    if day - today < timedelta(days=7):
        return f"{day:%A}"
    return f"{day:%A}, {day:%B} {_ordinal(day.day)}"


def _event(text):
    """'14:00–15:50 OS — Room 101 Main Hall' → 'at 2 PM, OS'."""
    match = re.match(r"^(\d{1,2}:\d{2})(?:\s*[–-]\s*\d{1,2}:\d{2})?\s+(.*)$", text)
    if match:
        return f"{match.group(2).split(' — ')[0].strip()} at {_clock(match.group(1))}"
    match = re.match(r"^(?:\(all[- ]day[^)]*\)|all[- ]day)\s*[—-]?\s*(.*)$", text, re.I)
    return (match.group(1) if match else text).split(" — ")[0].strip()


def _deadline(item, today):
    text = re.sub(r"^(?:\(all[- ]day[^)]*\)|all[- ]day|\d{1,2}:\d{2}(?:\s*[–-]\s*\d{1,2}:\d{2})?)"
                  r"\s*[—-]?\s*", "",
                  item["text"]).split(" — ")[0].strip()
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


def day_line(vault, day, today, limit=5):
    events = [_event(i["text"]) for i in vault.calendar_items(today, days=(day - today).days)
              if i["kind"] == "event" and i["date"] == day]
    if not events:
        return f"Nothing on your calendar {_when(day, today)}."
    more = f", plus {len(events) - limit} more" if len(events) > limit else ""
    return f"{_when(day, today).capitalize()} you have {_join(events[:limit])}{more}."


def tasks_line(tasks, today, limit=2):
    agenda = tasks.agenda(today)
    parts = []
    if agenda["overdue"]:
        names = _join(_say(t["title"]) for t in agenda["overdue"][:limit])
        parts.append(f"{len(agenda['overdue'])} overdue task{'s' if len(agenda['overdue']) != 1 else ''}: {names}.")
    due_today = [t for t in agenda["today"] if t["due"] == today.isoformat() or t["scheduled"]]
    if due_today:
        parts.append(f"Due today: {_join(_say(t['title']) for t in due_today[:3])}.")
    soon = agenda["upcoming"][:limit]
    if soon:
        parts.append("Coming up: " + _join(f"{_say(t['title'], 50)} {_when(date.fromisoformat(t['due'] or t['scheduled']), today)}"
                                           for t in soon) + ".")
    return " ".join(parts)


def deadlines_line(vault, today, limit=2):
    items = [i for i in vault.calendar_items(today) if i["kind"] == "deadline"][:limit]
    return ("Next deadline" + ("s" if len(items) > 1 else "") + ": "
            + _join(_deadline(i, today) for i in items) + ".") if items else ""


def answer(text, vault, tasks, now=None):
    """A spoken answer from the vault, or None when the question isn't one of these."""
    if vault is None:
        return None
    now = now or datetime.now()
    today = now.date() if isinstance(now, datetime) else now
    text = text.replace("’", "'").strip()
    if BRIEF.match(text):
        greeting = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening"
        parts = [f"{greeting}, David. It's {today:%A}, {today:%B} {_ordinal(today.day)}.",
                 day_line(vault, today, today), tasks_line(tasks, today), deadlines_line(vault, today)]
        return " ".join(p for p in parts if p)
    if (match := DAY.match(text)):
        word = next((g for g in match.groups() if g), "today").lower()
        day = today + timedelta(days=1) if word == "tomorrow" else today
        line = day_line(vault, day, today)
        if day == today:
            line = " ".join(p for p in (line, tasks_line(tasks, today)) if p)
        return line
    if (match := DUE.match(text)):
        if match.group(1) and match.group(1).lower() == "overdue" or re.search(r"behind|late|overdue", text, re.I):
            overdue = tasks.agenda(today)["overdue"]
            if not overdue:
                return "Nothing is overdue."
            def was(t):
                due = date.fromisoformat(t["due"])
                return f"{_say(t['title'])}, due {due:%B} {_ordinal(due.day)}"
            return f"{len(overdue)} overdue: " + _join(was(t) for t in overdue[:4]) + "."
        parts = [tasks_line(tasks, today), deadlines_line(vault, today, limit=3)]
        return " ".join(p for p in parts if p) or "Nothing is due in the next week."
    return None
