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
FILLER = re.compile(r"\b(?:(?:can|could|would|will) you|please|for me|go ahead and|"
                    r"i want you to|i'?d like you to|just|quickly|real quick|right now)\b")
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
    "skip": "next", "next": "next", "previous": "previous", "go back": "previous",
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
}
DOMAIN = re.compile(r"(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(/\S*)?\Z")

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
)


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
    text = LEAD.sub("", text.replace("’", "'").replace("‘", "'")).lower()
    text = re.sub(r"[?!.]+$", "", text.strip())
    text = re.sub(r"\s+", " ", FILLER.sub(" ", text)).strip()
    if not text:
        raise Unsure("nothing left after the wake word")
    aliases = _aliases(devices, origin, main)
    catalog = dict(APPS)
    for names in apps.values():
        for name in names or ():
            catalog.setdefault(_norm(name), name)

    clauses, verb = [], None
    for raw in SPLIT.split(text):
        clause = _clause(raw.strip(), aliases, catalog)
        if clause["verb"] is None:  # "open spotify ... and chrome on my other pc"
            if verb is None:
                raise Unsure("no action verb")
            clause["verb"] = verb
        verb = clause["verb"]
        clauses.append(clause)

    commands = []
    for clause in clauses:
        commands.append({**_command(clause, catalog), "device": clause["device"]})
    commands = _merge(commands)
    _assign_devices(commands, origin)
    for n, command in enumerate(commands, 1):
        command["id"] = f"c{n}"
    ids = {id(c): c["id"] for c in commands}
    for command in commands:
        command["depends_on"] = [ids[id(d)] for d in command.pop("after", [])]
    return [{k: c[k] for k in ("id", "intent", "device", "args", "depends_on")}
            for c in commands]


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


def _device(phrase, aliases):
    """The one device a phrase names ("my dell windows 11 pc" -> dell), else None."""
    words = phrase.split()
    hits, i = set(), 0
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
            return None
    return hits.pop() if len(hits) == 1 and None not in hits else None


def _verb(words):
    for phrase in VERB_PHRASES:
        size = len(phrase.split())
        if words[:size] == phrase.split():
            return VERBS[phrase], words[size:]
    return None, words


def _clause(text, aliases, catalog):
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
    # The verb first, so the "on" in "go on youtube" or "focus on" isn't read as a place.
    verb, rest = _verb(words)
    if device is None and "on" in rest:  # suffix: "open chrome on arch"
        cut = len(rest) - 1 - rest[::-1].index("on")
        tail = " ".join(rest[cut + 1:])
        found = _device(tail, aliases) if tail else None
        if found:
            device, rest = found, rest[:cut]
            words = words[:len(words) - len(tail.split()) - 1]  # drop "on <device>"
        elif _norm(tail) not in catalog:  # "on spotify" stays part of the object
            raise Unsure(f"unknown place {tail!r}")
    if device is None and len(rest) > 1 and rest[-1] == "here":  # "open spotify here"
        device, rest, words = aliases["here"], rest[:-1], words[:-1]
    if any(p.match(" ".join(words)) for p, _ in VOLUME):
        return {"verb": "volume", "object": " ".join(words), "device": device}
    return {"verb": verb, "object": " ".join(rest), "device": device}


def _app(obj, catalog):
    name = OBJECT_TAIL.sub("", OBJECT_LEAD.sub("", obj))
    if name in catalog:
        return catalog[name]
    close = difflib.get_close_matches(name, list(catalog), n=2, cutoff=0.86)
    if len(close) == 1:
        return catalog[close[0]]
    if len(close) > 1 and catalog[close[0]] != catalog[close[1]]:
        raise Unsure(f"{obj!r} could be more than one app")
    return catalog[close[0]] if close else None


def _site(obj):
    name = SITE_TAIL.sub("", OBJECT_LEAD.sub("", obj))
    name = re.sub(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+)\.com/?$", r"\1", name)
    if name in SITES:
        return SITES[name]
    match = DOMAIN.match(obj)
    return f"https://{match.group(1)}{match.group(2) or ''}" if match else None


def _command(clause, catalog):
    verb, obj = clause["verb"], clause["object"]
    if verb == "volume":
        for pattern, level in VOLUME:
            if match := pattern.match(obj):
                return {"intent": "volume", "args": {"level": level(match)}}
        raise Unsure(f"unclear volume request {obj!r}")
    if verb in ("open", "go"):
        if not obj:
            raise Unsure("open what?")
        app, site = _app(obj, catalog), _site(obj)
        if app and site:
            raise Unsure(f"{obj!r} is both an app and a site")
        if site:
            return {"intent": "open_url", "args": {"url": site}}
        if app:
            return {"intent": "open_app", "args": {"name": app}}
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
        app = _app(obj, catalog) if obj else None
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
