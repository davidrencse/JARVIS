"""Speech fragments: what the voice loop drops or completes before the brain sees it.

Real voice requests that reached the brain (memory heap, 2026-10-24..30): "Alel." (one
misheard word), "open up" and "go to my Arch Linux and open up" (cut off mid-command), "for
the day." (a tail end), and, inside the follow-up window that needs no wake word, 36- and
72-word stretches of David talking to someone else ("Transcribing. The voice API is shit...").
Each cost a brain call and got a confused answer.

judge(text) sorts a request into:
- "keep": answer it. Anything the strict parser or an instant answer understands always is.
- "unclear": too little to act on (one unknown word, a dangling tail): ask to repeat, the way
  a low-confidence transcript is handled.
- "incomplete": a command cut off before its object ("open up"): ask for the rest ("Open
  what?") and put the two halves together.
- "overheard": a long ramble in the follow-up window that isn't shaped like a request to
  JARVIS: dropped silently.

It is a plain rule set over words, deliberately conservative: a real request that looks odd
costs one "Sorry?", while a fragment answered costs a wrong action or a confused reply.
"""

import re

# Whole requests that are complete as they are.
SHORT_OK = frozenset({
    "hello", "hi", "hey", "yo", "thanks", "thank you", "thank you so much", "yes", "yeah",
    "yep", "no", "nope", "stop", "pause", "play", "resume", "next", "skip", "previous", "back",
    "cancel", "help", "louder", "quieter", "mute", "unmute", "good morning", "good night",
    "goodnight", "brief me", "never mind", "nevermind", "go ahead", "do it", "ok", "okay",
    "sure", "confirm", "approve", "deny", "what", "why", "how", "who", "when", "where",
    "volume up", "volume down", "stop scrolling", "keep scrolling", "scroll", "sleep",
})
# A command can't end on these: something was cut off after them.
DANGLING = frozenset({"up", "to", "on", "and", "the", "a", "an", "my", "with", "for", "of", "about",
                      "at", "in", "into", "then", "or", "but", "open", "close", "launch",
                      "start", "play", "go", "search", "send", "type", "show", "find", "turn"})
# ...and a request doesn't begin with these unless more follows.
LEADING = frozenset({"for", "of", "and", "but", "or", "to", "with", "the", "than", "because",
                     "so", "like", "um", "uh"})
VERBS = ("open", "close", "launch", "start", "play", "go", "search", "send", "type", "show",
         "find", "turn", "set", "tell", "ask", "check", "look", "give", "put", "pause", "stop",
         "skip", "scroll", "remind", "remember", "add", "make", "build", "write", "read")
ASK_FOR = {"open": "Open what?", "launch": "Launch what?", "start": "Start what?",
           "close": "Close what?", "play": "Play what?", "search": "Search for what?",
           "go": "Go where?", "send": "Send what?", "type": "Type what?", "show": "Show what?",
           "find": "Find what?", "turn": "Turn what?"}
QUESTION = re.compile(r"^\W*(?:what|what's|whats|who|who's|when|where|where's|why|how|which|"
                      r"is|are|can|could|would|will|do|does|did|should|have|has)\b", re.I)
OVERHEARD_WORDS = 30  # a follow-up this long, shaped like neither a command nor a question


def _words(text):
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def judge(text, follow_up=False, understood=None):
    """(verdict, say): verdict keep | unclear | incomplete | overheard; `say` is the
    question for incomplete ("Open what?"), else "". understood(text) -> bool: the parser
    or an instant answer handles it (those always pass). follow_up: the utterance came in
    the no-wake-word window after JARVIS spoke or after a bare "Jarvis"."""
    words = _words(text)
    if not words:
        return "unclear", ""
    phrase = " ".join(words)
    if phrase in SHORT_OK:
        return "keep", ""
    try:
        if understood and understood(text):
            return "keep", ""
    except Exception:  # a broken checker never blocks a request
        pass
    asked = "?" in str(text) or bool(QUESTION.match(str(text)))
    if len(words) == 1:
        if words[0] in ASK_FOR:
            return "incomplete", ASK_FOR[words[0]]
        return ("keep", "") if asked else ("unclear", "")
    # "up"/"on"/"down" end a command only straight after its verb ("open up"); after a
    # noun they belong to it ("turn the volume up", "switch the lights on").
    particle = words[-1] in ("up", "on", "down") and words[-2] not in VERBS
    if words[-1] in DANGLING and not asked and not particle:
        verb = next((w for w in reversed(words) if w in ASK_FOR), None)
        if verb:
            return "incomplete", ASK_FOR[verb]
        if len(words) <= 4:
            return "unclear", ""
    if words[0] in LEADING and len(words) <= 4 and not any(w in VERBS for w in words):
        return "unclear", ""
    # Overheard: a long stretch of several sentences in the no-wake-word window that
    # doesn't open like a request (a command, "can you...", or a question). A long request
    # David dictates to JARVIS is one or two sentences and opens like one.
    sentences = [x for x in re.split(r"[.!?]+", str(text)) if x.strip()]
    opens = words[0] in VERBS or phrase.startswith(("can you", "could you", "would you",
                                                    "please", "jarvis", "i want", "i need"))
    if follow_up and len(words) > OVERHEARD_WORDS and len(sentences) >= 3 and not opens \
            and not (QUESTION.match(str(text)) and len(_words(sentences[0])) <= 15
                     and str(text).split(".")[0].rstrip().endswith("?")):
        return "overheard", ""
    return "keep", ""


def completes(prefix, rest, understood=None):
    """Is `rest` the missing part of the cut-off `prefix` ("Spotify" after "open up"), or a
    request of its own: "never mind", "what time is it", the whole command said again?"""
    words = _words(rest)
    if not words or " ".join(words) in SHORT_OK or QUESTION.match(str(rest)):
        return False
    if words[0] in VERBS or words[0] in _words(prefix)[:1]:
        return False  # "open up Spotify" said again in full
    try:
        if understood and understood(rest):
            return False  # complete by itself
    except Exception:
        pass
    return len(words) <= 6


def complete(prefix, rest):
    """The cut-off command and its missing part, as one request ("open up" + "Spotify")."""
    return f"{str(prefix).strip().rstrip('.,!?')} {str(rest).strip()}".strip()
