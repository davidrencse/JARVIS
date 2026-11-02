"""The decider: fast, typed choices with probabilities (the Jev-type layer; see
docs/jarvis-os-architecture.md, "L2 Resolve" and "Calibration").

    decide.choose("mode", "reply to sam's last dm")
    -> {"winner": "agent", "probs": {"agent": .81, "act": .11, ...}, "confidence": .81}

Backend: the in-process MiniLM embeddings (jarvis/embed.py) against example phrases per
option ("prototypes"); probabilities are a softmax over cosine similarity at a calibrated
temperature. No server, about a millisecond per decision once the model is loaded.

It runs in shadow first: every decision is logged next to what JARVIS actually did
(record/label), calibrate() fits the temperature on those labels, and a job may act on its
own only once its measured precision at the chosen threshold reaches TARGET. Until then
the existing rules decide, and the decider can only ever restrict, never approve.
"""

import json
import math
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

TARGET = 0.98          # precision a job must reach before it may act on its own
DEFAULT_T = 0.05       # softmax temperature before any calibration
MIN_LABELS = 50        # labelled decisions needed before calibrating a job

# Example phrases per option. Learned examples can be added at run time (learn()).
PROTOTYPES = {
    "mode": {
        "converse": ["hello", "how are you", "what do you think about this", "tell me a joke",
                     "what's the weather like", "explain how this works", "thanks jarvis",
                     "who are you", "what should I eat tonight", "how was my day"],
        "act": ["open spotify", "close chrome on arch", "pause the music", "volume up",
                "switch to discord", "go to youtube", "open the terminal on my laptop",
                "mute", "skip this song", "launch steam on the other pc",
                # Live machine state is answered by tools, never from memory (grounding.jsonl).
                "how much battery do I have left", "how many windows are open",
                "which programs are installed on my laptop", "how much disk space is free"],
        "agent": ["reply to sam's last dm", "buy me a charger on amazon", "check my email",
                  "play that song kevin sent me", "search youtube for cats", "order pizza",
                  "post this on twitter", "book a table for two", "find flights to tokyo",
                  "play my liked songs on spotify",
                  "read me the top post on reddit", "check what's trending on twitter"],
        "build": ["build me a dashboard for my tasks", "refactor the parser",
                  "create a website for my project", "write a script that renames my files",
                  "fix the failing tests", "implement a new feature", "set up a backend api",
                  "migrate the database"],
    },
    # Does a request ask about David's own world (what his notes hold), or not? Vault
    # recall (vault.Vault.context) drops its keyword hits when the answer is clearly "none":
    # small talk, general knowledge, the clock, commands and live machine state share
    # words with notes without being about them. Written apart from tests/eval/cases.jsonl
    # so the scorecard measures this job on unseen text.
    "recall": {
        "notes": ["when is my exam", "what deadlines are coming up", "what do I have today",
                  "what's on my schedule tomorrow", "what did I write about the trip",
                  "what are my notes on linear algebra", "where am I with my thesis",
                  "what's the status of my internship applications", "what's my plan for the weekend",
                  "remind me what my doctor said", "what projects am I working on",
                  "what's my next step on the app", "what did I decide about the apartment",
                  "when is my dentist appointment", "how should I study for my final",
                  "what are my priorities today", "what's left on my reading list",
                  "what did I log yesterday", "how is my laptop set up", "what's my mom's birthday",
                  "what courses am I taking this semester", "what tasks are overdue",
                  "summarize my journal from last week", "what's in my notes about the job offer",
                  "what do I have going on today", "am I falling behind on anything",
                  "what's coming up this week", "what's my agenda"],
        "none": ["hey there", "hi jarvis", "thanks", "good night", "how's it going", "you're awesome",
                 "never mind", "got the time", "what's the date today", "which day of the week is it",
                 "what's the capital of peru", "who wrote hamlet", "how many grams in a kilo",
                 "make me laugh", "what's the weather", "launch discord", "close chrome on the laptop",
                 "turn the volume up", "play some music", "how do I install python",
                 "how much is ethereum right now", "explain recursion", "reply with one word",
                 "this is a test", "can you hear me", "say hi to everyone",
                 "what's running on my pc right now", "how much battery does my laptop have",
                 "why are you acting weird", "I'm hungry", "I'm going to the gym", "let's go",
                 "shut up", "what are you", "I'm thinking about buying a new phone",
                 "I'll take care of it", "don't worry about it", "come on, just do it"],
    },
}
RECALL_MARGIN = 0.09  # "none" must beat "notes" by this much to drop recall: missing a
                      # note costs more than a stray line, so close calls keep the notes


# A first-person statement ("I'm going to sell my car.", "We got nothing.") tells JARVIS
# something; it doesn't ask what his notes say. Unless it names notes, plans or the
# calendar, notes have to clearly win to be pulled in (shape, not wording: no eval text).
STATEMENT = re.compile(r"^\W*(?:i|i'm|im|i am|i'll|ill|i will|i've|i was|i just|we|we're|we'll|"
                       r"we got|we have|my|me)\b", re.I)
NOTESY = re.compile(r"\?|\b(?:notes?|vault|journal|wrote|write|written|log(?:ged)?|plan(?:s|ned)?|"
                    r"schedule|calendar|agenda|due|deadline|remind|remember|told|said|decided|"
                    r"what|when|where|which|how|who|why|status|progress|priorit\w*|tasks?|todo)\b", re.I)
STATEMENT_MARGIN = 0.05  # notes must beat "none" by this much for a plain statement


def wants_notes(text):
    """False only when a request is clearly not about David's own notes (small talk, general
    knowledge, commands); True when it is, or it's close, or the model is unavailable."""
    found = choose("recall", text)
    if found is None:
        return True
    gap = found["sims"]["none"] - found["sims"]["notes"]
    if STATEMENT.match(str(text)) and not NOTESY.search(str(text)):
        return gap < -STATEMENT_MARGIN
    return gap < RECALL_MARGIN

LABELS = Path(__file__).with_name("decider_labels.jsonl")


def _key(text):
    return re.sub(r"\W+", " ", str(text).lower()).strip()


_bad_labels = []  # logged once


def examples(job, held_out=()):
    """Labelled (text, option) pairs a job learns from beyond PROTOTYPES: the scorecard's
    cases (tests/eval/cases.jsonl, absent on a node) first, then decider_labels.jsonl; each
    text once. held_out: texts left out, so the scorecard can measure on unseen ones."""
    if job != "mode":
        return []
    from .scorecard import CASES, load, mode_for
    try:
        rows = [(c["text"], mode_for(c)) for c in load(CASES)[1]]
    except (OSError, ValueError):
        rows = []
    try:
        lines = LABELS.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line) if line.strip() else {}
        except ValueError:
            if not _bad_labels:
                _bad_labels.append(line[:80])
                print(f"decide: skipping malformed line in {LABELS.name}: {line[:80]!r}", flush=True)
            continue
        if isinstance(row, dict) and "text" in row:
            rows.append((row["text"], row.get("label")))
    seen, out = {_key(t) for t in held_out}, []
    for text, option in rows:
        if option in PROTOTYPES[job] and _key(text) not in seen:
            seen.add(_key(text))
            out.append((text, option))
    return out


BASE = {job: {o: list(p) for o, p in options.items()} for job, options in PROTOTYPES.items()}
for _text, _option in examples("mode"):
    if _text not in PROTOTYPES["mode"][_option]:
        PROTOTYPES["mode"][_option].append(_text)

_lock = threading.Lock()
_cache = {}  # job -> {option: matrix of prototype vectors}
_generation = 0  # bumped by learn(): a model fitted on older examples must not be stored


def _vectors(job):
    from .embed import embed
    with _lock:
        if job not in _cache:
            options = PROTOTYPES[job]
            built = {}
            for option, phrases in options.items():
                vectors = embed(phrases)
                if vectors is None:
                    return None
                built[option] = vectors
            _cache[job] = built
        return _cache[job]


def learn(job, option, phrase):
    """Add a verified example phrase to a job's option (takes effect immediately)."""
    global _generation
    PROTOTYPES[job][option].append(str(phrase))
    with _lock:
        _generation += 1
        _cache.pop(job, None)
        _models.pop(job, None)


# -- the mode job's classifier ---------------------------------------------------------------
# Nearest example alone let one stray neighbour win at 0.98 confidence ("what is
# doomscrolling" -> act). A logistic regression over the same embeddings, plus a few
# sentence-shape cues the embedding blurs (a question vs a command), scores 93% vs 87%
# cross-validated on tests/eval/cases.jsonl, covers 79% vs 48% of requests at 98%
# precision, and its most confident mistake drops from 0.98 to 0.85 (measured 2026-11-01).
# The cues are generic on purpose: none was written from a scorecard failure.
CUES = {
    "mode": [r"^(?:what|who|whats|what's|when|where|why|how|is|are|am|do|does|did|can i|"
             r"should|which)\b",
             r"\bmy\s+(?:notes?|vault|journal|calendar|schedule|tasks?|projects?|plans?|courses?)\b",
             r"^(?:open|close|launch|start|go to|switch to|pause|play|skip|mute|turn|set|focus|"
             r"quit|resume)\b",
             r"\b(?:build|refactor|implement|code|script|website|dashboard|deploy|debug|compile)\b",
             r"\b(?:reply|post|buy|order|search|find|email|message|book|send)\b",
             r"\b(?:i'm|i am|i'll|you|you're|okay|ok)\b"],
}
CUE_WEIGHT = 0.2      # cue features next to unit-length embeddings
FIT = {"c": 10.0, "iterations": 1500, "rate": 2.0}
_models = {}  # job -> (weights, bias, options)


def features(job, texts, vectors):
    """Embeddings with the job's cue flags appended (just the embeddings for other jobs)."""
    import numpy as np
    cues = CUES.get(job)
    if not cues:
        return vectors
    flags = np.array([[1.0 if re.search(p, clean(t).lower()) else 0.0 for p in cues]
                      for t in texts], dtype=vectors.dtype)
    return np.hstack([vectors, flags * CUE_WEIGHT])


def fit(x, y, classes, c=None, iterations=None, rate=None):
    """Multinomial logistic regression (numpy, class-balanced, L2), full-batch gradient
    descent: ~0.3 s for a few hundred examples. Returns (weights, bias)."""
    import numpy as np
    c, iterations, rate = c or FIT["c"], iterations or FIT["iterations"], rate or FIT["rate"]
    n, d = x.shape
    target = np.eye(classes)[y]
    balance = (n / (classes * np.bincount(y, minlength=classes).clip(1)))[y][:, None]
    weights, bias = np.zeros((d, classes)), np.zeros(classes)
    for _ in range(iterations):
        z = x @ weights + bias
        z -= z.max(1, keepdims=True)
        p = np.exp(z)
        p /= p.sum(1, keepdims=True)
        grad = (p - target) * balance / n
        weights -= rate * (x.T @ grad + weights / (c * n))
        bias -= rate * grad.sum(0)
    return weights, bias


def predict(model, x):
    """Class probabilities, one row per input row."""
    import numpy as np
    weights, bias = model
    z = x @ weights + bias
    z -= z.max(1, keepdims=True)
    p = np.exp(z)
    return p / p.sum(1, keepdims=True)


def _model(job):
    """The job's trained classifier, or None (no cues for it, or no embedding model)."""
    import numpy as np
    if job not in CUES:
        return None
    with _lock:
        if job in _models:
            return _models[job]
        generation = _generation
    protos = _vectors(job)  # the example embeddings choose() already holds
    if protos is None:
        return None
    options = list(PROTOTYPES[job])
    snapshot = {o: list(PROTOTYPES[job][o]) for o in options}
    if sum(len(v) for v in protos.values()) != sum(len(v) for v in snapshot.values()):
        return None  # learn() landed between the two reads; the next call rebuilds both
    texts = [t for o in options for t in snapshot[o]]
    labels = np.array([i for i, o in enumerate(options) for _ in snapshot[o]])
    vectors = np.vstack([protos[o] for o in options])
    model = (*fit(features(job, texts, vectors), labels, len(options)), options)
    with _lock:
        if generation == _generation:
            _models[job] = model
    return model


def clean(text):
    """A leading wake word says nothing about the request and drags it toward "converse"."""
    return re.sub(r"^\W*jarvis\b\W*", "", str(text), flags=re.I) or str(text)


def choose(job, text, temperature=None, options=None):
    """{winner, probs, confidence} over the job's options (or a subset); None if the
    embedding model is unavailable."""
    from .embed import embed
    protos = _vectors(job)
    query = embed([clean(text)])
    if protos is None or query is None:
        return None
    names = [o for o in (options or protos) if o in protos]
    # An option's score is its closest example, so one strong match counts.
    sims = {o: float((protos[o] @ query[0]).max()) for o in names}
    t = temperature or temperature_for(job)
    top = max(sims.values())
    weights = {o: math.exp((s - top) / t) for o, s in sims.items()}
    total = sum(weights.values())
    probs = {o: round(w / total, 4) for o, w in weights.items()}
    model = _model(job) if options is None and temperature is None else None
    if model is not None:  # the classifier decides; sims stay for the shadow log
        p = predict(model[:2], features(job, [text], query))[0]
        probs = {o: round(float(v), 4) for o, v in zip(model[2], p)}
    winner = max(probs, key=probs.get)
    return {"winner": winner, "probs": probs, "confidence": probs[winner],
            "similarity": round(sims[winner], 3), "sims": {o: round(v, 5) for o, v in sims.items()}}


# Routing (chatgpt.Brain.pick): the decider can send a request the keyword rules missed to
# the tools lane. 0.75 sits just above the scorecard's cross-validated 98%-precision cutoff
# for the mode job (0.734 on 2026-11-01, on text it never trained on). A wrong pick costs a
# slower answer, never an action: the tools lane still decides what to do.
ROUTE_THRESHOLD = 0.75
ROUTE_TO_TOOLS = ("act", "agent")
WARM_RETRY_S = 300  # after a failed build, wait this long before trying again
_warming = []        # jobs whose model is being built right now
_warm_failed = {}    # job -> monotonic time of its last failed build


def ready(job):
    """True once the job's classifier is built, so asking costs milliseconds."""
    return job in _models


def warm(job):
    """Build the job's model once in the background (about 2 s): never on a request.
    A failed build is logged and retried after WARM_RETRY_S, not on every request."""
    with _lock:
        if job in _warming or job in _models:
            return
        failed = _warm_failed.get(job)
        if failed is not None and time.monotonic() - failed < WARM_RETRY_S:
            return
        _warming.append(job)

    def build():
        try:
            choose(job, "warm up")
            # choose() returns None, not an error, when the embedding model is missing.
            why = None if job in _models else "no embedding model or no cues for the job"
        except Exception as exc:
            why = repr(exc)
        with _lock:
            if why is None:
                _warm_failed.pop(job, None)
            else:
                _warm_failed[job] = time.monotonic()
            if job in _warming:
                _warming.remove(job)
        if why is not None:
            import sys
            print(f"jarvis > decider: building the {job} model failed ({why}); "
                  f"retrying in {WARM_RETRY_S} s", file=sys.stderr)
    threading.Thread(target=build, name=f"jarvis-decide-{job}", daemon=True).start()


def wants_tools(text):
    """True when the mode classifier is ready and confidently says act or agent. Never
    blocks: until the model is built it says False and starts building it."""
    if not ready("mode"):
        warm("mode")
        return False
    try:
        found = choose("mode", text)
    except Exception:
        return False
    return bool(found) and found["winner"] in ROUTE_TO_TOOLS and found["confidence"] >= ROUTE_THRESHOLD


# -- shadow log and calibration (history.sqlite3, table decisions) -----------------------------
@contextmanager
def _db(state):
    """A connection that commits and is always closed (an open one locks the file on Windows)."""
    import sqlite3
    path = Path(state).resolve() / "history.sqlite3"
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("""CREATE TABLE IF NOT EXISTS decisions(
                id TEXT PRIMARY KEY, at REAL NOT NULL, job TEXT NOT NULL, text TEXT NOT NULL,
                winner TEXT, probs TEXT, confidence REAL, label TEXT, source TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS decisions_job ON decisions(job, at)")
            yield db
    finally:
        db.close()


def record(state, job, text, decision, label=None, source=None):
    """Log one shadow decision (and, if known, what JARVIS actually did). Returns its id."""
    did = uuid.uuid4().hex
    with _db(state) as db:
        db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?)",
                   (did, time.time(), job, str(text)[:500],
                    (decision or {}).get("winner"),
                    json.dumps({"probs": (decision or {}).get("probs"), "sims": (decision or {}).get("sims")}),
                    (decision or {}).get("confidence"), label, source))
        # Calibration needs recent behaviour, not all of it: drop rows past the history's age.
        from .history import KEEP_DAYS
        db.execute("DELETE FROM decisions WHERE at<?", (time.time() - KEEP_DAYS * 86400,))
    return did


def label(state, decision_id, value, source="outcome"):
    with _db(state) as db:
        db.execute("UPDATE decisions SET label=?, source=? WHERE id=?", (value, source, decision_id))


def _trained_on(job):
    """Keys of every text the job's model learns from (built-in examples and examples()),
    so calibration never scores the model on what it was trained on."""
    texts = [t for option in PROTOTYPES.get(job, {}).values() for t in option]
    texts += [t for t, _ in examples(job)]
    return {_key(clean(t)) for t in texts}


def _labelled(state, job, held_out=True):
    """Labelled shadow decisions, each text once. held_out (default): texts the model was
    trained on are left out (audit 2026-11-01: 35 relabelled shadow rows sit in
    decider_labels.jsonl and eval cases come from the same history, so they leaked)."""
    with _db(state) as db:
        rows = [dict(r) for r in db.execute(
            "SELECT text, probs, winner, confidence, label FROM decisions WHERE job=? "
            "AND label IS NOT NULL AND probs IS NOT NULL AND probs != 'null' ORDER BY at",
            (job,))]
    skip = _trained_on(job) if held_out else set()
    seen, out = set(), []
    for row in rows:
        key = _key(clean(row.get("text") or ""))
        if key in skip or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


_TEMPS = {}


def temperature_for(job):
    return _TEMPS.get(job, DEFAULT_T)


def calibrate(state, job):
    """Fit the softmax temperature to the labelled shadow log (minimum negative log
    likelihood), then find the lowest confidence threshold whose precision reaches TARGET.
    Returns a report; the job stays in shadow when there isn't enough data or precision."""
    rows = _labelled(state, job)
    if len(rows) < MIN_LABELS:
        return {"job": job, "labels": len(rows), "ready": False,
                "note": f"needs {MIN_LABELS} labelled decisions to calibrate"}
    # Raw similarities are logged, so any temperature can be tried against them.
    samples = []
    for r in rows:
        sims = (json.loads(r["probs"]) or {}).get("sims") or {}
        if r["label"] in sims:
            samples.append((sims, r["label"]))
    if len(samples) < MIN_LABELS:  # rows whose label isn't an option don't count
        return {"job": job, "labels": len(samples), "ready": False,
                "note": f"needs {MIN_LABELS} usable labelled decisions to calibrate"}
    if job in CUES:
        # The classifier's own probabilities were logged: no temperature to fit, just the
        # precision of acting at each confidence. (Rows logged before it existed carry the
        # old nearest-example probabilities, which ages out with the history.)
        scored = []
        for r in rows:
            probs = (json.loads(r["probs"]) or {}).get("probs") or {}
            if r["label"] in probs:
                winner = max(probs, key=probs.get)
                scored.append((probs[winner], winner == r["label"]))
        return _precision(job, scored, None)
    best_t, best_nll = DEFAULT_T, float("inf")
    for t in [0.01 * k for k in range(1, 31)]:
        nll = 0.0
        for logits, truth in samples:
            top = max(logits.values())
            z = sum(math.exp((v - top) / t) for v in logits.values())
            nll -= (logits[truth] - top) / t - math.log(z)
        if nll < best_nll:
            best_t, best_nll = t, nll
    _TEMPS[job] = best_t
    # Precision of acting only when confidence >= threshold, at the fitted temperature.
    scored = []
    for logits, truth in samples:
        top = max(logits.values())
        weights = {o: math.exp((v - top) / best_t) for o, v in logits.items()}
        total = sum(weights.values())
        winner = max(weights, key=weights.get)
        scored.append((weights[winner] / total, winner == truth))
    return _precision(job, scored, best_t)


def _precision(job, scored, temperature):
    """The lowest confidence whose precision reaches TARGET (over at least 20 picks)."""
    if len(scored) < MIN_LABELS:
        return {"job": job, "labels": len(scored), "ready": False,
                "note": f"needs {MIN_LABELS} usable labelled decisions to calibrate"}
    scored.sort(reverse=True)
    threshold, coverage = None, 0.0
    correct = 0
    for n, (conf, ok) in enumerate(scored, 1):
        correct += ok
        if n >= 20 and correct / n >= TARGET:
            threshold, coverage = conf, n / len(scored)
    accuracy = sum(ok for _, ok in scored) / len(scored)
    return {"job": job, "labels": len(scored), "temperature": temperature,
            "accuracy": round(accuracy, 3), "threshold": threshold and round(threshold, 4),
            "coverage": round(coverage, 3), "ready": threshold is not None}
