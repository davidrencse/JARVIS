"""Knowledge engine: a wiki that writes itself from sources (Karpathy's LLM-wiki pattern).

Three roles, all plain scripts and algorithms; a model is optional and only phrases answers:

- Reader collects. raw/ holds every source exactly as it arrived (file, page, note,
  voice memo), read-only and hashed, plus a text extract. Nothing ever edits raw/.
- Librarian writes. It pulls claims out of a source, files each one into the wiki page
  it is about (TF-IDF routing) or into a new page named by the source's keyphrases
  (RAKE), reconfirms claims a new source repeats, keeps contradicting claims side by
  side instead of overwriting, and relinks related pages.
- Checker doubts. Nightly: raw files still match their hashes, contradictions across
  the whole wiki, claims with no fresh source for 30 days become unconfirmed, pages
  nothing links to, links to nothing.

Questions are answered from claims, and a good answer is filed back as a page, so the
wiki grows from use as well as from reading. Every claim line carries its sources and
the date it was last confirmed:

    - Review runs daily at 02:00. ^c3f2a1 — source: [[source-042]] · confirmed: 2026-10-18
"""

import copy
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from .platforms import NO_WINDOW
from .fsio import atomic_write
from .stemming import stem
from .locking import exclusive_lock

STALE_DAYS = 30
MAX_CLAIMS_PER_SOURCE = 80
MAX_NEW_PAGES_PER_SOURCE = 10
ROUTE_THRESHOLD = 0.22  # cosine of a claim to a page's centroid to file it there
TEXT_KINDS = {".md", ".markdown", ".txt", ".rst", ".csv", ".json", ".html", ".htm"}
AUDIO_KINDS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".opus", ".webm"}

STOP = frozenset("""
a about above after again against all also am an and any are aren't as at be because
been before being below between both but by can can't could did didn't do does doesn't
doing don't down during each few for from further had has have having he her here hers
him his how i if in into is isn't it it's its itself just let me more most my no nor not
of off on once only or other our ours out over own same she should so some such than that
the their them then there these they this those through to too under until up very was
we were what when where which while who whom why will with would you your yours via per
etc one two use used using uses get gets got make makes made may might must shall new now
like see also each every many much well way ways thing things need needs want wants still
yet already even just really
""".split())
NEGATION = frozenset("not never no cannot can't won't isn't doesn't don't aren't wasn't "
                     "weren't without nothing none".split())
FREQUENCY = {"hourly": "hourly", "daily": "daily", "nightly": "daily", "everyday": "daily",
             "weekly": "weekly", "fortnightly": "biweekly", "biweekly": "biweekly",
             "monthly": "monthly", "quarterly": "quarterly", "yearly": "yearly",
             "annually": "yearly", "annual": "yearly"}
EVERY = {"hour": "hourly", "day": "daily", "night": "daily", "week": "weekly",
         "month": "monthly", "quarter": "quarterly", "year": "yearly"}
SWITCH = {"enabled": "on", "disabled": "off", "enable": "on", "disable": "off",
          "true": "on", "false": "off", "allowed": "on", "blocked": "off",
          "forbidden": "off", "required": "on", "optional": "off", "online": "on",
          "offline": "off"}
# unit word -> (dimension, factor to a base unit), so "90 s" and "1.5 min" agree
UNITS = {"%": ("pct", 1), "percent": ("pct", 1), "ms": ("time", 0.001), "s": ("time", 1),
         "sec": ("time", 1), "secs": ("time", 1), "second": ("time", 1),
         "seconds": ("time", 1), "min": ("time", 60), "mins": ("time", 60),
         "minute": ("time", 60), "minutes": ("time", 60), "h": ("time", 3600),
         "hr": ("time", 3600), "hrs": ("time", 3600), "hour": ("time", 3600),
         "hours": ("time", 3600), "day": ("time", 86400), "days": ("time", 86400),
         "week": ("time", 604800), "weeks": ("time", 604800), "kb": ("bytes", 1e3),
         "mb": ("bytes", 1e6), "gb": ("bytes", 1e9), "tb": ("bytes", 1e12),
         "kib": ("bytes", 1024), "mib": ("bytes", 1024 ** 2), "gib": ("bytes", 1024 ** 3),
         "usd": ("usd", 1), "$": ("usd", 1), "dollars": ("usd", 1), "fps": ("fps", 1),
         "hz": ("hz", 1), "khz": ("hz", 1e3), "mhz": ("hz", 1e6), "ghz": ("hz", 1e9),
         "mbps": ("bps", 1e6), "gbps": ("bps", 1e9), "tokens": ("tokens", 1),
         "chars": ("chars", 1), "characters": ("chars", 1), "x": ("ratio", 1)}
FACET_WORDS = (set(FREQUENCY) | set(SWITCH) | set(UNITS) | {"every", "am", "pm", "version", "release", "build"})
BOILERPLATE = re.compile(r"cookie|subscribe|sign up|all rights reserved|click here|log in to|"
                         r"privacy policy|terms of service|newsletter|share this", re.I)

CLAIM_LINE = re.compile(
    r"^- (?:\[!(?P<flag>conflict|unconfirmed|superseded)(?: (?P<with>[^\]]*))?\] )?(?P<text>.+?) "
    r"\^(?P<id>c[0-9a-f]{6}) — source: (?P<src>.*?) · confirmed: (?P<date>\d{4}-\d{2}-\d{2})\s*$")
LINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


# -- text ---------------------------------------------------------------------------------
def words(text):
    return [w.strip("'.-") for w in re.findall(r"[a-z0-9$%][a-z0-9'+#.%-]*", (text or "").lower())]


def content(text):
    return [stem(w) for w in words(text)
            if len(w) > 1 and w not in STOP and w not in NEGATION and not w[0].isdigit()]


@lru_cache(maxsize=65536)
def _terms(text):
    """content() memoized for retrieve: recall runs every talk turn over every claim, and
    claim and title text rarely change between turns. A tuple so no caller can mutate it."""
    return tuple(content(text))


def plain(text):
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[*_`~]{1,3}", "", text)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text):
    """Sentences and list items from prose or markdown; headings, code and tables dropped."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text or "", flags=re.S)
    text = re.sub(r"```.*?```", "\n", text, flags=re.S)
    out = []
    for block in re.split(r"\n\s*\n", text):
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines or all(l.lstrip().startswith(("#", "|", ">", "<!--")) for l in lines):
            continue
        items, current = [], []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("#", "|")):
                continue
            if re.match(r"([-*+]|\d+[.)])\s+", stripped):
                if current:
                    items.append(" ".join(current))
                current = [re.sub(r"^([-*+]|\d+[.)])\s+", "", stripped)]
            else:
                current.append(stripped.lstrip("> "))
        if current:
            items.append(" ".join(current))
        for item in items:
            item = plain(item)
            out += [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])", item) if s.strip()]
    return out


def keyphrases(sents, idf=None, limit=12):
    """RAKE: phrases are runs of content words; a word scores degree/frequency, a
    phrase the sum of its words, weighted by how often it recurs and how rare it is."""
    phrases, freq, degree = Counter(), Counter(), Counter()
    surface = {}
    for sentence in sents:
        for clause in re.split(r"[,;:()\[\]\"—–]|\s-\s", sentence.lower()):
            run = []
            for w in re.findall(r"[a-z][a-z0-9+#-]*", clause) + ["."]:
                if w in STOP or w in NEGATION or w in FACET_WORDS or len(w) < 3 or w == ".":
                    if 1 <= len(run) <= 3:
                        key = tuple(stem(x) for x in run)
                        phrases[key] += 1
                        surface.setdefault(key, " ".join(run))
                        for x in key:
                            freq[x] += 1
                            degree[x] += len(key)
                    run = []
                else:
                    run.append(w)
    scored = []
    for key, count in phrases.items():
        if count < 2 and len(key) == 1:
            continue
        rarity = sum(idf[x] if idf else 1.0 for x in key) / len(key)
        score = sum(degree[x] / freq[x] for x in key) * math.log1p(count) * rarity
        scored.append((score, key))
    scored.sort(reverse=True)
    return [(surface[k], k, round(s, 3)) for s, k in scored[:limit]]


GENERIC = frozenset(stem(w) for w in """
system data file files time way part case result results value values number work works
step steps test tests run runs item items user users page pages section note notes point
example examples thing default current first last next same different other change changes
fails remains uncertain explicit separate install date raw local remote check later live true
group person people body experience someone old participants social mental mind minds lecture
study project function thing others way part
false single multiple available possible specific general simple real full main
""".split())


# A word is noun-like when determiners and prepositions come before it or a verb after it.
BEFORE_NOUN = frozenset("the a an this that these those each every its their his her our your "
                        "my of for in on with by from per no any some all".split())
AFTER_NOUN = frozenset("is are was were has have had uses runs can will should must does did "
                       "may might stays keeps holds".split())


def topics(claims, idf=None, limit=MAX_NEW_PAGES_PER_SOURCE):
    """Greedy topic cover. A topic is a word or two-word term that several claims share
    and the rest of the wiki rarely uses; take the best, give it every claim that
    mentions it, repeat on what's left. Returns [(name, [claims])]."""
    rows, surface, nouny, seen = [], defaultdict(Counter), Counter(), Counter()
    for claim in claims:
        stems, raw = [], re.findall(r"[a-z][a-z0-9+#-]*", claim.lower())
        for i, w in enumerate(raw):
            if w in STOP or w in NEGATION or w in FACET_WORDS or len(w) < 3:
                stems.append(None)
                continue
            st = stem(w)
            surface[(st,)][w] += 1
            seen[st] += 1
            after = raw[i + 1] if i + 1 < len(raw) else ""
            verbish = after in AFTER_NOUN or (re.fullmatch(r"[a-z]{3,}(?<!s)s|[a-z]{3,}ed", after)
                                               and after not in STOP)
            # a determiner before it, or a verb after it ("Kokoro speaks", not "Later the")
            nouny[st] += bool((i and raw[i - 1] in BEFORE_NOUN) or verbish)
            stems.append(st)
        keys = {(x,) for x in stems if x and x not in GENERIC}
        for i, (a, b) in enumerate(zip(stems, stems[1:], strict=False)):
            if a and b and a != b:
                keys.add((a, b))
                surface[(a, b)][" ".join(re.findall(r"[a-z][a-z0-9+#-]*", claim.lower())[i:i + 2])] += 1
        rows.append((claim, keys))
    support = 3 if len(claims) >= 12 else 2
    out, left = [], rows
    while left and len(out) < limit:
        df = Counter(k for _, keys in left for k in keys)
        best, pick = 0.0, None
        for key, n in df.items():
            if n < support or (len(key) == 1 and len(claims) >= 10 and n > 0.6 * len(claims)):
                continue
            if nouny[key[-1]] < 0.5 * seen[key[-1]]:  # name topics after things, not actions
                continue
            if len(key) == 1 and re.search(r"(ly|ed|ing)$", surface[key].most_common(1)[0][0]):
                continue
            score = n * sum(idf[x] if idf else 1.0 for x in key) / len(key) * (1.4 if len(key) == 2 else 1.0)
            if score > best:
                best, pick = score, key
        if pick is None:
            break
        group = [c for c, keys in left if pick in keys]
        out.append((surface[pick].most_common(1)[0][0], group))
        left = [(c, keys) for c, keys in left if pick not in keys]
    return out


RESERVED = {"index"}  # generated pages a topic must never overwrite


def page_slug(slug):
    return f"{slug}-page" if slug in RESERVED else slug


def slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")[:60]


def claim_id(text):
    return "c" + hashlib.sha1(" ".join(words(text)).encode()).hexdigest()[:6]


# -- facets and the contradiction test -----------------------------------------------------
@lru_cache(maxsize=20000)
def signature(text):
    """(subject stems, facets) of a claim. Facets are the parts two claims about the same
    subject can disagree on: frequency, quantities with units, clock times, on/off, and
    negation. The subject is everything else that carries meaning."""
    low = text.lower()
    tokens = words(low)
    freq = {FREQUENCY[w] for w in tokens if w in FREQUENCY}
    freq |= {EVERY[m] for m in re.findall(r"\bevery\s+(hour|day|night|week|month|quarter|year)\b", low)}
    nums = defaultdict(set)
    for dollar, value, unit in re.findall(r"(\$)?(\d+(?:\.\d+)?)\s*(%|[a-z]+)?", low):
        unit = "$" if dollar else unit
        if unit in UNITS:
            dim, factor = UNITS[unit]
            nums[dim].add(round(float(value) * factor, 6))
    for label, value in re.findall(r"\b(port|version|release|build|vlan|channel|python)\s+v?#?(\d+(?:\.\d+)*)", low):
        nums[label].add(value)  # labelled identifiers compare as text: 3.10 is not 3.1
    times = set()
    for h, m, ampm in re.findall(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", low):
        times.add((int(h) % 12 + (12 if ampm == "pm" else 0) if ampm else int(h)) * 60 + int(m))
    for h, ampm in re.findall(r"\b(\d{1,2})\s*(am|pm)\b", low):
        times.add((int(h) % 12 + (12 if ampm == "pm" else 0)) * 60)
    switch = {SWITCH[w] for w in tokens if w in SWITCH}
    negated = any(w in NEGATION or w.endswith("n't") for w in tokens)
    subject = frozenset(s for w in tokens if w not in FACET_WORDS and w not in EVERY
                        and w not in STOP and w not in NEGATION and len(w) > 1
                        and not w[0].isdigit() and not w.startswith("$") for s in [stem(w)])
    return subject, (frozenset(freq), {k: frozenset(v) for k, v in nums.items()},
                     frozenset(times), frozenset(switch), negated)


def _show(dim, values):
    def one(v):
        if isinstance(v, str):
            return v
        if dim == "bytes":
            unit = next(((f, u) for f, u in ((1e12, "TB"), (1e9, "GB"), (1e6, "MB"), (1e3, "KB")) if v >= f), (1, "B"))
            return f"{v / unit[0]:g} {unit[1]}"
        if dim == "time":
            unit = next(((f, u) for f, u in ((86400, "d"), (3600, "h"), (60, "min")) if v >= f and v % f == 0), (1, "s"))
            return f"{v / unit[0]:g} {unit[1]}"
        return f"{v:g}{'%' if dim == 'pct' else ''}"
    return "/".join(one(v) for v in sorted(values, key=lambda v: (isinstance(v, str), v)))


def relation(a, b):
    """("duplicate", None), ("conflict", why) or None for two claim texts."""
    (sa, (fa, na, ta, wa, nega)), (sb, (fb, nb, tb, wb, negb)) = signature(a), signature(b)
    shared = sa & sb
    if len(shared) < 2:
        return None
    overlap = len(shared) / len(sa | sb)
    # Same subject: most words in common, or three or more shared words covering most of
    # the shorter claim ("runs daily at 02:00 and checks every page" / "runs weekly").
    if overlap < 0.5 and not (len(shared) >= 3 and len(shared) / min(len(sa), len(sb)) >= 0.6):
        return None
    if fa and fb and not fa & fb:
        return "conflict", f"{'/'.join(sorted(fa))} ≠ {'/'.join(sorted(fb))}"
    for dim in set(na) & set(nb):
        if not any(x == y if isinstance(x, str) or isinstance(y, str) else
                   abs(x - y) <= 0.01 * max(abs(x), abs(y), 1e-9) for x in na[dim] for y in nb[dim]):
            return "conflict", f"{dim} {_show(dim, na[dim])} ≠ {_show(dim, nb[dim])}"
    if ta and tb and not ta & tb:
        clock = lambda ts: "/".join(f"{t // 60:02d}:{t % 60:02d}" for t in sorted(ts))
        return "conflict", f"{clock(ta)} ≠ {clock(tb)}"
    if wa and wb and not wa & wb:
        return "conflict", f"{'/'.join(sorted(wa))} ≠ {'/'.join(sorted(wb))}"
    if nega != negb and overlap >= 0.7:
        return "conflict", "one says not"
    if overlap >= 0.75 and nega == negb and (fa, ta, wa) == (fb, tb, wb) and na == nb:
        return "duplicate", None
    return None


# -- vectors ------------------------------------------------------------------------------
class Idf(dict):
    """Inverse document frequencies; a term no document has is as rare as it gets."""
    unseen = 1.0

    def __missing__(self, term):
        return self.unseen


def idf_table(docs):
    n = len(docs) or 1
    df = Counter(t for d in docs for t in set(d))
    table = Idf({t: math.log((1 + n) / (1 + c)) + 1 for t, c in df.items()})
    table.unseen = math.log(1 + n) + 1
    return table


def vector(tokens, idf):
    tf = Counter(tokens)
    vec = {t: (1 + math.log(c)) * idf[t] for t, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def extract_claims(text, idf=None, limit=MAX_CLAIMS_PER_SOURCE):
    """Declarative, self-contained sentences worth keeping, best first (then in order)."""
    picked = []
    for i, s in enumerate(sentences(text)):
        n = len(s.split())
        terms = content(s)
        if not 5 <= n <= 60 or s.endswith("?") or len(terms) < 3 or BOILERPLATE.search(s):
            continue
        if sum(ch.isalpha() for ch in s) < 0.6 * len(s) or s.endswith(":"):
            continue
        freq, nums, times, switch, _ = signature(s)[1]
        info = sum(idf[t] if idf else 1.0 for t in set(terms)) / math.sqrt(len(terms))
        bonus = 0.6 * bool(freq or nums or times or switch)  # checkable specifics
        picked.append((info + bonus + 0.3 * bool(re.search(r"\d", s)), i, s if s[-1] in ".!" else s + "."))
    best = sorted(picked, reverse=True)[:limit]
    seen, out = set(), []
    for _, _, s in sorted(best, key=lambda x: x[1]):
        cid = claim_id(s)
        if cid not in seen:
            seen.add(cid)
            out.append(s)
    return out


# -- pages --------------------------------------------------------------------------------
def _front(text):
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.S)
    if not match:
        return {}, text
    meta, raw = {}, {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
        elif value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except ValueError:  # hand-edited quoting
                value = value.strip('"')
        meta[key] = value
        raw[key] = line
    meta["_raw"] = raw
    return meta, text[match.end():]


OWN_KEYS = {"title", "type", "status", "confirmed", "sources", "question"}


def parse_page(slug, text):
    meta, body = _front(text)
    page = {"slug": slug, "title": meta.get("title") or slug.replace("-", " ").title(),
            "type": meta.get("type", "page"), "question": meta.get("question", ""),
            "claims": [], "related": [], "answer": "", "extra": "", "notes": [],
            # front-matter keys someone else added (tags, aliases...) ride along untouched
            "front": [l for k, l in meta.get("_raw", {}).items() if k and k not in OWN_KEYS]}
    section, extra = None, []
    for line in body.splitlines():
        if line.startswith("# ") and section is None:
            continue
        if line.startswith("## "):
            section = line[3:].strip().lower()
            if section not in ("claims", "related", "answer", "notes"):
                extra.append(line)
            continue
        if section == "claims":
            match = CLAIM_LINE.match(line)
            try:
                valid = bool(match) and bool(date.fromisoformat(match["date"]))
            except ValueError:
                valid = False
            if valid:
                page["claims"].append({
                    "id": match["id"], "text": match["text"], "date": match["date"],
                    "sources": LINK.findall(match["src"]), "flag": match["flag"],
                    "with": [w for w in (match["with"] or "").split() if w]})
            elif line.strip():
                page["notes"].append(line)  # hand-written lines survive, under Notes
        elif section == "related":
            page["related"] += LINK.findall(line)
            if LINK.sub("", line).strip(" ·"):
                page["notes"].append(line)
        elif section == "notes":
            if line.strip():
                page["notes"].append(line)
        elif section == "answer":
            page["answer"] += line + "\n"
        else:
            extra.append(line)
    page["answer"] = page["answer"].strip()
    page["extra"] = "\n".join(extra).strip()
    return page


def claim_line(claim):
    flag = ""
    if claim.get("flag") in ("conflict", "superseded"):
        flag = f"[!{claim['flag']} {' '.join(claim.get('with') or [])}] ".replace(" ]", "]")
    elif claim.get("flag") == "unconfirmed":
        flag = "[!unconfirmed] "
    sources = ", ".join(f"[[{s}]]" for s in claim["sources"]) or "none"
    return f"- {flag}{claim['text']} ^{claim['id']} — source: {sources} · confirmed: {claim['date']}"


def page_status(page, today, stale_days=STALE_DAYS):
    if any(c.get("flag") == "conflict" for c in page["claims"]):
        return "review"
    confirmed = page_confirmed(page)
    if confirmed and (today - date.fromisoformat(confirmed)).days > stale_days:
        return "unconfirmed"
    return "confirmed"


def page_confirmed(page):
    return max((c["date"] for c in page["claims"]), default="")


def render_page(page, today, stale_days=STALE_DAYS):
    sources = sorted({s for c in page["claims"] for s in c["sources"]})
    lines = ["---", f"title: {json.dumps(page['title'], ensure_ascii=False)}",
             f"type: {page['type']}", f"status: {page_status(page, today, stale_days)}",
             f"confirmed: {page_confirmed(page)}", f"sources: [{', '.join(sources)}]",
             *page.get("front", [])]
    if page.get("question"):
        lines.append(f"question: {json.dumps(page['question'], ensure_ascii=False)}")
    lines += ["---", "", f"# {page['title']}", ""]
    if page.get("answer"):
        lines += ["## Answer", page["answer"], ""]
    lines += ["## Claims", *map(claim_line, page["claims"]), ""]
    if page["related"]:
        lines += ["## Related", " · ".join(f"[[{r}]]" for r in page["related"]), ""]
    if page.get("notes"):
        lines += ["## Notes", *page["notes"], ""]
    if page.get("extra"):
        lines += [page["extra"], ""]
    return "\n".join(lines).rstrip() + "\n"


# -- the engine ---------------------------------------------------------------------------
class KnowledgeBase:
    def __init__(self, root, state=None, settings=None, on_event=None, today=None):
        self.root = Path(root).expanduser().resolve()
        self.raw, self.wiki = self.root / "raw", self.root / "wiki"
        for folder in (self.raw, self.wiki, self.root / "checks"):
            folder.mkdir(parents=True, exist_ok=True)
        self.settings = settings or {}
        self.stale_days = int(self.settings.get("stale_days", STALE_DAYS))
        self.state, self.on_event, self._today = state, on_event, today
        self._local = threading.RLock()
        self._depth = 0
        self._sha = {}  # raw file -> (mtime, size, sha256): snapshot() runs on every refresh
        self._hashes = (None, {})  # (raw/ listing stamp, {sha256: meta file}) for _by_hash

    def today(self):
        return self._today or date.today()

    # ---- plumbing ----------------------------------------------------------------------
    @contextmanager
    def lock(self):
        """One writer at a time, across threads and processes (CLI, console, timer)."""
        with self._local:
            self._depth += 1
            try:
                if self._depth > 1:
                    yield
                    return
                deadline = time.time() + 60
                while True:
                    held = ExitStack()
                    try:
                        held.enter_context(exclusive_lock(self.root / ".lock"))
                        break
                    except ValueError:  # only the acquire; errors in the body are the caller's
                        held.close()
                        if time.time() > deadline:
                            raise ValueError("The knowledge base is busy in another process.") from None
                        time.sleep(0.2)
                with held:
                    yield
            finally:
                self._depth -= 1

    def _engine(self, update=None):
        path = self.root / "engine.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"next_source": 1, "next_answer": 1}
        if update:
            data.update(update)
            self._atomic(path, json.dumps(data, indent=2))
        return data

    @staticmethod
    def _atomic(path, text):
        atomic_write(path, text)  # its own temporary name: two syncs at once never collide

    def event(self, bot, action, target="", message="", **data):
        record = {"at": datetime.now().isoformat(timespec="seconds"), "bot": bot,
                  "action": action, "target": target, "message": message, **data}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with _LOG_LOCK, open(self.root / "log.jsonl", "a", encoding="utf-8") as log:
            log.write(line)  # one write per record, serialized across threads
        if self.on_event:
            self.on_event("kb." + action, record)
        elif self.state:
            from .events import emit
            emit(self.state, "kb." + action, **record)
        return record

    def bot(self, name, update=None):
        """Each bot's own memory: bots/<name>.json. The checker remembers your rulings on
        contradictions so it never raises them again; the librarian remembers merged
        topics so it files future claims where you put them."""
        folder = self.root / "bots"
        folder.mkdir(exist_ok=True)
        path = folder / f"{name}.json"

        def load():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        if not update:
            return load()
        with self.lock():  # read, change and write as one: a concurrent sync's update survives
            data = load()
            update(data)
            self._atomic(path, json.dumps(data, indent=2, ensure_ascii=False))
        return data

    def log(self, limit=80):
        path = self.root / "log.jsonl"
        if not path.is_file():
            return []
        size = path.stat().st_size
        with open(path, "rb") as stream:
            stream.seek(max(0, size - 400_000))
            lines = stream.read().decode("utf-8", "replace").splitlines()[1 if size > 400_000 else 0:]
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # ---- sources -----------------------------------------------------------------------
    def sources(self):
        metas = []
        for path in sorted(self.raw.glob("source-*.meta.json")):
            try:
                metas.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return metas

    def source(self, sid):
        path = self.raw / f"{sid}.meta.json"
        if not re.fullmatch(r"source-\d{3,}", sid or "") or not path.is_file():
            raise LookupError(f"Unknown source: {sid}")
        return json.loads(path.read_text(encoding="utf-8"))

    def source_text(self, sid):
        meta = self.source(sid)
        return (self.raw / meta["extract"]).read_text(encoding="utf-8", errors="replace")

    def _write_raw(self, path, data):
        path.write_bytes(data)
        os.chmod(path, 0o444)  # the reader never edits; neither does anyone else by accident

    # ---- reader ------------------------------------------------------------------------
    def ingest(self, item=None, text=None, title=None, digest=True, fetch=None, transcribe=None,
               page=None):
        """Reader: keep a source untouched in raw/, then hand it to the librarian. Fetching,
        PDF extraction and transcription happen before the lock, so a slow source never
        blocks a ruling or the nightly check. `page` files the whole source on one page of
        that name (an Obsidian note is already one topic) instead of splitting by topic."""
        if text is not None:
            if not text.strip():
                raise ValueError("Nothing to ingest.")
            data, ext, origin = text.encode("utf-8"), ".md", "typed"
        elif re.match(r"https?://", str(item or ""), re.I):
            from .vault import fetch_page
            fetched = (fetch or fetch_page)(str(item).strip())
            title = title or fetched.get("title") or fetched["url"]
            data = f"# {title}\n\nSource: <{fetched['url']}>\n\n{fetched['text']}\n".encode("utf-8")
            ext, origin = ".md", fetched["url"]
        else:
            path = Path(str(item or "")).expanduser()
            if not path.is_file():
                raise ValueError(f"No such file: {path}")
            ext, origin = path.suffix.lower() or ".txt", str(path.resolve())
            if ext not in TEXT_KINDS | AUDIO_KINDS | {".pdf"}:
                raise ValueError(f"Can't read {ext} files (text, markdown, html, pdf, audio).")
            data = path.read_bytes()
            title = title or path.stem.replace("-", " ").replace("_", " ")
        digest_hex = hashlib.sha256(data).hexdigest()
        known = self._by_hash(digest_hex)
        if known:
            return self._duplicate(known)
        stage = self.root / "inbox"
        stage.mkdir(exist_ok=True)
        staged = stage / f"stage-{digest_hex[:16]}{ext}"
        try:
            staged.write_bytes(data)
            extract = self._extract(staged, ext, data, transcribe)
        finally:
            staged.unlink(missing_ok=True)
        with self.lock():
            known = self._by_hash(digest_hex)  # another process may have filed it meanwhile
            if known:
                return self._duplicate(known)
            sid = f"source-{self._next_number('source'):03d}"
            own_text = ext in (".md", ".txt", ".markdown")
            first = next((l.lstrip("# ").strip() for l in extract.splitlines() if l.strip()), "")
            meta = {"id": sid, "title": (title or first or sid)[:120], "origin": origin,
                    "file": f"{sid}{ext}", "extract": f"{sid}{ext}" if own_text else f"{sid}.txt",
                    "sha256": digest_hex, "bytes": len(data), "added": self.today().isoformat(),
                    "kind": self._kind(ext)}
            if page:
                meta["page"] = page_slug(slugify(page))
            written = []
            try:
                for name, blob in ((meta["file"], data),
                                   *(() if own_text else ((meta["extract"], extract.encode("utf-8")),))):
                    self._write_raw(self.raw / name, blob)
                    written.append(self.raw / name)
                # The meta lands last: no meta, no source.
                atomic_write(self.raw / f"{sid}.meta.json", json.dumps(meta, indent=2), mode=0o444)
                self._remember_hash(meta)
            except BaseException:
                for path in written:
                    os.chmod(path, 0o644)
                    path.unlink(missing_ok=True)
                raise
            self.event("reader", "ingest", f"raw/{meta['file']}", "locked · read only · 0 mutations",
                       source=sid, title=meta["title"], bytes=len(data))
            result = {"source": meta, "duplicate": False}
            if digest:
                result["librarian"] = self.digest(sid)
            return result

    VAULT_SKIP = ("daily/", "templates/", "archive/", "attachments/")

    def sync_vault(self, vault, skip=VAULT_SKIP):
        """Reader: bring the Obsidian vault in as sources. Only notes that changed since the
        last sync are read again; an edited note becomes a new source, so the librarian
        reconfirms what still holds. Daily notes, templates and the archive stay out."""
        seen = self.bot("reader").get("vault", {})
        added, unchanged, failed = [], 0, []
        for note in vault.notes():
            rel = note["path"]
            if rel.startswith(skip) or not rel.endswith(".md"):
                continue
            path = vault.root / rel
            try:
                digest_hex = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            if seen.get(rel) == digest_hex:
                unchanged += 1
                continue
            try:
                result = self.ingest(path, title=note.get("title") or None, page=Path(rel).stem)
            except (ValueError, OSError) as exc:
                failed.append({"path": rel, "error": str(exc)})
                continue
            seen[rel] = digest_hex
            if not result["duplicate"]:
                added.append(rel)
        # Merged into what is on disk now, not a copy read at the start: another sync that
        # ran meanwhile keeps the notes it recorded.
        self.bot("reader", lambda m: m.update(vault={**m.get("vault", {}), **seen},
                                              vault_root=str(vault.root)))
        self.event("reader", "sync", f"vault/{vault.root.name}",
                   f"{len(added)} notes read · {unchanged} unchanged · raw/ untouched",
                   added=len(added), unchanged=unchanged, failed=failed[:20])
        return {"added": added, "unchanged": unchanged, "failed": failed}

    def _by_hash(self, digest_hex):
        """The source with these bytes, or None. Reading every meta file per ingest cost
        ~57k JSON reads on a first vault sync; the index is rebuilt only when raw/'s listing
        changes (a source added or removed, by us or another process)."""
        stamp, index = self._hashes
        try:
            now = self.raw.stat().st_mtime_ns
        except OSError:
            now = None
        if now is None or now != stamp:
            index = {}
            for meta in self.sources():
                if meta.get("sha256") and meta.get("id"):
                    index[meta["sha256"]] = meta["id"]
            self._hashes = (now, index)
        sid = index.get(digest_hex)
        if sid is None:
            return None
        try:
            meta = self.source(sid)
        except (LookupError, OSError, ValueError):
            self._hashes = (None, {})  # removed since: rebuild next time
            return None
        return meta if meta.get("sha256") == digest_hex else None

    def _remember_hash(self, meta):
        """Our own ingest: add it without rereading every meta (we hold the lock)."""
        _, index = self._hashes
        index[meta["sha256"]] = meta["id"]
        try:
            self._hashes = (self.raw.stat().st_mtime_ns, index)
        except OSError:
            self._hashes = (None, {})

    def _duplicate(self, meta):
        self.event("reader", "duplicate", f"raw/{meta['file']}", "already in raw/ — nothing written")
        return {"source": meta, "duplicate": True}

    def _next_number(self, kind):
        """Next source/answer number: past anything on disk, so a lost engine.json can never
        hand out a number that already names a file."""
        folder, pattern = (self.raw, r"source-(\d+)\.") if kind == "source" else (self.wiki, r"answer-(\d+)\.md$")
        used = [int(m.group(1)) for f in folder.iterdir() if (m := re.match(pattern, f.name))]
        engine = self._engine()
        number = max([engine.get(f"next_{kind}", 1), *(n + 1 for n in used)])
        self._engine({f"next_{kind}": number + 1})
        return number

    @staticmethod
    def _kind(ext):
        return ("pdf" if ext == ".pdf" else "audio" if ext in AUDIO_KINDS else
                "page" if ext in (".html", ".htm") else "text")

    @staticmethod
    def _extract(path, ext, data, transcribe=None):
        if ext in (".md", ".txt", ".markdown"):
            return data.decode("utf-8", "replace")
        if ext in (".html", ".htm"):
            from .vault import _Text
            parser = _Text()
            parser.feed(data.decode("utf-8", "replace"))
            return parser.text()
        if ext == ".pdf":
            binary = shutil.which("pdftotext")
            if not binary:
                raise ValueError("PDFs need pdftotext (Arch: pacman -S poppler).")
            return subprocess.run([binary, "-q", str(path), "-"], capture_output=True,
                                  timeout=120).stdout.decode("utf-8", "replace")
        if ext in AUDIO_KINDS:
            if transcribe is None:
                from .listen import Transcriber
                transcribe = Transcriber({})
            return transcribe(str(path))
        return data.decode("utf-8", "replace")

    # ---- wiki --------------------------------------------------------------------------
    def pages(self, shared=False):
        """Every wiki page, parsed. Parsing is cached per file by (mtime_ns, size), so an
        unchanged page is never re-parsed; only changed, new and gone files cost anything.
        The default hands back private copies (writers mutate pages); read-only callers such
        as recall pass shared=True and get the cached objects, which they must not modify."""
        cache = self.__dict__.setdefault("_page_cache", {})
        pages = {}
        for path in sorted(self.wiki.glob("*.md")):
            if path.stem == "index":
                continue
            try:
                stat = path.stat()
                key = (stat.st_mtime_ns, stat.st_size)
                hit = cache.get(path.stem)
                if not hit or hit[0] != key:
                    hit = cache[path.stem] = (key, parse_page(path.stem, path.read_text(encoding="utf-8")))
                pages[path.stem] = hit[1]
            except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
                cache.pop(path.stem, None)
                self._skipped = getattr(self, "_skipped", set())
                if path.stem not in self._skipped:
                    self._skipped.add(path.stem)
                    self.event("librarian", "error", f"wiki/{path.name}", f"unreadable, skipped: {exc}")
                continue
        for gone in set(cache) - set(pages):
            del cache[gone]
        return pages if shared else copy.deepcopy(pages)

    def page(self, slug):
        path = self.wiki / f"{slug}.md"
        if not re.fullmatch(r"[a-z0-9-]+", slug or "") or not path.is_file():
            raise LookupError(f"Unknown page: {slug}")
        text = path.read_text(encoding="utf-8")
        return {**parse_page(slug, text), "markdown": text}

    def _save(self, pages, before, bot, reason):
        """Write the pages that changed, atomically, each with its diff in the log."""
        writes = []
        for slug, page in pages.items():
            after = render_page(page, self.today(), self.stale_days)
            path = self.wiki / f"{slug}.md"
            old = before.get(slug)
            if old is None and path.is_file():
                old = path.read_text(encoding="utf-8")
            if old == after:
                continue
            self._atomic(path, after)
            diff = list(difflib.unified_diff((old or "").splitlines(), after.splitlines(),
                                             f"a/wiki/{slug}.md", f"b/wiki/{slug}.md", lineterm="", n=1))
            added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
            writes.append(slug)
            self.event(bot, "patch" if old else "create", f"wiki/{slug}.md", reason,
                       diff=diff[:120], added=added, removed=removed)
        return writes

    def _index(self, pages):
        rows = ["---", 'title: "Index"', "type: index", "---", "", "# Index", "",
                "Generated by the librarian. Every page, its claims and when it was last confirmed.", ""]
        for kind, heading in (("page", "Pages"), ("answer", "Filed answers")):
            group = sorted((p for p in pages.values() if p["type"] == kind), key=lambda p: p["slug"])
            if group:
                rows += [f"## {heading}"]
                rows += [f"- [[{p['slug']}]] — {len(p['claims'])} claims · "
                         f"{page_status(p, self.today(), self.stale_days)} · confirmed {page_confirmed(p) or '—'}"
                         for p in group]
                rows.append("")
        text = "\n".join(rows)
        path = self.wiki / "index.md"
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            self._atomic(path, text)

    def _vectors(self, pages):
        docs = {s: content(p["title"]) * 3 + [t for c in p["claims"] for t in content(c["text"])]
                for s, p in pages.items()}
        idf = idf_table(list(docs.values()))
        return idf, {s: vector(d, idf) for s, d in docs.items() if d}

    def _relink(self, pages):
        """Related = the most similar pages by TF-IDF, nudged up by shared sources."""
        _, vecs = self._vectors({s: p for s, p in pages.items() if p["type"] == "page"})
        cited = {s: {x for c in p["claims"] for x in c["sources"]} for s, p in pages.items()}
        for slug, page in pages.items():
            if page["type"] == "answer":
                continue
            scores = []
            for other, vec in vecs.items():
                if other == slug or slug not in vecs:
                    continue
                score = cosine(vecs[slug], vec) + 0.15 * len(cited[slug] & cited[other])
                if score >= 0.12:
                    scores.append((score, other))
            page["related"] = [s for _, s in sorted(scores, reverse=True)[:4]]

    # ---- librarian ---------------------------------------------------------------------
    def digest(self, sid):
        """Librarian: file a source's claims into the wiki, rewriting every page it touches."""
        with self.lock():
            meta, text = self.source(sid), self.source_text(sid)
            pages = self.pages()
            before = {s: (self.wiki / f"{s}.md").read_text(encoding="utf-8") for s in pages}
            idf, vecs = self._vectors({s: p for s, p in pages.items() if p["type"] == "page"})
            sents = sentences(text)
            local_idf = idf_table([content(s) for s in sents]) if not idf else idf
            claims = extract_claims(text, local_idf)
            today = self.today().isoformat()
            homes, loose = {}, []
            existing = {c["id"]: (s, c) for s, p in pages.items() if p["type"] == "page"
                        for c in p["claims"]}
            whole = meta.get("page")
            if whole and whole not in pages:
                pages[whole] = {"slug": whole, "title": meta["title"], "type": "page", "claims": [],
                                "related": [], "answer": "", "extra": "", "question": ""}
            for claim in claims:
                cid = claim_id(claim)
                if cid in existing:
                    homes[claim] = existing[cid][0]
                    continue
                if whole:
                    homes[claim] = whole
                    continue
                terms = content(claim)
                cv = vector(terms, idf or local_idf)
                best, where = 0.0, None
                for slug, vec in vecs.items():
                    title = set(content(pages[slug]["title"]))
                    score = cosine(cv, vec) + (0.3 if title and title <= set(terms) else 0.0)
                    if score > best:
                        best, where = score, slug
                if where and best >= ROUTE_THRESHOLD:
                    homes[claim] = where
                else:
                    loose.append(claim)
            created = [whole] if whole and not before.get(whole) else []
            aliases = self.bot("librarian").get("aliases", {})
            for surface, group in topics(loose, local_idf):
                slug = page_slug(aliases.get(slugify(surface), slugify(surface)))
                if not slug:
                    continue
                if slug not in pages:
                    pages[slug] = {"slug": slug, "title": surface.title(), "type": "page",
                                   "claims": [], "related": [], "answer": "", "extra": "",
                                   "question": ""}
                    created.append(slug)
                for c in group:
                    homes[c] = slug
                loose = [c for c in loose if c not in group]
            if loose:  # whatever fits no topic lives on the source's own page
                slug = page_slug(slugify(meta["title"]) or sid)
                slug = aliases.get(slug, slug)
                if slug not in pages:
                    pages[slug] = {"slug": slug, "title": meta["title"], "type": "page", "claims": [],
                                   "related": [], "answer": "", "extra": "", "question": ""}
                    created.append(slug)
                for c in loose:
                    homes[c] = slug
            index = self._subject_index(pages)
            decided = self.bot("checker").get("decisions", {})
            added = reconfirmed = 0
            conflicts = []
            touched = set()
            for claim, slug in homes.items():
                page, cid = pages[slug], claim_id(claim)
                touched.add(slug)
                same = next((c for c in page["claims"] if c["id"] == cid), None)
                if same is None:
                    same = next((c for c in page["claims"] if relation(claim, c["text"]) == ("duplicate", None)), None)
                if same is not None:
                    if sid not in same["sources"]:
                        same["sources"].append(sid)
                    same["date"] = today
                    if same.get("flag") == "unconfirmed":
                        same["flag"] = None
                    reconfirmed += 1
                    continue
                new = {"id": cid, "text": claim, "date": today, "sources": [sid], "flag": None, "with": []}
                for other_slug, other in self._candidates(index, claim):
                    rel = relation(claim, other["text"])
                    if "|".join(sorted((cid, other["id"]))) in decided or sid in other["sources"]:
                        continue  # you already ruled, or it's one author contrasting two cases
                    if rel and rel[0] == "conflict" and other["id"] != cid:
                        new["flag"] = "conflict"
                        new["with"].append(other["id"])
                        other["flag"] = "conflict"
                        other["with"] = sorted(set(other.get("with") or []) | {cid})
                        touched.add(other_slug)
                        conflicts.append({"claim": cid, "page": slug, "other": other["id"],
                                          "other_page": other_slug, "why": rel[1],
                                          "text": claim, "other_text": other["text"]})
                page["claims"].append(new)
                index_add(index, slug, new)
                added += 1
            self._relink(pages)
            writes = self._save(pages, before, "librarian", f"--source {sid}")
            self._index(pages)
            for c in conflicts:
                self.event("librarian", "conflict", f"wiki/{c['page']}.md",
                           f"{c['why']} — both claims kept, flagged for review", **c)
            summary = {"source": sid, "claims": len(claims), "added": added,
                       "reconfirmed": reconfirmed, "conflicts": len(conflicts),
                       "created": created, "touched": sorted(touched), "writes": writes}
            self.event("librarian", "commit", f"--source {sid}",
                       f"{len(writes)}/{len(writes)} atomic writes · no raw edits", **summary)
            return summary

    @staticmethod
    def _subject_index(pages):
        index = defaultdict(list)
        for slug, page in pages.items():
            if page["type"] != "answer":
                for claim in page["claims"]:
                    if claim.get("flag") != "superseded":
                        index_add(index, slug, claim)
        return index

    @staticmethod
    def _candidates(index, text):
        subject = signature(text)[0]
        counts = Counter()
        pool = {}
        for term in subject:
            for slug, claim in index.get(term, ()):
                key = (slug, claim["id"])
                counts[key] += 1
                pool[key] = (slug, claim)
        return [pool[k] for k, n in counts.items() if n >= 2]

    # ---- your rulings ------------------------------------------------------------------
    def resolve(self, claim, decision, note="", against=None):
        """Answer the checker. keep: this claim is right, the ones it contradicts become
        superseded. drop: this claim is the wrong one. both: they don't really conflict.
        Superseded claims stay on the page as history; nothing is deleted. `against`
        limits the ruling to one rival, the pair you were shown."""
        if decision not in ("keep", "drop", "both"):
            raise ValueError("Decide keep, drop or both.")
        with self.lock():
            pages = self.pages()
            before = {s: (self.wiki / f"{s}.md").read_text(encoding="utf-8") for s in pages}
            copies = defaultdict(list)  # the same claim can sit on a page and in answers
            for slug, page in pages.items():
                for c in page["claims"]:
                    copies[c["id"]].append((slug, c))
            if claim not in copies:
                raise LookupError(f"Unknown claim: {claim}")
            home, mine = next(((s, c) for s, c in copies[claim] if pages[s]["type"] == "page"), copies[claim][0])
            rivals = list(mine.get("with") or []) if mine.get("flag") == "conflict" else []
            if against:
                if against not in rivals:
                    raise ValueError(f"{claim} isn't in conflict with {against}.")
                rivals = [against]
            if not rivals:
                raise ValueError(f"{claim} isn't in conflict with anything.")
            losers = rivals if decision == "keep" else [claim] if decision == "drop" else []
            stamp = datetime.now().isoformat(timespec="seconds")

            def unlink(a, b):
                for _, c in copies[a]:
                    c["with"] = [x for x in c.get("with") or [] if x != b]
                    if c.get("flag") == "conflict" and not c["with"]:
                        c["flag"] = None

            for rival in rivals:
                unlink(claim, rival)
                unlink(rival, claim)
            for loser in losers:
                winners = [claim] if decision == "keep" else rivals
                for other in sorted({x for _, c in copies[loser] for x in c.get("with") or []}):
                    unlink(loser, other)  # a wrong claim's other disputes are moot
                    unlink(other, loser)
                for _, c in copies[loser]:
                    c["flag"], c["with"] = "superseded", winners

            def remember(memory):
                rulings = memory.setdefault("decisions", {})
                for rival in rivals:
                    rulings["|".join(sorted((claim, rival)))] = {
                        "decision": decision, "claim": claim, "at": stamp, "note": note,
                        "winner": claim if decision == "keep" else rival if decision == "drop" else None}
            self.bot("checker", remember)
            writes = self._save(pages, before, "librarian", f"--resolve {claim} {decision}")
            self._index(pages)
            verdict = {"keep": f"kept {claim}; {', '.join(rivals)} superseded",
                       "drop": f"{claim} superseded by {', '.join(rivals)}",
                       "both": f"{claim} and {', '.join(rivals)} both stand"}[decision]
            self.event("you", "resolve", f"wiki/{home}.md", verdict, claim=claim, decision=decision,
                       rivals=rivals, writes=writes)
            return {"claim": claim, "decision": decision, "rivals": rivals, "writes": writes}

    def merge(self, src, dst):
        """Fold page src into dst (created if new) and remember it, so the librarian files
        future claims about src under dst."""
        dst = slugify(dst)
        with self.lock():
            pages = self.pages()
            if src not in pages or pages[src]["type"] != "page":
                raise LookupError(f"Unknown page: {src}")
            if not dst or dst == src or dst in RESERVED or (dst in pages and pages[dst]["type"] != "page"):
                raise ValueError("Merge into a different, ordinary page name.")
            before = {s: (self.wiki / f"{s}.md").read_text(encoding="utf-8") for s in pages}
            target = pages.setdefault(dst, {"slug": dst, "title": dst.replace("-", " ").title(),
                                            "type": "page", "claims": [], "related": [],
                                            "answer": "", "extra": "", "question": ""})
            have = {c["id"]: c for c in target["claims"]}
            for c in pages[src]["claims"]:
                if c["id"] in have:
                    same = have[c["id"]]
                    same["sources"] = sorted(set(same["sources"]) | set(c["sources"]))
                    same["date"] = max(same["date"], c["date"])
                else:
                    target["claims"].append(c)
            if pages[src].get("extra"):
                target["extra"] = "\n\n".join(x for x in (target.get("extra"), pages[src]["extra"]) if x)
            del pages[src]
            for page in pages.values():
                page["related"] = [dst if r == src else r for r in page["related"]]
            self._relink(pages)
            writes = self._save(pages, before, "librarian", f"--merge {src} into {dst}")
            (self.wiki / f"{src}.md").unlink(missing_ok=True)
            self._index(pages)
            self.bot("librarian", lambda m: m.setdefault("aliases", {}).update(
                {**{k: dst for k, v in m.get("aliases", {}).items() if v == src}, src: dst}))
            self.event("librarian", "merge", f"wiki/{dst}.md", f"{src}.md folded in · alias kept",
                       src=src, dst=dst, writes=writes)
            return {"merged": src, "into": dst, "claims": len(target["claims"]), "writes": writes}

    # ---- checker -----------------------------------------------------------------------
    def check(self, notify=False):
        """Checker: hashes, contradictions, staleness, orphans and broken links."""
        with self.lock():
            today = self.today()
            self.event("checker", "start", "--nightly --source-links", "nightly routine started")
            mutated = []
            for meta in self.sources():
                path = self.raw / meta["file"]
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != meta["sha256"]:
                    mutated.append(meta["id"])
            pages = self.pages()
            before = {s: (self.wiki / f"{s}.md").read_text(encoding="utf-8") for s in pages}
            index = self._subject_index(pages)
            decided = self.bot("checker").get("decisions", {})
            new_conflicts, seen = [], set()
            for slug, page in pages.items():
                if page["type"] == "answer":
                    continue
                for claim in page["claims"]:
                    if claim.get("flag") == "superseded":
                        continue
                    for other_slug, other in self._candidates(index, claim["text"]):
                        pair = tuple(sorted((claim["id"], other["id"])))
                        if other["id"] == claim["id"] or pair in seen or "|".join(pair) in decided:
                            continue
                        seen.add(pair)
                        if set(claim["sources"]) & set(other["sources"]):
                            continue  # one source contrasting two cases isn't a contradiction
                        rel = relation(claim["text"], other["text"])
                        if not rel or rel[0] != "conflict":
                            continue
                        known = other["id"] in (claim.get("with") or [])
                        for c, o in ((claim, other), (other, claim)):
                            c["flag"] = "conflict"
                            c["with"] = sorted(set(c.get("with") or []) | {o["id"]})
                        if not known:
                            new_conflicts.append({"claim": claim["id"], "page": slug,
                                                  "other": other["id"], "other_page": other_slug,
                                                  "why": rel[1], "text": claim["text"],
                                                  "other_text": other["text"]})
            stale = []
            for slug, page in pages.items():
                for claim in page["claims"]:
                    age = (today - date.fromisoformat(claim["date"])).days
                    if age > self.stale_days and claim.get("flag") is None:
                        claim["flag"] = "unconfirmed"
                        stale.append({"claim": claim["id"], "page": slug, "age_days": age})
            names = set(pages) | {"index"} | {m["id"] for m in self.sources()}
            inbound = Counter(r for p in pages.values() for r in p["related"] if r in pages)
            orphans = sorted(s for s, p in pages.items() if p["type"] == "page" and not inbound[s])
            broken = sorted({(s, link) for s, p in pages.items()
                             for link in p["related"] + LINK.findall(p.get("extra", ""))
                             + [x for c in p["claims"] for x in c["sources"]]
                             if link.split("/")[-1] not in names})
            uncited = [(s, c["id"]) for s, p in pages.items() for c in p["claims"] if not c["sources"]]
            writes = self._save(pages, before, "checker", "--nightly flags")
            self._index(pages)
            claims = [c for p in pages.values() for c in p["claims"]]
            report = {"at": datetime.now().isoformat(timespec="seconds"),
                      "scanned": len(claims), "pages": len(pages), "sources": len(self.sources()),
                      "mutated": mutated, "new_conflicts": new_conflicts,
                      "open_conflicts": sum(c.get("flag") == "conflict" for c in claims),
                      "stale": stale, "orphans": orphans, "broken": [list(b) for b in broken],
                      "uncited": uncited, "writes": writes}
            stamp = today.isoformat()
            self._atomic(self.root / "checks" / f"{stamp}.json", json.dumps(report, indent=2))
            self._engine({"last_check": report["at"], "last_report": stamp})
            for c in new_conflicts:
                self.event("checker", "flag", f"wiki/{c['page']}.md",
                           f"@you contradiction: {c['why']} — both claims kept", **c)
            for sid in mutated:
                self.event("checker", "flag", f"raw/{sid}", "@you raw source changed since ingest")
            flags = len(new_conflicts) + len(stale) + len(mutated) + len(broken)
            # An empty base scans 0 claims every night; say why so it doesn't read as a fault.
            summary = (f"scanned {len(claims)} claims · {flags} flags" if pages else
                       "nothing to check yet: no sources ingested")
            self.event("checker", "audit", "--nightly --source-links", summary,
                       scanned=len(claims), conflicts=len(new_conflicts), stale=len(stale),
                       orphans=len(orphans), mutated=len(mutated), broken=len(broken))
            if notify and (new_conflicts or mutated):
                self._notify(report)
            return report

    def _notify(self, report):
        lines = [f"{c['page']}: {c['why']}" for c in report["new_conflicts"][:3]]
        lines += [f"raw/{s} changed" for s in report["mutated"][:2]]
        title = "JARVIS checker needs you"
        try:
            if os.name != "nt" and shutil.which("notify-send"):
                subprocess.run(["notify-send", "-a", "JARVIS", title, "\n".join(lines)], timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass

    # ---- questions ---------------------------------------------------------------------
    def retrieve(self, question, limit=6, pages=None):
        """BM25 over every claim (page title included), best first."""
        pages = pages if pages is not None else self.pages()
        docs = [(slug, c, _terms(c["text"]) + _terms(p["title"]))
                for slug, p in pages.items() if p["type"] == "page" for c in p["claims"]
                if c.get("flag") != "superseded"]
        if not docs:
            return []
        query = set(content(question))
        n, avg = len(docs), sum(len(d) for *_, d in docs) / len(docs)
        df = Counter(t for *_, d in docs for t in set(d))
        scored = []
        for slug, claim, terms in docs:
            tf = Counter(terms)
            score = 0.0
            for t in query & set(tf):
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(terms) / avg))
            if score > 0:
                scored.append((score, slug, claim))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return []
        top = scored[0][0]
        return [{"page": s, "score": round(sc, 3), **c} for sc, s, c in scored[:limit] if sc >= 0.45 * top]

    def ask(self, question, file=True, llm=None):
        """Answer from claims; a good answer (two or more claims) is filed back as a page."""
        question = (question or "").strip()
        if not question:
            raise ValueError("Ask something.")
        self.event("you", "ask", "", question)
        with self.lock():
            hits = self.retrieve(question, pages=self.pages())
        if not hits:
            answer = "Nothing in the wiki covers that yet. Give the reader a source about it."
            self.event("librarian", "answer", "", answer, cited=[])
            return {"answer": answer, "claims": [], "filed": None}
        # The model call can take two minutes; other writers must not wait on it.
        llm = llm if llm is not None else self.settings.get("llm", "none")
        answer = self._llm_answer(question, hits) if llm == "claude" else None
        answer = answer or self._extractive(hits)
        with self.lock():
            filed = self._file_answer(question, answer, hits, self.pages()) if file and len(hits) >= 2 else None
        self.event("librarian", "answer", f"wiki/{filed}.md" if filed else "", answer,
                   cited=[h["id"] for h in hits], filed=filed)
        return {"answer": answer, "claims": hits, "filed": filed}

    @staticmethod
    def _extractive(hits):
        parts = []
        for h in hits[:4]:
            note = (" (disputed: see the conflicting claim)" if h.get("flag") == "conflict" else
                    " (unconfirmed for 30+ days)" if h.get("flag") == "unconfirmed" else "")
            parts.append(f"{h['text'].rstrip('.')}{note} [{', '.join(h['sources'])}, {h['date']}].")
        return " ".join(parts)

    def _llm_answer(self, question, hits):
        """Optional: let Claude phrase the answer, but only from the given claims, and only
        accept it if every citation is one of them. Anything else falls back to extractive."""
        binary = shutil.which("claude")
        if not binary:
            return None
        listing = "\n".join(f"[{h['id']}] {h['text']} (sources: {', '.join(h['sources'])}; "
                            f"confirmed {h['date']}{'; DISPUTED' if h.get('flag') == 'conflict' else ''})"
                            for h in hits)
        prompt = (f"Answer the question using ONLY these claims from David's wiki. Cite every "
                  f"sentence with claim ids in square brackets like [c1a2b3c]. If claims are "
                  f"disputed, say so. At most four sentences, plain text.\n\nClaims:\n{listing}\n\n"
                  f"Question: {question}")
        try:
            from .fastlane import subscription_env
            out = subprocess.run([binary, "-p", "--model", self.settings.get("llm_model", "haiku")],
                                 input=prompt, capture_output=True, text=True, timeout=120,
                                 encoding="utf-8", env=subscription_env(),
                                 creationflags=NO_WINDOW).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None
        cited = set(re.findall(r"\[(c[0-9a-f]{6})\]", out))
        return out if out and cited and cited <= {h["id"] for h in hits} else None

    def _file_answer(self, question, answer, hits, pages):
        qv = set(content(question))
        for slug, page in pages.items():  # the same question again refreshes its page
            if page["type"] == "answer":
                pq = set(content(page.get("question", "")))
                if pq and len(pq & qv) / len(pq | qv) >= 0.8:
                    break
        else:
            slug = f"answer-{self._next_number('answer'):03d}"
            page = {"slug": slug, "title": question[:100], "type": "answer", "question": question,
                    "claims": [], "related": [], "answer": "", "extra": ""}
            pages[slug] = page
        before = {slug: (self.wiki / f"{slug}.md").read_text(encoding="utf-8")
                  for slug in [slug] if (self.wiki / f"{slug}.md").is_file()}
        page["answer"] = answer
        page["claims"] = [{k: h[k] for k in ("id", "text", "date", "sources", "flag", "with")} for h in hits]
        page["related"] = list(dict.fromkeys(h["page"] for h in hits))
        self._save({slug: page}, before, "librarian", "--save answer")
        self._index(pages)
        return slug

    # ---- views -------------------------------------------------------------------------
    def graph(self):
        pages, nodes, edges = self.pages(), [], []
        metas = {m["id"]: m for m in self.sources()}
        for sid, meta in metas.items():
            nodes.append({"id": f"s:{sid}", "kind": "source", "label": f"raw/{meta['file']}",
                          "title": meta["title"], "added": meta["added"], "origin": meta["origin"]})
        for slug, page in pages.items():
            nodes.append({"id": f"p:{slug}", "kind": page["type"], "label": f"{slug}.md",
                          "title": page["title"], "claims": len(page["claims"]),
                          "status": page_status(page, self.today(), self.stale_days),
                          "confirmed": page_confirmed(page)})
            for claim in page["claims"]:
                cid = f"c:{claim['id']}@{slug}"
                nodes.append({"id": cid, "kind": "claim", "page": slug, "label": claim["id"],
                              "text": claim["text"], "flag": claim.get("flag"),
                              "sources": claim["sources"], "confirmed": claim["date"]})
                edges.append({"src": f"p:{slug}", "dst": cid, "rel": "has"})
                for sid in claim["sources"]:
                    if sid in metas:
                        edges.append({"src": cid, "dst": f"s:{sid}", "rel": "cites"})
            for other in page["related"]:
                if other in pages:
                    edges.append({"src": f"p:{slug}", "dst": f"p:{other}", "rel": "related"})
        return {"nodes": nodes, "edges": edges}

    def _raw_sha(self, path):
        """sha256 of a raw file, re-hashed only when its mtime or size moves; None if missing."""
        try:
            stat = path.stat()
        except OSError:
            return None
        cached = self._sha.get(path)
        if cached and cached[:2] == (stat.st_mtime, stat.st_size):
            return cached[2]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self._sha[path] = (stat.st_mtime, stat.st_size, digest)
        return digest

    def snapshot(self):
        """Everything the knowledge console's panels show, in one read."""
        pages, metas, log = self.pages(), self.sources(), self.log(120)
        claims = [(s, c) for s, p in pages.items() for c in p["claims"]]
        today = self.today()
        verify = []
        for meta in metas:
            path = self.raw / meta["file"]
            ok = self._raw_sha(path) == meta["sha256"]
            verify.append({"id": meta["id"], "file": meta["file"], "title": meta["title"],
                           "sha": meta["sha256"][:12], "kind": meta["kind"], "added": meta["added"],
                           "bytes": meta["bytes"], "ok": ok,
                           "readonly": not os.access(path, os.W_OK) if path.is_file() else False})
        commit = next((e for e in reversed(log) if e["action"] == "commit"), None)
        last_diff = next((e for e in reversed(log) if e["action"] in ("patch", "create")), None)
        ranked = sorted((p for p in pages.values() if p["type"] == "page"), key=lambda p: -len(p["claims"]))[:14]
        cols = [m["id"] for m in metas][-12:]
        matrix = {"rows": [p["slug"] for p in ranked], "cols": cols,
                  "cells": [[2 if any(sid in c["sources"] and c.get("flag") == "conflict" for c in p["claims"])
                             else 1 if any(sid in c["sources"] for c in p["claims"]) else 0
                             for sid in cols] for p in ranked]}
        ages = Counter(min(23, (today - date.fromisoformat(c["date"])).days // 2) for _, c in claims)
        engine = self._engine()
        last, report = engine.get("last_check"), None
        if last:
            try:
                stamp = engine.get("last_report") or last[:10]
                report = json.loads((self.root / "checks" / f"{stamp}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report = None
        links = sum(len(p["related"]) for p in pages.values())
        return {
            "root": str(self.root), "today": today.isoformat(),
            "stats": {"sources": len(metas), "pages": sum(p["type"] == "page" for p in pages.values()),
                      "answers": sum(p["type"] == "answer" for p in pages.values()),
                      "claims": len(claims), "links": links,
                      "conflicts": sum(c.get("flag") == "conflict" for _, c in claims),
                      "unconfirmed": sum(c.get("flag") == "unconfirmed" for _, c in claims),
                      "superseded": sum(c.get("flag") == "superseded" for _, c in claims)},
            "open_conflicts": self.open_conflicts(pages),
            "sources": verify, "log": log[-60:], "commit": commit, "diff": last_diff,
            "writes": (commit or {}).get("writes", [])[:14],
            "matrix": matrix, "ages": [ages.get(i, 0) for i in range(24)],
            "checker": {"last": last, "report": report, "schedule": self.schedule_status(),
                        "stale_days": self.stale_days},
            "pages": [{"slug": p["slug"], "title": p["title"], "type": p["type"],
                       "claims": len(p["claims"]), "status": page_status(p, today, self.stale_days),
                       "confirmed": page_confirmed(p)} for p in pages.values()],
        }

    @staticmethod
    def open_conflicts(pages, limit=20):
        """Contradictions waiting for your ruling, one entry per pair."""
        by_id = {c["id"]: (s, c) for s, p in pages.items() if p["type"] == "page" for c in p["claims"]}
        out, seen = [], set()
        for cid, (slug, claim) in by_id.items():
            if claim.get("flag") != "conflict":
                continue
            for rival in claim.get("with") or []:
                pair = tuple(sorted((cid, rival)))
                if pair in seen or rival not in by_id:
                    continue
                seen.add(pair)
                other_slug, other = by_id[rival]
                out.append({"claim": cid, "page": slug, "text": claim["text"], "sources": claim["sources"],
                            "date": claim["date"], "rival": rival, "rival_page": other_slug,
                            "rival_text": other["text"], "rival_sources": other["sources"],
                            "rival_date": other["date"],
                            "why": (relation(claim["text"], other["text"]) or ("", ""))[1]})
        return out[:limit]

    def recall(self, text, limit=4):
        lines = []
        for hit in self.retrieve(text, limit=limit, pages=self.pages(shared=True)):
            mark = " DISPUTED" if hit.get("flag") == "conflict" else (
                " UNCONFIRMED" if hit.get("flag") == "unconfirmed" else "")
            lines.append(f"[wiki {hit['page']}] {hit['text']} ({', '.join(hit['sources'])}, "
                         f"confirmed {hit['date']}{mark})")
        return lines

    # ---- schedule ----------------------------------------------------------------------
    UNIT = "jarvis-knowledge-check"

    def _unit_dir(self):
        return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"

    def schedule_status(self):
        if os.name == "nt":
            return {"kind": "schtasks", "at": "02:00"}
        timer = self._unit_dir() / f"{self.UNIT}.timer"
        return {"kind": "systemd", "at": "02:00", "installed": timer.is_file()}

    def schedule(self, enable, config_path, state, at="02:00"):
        """Run the checker nightly: a systemd user timer on Arch, Task Scheduler on Windows."""
        command = [sys.executable, "-m", "jarvis", "--config", str(Path(config_path).resolve()),
                   "--state", str(Path(state).resolve()), "kb", "check", "--notify"]
        if os.name == "nt":
            name = "JARVIS knowledge check"
            if not enable:
                subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True)
                return {"schedule": False}
            line = subprocess.list2cmdline(command)
            done = subprocess.run(["schtasks", "/Create", "/SC", "DAILY", "/ST", at, "/TN", name,
                                   "/TR", line, "/F"], capture_output=True, text=True)
            return {"schedule": done.returncode == 0, "task": name, "detail": done.stdout.strip()}
        folder = self._unit_dir()
        service, timer = folder / f"{self.UNIT}.service", folder / f"{self.UNIT}.timer"

        def ctl(*args):
            try:
                return subprocess.run(["systemctl", "--user", *args], capture_output=True,
                                      timeout=30).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                return False

        if not enable:
            ctl("disable", "--now", timer.name)
            service.unlink(missing_ok=True)
            timer.unlink(missing_ok=True)
            ctl("daemon-reload")
            return {"schedule": False}
        folder.mkdir(parents=True, exist_ok=True)
        quoted = " ".join(f'"{c}"' for c in command)
        service.write_text(
            "[Unit]\nDescription=JARVIS knowledge checker (contradictions, stale claims, orphans)\n\n"
            f"[Service]\nType=oneshot\nWorkingDirectory={Path(__file__).resolve().parent.parent}\n"
            f'Environment="PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin")}"\n'
            f"ExecStart={quoted}\n", encoding="utf-8")
        timer.write_text(
            "[Unit]\nDescription=Nightly JARVIS knowledge check\n\n"
            f"[Timer]\nOnCalendar=*-*-* {at}:00\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n", encoding="utf-8")
        enabled = ctl("daemon-reload") and ctl("enable", "--now", timer.name)
        return {"schedule": True, "timer": str(timer), "enabled": enabled}


def index_add(index, slug, claim):
    for term in signature(claim["text"])[0]:
        index[term].append((slug, claim))


_OPEN, _OPEN_LOCK = {}, threading.Lock()
_LOG_LOCK = threading.Lock()


def knowledge_path(config, state):
    settings = (config or {}).get("knowledge") or {}
    path = settings.get("path")
    if isinstance(path, dict):
        path = path.get("windows" if os.name == "nt" else "linux")
    return Path(path).expanduser() if path else Path(state).resolve() / "knowledge"


def kb_for(config, state, on_event=None):
    """The configured knowledge base: shared per process, or a fresh one bound to a
    caller's event callback (the console keeps its own)."""
    root = knowledge_path(config, state).resolve()
    if on_event is not None:
        return KnowledgeBase(root, state, (config or {}).get("knowledge") or {}, on_event)
    key = str(root)
    with _OPEN_LOCK:
        if key not in _OPEN:
            _OPEN[key] = KnowledgeBase(root, state, (config or {}).get("knowledge") or {}, on_event)
        return _OPEN[key]


def recall(config, state, text, limit=4):
    """Wiki claims for a request's prompt; [] on any problem (recall must never break a turn)."""
    try:
        root = knowledge_path(config, state)
        if not (root / "wiki").is_dir():
            return []
        return kb_for(config, state).recall(text, limit)
    except Exception:
        return []
