"""The strict command parser: L1's free first pass (docs/jarvis-os-architecture.md).

It turns a request into Registry commands only when it is certain: every word is
accounted for, there is exactly one reading, and every command is a Registry intent.
Anything else (chat, questions, pronouns, conditions, unknown words, an app that is
also a site) returns no commands and a reason, and the request goes to the
interpreter, the ChatGPT brain. A wrong fast parse is worse than a one-second model
call, so this never guesses.

    parse("open chrome on arch and spotify on the dell", devices, origin="dell", main="dell")
    -> {"commands": [open_app(zenbook, Chrome), open_app(dell, Spotify)], "reason": None}
"""

import difflib
import re

from . import registry

LEAD = re.compile(r"^\W*(?:(?:hey|ok|okay)\s+)?(?:jarvis|jervis)\b[\s,.!?:-]*", re.I)
# "hello, open chrome": a greeting before a command is not chat. Only the command after
# it is parsed; a greeting alone ("hello there") still has no verb and goes to the brain.
GREETING = re.compile(r"^(?:(?:hello|hi|hey|yo|ok|okay|alright|good (?:morning|afternoon|evening))"
                      r"(?:\s+(?:jarvis|jervis|jarvas|drivers|travis|there))?[\s,.!?:-]+)+(?=\w)")
# "...go to youtube and yeah that's it, I just want to": closing chatter, not a clause.
TRAILER = re.compile(r"\s*,?\s*(?:and\s+)?(?:yeah\s+|ok(?:ay)?\s+)?that'?s\s+(?:it|all)\b(.*)\Z")
# Chatter after "that's it" is dropped, but a correction or another command is not chatter.
TRAILER_UNSAFE = re.compile(r"\b(?:don'?t|do not|no|wait|actually|cancel|never ?mind|stop|undo|"
                            r"open|close|play|pause|go to|launch|start|quit|then)\b")
FILLER = re.compile(r"\b(?:(?:can|could|would|will) you|please|for me|go ahead and|"
                    r"i want you to|i'?d like you to|just|quickly|real quick|right now|"
                    r"again|agian|agan)\b")
SPLIT = re.compile(r"\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+then\s+|\s+then\s+|"
                   r"\s+and\s+|\s+also\s+|\s+plus\s+")

# Verb phrase -> verb class; the longest phrase at the start of a clause wins.
VERBS = {
    "open": "open", "open up": "open", "launch": "open", "start": "open", "start up": "open",
    "run": "open", "fire up": "open", "pull up": "open", "bring up": "open", "boot up": "open",
    "load": "open", "load up": "open",
    "go to": "go", "go on": "go", "go": "go", "visit": "go", "navigate to": "go",
    "head to": "go", "take me to": "go",
    "play": "play", "play me": "play", "resume": "play", "put on": "play",
    "pause": "pause", "stop": "pause",
    "skip": "next", "next": "next", "previous": "previous", "go back": "back",
    "close": "close", "quit": "close", "exit": "close",
    "switch to": "focus", "focus": "focus", "focus on": "focus",
}
VERB_PHRASES = sorted(VERBS, key=lambda p: -len(p.split()))

# Built-in app names (normalized -> display name). The caller adds each device's real
# app list; whether an app is installed is a gate's job (U6), not the parser's.
APPS = {
    "spotify": "Spotify", "chrome": "Chrome", "google chrome": "Chrome",
    "firefox": "Firefox", "edge": "Microsoft Edge", "microsoft edge": "Microsoft Edge",
    "discord": "Discord", "steam": "Steam", "obsidian": "Obsidian", "slack": "Slack",
    "zoom": "Zoom", "notion": "Notion", "terminal": "Terminal", "kitty": "kitty",
    "visual studio code": "Visual Studio Code", "vs code": "Visual Studio Code",
    "vscode": "Visual Studio Code", "notepad": "Notepad", "calculator": "Calculator",
    "file explorer": "File Explorer", "explorer": "File Explorer", "settings": "Settings",
    "grok bot": "Grok Bot", "grokbot": "Grok Bot",
    "word": "Word", "microsoft word": "Word", "excel": "Excel", "microsoft excel": "Excel",
    "powerpoint": "PowerPoint", "outlook": "Outlook", "onenote": "OneNote", "teams": "Teams",
}
BROWSERS = {"Chrome", "Firefox", "Microsoft Edge"}
SITES = {
    "youtube": "https://www.youtube.com", "yt": "https://www.youtube.com",
    "google": "https://www.google.com", "gmail": "https://mail.google.com",
    "github": "https://github.com", "reddit": "https://www.reddit.com",
    "twitter": "https://x.com", "instagram": "https://www.instagram.com",
    "netflix": "https://www.netflix.com", "chatgpt": "https://chatgpt.com",
    "notion": "https://www.notion.so", "amazon": "https://www.amazon.com",
    "wikipedia": "https://www.wikipedia.org", "twitch": "https://www.twitch.tv",
    "linkedin": "https://www.linkedin.com",
    "youtube shorts": "https://www.youtube.com/shorts", "yt shorts": "https://www.youtube.com/shorts",
    "shorts": "https://www.youtube.com/shorts", "tiktok": "https://www.tiktok.com",
    "instagram reels": "https://www.instagram.com/reels", "reels": "https://www.instagram.com/reels",
}
DOMAIN = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(/\S*)?\Z")
FILE_EXTENSIONS = frozenset(
    "py md txt json html htm sh exe js ts jsx tsx css csv pdf doc docx xls xlsx ppt pptx png "
    "jpg jpeg gif webp zip rar tar gz tgz 7z log yaml yml toml ini cfg conf bat ps1 msi dmg iso "
    "mp3 mp4 wav flac mov mkv rs go cpp hpp java rb php sql db sqlite xml svg jar apk deb rpm "
    "ipynb lua kt swift odt ods rtf epub".split())
# A link's path and query keep their case ("watch?v=dQw4w9WgXcQ"); only the rest is lowered.
URL_PATH = re.compile(r"\b((?:https?://)?(?:[a-z0-9-]+\.)+[a-z]{2,})(/\S*)", re.I)

ORIGIN_WORDS = {"here", "this", "this one", "this pc", "this computer", "this laptop",
                "this machine"}
DEVICE_NOISE = {"my", "the", "our", "pc", "laptop", "computer", "machine", "desktop", "box",
                "one", "os"}
OBJECT_LEAD = re.compile(r"^(?:the|my|a|an)\s+")
OBJECT_TAIL = re.compile(r"\s+(?:app|application|program)$")
SITE_TAIL = re.compile(r"\s+(?:website|site|web ?page|\.com)$")

MEDIA_OBJECTS = {"", "it", "music", "the music", "song", "the song", "this song", "track",
                 "the track", "this track", "spotify", "playback"}
LIKED = re.compile(r"(?:(?:a|some|any|one)\s+(?:song|track|tune)s?\s+|something\s+|"
                   r"songs?\s+)?(?:from|off|in|of)?\s*(?:my|the)?\s*liked\s+"
                   r"(?:songs|tracks|music)(?:\s+(?:on|in)\s+spotify)?\Z")
VOLUME = (
    (re.compile(r"(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(\d{1,3})(?:\s*(?:percent|%))?\Z"),
     lambda m: m.group(1)),
    (re.compile(r"(?:turn\s+)?(?:the\s+)?volume\s+(up|down)\Z"), lambda m: m.group(1)),
    (re.compile(r"turn\s+(?:it|the\s+volume|the\s+music|the\s+sound)\s+(up|down)\Z"),
     lambda m: m.group(1)),
    (re.compile(r"(mute|unmute)(?:\s+(?:the\s+)?(?:volume|sound|audio|it|music))?\Z"),
     lambda m: m.group(1)),
    (re.compile(r"turn\s+(up|down)\s+(?:the\s+)?(?:volume|sound|music)\Z"), lambda m: m.group(1)),
    (re.compile(r"(raise|increase|lower|decrease|reduce)\s+(?:the\s+)?(?:volume|sound)\Z"),
     lambda m: "up" if m.group(1) in ("raise", "increase") else "down"),
)

# Scrolling, read before the verb table so "start scrolling" isn't "open scrolling".
# "in the agent browser" anywhere moves pages and scrolling to JARVIS's own browser
# (agent_browser.py, shown live in the console) instead of David's.
AGENT_BROWSER = re.compile(r"(?:\b(?:in|on|with|using|via|through)\s+)?(?:\b(?:the|your|my|"
                           r"jarvis'?s?)\s+)?\bagent(?:'s)?\s*browser\b")
CLOSE_AGENT = re.compile(r"(?:close|quit|shut(?:\s+down)?|exit)\Z")
FEED = r"(?:\s+(?:through\s+)?(?:it|them|this|that|now|(?:the\s+)?(?:shorts|videos|reels|feed|page)))?"
SCROLL = (
    (re.compile(r"(?:stop|quit|end|cancel|pause)\s+(?:the\s+)?(?:auto\s*)?scrolling" + FEED + r"\Z"),
     lambda m: {"mode": "stop"}),
    (re.compile(r"(?:(?:start|begin|keep|continue|go\s+on|carry\s+on)\s+(?:auto\s*)?scrolling|"
                r"auto\s*scroll(?:ing)?|scroll\s+(?:automatically|nonstop|continuously))"
                r"(?:\s+(down|up))?" + FEED + r"\Z"),
     lambda m: {"mode": "auto", "direction": m.group(1) or "down"}),
    (re.compile(r"scroll(?:\s+(down|up))?(?:\s+(?:a\s+(?:bit|little)|once|one))?" + FEED + r"\Z"),
     lambda m: {"mode": "once", "direction": m.group(1) or "down"}),
)
# "next short" moves one video, as the arrow key does on Shorts, TikTok and Reels.
NEXT_VIDEO = re.compile(r"(?:short|video|reel|tiktok)s?\Z")


def _lower(text):
    """text.lower(), except the path of a link: video ids and the like are case-sensitive."""
    parts, end = [], 0
    for match in URL_PATH.finditer(text):
        parts += [text[end:match.start()].lower(), match.group(1).lower(), match.group(2)]
        end = match.end()
    return "".join(parts) + text[end:].lower()


def _spelled(text):
    """Typed or misheard scroll words ("scorllling") read as the real word."""
    def fix(match):
        word = match.group(0)
        if word in ("scroll", "scrolling", "scrolls"):
            return word
        for real, cutoff in (("scrolling", 0.8), ("scroll", 0.85)):
            if word != real and difflib.SequenceMatcher(None, word, real).ratio() >= cutoff:
                return real
        return word
    text = re.sub(r"\bsc[a-z]{3,9}\b", fix, text)

    def verb(word):
        # "aopen", "oepn", "opne": one stray or swapped letter in "open".
        if len(word) == 5 and any(word[:i] + word[i + 1:] == "open" for i in range(5)):
            return "open"
        if len(word) == 4 and sorted(word) == sorted("open") and _edits(word, "open") == 1:
            return "open"
        return word
    # A bare word, never a piece of a URL ("opens.org", "github.com/opens/repo").
    urlish = re.compile(r"[./?=:]")

    def bare(match):
        before, after = text[:match.start()].split(" "), text[match.end():].split(" ")
        near = before[-2:-1] + after[1:2]  # the neighbouring tokens
        return match.group(0) if any(urlish.search(t) for t in near) else verb(match.group(0))
    return re.sub(r"(?<![^\s,])[a-z]{4,5}(?![^\s,])", bare, text)


def _edits(a, b):
    """Optimal string alignment distance: insertions, deletions, substitutions and
    swaps of two neighbours ("psotify"), each counting one."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            row[j] = min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], prev2[j - 2] + 1)
        prev2, prev = prev, row
    return prev[-1]


def _typo(word, table, shortest, same_start=False):
    """The one value `word` is a one-edit typo of, among `table`'s single-word keys at
    least `shortest` long; None when nothing or more than one value is that close."""
    if len(word) < shortest or not word.isalpha():
        return None
    hits = {value for key, value in table.items()
            if " " not in key and len(key) >= shortest - 1 and key != word
            and (not same_start or key[0] == word[0]) and _edits(word, key) == 1}
    return hits.pop() if len(hits) == 1 and None not in hits else None


class Unsure(ValueError):
    """The parser can't be certain; the request goes to the interpreter."""


def parse_for(text, config, origin, apps=None):
    """parse() with devices and the main PC taken from jarvis.config.json."""
    main = (config.get("cluster") or {}).get("main_device") or origin
    return parse(text, config.get("devices", {}), origin, main, apps)


def parse(text, devices, origin, main=None, apps=None):
    """{"commands": [...], "reason": None} when certain; else {"commands": None, "reason"}.
    devices: {id: {"hostname", "aliases"}}; origin: where the request came from;
    main: the main/default PC; apps: optional {device: [app names]} to recognize."""
    try:
        if origin not in devices:
            raise Unsure(f"unknown origin device {origin!r}")
        commands = _parse(text, devices, origin, main or origin, apps or {})
        errors = registry.validate(commands, devices)
        if errors:
            raise Unsure(errors[0])
        return {"commands": commands, "reason": None}
    except Unsure as exc:
        return {"commands": None, "reason": str(exc)}


def _parse(text, devices, origin, main, apps):
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise Unsure("empty or too long")
    text = _lower(LEAD.sub("", text.replace("’", "'").replace("‘", "'")))
    text = GREETING.sub("", text.strip())
    text = re.sub(r"[?!.]+$", "", text.strip())
    if (trailer := TRAILER.search(text)) and TRAILER_UNSAFE.search(trailer.group(1)):
        raise Unsure("a correction or command follows \"that's it\"")
    text = TRAILER.sub("", text)
    # Filler leaves its comma behind ("open chrome, please." -> "open chrome, "): strip it.
    text = re.sub(r"\s+", " ", FILLER.sub(" ", text)).strip(" ,;")
    # Whisper hears a spoken "open" as "opened" ("opened spotify here"): the request's first
    # word only. Mid-sentence "opened" and past-tense talk ("opened it yesterday") stay put.
    # "opened up chrome" is the same mishearing of "open up chrome".
    text = _spelled(re.sub(r"^opened\s+(?!(?:it|that|this|them|those|these|the|my|a|an|"
                           r"yesterday|today|earlier|already|last|before|and)\b)", "open ", text,
                           flags=re.I))
    if not text:
        raise Unsure("nothing left after the wake word")
    agent = bool(AGENT_BROWSER.search(text))
    if agent:
        text = re.sub(r"\s+", " ", AGENT_BROWSER.sub(" ", text)).strip(" ,;:")
        if CLOSE_AGENT.match(text):  # "close the agent browser"
            return [{"id": "c1", "intent": "browse", "device": origin,
                     "args": {"action": "close"}, "depends_on": []}]
    aliases = _aliases(devices, origin, main)
    catalog = dict(APPS)
    for names in apps.values():
        for name in names or ():
            catalog.setdefault(_norm(name), name)

    clauses, verb = [], None
    for raw in (part for piece in SPLIT.split(text) for part in _unglue(piece, aliases, catalog)):
        if not raw.strip(" ,;"):  # "open chrome, please, and spotify" leaves a hollow clause
            continue
        clause = _clause(raw.strip(" ,;"), aliases, catalog, verb)
        if clause["verb"] is None:  # "open spotify ... and chrome on my other pc"
            if verb is None:
                raise Unsure("no action verb")
            clause["verb"], clause["inherited"] = verb, True
        verb = clause["verb"]
        clauses.append(clause)

    for i, clause in enumerate(clauses):
        if clause.get("inherited") and clause["device"]:
            for prev in reversed(clauses[:i]):
                if prev["device"] is not None:
                    break
                prev["device"] = clause["device"]
                if not prev.get("inherited"):
                    break
    commands = []
    for clause in clauses:
        commands.append({**_command(clause, catalog), "device": clause["device"]})
    for prev, (clause, command) in zip(commands, list(zip(clauses, commands))[1:]):
        # "go to youtube on firefox and chrome": YouTube in both, or open Chrome too?
        if prev["intent"] == "open_url" and prev["args"].get("browser") and clause.get("inherited") \
                and command["intent"] == "open_app" and command["args"]["name"] in BROWSERS:
            raise Unsure("which browser gets the site?")
    commands = _merge(commands)
    if agent:
        commands = [_in_agent_browser(c) for c in commands]
    _assign_devices(commands, origin)
    for n, command in enumerate(commands, 1):
        command["id"] = f"c{n}"
    ids = {id(c): c["id"] for c in commands}
    for command in commands:
        command["depends_on"] = [ids[id(d)] for d in command.pop("after", [])]
    return [{k: c[k] for k in ("id", "intent", "device", "args", "depends_on")}
            for c in commands]


def _in_agent_browser(command):
    """open_url / scroll as the agent browser's browse; "stop scrolling" stays a scroll
    (it stops both). Anything else can't happen in a browser. Changed in place: later
    commands' "after" lists point at these same dicts."""
    args = command["args"]
    if command["intent"] == "open_url":
        command.update(intent="browse", args={"action": "open", "url": args["url"]})
        return command
    if command["intent"] == "scroll" and args.get("mode") != "stop":
        keep = {k: args[k] for k in ("direction", "mode", "every") if k in args}
        command.update(intent="browse", args={"action": "scroll", **keep})
        return command
    if command["intent"] == "scroll":
        return command
    raise Unsure(f"the agent browser can't {command['intent'].replace('_', ' ')}")


def _norm(name):
    return re.sub(r"\s+", " ", str(name).strip().lower())


def _aliases(devices, origin, main):
    table = {}

    def add(word, device):
        word = _norm(word)
        if word and table.setdefault(word, device) != device:
            table[word] = None  # two devices share this name: never resolve it

    for key, device in devices.items():
        add(key, key)
        add(device.get("hostname", ""), key)
        for alias in device.get("aliases", []):
            add(alias, key)
    for word in ("main", "default", "main default", "default main", "primary"):
        add(word, main)
    for word in ORIGIN_WORDS:
        add(word, origin)
    if len(devices) == 2:
        other = next(k for k in devices if k != origin)
        for word in ("other", "the other one", "other one", "other pc", "other laptop"):
            add(word, other)
    return table


def _device(phrase, aliases, typos=True):
    """The one device a phrase names ("my dell windows 11 pc" -> dell), else None."""
    words = phrase.split()
    hits, i, typed = set(), 0, 0
    while i < len(words):
        for j in range(len(words), i, -1):
            gram = " ".join(words[i:j])
            if gram in aliases:
                hits.add(aliases[gram])
                i = j
                break
        else:
            if words[i] in DEVICE_NOISE or words[i].isdigit():
                i += 1
                continue
            # "deoll", "arhclinux": one edit from exactly one device's name, same first letter.
            if typos and words[i] not in NOT_DEVICES and (found := _typo(words[i], aliases, 5, same_start=True)):
                hits.add(found)
                typed += 1
                if typed > 1:  # "dellx dellx dellx": junk, not a mishearing
                    return None
                i += 1
                continue
            return None
    return hits.pop() if len(hits) == 1 and None not in hits else None


RELATIVE = {"main", "default", "main default", "default main", "primary", "other",
            "the other one", "other one", "other pc", "other laptop"}


PLACE_NOUNS = {"laptop", "computer", "pc", "machine"}


def _place(words, aliases, shortest):
    """A place slot no alias spells exactly ("on my della"): the one device a single
    last word is a one-edit typo of. Anything else left in the slot is not a device
    ("my windows phone", "the arch wiki"), so nothing is guessed."""
    words = [w for w in words if w not in DEVICE_NOISE and w not in PLACE_NOUNS]
    if len(words) != 1 or words[0] in NOT_DEVICES:
        return None
    return _typo(words[0], aliases, shortest, same_start=True)


# Real words one edit from a device name: never a typo of it.
NOT_DEVICES = {"linus"}


def _known(obj, aliases, catalog):
    """`obj` names one app or site, optionally "on <device>": safe to end a clause on."""
    words = obj.split()
    if "on" in words:
        cut = len(words) - 1 - words[::-1].index("on")
        if not _device(" ".join(words[cut + 1:]), aliases):
            return False
        obj = " ".join(words[:cut])
    try:
        return bool(obj) and bool(_app(obj, catalog) or _site(obj))
    except Unsure:
        return False


def _unglue(text, aliases, catalog):
    """Split clauses run together without "and": "open chrome go to youtube" ->
    ["open chrome", "go to youtube"]. Only after a verb whose object is a known app or
    site and before another verb or a scroll, so nothing ambiguous is ever cut."""
    words = text.strip(" ,;").split()
    verb, rest = _verb(words)
    if verb is None:
        return [text]
    lead = len(words) - len(rest)
    for k in range(1, len(rest)):
        tail = " ".join(rest[k:])
        if (_verb(rest[k:])[0] or any(p.match(tail) for p, _ in SCROLL)) \
                and _known(" ".join(rest[:k]), aliases, catalog):
            return [" ".join(words[:lead + k]), *_unglue(tail, aliases, catalog)]
    return [text]


def _verb(words):
    for phrase in VERB_PHRASES:
        size = len(phrase.split())
        if words[:size] == phrase.split():
            return VERBS[phrase], words[size:]
    return None, words


def _clause(text, aliases, catalog, inherited=None):
    """{"verb", "object", "device"} for one clause; device None when not named."""
    if not text:
        raise Unsure("empty clause")
    words, device = text.split(), None
    if words[0] == "on" and len(words) > 2:  # prefix: "on arch open chrome"
        for k in range(2, len(words)):
            if _verb(words[k:])[0] and (found := _device(" ".join(words[1:k]), aliases)):
                device, words = found, words[k:]
                break
        else:
            raise Unsure(f"can't tell which PC {text!r} means")
    for pattern, spec in SCROLL:
        if match := pattern.match(" ".join(words)):
            return {"verb": "scroll", "object": "", "device": device, "scroll": spec(match)}
    # The verb first, so the "on" in "go on youtube" or "focus on" isn't read as a place.
    verb, rest = _verb(words)
    head = " ".join(words[:words.index("on")] if "on" in words else words)
    if (verb is None and inherited is None and device is None
            and not any(p.match(head) for p, _ in VOLUME)):
        # No verb to act on: chat ("what's on my calendar"), not an unknown place.
        raise Unsure("no action verb")
    if device is None and "on" in rest:  # suffix: "open chrome on arch"
        cut = len(rest) - 1 - rest[::-1].index("on")
        tail = " ".join(rest[cut + 1:])
        found = _device(tail, aliases) if tail else None
        split = tail.split()
        if not split:
            raise Unsure("on which place?")
        if not found and split[0] in ("my", "the") and _norm(tail) not in catalog:
            found = _place(split[1:], aliases, 5)  # "on my della": clearly a place
        if not found and len(split) > 1 and catalog.get(split[0]) in BROWSERS:
            # "open yt on chrome qarch": a browser, then the place.
            place = _device(" ".join(split[1:]), aliases, typos=False) \
                or _place(split[1:], aliases, 7)
            if place:
                device, words = place, words[:len(words) - len(split) + 1]
                rest = rest[:len(rest) - len(split) + 1]
        if found:
            device, rest = found, rest[:cut]
            words = words[:len(words) - len(tail.split()) - 1]  # drop "on <device>"
        elif device is None and _norm(tail) not in catalog:  # "on spotify" stays in the object
            raise Unsure(f"unknown place {tail!r}")
    if device is None and len(rest) > 1 and rest[-1] == "here":  # "open spotify here"
        device, rest, words = aliases["here"], rest[:-1], words[:-1]
    if any(p.match(" ".join(words)) for p, _ in VOLUME):
        return {"verb": "volume", "object": " ".join(words), "device": device}
    return {"verb": verb, "object": " ".join(rest), "device": device}


def _app(obj, catalog, typo=True):
    name = OBJECT_TAIL.sub("", OBJECT_LEAD.sub("", obj))
    if name in catalog:
        return catalog[name]
    close = difflib.get_close_matches(name, list(catalog), n=2, cutoff=0.86)
    if len(close) == 1:
        return catalog[close[0]]
    if len(close) > 1 and catalog[close[0]] != catalog[close[1]]:
        raise Unsure(f"{obj!r} could be more than one app")
    if close:
        return catalog[close[0]]
    # "psotify": letters swapped. Fine for opening; never for closing the wrong app.
    return _typo(name, catalog, 6) if typo else None


def _site(obj):
    name = SITE_TAIL.sub("", OBJECT_LEAD.sub("", obj))
    name = re.sub(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+)\.com/?$", r"\1", name)
    if name in SITES:
        return SITES[name]
    match = DOMAIN.match(obj)
    if not match:
        return None
    # "open main.py", "open notes.txt", "run setup.exe": a file, not a site (audit
    # 2026-11-01: they became https://main.py...). With a scheme, www. or a path, a link.
    tld = match.group(1).rsplit(".", 1)[-1]
    if tld in FILE_EXTENSIONS and not match.group(2) and not re.match(r"(?:https?://|www\.)", obj):
        raise Unsure(f"is {match.group(1)} a file or a website?")
    return f"https://{match.group(1)}{match.group(2) or ''}"


def _command(clause, catalog):
    verb, obj = clause["verb"], clause["object"]
    if verb == "back":
        # "go back" alone could be a track, a browser page or an undo: only a named
        # track or video ("go back a song") is certain.
        what = re.sub(r"^(?:to\s+)?(?:the\s+)?(?:previous|last)?\s*", "", obj)
        if re.sub(r"^(?:a|one)\s+", "", what) in ("", "it"):
            raise Unsure("go back to what?")
        verb = "previous"
    if verb == "scroll":
        return {"intent": "scroll", "args": dict(clause["scroll"])}
    if verb in ("next", "previous") and NEXT_VIDEO.match(
            re.sub(r"^(?:to\s+)?(?:the\s+)?(?:next|previous|last)?\s*", "", obj)):
        return {"intent": "scroll", "args": {"mode": "once",
                                             "direction": "down" if verb == "next" else "up"}}
    if verb == "volume":
        for pattern, level in VOLUME:
            if match := pattern.match(obj):
                return {"intent": "volume", "args": {"level": level(match)}}
        raise Unsure(f"unclear volume request {obj!r}")
    if verb in ("open", "go"):
        if not obj:
            raise Unsure("open what?")
        # "go to youtube on chrome": a site in a named browser.
        if (m := re.match(r"(.+?)\s+(?:on|in|with|using)\s+(\S.*)\Z", obj)) \
                and _app(m.group(2), catalog) in BROWSERS and _site(m.group(1)) \
                and not _app(m.group(1), catalog):
            return {"intent": "open_url", "args": {"url": _site(m.group(1)),
                                                    "browser": _app(m.group(2), catalog)}}
        app, site = _app(obj, catalog), _site(obj)
        if app and site:
            raise Unsure(f"{obj!r} is both an app and a site")
        if site:
            return {"intent": "open_url", "args": {"url": site}}
        if app:
            return {"intent": "open_app", "args": {"name": app}}
        words = obj.split()
        if len(words) > 1 and len(words[-1]) <= 3 and any(ch.isdigit() for ch in words[-1]):
            # "sppotifyc 0p": a stray keystroke after the name.
            return _command({**clause, "object": " ".join(words[:-1])}, catalog)
        raise Unsure(f"don't know {obj!r}")
    if verb == "play":
        if LIKED.match(obj):
            return {"intent": "spotify_play", "args": {"target": "liked"}}
        if obj in MEDIA_OBJECTS:
            return {"intent": "media", "args": {"command": "play"}}
        raise Unsure(f"play what: {obj!r}")  # a specific song needs judgement
    if verb in ("pause", "next", "previous"):
        obj = re.sub(r"^(?:to\s+)?(?:the\s+)?(?:next|previous|last)\s+", "", obj)
        obj = re.sub(r"^(?:a|one)\s+", "", obj)
        if obj not in MEDIA_OBJECTS:
            raise Unsure(f"{verb} what: {obj!r}")
        return {"intent": "media", "args": {"command": verb}}
    if verb in ("close", "focus"):
        app = _app(obj, catalog, typo=False) if obj else None
        if not app:
            raise Unsure(f"{verb} what: {obj!r}")
        return {"intent": "close_app" if verb == "close" else "focus_window",
                "args": {"name": app}}
    raise Unsure("no action verb")


def _compatible(a, b):
    return a["device"] is None or b["device"] is None or a["device"] == b["device"]


def _merge(commands):
    """open <browser> + go to <site> -> open_url with that browser; open Spotify + play
    liked songs -> spotify_play (it opens Spotify itself); open <app> + play -> media
    play after the app opens."""
    out = []
    for command in commands:
        prev = out[-1] if out else None
        if prev and prev["intent"] == "open_url" and command["intent"] == "scroll" \
                and command["args"].get("mode") != "stop" and _compatible(prev, command):
            # "go to yt shorts and start scrolling": scroll the page just opened, once it is.
            from .observe import predicate_for
            target = (predicate_for(prev) or {}).get("text")
            args = {**command["args"], "url": prev["args"]["url"],
                    **({"target": target.strip(" /")} if target else {}),
                    **({"app": prev["args"]["browser"]} if prev["args"].get("browser") else {})}
            command = {**command, "args": args, "after": [prev],
                       "device": command["device"] or prev["device"]}
        if prev and prev["intent"] == "open_app" and _compatible(prev, command):
            device = prev["device"] or command["device"]
            if command["intent"] == "open_url" and prev["args"]["name"] in BROWSERS \
                    and "browser" not in command["args"]:
                out[-1] = {**command, "device": device,
                           "args": {**command["args"], "browser": prev["args"]["name"]}}
                continue
            if command["intent"] == "spotify_play" and prev["args"]["name"] == "Spotify":
                out[-1] = {**command, "device": device}
                continue
            if command["intent"] == "media" and command["args"]["command"] == "play":
                command = {**command, "after": [prev]}
                if command["device"] is None and prev["device"] is not None:
                    command["device"] = prev["device"]
        out.append(command)
    return out


def _assign_devices(commands, origin):
    """Explicit devices stay. A command without one inherits the previous command's
    device, or the origin when the request names no device at all. Otherwise it's
    ambiguous ("open spotify and chrome on arch": is Spotify on arch too?)."""
    named = any(c["device"] for c in commands)
    for n, command in enumerate(commands):
        if command["device"]:
            continue
        if n and commands[n - 1]["device"]:
            command["device"] = commands[n - 1]["device"]
        elif not named:
            command["device"] = origin
        else:
            raise Unsure("can't tell which PC every part is for")
    # A follow-up (media play after open_app) shares its app's device.
    for command in commands:
        for dep in command.get("after", []):
            if dep["device"] != command["device"]:
                raise Unsure("dependent commands on different PCs")
