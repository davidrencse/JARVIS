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
                    r"pls|plz|again|agian|agan)\b")
SPLIT = re.compile(r"(\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+then\s+|\s+then\s+|"
                   r"\s+(?:and\s+)?after that\s+|\s+and\s+|\s+also\s+|\s+plus\s+)")
# A URL or path ("reddit.com/r/again") is data: filler and joiner words inside it stay.
URL_TOKEN = re.compile(r"[^\s,;]*/[^\s,;]*")

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
    # Shorthand David types: each is one app and nothing else (no "ps": Photoshop or PowerShell).
    "calc": "Calculator", "ppt": "PowerPoint", "code": "Visual Studio Code",
    "vsc": "Visual Studio Code", "term": "Terminal", "ms word": "Word", "ms excel": "Excel",
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
    "gh": "https://github.com", "ig": "https://www.instagram.com",
    "insta": "https://www.instagram.com", "fb": "https://www.facebook.com",
    "facebook": "https://www.facebook.com",
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

SPOTIFY_HINT = re.compile(r"(?:^|\s+)(?:on|in|with|using)\s+spotify\Z")
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

# System intents (sysintents.py): whole-clause phrases, read before the verb table so "switch
# to workspace 3" isn't "focus workspace 3" and "what's the weather" isn't chat. Each spec
# returns (intent, args). Anything that doesn't fit one of these exactly stays unparsed.
NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20,
                "thirty": 30, "forty five": 45, "forty-five": 45, "sixty": 60, "half an": 0.5}
_NUM = r"(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|" \
       r"thirty|forty[ -]five|sixty|half an)"
_UNIT = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"
_BRIGHT = r"(?:the\s+)?(?:screen\s+|display\s+)?brightness"


def _minutes(number, unit):
    value = float(NUMBER_WORDS.get(number, number))
    value *= {"s": 1 / 60, "m": 1, "h": 60}[unit[0]]
    return f"{value:g}"


def _remind(number, unit, text=None):
    args = {"minutes": _minutes(number, unit)}
    return ("reminder", {**args, "text": text} if text else args)


def _clean(text):
    return re.sub(r"\s+", " ", text).strip(" ,;")


SYSTEM = (
    (re.compile(r"(?:set\s+)?" + _BRIGHT + r"\s+(?:to\s+)?(\d{1,3})(?:\s*(?:percent|%))?\Z"),
     lambda m: ("brightness", {"level": m.group(1)})),
    (re.compile(r"(?:set\s+)?(?:the\s+)?(?:screen\s+|display\s+)?brightness\s+(?:to\s+)?(?:half|"
                r"fifty)\Z"), lambda m: ("brightness", {"level": "50"})),
    (re.compile(r"(?:turn\s+|bring\s+)?" + _BRIGHT + r"\s+(up|down)\Z"),
     lambda m: ("brightness", {"level": m.group(1)})),
    (re.compile(r"(?:turn|bring)\s+(up|down)\s+" + _BRIGHT + r"\Z"),
     lambda m: ("brightness", {"level": m.group(1)})),
    (re.compile(r"(raise|increase|lower|decrease|reduce)\s+" + _BRIGHT + r"\Z"),
     lambda m: ("brightness", {"level": "up" if m.group(1) in ("raise", "increase") else "down"})),
    (re.compile(r"dim\s+(?:the\s+)?(?:screen|display)\Z"), lambda m: ("brightness", {"level": "down"})),
    (re.compile(r"brighten\s+(?:the\s+)?(?:screen|display)\Z"), lambda m: ("brightness", {"level": "up"})),
    (re.compile(r"make\s+(?:the\s+)?(?:screen|display)\s+(brighter|dimmer|darker)\Z"),
     lambda m: ("brightness", {"level": "up" if m.group(1) == "brighter" else "down"})),
    (re.compile(r"(?:take\s+(?:a\s+)?)?screen\s?shot\s+(?:of\s+)?(?:this|the\s+(?:active|current|"
                r"focused))\s+window\Z"), lambda m: ("screenshot", {"window": True})),
    (re.compile(r"(?:screen\s?shot|capture)\s+(?:the\s+)?(?:active|current|focused)\s+window\Z"),
     lambda m: ("screenshot", {"window": True})),
    (re.compile(r"(?:take\s+(?:a\s+)?)?screen\s?shot(?:\s+of\s+(?:my|the)\s+(?:screen|desktop))?\Z"),
     lambda m: ("screenshot", {})),
    (re.compile(r"(?:how\s+much\s+(?:battery|charge)(?:\s+(?:(?:do\s+i\s+have|have\s+i\s+got)(?:\s+left)?|"
                r"is\s+left|left))?|what(?:'s|\s+is)\s+(?:my|the)\s+battery(?:\s+(?:level|percentage|percent|"
                r"status))?(?:\s+at)?|"
                r"(?:check|show)\s+(?:my\s+)?battery(?:\s+level)?|battery\s+(?:level|status|"
                r"percentage)|(?:is|am)\s+(?:my\s+(?:laptop|battery)|it|i)\s+charging)\Z"),
     lambda m: ("system_status", {"what": "battery"})),
    (re.compile(r"(?:what\s+wi-?fi(?:\s+network)?\s+am\s+i\s+(?:on|connected\s+to)|"
                r"what(?:'s|\s+is)\s+(?:my|the)\s+wi-?fi(?:\s+(?:network|name|ssid))?|"
                r"which\s+wi-?fi(?:\s+network)?\s+am\s+i\s+(?:on|connected\s+to)|"
                r"am\s+i\s+connected\s+to\s+wi-?fi|is\s+(?:the\s+|my\s+)?wi-?fi\s+(?:on|connected|"
                r"working)|(?:what|which)\s+(?:wi-?fi\s+)?network\s+am\s+i\s+(?:on|connected\s+to)|"
                r"what(?:'s|\s+is)\s+my\s+(?:wi-?fi\s+)?network(?:\s+name)?)\Z"),
     lambda m: ("system_status", {"what": "wifi"})),
    (re.compile(r"(?:is\s+(?:the\s+|my\s+)?bluetooth\s+(?:on|off|enabled|active)|"
                r"(?:what(?:'s|\s+is)\s+)?(?:the\s+)?bluetooth\s+status)\Z"),
     lambda m: ("system_status", {"what": "bluetooth"})),
    (re.compile(r"(?:how\s+much\s+(?:free\s+)?(?:disk\s+|storage\s+|drive\s+)?space\s+(?:do\s+i\s+have|is(?:\s+(?:there|left|free))?|"
                r"have\s+i\s+got)(?:\s+left)?(?:\s+on\s+my\s+(?:disk|drive|laptop|pc))?|"
                r"how\s+much\s+(?:free\s+)?(?:disk|storage)\s+(?:space\s+)?(?:is\s+)?(?:left|free)|"
                r"(?:check|show)\s+(?:my\s+)?(?:free\s+)?(?:disk|storage)\s+space|"
                r"(?:free|disk)\s+(?:disk\s+)?space)\Z"),
     lambda m: ("system_status", {"what": "disk"})),
    (re.compile(r"(?:(?:what|which|how\s+many)\s+(?:apps|windows|programs|applications)\s+(?:do\s+i\s+have\s+|"
                r"are\s+)?(?:open|running)(?:\s+right\s+now)?|(?:list|show)\s+(?:my\s+)?open\s+"
                r"(?:windows|apps))\Z"), lambda m: ("system_status", {"what": "windows"})),
    (re.compile(r"(?:switch|go|move|jump)\s+to\s+work\s?space\s+(\d{1,2}|one|two|three|four|five|"
                r"six|seven|eight|nine|ten)\Z"),
     lambda m: ("workspace", {"n": str(NUMBER_WORDS.get(m.group(1), m.group(1)))})),
    (re.compile(r"lock\s+(?:my\s+|the\s+)?(?:screen|computer|laptop|pc|session)\Z"),
     lambda m: ("lock_screen", {})),
    (re.compile(r"remind\s+me\s+in\s+" + _NUM + r"\s*" + _UNIT + r"\s+to\s+(.+)\Z"),
     lambda m: _remind(m.group(1), m.group(2), _clean(m.group(3)))),
    (re.compile(r"remind\s+me\s+to\s+(.+?)\s+in\s+" + _NUM + r"\s*" + _UNIT + r"\Z"),
     lambda m: _remind(m.group(2), m.group(3), _clean(m.group(1)))),
    (re.compile(r"(?:(?:set|start|begin)\s+(?:a\s+)?(?:timer|alarm)\s+(?:for|of)|"
                r"(?:set|start)\s+(?:a\s+)?" + _NUM + r"[ -]?" + _UNIT + r"\s+timer)(?:\s+" + _NUM
                + r"\s*" + _UNIT + r")?\Z"),
     lambda m: _remind(*[g for g in m.groups() if g][:2])),
    (re.compile(r"(?:what(?:'s|\s+is)|how(?:'s|\s+is)|check|show\s+me|tell\s+me)?\s*(?:the\s+)?"
                r"weather(?:\s+forecast)?(?:\s+like)?(?:\s+(today|tomorrow))?"
                r"(?:\s+(?:in|for|at)\s+([a-z][a-z .'-]{1,58}?))?(?:\s+(today|tomorrow))?\Z"),
     lambda m: ("weather", {**({"city": m.group(2)} if m.group(2) else {}),
                            **({"day": m.group(1) or m.group(3)} if m.group(1) or m.group(3) else {})})),
)

# Bluetooth (sysintents.bluetooth). Device names are data: only matched against paired
# devices later. Words that mean a different connection are never read as a device.
_NOT_BT = r"(?!(?:to\s+)?(?:the\s+)?(?:wi-?fi|wifi|vpn|internet|network|server|ssh|wireless|it|that|this|them|" \
          r"me|you|jarvis|github|database|db|site|website|phone)\b)"
_DEVICE_NAME = r"(?:my\s+|the\s+)?([a-z0-9][a-z0-9 .'+_-]{0,59}?)(?:\s+(?:via\s+|over\s+|on\s+)?bluetooth)?"


def _bluetooth(action, name=None, **extra):
    return ("bluetooth", {"action": action, **({"name": name} if name else {}), **extra})


BLUETOOTH = (
    (re.compile(r"(?:turn|switch)\s+(on|off)\s+(?:the\s+|my\s+)?bluetooth\Z"),
     lambda m: _bluetooth(m.group(1))),
    (re.compile(r"(?:turn|switch)\s+(?:the\s+|my\s+)?bluetooth\s+(on|off)\Z"),
     lambda m: _bluetooth(m.group(1))),
    (re.compile(r"(enable|disable)\s+(?:the\s+|my\s+)?bluetooth\Z"),
     lambda m: _bluetooth("on" if m.group(1) == "enable" else "off")),
    (re.compile(r"(?:scan|search|look)\s+for\s+(?:nearby\s+|new\s+)?bluetooth\s+(?:devices|things)"
                r"(?:\s+(?:nearby|around(?:\s+me)?))?(\s+including\s+unnamed(?:\s+ones)?)?\Z"),
     lambda m: _bluetooth("scan", **({"unnamed": True} if m.group(1) else {}))),
    (re.compile(r"what\s+bluetooth\s+devices\s+are\s+(?:nearby|around(?:\s+me)?|available|close"
                r"(?:\s+by)?|in\s+range)(\s+including\s+unnamed(?:\s+ones)?)?\Z"),
     lambda m: _bluetooth("scan", **({"unnamed": True} if m.group(1) else {}))),
    (re.compile(r"find\s+(?:my\s+)?(headphones|headset|earbuds|earphones|airpods|speaker|keyboard|mouse)\Z"),
     lambda m: _bluetooth("scan", m.group(1))),
    (re.compile(r"what\s+bluetooth\s+devices\s+are\s+(?:connected|on)\Z"), lambda m: _bluetooth("connected")),
    (re.compile(r"(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:my\s+)?(?:paired\s+)?bluetooth\s+devices\Z"),
     lambda m: _bluetooth("list")),
    (re.compile(r"what\s+bluetooth\s+devices\s+(?:do\s+i\s+have|are\s+paired)\Z"), lambda m: _bluetooth("list")),
    (re.compile(r"(connect|disconnect|pair|forget|unpair)\s+(?:with\s+|from\s+|to\s+)?" + _NOT_BT + _DEVICE_NAME + r"\Z"),
     lambda m: _bluetooth({"unpair": "forget"}.get(m.group(1), m.group(1)), _clean(m.group(2)))),
    (re.compile(r"connect\s+to\s+" + _NOT_BT + _DEVICE_NAME + r"\Z"),
     lambda m: _bluetooth("connect", _clean(m.group(1)))),
)
SYSTEM = SYSTEM + BLUETOOTH

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


SHORTHAND = re.compile(r"(?<![\w/.:@-])(vol|prev|bright|nxt)(?![\w/.:@-])")
SHORT_WORDS = {"vol": "volume", "prev": "previous", "bright": "brightness", "nxt": "next"}
# Words that only mean one thing here. A one-edit slip of one reads as it ("volumn", "screnshot");
# the clause still has to match a whole-clause pattern, so this never widens what runs.
SPELLED_NOUNS = ("volume", "brightness", "screenshot", "bluetooth", "previous", "workspace")


def _shorthand(text):
    """"vol up", "prev song", "bright up" and slips of the words the system patterns key on."""
    text = SHORTHAND.sub(lambda m: SHORT_WORDS[m.group(1)], text)

    def noun(match):
        word = match.group(0)
        if word in SPELLED_NOUNS or len(word) < 6:
            return word
        close = [n for n in SPELLED_NOUNS if _edits(word, n) == 1]
        return close[0] if len(close) == 1 else word
    return re.sub(r"(?<![\w/.:@-])[a-z]{6,12}(?![\w/.:@-])", noun, text)


# Verbs a slip of the keys is read as: harmless ones only. Never close/quit/delete: a typo must
# not be what decides to end something (those go to the interpreter, which asks first).
SLIP_VERBS = ("open", "play", "pause", "launch", "resume", "skip")
SHORT_SLIPS = {"opn": "open", "oen": "open"}
REAL_WORDS = frozenset(("lunch", "pays", "plays", "skips", "opens", "paused", "paste"))


def _slips(word, verbs):
    """The verbs `word` is one slip of the keys from: an adjacent swap, a dropped letter (verbs
    of 5+ letters) or a doubled letter. Never a substitution, so "clone" isn't "close"."""
    hits = set()
    for real in verbs:
        if _edits(word, real) != 1:
            continue
        swapped = len(word) == len(real) and sorted(word) == sorted(real)
        dropped = len(word) == len(real) - 1 and len(real) >= 5
        doubled = len(word) == len(real) + 1 and any(a == b for a, b in zip(word, word[1:]))
        if swapped or dropped or doubled:
            hits.add(real)
    return hits


def _verb_slip(word):
    """"paly" -> play, "puase" -> pause, "lauch" -> launch, "opn" -> open: a slip of a harmless verb."""
    if word in VERBS or word in REAL_WORDS or not word.isalpha():
        return None
    if word in SHORT_SLIPS:
        return SHORT_SLIPS[word]
    if len(word) < 4:
        return None
    hits = _slips(word, SLIP_VERBS)
    return hits.pop() if len(hits) == 1 else None


# Verbs whose typo must never run anything: the request goes to the interpreter, which asks
# "close chrome?" rather than act on a guess (the parser itself never reads these slips).
RISKY_VERBS = ("close", "quit", "kill", "delete", "remove", "send", "order", "reply", "email",
               "connect", "disconnect", "pair", "forget", "uninstall", "shutdown", "restart",
               "reboot", "install", "cancel", "submit", "unpair")


def slipped_action(word):
    """The action verb a lead word looks like a slip of ("clsoe" -> close), else None. For
    routing only: a request that starts this way is an action asked for with a typo."""
    word = re.sub(r"[^a-z]", "", str(word).lower())
    if len(word) < 4 or word in VERBS or word in REAL_WORDS:
        return None
    hits = _slips(word, SLIP_VERBS + RISKY_VERBS)
    return hits.pop() if len(hits) == 1 else None


# Short replies that lean on the last turn ("do that again", "same on the dell", "and spotify
# too", "the other one"). Only the interpreter, which is shown the recent turns, can read them.
_FOLLOWUP = re.compile(
    r"^(?:(?:do|try|run|say|play|open|show|send|make)\s+)?(?:(?:it|that|this|them)\s+)?again\b|"
    r"^(?:do|try|run)\s+(?:it|that|this|the same)(?:\s+(?:thing|again|on|for|there|too))?\b|"
    r"^(?:same|and|now|also)\b|^(?:on|for|to)\s+the\s+other\b|^the\s+other\b|"
    r"^undo\b|^not that\b|\b(?:instead|too|as well)$")
_FOLLOWUP_WORDS = 8


def follows_up(text):
    """True for a short request that only makes sense next to the previous one."""
    words = _lower(LEAD.sub("", str(text).replace("\u2019", "'"))).strip(" .!?,")
    return 0 < len(words.split()) <= _FOLLOWUP_WORDS and bool(_FOLLOWUP.search(words))


def bare_device(text, config, origin):
    """The device id when `text` is only a device name ("the dell", "arch one", "on zen"): a
    reply to "which device?" and nothing else."""
    words = _lower(LEAD.sub("", str(text).replace("\u2019", "'"))).strip(" .!?,")
    if not words or len(words.split()) > 4 or origin not in config.get("devices", {}):
        return None
    words = re.sub(r"^(?:on|to|for|at|in|use|do it on)\s+", "", words)
    main = (config.get("cluster") or {}).get("main_device") or origin
    aliases = _aliases(config.get("devices", {}), origin, main)
    return _device(words, aliases)


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
    urls = []

    def stash(match):
        urls.append(match.group(0))
        return f"zurlz{len(urls) - 1}z"

    text = URL_TOKEN.sub(stash, text)
    if (trailer := TRAILER.search(text)) and TRAILER_UNSAFE.search(trailer.group(1)):
        raise Unsure("a correction or command follows \"that's it\"")
    text = TRAILER.sub("", text)
    # Filler leaves its comma behind ("open chrome, please." -> "open chrome, "): strip it.
    text = re.sub(r"\s+", " ", FILLER.sub(" ", text)).strip(" ,;")
    # Whisper hears a spoken "open" as "opened" ("opened spotify here"): the request's first
    # word only. Mid-sentence "opened" and past-tense talk ("opened it yesterday") stay put.
    # "opened up chrome" is the same mishearing of "open up chrome".
    text = _shorthand(_spelled(re.sub(r"^opened\s+(?!(?:it|that|this|them|those|these|the|my|a|an|"
                           r"yesterday|today|earlier|already|last|before|and)\b)", "open ", text,
                           flags=re.I)))
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

    parts = SPLIT.split(text)  # piece, separator, piece, ...
    todo = []
    for i in range(0, len(parts), 2):
        piece = re.sub(r"zurlz(\d+)z", lambda m: urls[int(m.group(1))], parts[i])
        then = bool(i) and re.search(r"then|after that", parts[i - 1]) is not None
        todo += [(raw, then and n == 0) for n, raw in enumerate(_unglue(piece, aliases, catalog))]
    clauses, verb = [], None
    for raw, then in todo:
        if not raw.strip(" ,;"):  # "open chrome, please, and spotify" leaves a hollow clause
            continue
        clause = _clause(raw.strip(" ,;"), aliases, catalog, verb)
        clause["then"] = then
        if clause["verb"] is None:  # "open spotify ... and chrome on my other pc"
            if verb is None or verb in ("scroll", "system"):  # neither has an object to carry over
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
        commands.append({**_command(clause, catalog), "device": clause["device"],
                         "then": clause["then"]})
    for prev, (clause, command) in zip(commands, list(zip(clauses, commands))[1:]):
        # "go to youtube on firefox and chrome": YouTube in both, or open Chrome too?
        if prev["intent"] == "open_url" and prev["args"].get("browser") and clause.get("inherited") \
                and command["intent"] == "open_app" and command["args"]["name"] in BROWSERS:
            raise Unsure("which browser gets the site?")
    commands = _merge(commands)
    if agent:
        commands = [_in_agent_browser(c) for c in commands]
    _assign_devices(commands, origin)
    for command in commands:  # after assignment: "... on the dell then what windows are open"
        if (command["intent"] == "system_status" and command["device"] != origin
                and command["args"].get("what") != "disk"):
            raise Unsure("another PC answers only disk space here")
    for i, command in enumerate(commands):
        # "close spotify then open spotify" must run in order; plain "and" only when both
        # touch the same app, or both the sound (media/volume: "mute and unmute" ends in
        # whatever finished last when parallel). Either way only on one device: PCs run
        # independently. Depends on the latest earlier command it shares a resource with.
        for prev in reversed(commands[:i]):
            if command["device"] != prev["device"]:
                continue
            adjacent = prev is commands[i - 1]
            if (command.get("then") and adjacent) or _shares(prev, command):
                if prev not in command.get("after", []):
                    command["after"] = [*command.get("after", []), prev]
                break
    for n, command in enumerate(commands, 1):
        command["id"] = f"c{n}"
    ids = {id(c): c["id"] for c in commands}
    for command in commands:
        command["depends_on"] = [ids[id(d)] for d in command.pop("after", [])]
    return [{k: c[k] for k in ("id", "intent", "device", "args", "depends_on")}
            for c in commands]  # "then" and "after" are working fields only


AUDIO = ("media", "volume")
ORDERED = ("screenshot", "workspace", "lock_screen")
APP_LIFECYCLE = ("open_app", "close_app")


def _shares(prev, command):
    """Whether two same-device commands touch the same thing: the same app, or both the
    sound (media/volume). Playing media after opening/closing an app counts too: the
    player is that app ("close spotify and play it")."""
    name, other = command["args"].get("name"), prev["args"].get("name")
    if command["intent"] in ORDERED:  # "open chrome and take a screenshot": after it, not beside it
        return True
    if name and name == other:
        return True
    if command["intent"] in AUDIO and prev["intent"] in AUDIO:
        return True
    return (command["intent"] == "media" and prev["intent"] in APP_LIFECYCLE) or \
        (prev["intent"] == "media" and command["intent"] in APP_LIFECYCLE)


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


class _Aliases(dict):
    """The alias table, plus `names`: only what a device is actually called (its id, hostname
    and aliases), not "here" or "the other one": abbreviations read against these alone."""
    names = {}


def _aliases(devices, origin, main):
    table = _Aliases()
    table.names = names = {}

    def add(word, device):
        word = _norm(word)
        if word and table.setdefault(word, device) != device:
            table[word] = None  # two devices share this name: never resolve it

    for key, device in devices.items():
        add(key, key)
        add(device.get("hostname", ""), key)
        for alias in device.get("aliases", []):
            add(alias, key)
        for name in (key, device.get("hostname", ""), *device.get("aliases", [])):
            name = _norm(name)
            if name and names.setdefault(name, key) != key:
                names[name] = None
    for word in ("main", "default", "main default", "default main", "primary"):
        add(word, main)
    for word in ORIGIN_WORDS:
        add(word, origin)
    if len(devices) == 2:
        other = next(k for k in devices if k != origin)
        for word in ("other", "the other one", "other one", "other pc", "other laptop"):
            add(word, other)
    return table


# Short words that are something else first: never an abbreviation of a device ("arc" is a browser).
NOT_ABBREVIATIONS = {"arc", "the", "pc", "my", "one", "new", "old", "all", "app", "web", "any",
                     "not", "now", "set", "get", "got"}


def _abbreviated(word, aliases, avoid=()):
    """"del" -> dell, "zen" -> zenbook, "win" -> windows laptop: a 3+ letter start of what exactly
    one device is called. Not when the word is also an app here, or starts two devices' names."""
    names = getattr(aliases, "names", {})
    if len(word) < 3 or not word.isalpha() or word in NOT_ABBREVIATIONS or word in avoid \
            or word in names:
        return None
    hits = {device for name, device in names.items()
            if name.split()[0] != word and name.split()[0].startswith(word)}
    return hits.pop() if len(hits) == 1 and None not in hits else None


def _compact(gram, aliases):
    """"dellg15" or "g 15" for "dell g15"; "archlnux" for "arch linux" with one slip: the
    names run together. Exact, or one edit from exactly one device's name (6+ letters)."""
    names = getattr(aliases, "names", {})
    squashed = {}
    for name, device in names.items():
        squashed.setdefault(name.replace(" ", ""), set()).add(device)
    gram = gram.replace(" ", "")
    if gram in squashed:
        found = squashed[gram]
    elif len(gram) >= 6 and gram.isalpha():
        found = {d for key, devices in squashed.items() if len(key) >= 5 and key[0] == gram[0]
                 and _edits(gram, key) == 1 for d in devices}
    else:
        return None
    return found.pop() if len(found) == 1 and None not in found else None


def _device(phrase, aliases, typos=True, avoid=()):
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
            if typos:  # "dellg15", "g 15", "arch lnux": names run together or slipped
                for j in range(min(len(words), i + 3), i, -1):
                    if (found := _compact(" ".join(words[i:j]), aliases)):
                        hits.add(found)
                        i = j
                        break
                else:
                    found = None
                if found:
                    continue
            if typos and (found := _abbreviated(words[i], aliases, avoid)):
                hits.add(found)
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
    if _verb(words)[0] is None and (fixed := _verb_slip(words[0])):
        words = [fixed, *words[1:]]  # "opn chrome", "paly drake": a harmless verb, one slip away
    for pattern, spec in SCROLL:
        if match := pattern.match(" ".join(words)):
            return {"verb": "scroll", "object": "", "device": device, "scroll": spec(match)}
    for pattern, spec in SYSTEM:
        if match := pattern.match(" ".join(words)):
            intent, args = spec(match)
            if args.get("name") and _device(args["name"], aliases):  # "connect to my arch linux"
                raise Unsure(f"{args['name']!r} is a computer here, not a bluetooth device")
            return {"verb": "system", "object": "", "device": device, "system": (intent, args)}
    # A status read naming a PC ("how much disk space is left on the dell", "how many windows
    # do I have open on this laptop") went to the brain (9.1 s 11-01, 5.7 s 11-07). Reads only:
    # this PC answers every status; another answers disk, the one read every OS has.
    if device is None and "on" in words[1:-1]:
        cut = len(words) - 1 - words[::-1].index("on")
        place = _device(" ".join(words[cut + 1:]), aliases, typos=False)
        for pattern, spec in SYSTEM if place else ():
            if match := pattern.match(" ".join(words[:cut])):
                intent, args = spec(match)
                if intent == "system_status" and (place == aliases.get("here")
                                                  or args.get("what") == "disk"):
                    return {"verb": "system", "object": "", "device": place,
                            "system": (intent, args)}
                break
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
        found = _device(tail, aliases, avoid=catalog) if tail else None
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
    if not typo:  # closing or focusing: an exact catalog hit only, never a near miss
        return None
    close = difflib.get_close_matches(name, list(catalog), n=2, cutoff=0.86)
    if len(close) == 1:
        return catalog[close[0]]
    if len(close) > 1 and catalog[close[0]] != catalog[close[1]]:
        raise Unsure(f"{obj!r} could be more than one app")
    if close:
        return catalog[close[0]]
    # "psotify": letters swapped. Fine for opening; never for closing the wrong app.
    return _typo(name, catalog, 6)


def _site(obj, typo=False):
    name = SITE_TAIL.sub("", OBJECT_LEAD.sub("", obj))
    name = re.sub(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+)\.com/?$", r"\1", name)
    if name in SITES and not obj.startswith("http://"):
        return SITES[name]
    if typo and name.isalpha() and len(name) >= 5:
        # "yotube", "gogle", "netflx": a one-edit slip of exactly one well-known site. Opening
        # a page is the mildest action there is; nothing here can close or send anything.
        close = {SITES[key] for key in SITES if " " not in key and len(key) >= 5
                 and key[0] == name[0] and _edits(name, key) == 1}
        if len(close) == 1:
            return close.pop()
    match = DOMAIN.match(obj)
    if not match:
        return None
    # "open main.py", "open notes.txt", "run setup.exe": a file, not a site (audit
    # 2026-11-01: they became https://main.py...). With a scheme, www. or a path, a link.
    tld = match.group(1).rsplit(".", 1)[-1]
    if tld in FILE_EXTENSIONS and not match.group(2) and not re.match(r"(?:https?://|www\.)", obj):
        raise Unsure(f"is {match.group(1)} a file or a website?")
    scheme = "http://" if obj.startswith("http://") else "https://"  # keep what was said
    return f"{scheme}{match.group(1)}{match.group(2) or ''}"


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
    if verb == "system":
        intent, args = clause["system"]
        return {"intent": intent, "args": dict(args)}
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
        app, site = _app(obj, catalog), _site(obj, typo=True)
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
        obj = SPOTIFY_HINT.sub("", obj)  # "play music on spotify": the player, not a place
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
        if command["intent"] == "system_status" and any(
                c["intent"] == "system_status" and c["args"] == command["args"]
                and c["device"] == command["device"] for c in out):
            continue  # "is wifi connected and what network am i on": one read answers both
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
                out[-1] = {**command, "device": device, "then": prev["then"],
                           "args": {**command["args"], "browser": prev["args"]["name"]}}
                continue
            if command["intent"] == "spotify_play" and prev["args"]["name"] == "Spotify":
                out[-1] = {**command, "device": device, "then": prev["then"]}
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
