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
                "mute", "skip this song", "launch steam on the other pc"],
        "agent": ["reply to sam's last dm", "buy me a charger on amazon", "check my email",
                  "play that song kevin sent me", "search youtube for cats", "order pizza",
                  "post this on twitter", "book a table for two", "find flights to tokyo",
                  "play my liked songs on spotify"],
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
        "notes": ["when is my exam", "what's due this week", "what do I have today",
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
        "none": ["hello", "hi jarvis", "thanks", "good night", "how are you", "you're awesome",
                 "never mind", "what time is it", "what's today's date", "what day is it",
                 "what's the capital of peru", "who wrote hamlet", "how many grams in a kilo",
                 "tell me a joke", "what's the weather", "open spotify", "close chrome on the laptop",
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


def wants_notes(text):
    """False only when a request is clearly not about David's own notes (small talk, general
    knowledge, commands); True when it is, or it's close, or the model is unavailable."""
    found = choose("recall", text)
    if found is None:
        return True
    return found["sims"]["none"] - found["sims"]["notes"] < RECALL_MARGIN

LABELS = Path(__file__).with_name("decider_labels.jsonl")


def _key(text):
    return re.sub(r"\W+", " ", str(text).lower()).strip()


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
        row = json.loads(line) if line.strip() else {}
        if "text" in row:
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
    PROTOTYPES[job][option].append(str(phrase))
    with _lock:
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
    protos = _vectors(job)  # the example embeddings choose() already holds
    if protos is None:
        return None
    options = list(PROTOTYPES[job])
    texts = [t for o in options for t in PROTOTYPES[job][o]]
    labels = np.array([i for i, o in enumerate(options) for _ in PROTOTYPES[job][o]])
    vectors = np.vstack([protos[o] for o in options])
    model = (*fit(features(job, texts, vectors), labels, len(options)), options)
    with _lock:
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


def _labelled(state, job):
    with _db(state) as db:
        return [dict(r) for r in db.execute(
            "SELECT probs, winner, confidence, label FROM decisions WHERE job=? AND label IS NOT NULL "
            "AND probs IS NOT NULL AND probs != 'null'", (job,))]


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
