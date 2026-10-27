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
}

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


def choose(job, text, temperature=None, options=None):
    """{winner, probs, confidence} over the job's options (or a subset); None if the
    embedding model is unavailable."""
    from .embed import embed
    protos = _vectors(job)
    query = embed([text])
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
    scored.sort(reverse=True)
    threshold, coverage = None, 0.0
    correct = 0
    for n, (conf, ok) in enumerate(scored, 1):
        correct += ok
        if n >= 20 and correct / n >= TARGET:
            threshold, coverage = conf, n / len(scored)
    accuracy = sum(ok for _, ok in scored) / len(scored)
    return {"job": job, "labels": len(samples), "temperature": best_t, "accuracy": round(accuracy, 3),
            "threshold": threshold and round(threshold, 4), "coverage": round(coverage, 3),
            "ready": threshold is not None}
