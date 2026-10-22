"""Answers JARVIS knows without asking anyone: the local clock and calendar.

A cold ChatGPT turn took 5.3 s to say what day it was. These answers come from the
laptop's clock in microseconds, so "what day is it" is spoken as soon as Kokoro can
say it. Anything not clearly matched (e.g. "what time is it in Tokyo") returns None
and goes to the brain as usual.
"""

import re
from datetime import datetime

_LEAD = r"^\W*(?:hey\s+|ok(?:ay)?\s+)?(?:jarvis\W+)?(?:so\s+|um\s+|uh\s+)?"
_TAIL = r"(?:\s+(?:right\s+)?now|\s+today|\s+please|\s+again)*\W*$"
DATE = re.compile(_LEAD + r"(?:what(?:'s|s|\s+is)\s+(?:the\s+|today'?s\s+)?(?:date|day)"
                  r"(?:\s+(?:is\s+)?(?:it|today))?|what\s+day\s+(?:is\s+(?:it|today)|"
                  r"of\s+the\s+week\s+is\s+it)|what(?:'s|s|\s+is)\s+today)" + _TAIL, re.I)
TIME = re.compile(_LEAD + r"(?:what(?:'s|s|\s+is)\s+the\s+time|what\s+time\s+is\s+it|"
                  r"(?:do\s+you\s+(?:know|have)\s+)?the\s+time)" + _TAIL, re.I)


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def answer(text, now=None):
    now = now or datetime.now().astimezone()
    text = text.replace("’", "'")  # Whisper sometimes emits curly apostrophes
    if DATE.match(text):
        return f"It's {now:%A}, {now:%B} {_ordinal(now.day)}, {now.year}."
    if TIME.match(text):
        return f"It's {now.strftime('%I:%M %p').lstrip('0')}."
    return None
