"""The scorecard: one number per part of JARVIS, measured on David's real requests.

tests/eval/cases.jsonl holds requests taken from the memory heap and the command history,
each labelled with what should happen to it:

    {"text": "open chrome on arch", "type": "act",
     "commands": [{"intent": "open_app", "device": "zenbook", "args": {"name": "Chrome"}}]}
    {"text": "what's due this week", "type": "vault", "vault": {"answer": "instant"}}
    {"text": "when is my OS midterm", "type": "vault", "vault": {"notes": ["courses/operating-systems"]}}

type: act (direct Registry intents), agent (Grok Bot: judgement in an app or account),
converse (a question for the brain), build (software work), instant (the clock answers),
vault (the vault answers: instantly, or by recalling the named notes), clarify (JARVIS
can't tell what or where). The first line may be {"_meta": {"origin": "zenbook"}}: the
device a case came from when it doesn't say. "mode" overrides the mode the decider and
the brain should pick, for act cases JARVIS hands to Grok Bot anyway (spotify_play).

`jarvis eval` scores the parser, the Registry, the decider and the vault for free (local
code and the in-process embeddings). `--brain` also asks the interpreter, the brain's
structured lane, about a small fixed sample on the subscription, and only then. Every run
is appended to .jarvis/eval/runs.jsonl and compared with the last one, so a drop shows.
"""

from .home import home as _jarvis_home
import hashlib
import json
import random
import subprocess
import time
from pathlib import Path

HOME = _jarvis_home()
CASES = HOME / "tests" / "eval" / "cases.jsonl"
GROUNDING = HOME / "tests" / "eval" / "grounding.jsonl"
TYPES = ("act", "agent", "converse", "build", "instant", "vault", "clarify")
# What the decider's "mode" job should pick; it has no clarify option.
DECIDER_MODE = {"act": "act", "agent": "agent", "converse": "converse", "build": "build",
                "instant": "converse", "vault": "converse"}
BRAIN_MODE = {**DECIDER_MODE, "clarify": "clarify"}
RANK = {"allow": 0, "confirm": 1, "ratchet": 1, "deny": 2}
PARTS = ("parser", "registry", "decider", "vault", "grounding", "judge", "brain")
JUDGE = _jarvis_home() / "tests" / "eval" / "judge.jsonl"
SHOW = 5  # failures listed per part
FOLDS = 5  # cross-validation folds for the decider


def load(path=CASES):
    """(meta, cases) from a cases file; ValueError naming the line of a bad case."""
    meta, cases = {}, []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        case = json.loads(line)
        if "_meta" in case:
            meta = case["_meta"]
            continue
        problem = _invalid(case)
        if problem:
            raise ValueError(f"{path}:{n}: {problem}")
        cases.append({**case, "line": n})
    return meta, cases


def _invalid(case):
    if not isinstance(case.get("text"), str) or not case["text"].strip():
        return "needs text"
    if case.get("type") not in TYPES:
        return f"type must be one of {', '.join(TYPES)}"
    if case["type"] == "act":
        commands = case.get("commands")
        if not isinstance(commands, list) or not commands or not all(
                isinstance(c, dict) and c.get("intent") and c.get("device") for c in commands):
            return "an act case needs commands with intent and device"
    if case.get("mode") not in (None, *BRAIN_MODE.values()):
        return f"mode must be one of {', '.join(sorted(set(BRAIN_MODE.values())))}"
    if case["type"] == "vault":
        vault = case.get("vault") or {}
        if vault.get("answer") != "instant" and not vault.get("notes"):
            return 'a vault case needs vault {"answer": "instant"} or {"notes": [...]}'
    return None


def matches(got, want):
    """Commands against labels: same order and intents and devices; args and depends_on
    only where the label gives them."""
    if not got or len(got) != len(want):
        return False
    return all(g.get("intent") == w["intent"] and g.get("device") == w["device"]
               and ("args" not in w or g.get("args") == w["args"])
               and ("depends_on" not in w or (g.get("depends_on") or []) == w["depends_on"])
               for g, w in zip(got, want))


def _short(commands):
    return [[c.get("intent"), c.get("device"), c.get("args")] for c in commands or ()]


def _part(rows, **extra):
    """{"score", "n", "correct", "fails"} from [(ok, failure-or-None)]."""
    correct = sum(ok for ok, _ in rows)
    return {"score": round(correct / len(rows), 4) if rows else None, "n": len(rows),
            "correct": correct, **extra, "fails": [f for ok, f in rows if not ok]}


# -- the parts ------------------------------------------------------------------------------
def score_parser(cases, config, origin):
    """Act cases must parse to their labels; everything else must go on to the interpreter.
    `wrong` counts certain-but-wrong parses, the failure the parser must never have."""
    from .parse import parse_for
    rows, wrong, missed = [], 0, 0
    for case in cases:
        got = parse_for(case["text"], config, case.get("origin", origin))
        commands = got["commands"]
        if case["type"] == "act":
            ok = commands is not None and matches(commands, case["commands"])
            wrong += commands is not None and not ok
            missed += commands is None
            want = _short(case["commands"])
        else:
            ok = commands is None
            wrong += not ok
            want = "no parse"
        rows.append((ok, None if ok else {"text": case["text"], "want": want,
                                          "got": _short(commands) or got["reason"]}))
    return _part(rows, wrong=wrong, missed=missed)


def score_registry(cases, devices):
    """Act cases whose labels name every argument: they validate and their risk is the
    label's (allow unless it says otherwise)."""
    from . import registry
    rows = []
    for case in cases:
        if case["type"] != "act" or not all("args" in c for c in case["commands"]):
            continue
        commands = [{"id": f"c{n}", "intent": c["intent"], "device": c["device"],
                     "args": c["args"], "depends_on": c.get("depends_on", [])}
                    for n, c in enumerate(case["commands"], 1)]
        errors = registry.validate(commands, devices)
        risk = None
        if not errors:
            risk = max((registry.risk(c) for c in commands), key=RANK.get)
            if risk != case.get("risk", "allow"):
                errors.append(f"risk {risk}, want {case.get('risk', 'allow')}")
        rows.append((not errors, errors and {"text": case["text"], "got": errors}))
    return _part(rows)


def mode_for(case):
    """The mode the decider and the brain should pick for a case (None: not a decider option)."""
    return case.get("mode") or DECIDER_MODE.get(case["type"])


def _fold(text, folds):
    from .decide import _key, clean
    return int(hashlib.sha256(_key(clean(text)).encode()).hexdigest(), 16) % folds


def score_decider(cases, folds=FOLDS):
    """The decider's "mode" pick against each case's type, cross-validated: the decider
    learns from these cases too, so each one is scored by a decider that never saw its
    fold. Also the confidence threshold whose precision reaches decide.TARGET, and how
    many requests it would cover; `ready` only when every option has decide.MIN_LABELS
    labels as well. None when the embedding model isn't there (nothing to measure)."""
    from . import decide
    from .embed import embed
    scored = [c for c in cases if mode_for(c)]
    options = list(decide.BASE["mode"])
    fixed = [(t, o) for o, ts in decide.BASE["mode"].items() for t in ts]
    n_builtin = len(fixed)
    fixed += decide.examples("mode", held_out=[c["text"] for c in cases])
    vectors = embed([decide.clean(t) for t, _ in fixed] + [decide.clean(c["text"]) for c in scored])
    if vectors is None:
        return None
    import numpy as np
    train = decide.features("mode", [t for t, _ in fixed], vectors[:len(fixed)])
    test = decide.features("mode", [c["text"] for c in scored], vectors[len(fixed):])
    fixed_y = np.array([options.index(o) for _, o in fixed])
    case_y = np.array([options.index(mode_for(c)) for c in scored])
    fold = np.array([_fold(c["text"], folds) for c in scored])
    probs = np.zeros((len(scored), len(options)))
    builtin = np.array([decide._key(decide.clean(t)) for t, _ in fixed[:n_builtin]] +
                       [None] * (len(fixed) - n_builtin), dtype=object)
    cleaned = [decide._key(decide.clean(c["text"])) for c in scored]
    for k in range(folds):  # the same classifier decide.choose() runs, trained without fold k
        seen = fold != k
        # A built-in example word for word equal to a test case would be scored on its own training.
        held = {_t for _t, f in zip(cleaned, fold) if f == k}
        keep = np.array([b is None or b not in held for b in builtin])
        model = decide.fit(np.vstack([train[keep], test[seen]]),
                           np.concatenate([fixed_y[keep], case_y[seen]]), len(options))
        if (~seen).any():
            probs[~seen] = decide.predict(model, test[~seen])
    rows, picks = [], []
    for i, case in enumerate(scored):
        winner = options[int(probs[i].argmax())]
        confidence = float(probs[i].max())
        want = mode_for(case)
        picks.append((confidence, winner == want))
        rows.append((winner == want, None if winner == want else
                     {"text": case["text"], "want": want, "got": f"{winner} {confidence:.2f}"}))
    picks.sort(reverse=True)
    threshold, coverage, correct = None, 0.0, 0
    for n, (confidence, ok) in enumerate(picks, 1):
        correct += ok
        if n >= 20 and correct / n >= decide.TARGET:
            threshold, coverage = confidence, n / len(picks)
    labels = {o: sum(o == opt for _, opt in fixed) + sum(mode_for(c) == o for c in scored)
              for o in options}
    ready = threshold is not None and min(labels.values()) >= decide.MIN_LABELS
    return _part(rows, folds=folds, labels=labels, threshold=threshold and round(threshold, 4),
                 coverage=round(coverage, 3), ready=ready)


def score_vault(cases, answer, recall):
    """Vault cases get their instant answer or recall their notes; questions that aren't
    about the vault get neither (a wrong instant answer hijacks the turn, stray recall is
    noise in the brain's prompt). answer(text) -> str|None, recall(text) -> [lines]."""
    rows, false_answers, noisy = [], 0, 0
    for case in cases:
        if case["type"] not in ("vault", "converse", "instant"):
            continue
        text, label = case["text"], case.get("vault") or {}
        said = answer(text)
        if case["type"] != "vault":
            lines = recall(text)
            ok = said is None and not lines
            false_answers += said is not None
            noisy += bool(lines)
            got = said or lines
            want = "nothing from the vault"
        elif label.get("answer") == "instant":
            ok, got, want = said is not None, said, "an instant answer"
        else:
            lines = recall(text)
            ok = any(f"[vault:{note}" in line for note in label["notes"] for line in lines)
            got, want = [line.split("]")[0] + "]" for line in lines], label["notes"]
        rows.append((ok, None if ok else {"text": text, "want": want, "got": got}))
    return _part(rows, false_answers=false_answers, noisy_recall=noisy)


def score_grounding(path=GROUNDING):
    """Phase 4, for free: does a live-state request reach the tools lane (Brain.pick with
    its default regexes), and is an excuse with no grounds caught (the talk lane's
    is_refusal, the tools lane's ungrounded) while real answers and real limits pass?"""
    from .chatgpt import Brain, is_refusal, ungrounded
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    brain, rows = Brain(None, None), []
    for line in lines:
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        case = json.loads(line)
        if "_meta" in case:
            continue
        if "route" in case:
            text, want, got = case["route"], case["lane"], brain.pick(case["route"])
        elif "talk" in case:
            text, want, got = case["talk"], case["excuse"], is_refusal(case["talk"])
        else:
            log = [tuple(x) for x in case.get("log") or ()]
            text, want, got = case["tools"], case["excuse"], ungrounded(case["tools"], log) is not None
        rows.append((got == want, None if got == want else {"text": text, "want": want, "got": got}))
    return _part(rows)


def score_judge(cases, path=JUDGE):
    """brain.judge: does a request get planned? Labelled rows in tests/eval/judge.jsonl, plus
    every act/instant/vault case as "no plan" (the fast path must never be slowed down)."""
    from .brain import judge
    rows = [(c["text"], bool(c["plan"])) for c in _jsonl(path)] if Path(path).is_file() else []
    rows += [(c["text"], False) for c in cases if c.get("type") in ("act", "instant", "vault")]
    out = []
    for text, want in rows:
        got = judge(text)["plan"]
        out.append((got == want, None if got == want else {"text": text, "want": want, "got": got}))
    return _part(out)


def _jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("{\"_meta\"")]


def sample(cases, size, seed=0):
    """A fixed sample spread over the types, so brain runs compare with each other."""
    rng = random.Random(seed)
    by_type = {}
    for case in cases:
        by_type.setdefault(case["type"], []).append(case)
    for group in by_type.values():
        rng.shuffle(group)
    picked = []
    while len(picked) < size and any(by_type.values()):
        for kind in TYPES:
            if by_type.get(kind) and len(picked) < size:
                picked.append(by_type[kind].pop())
    return picked


def score_brain(cases, config, base, origin, ask=None):
    """The interpreter's reading of each case: the mode its type implies, and for act the
    labelled commands. No reading at all counts as wrong."""
    from .chatgpt import usage_limit
    from .interpret import interpret
    rows = []
    for case in cases:
        if ask is None and usage_limit():  # the plan said no: that's not the brain being wrong
            return {"skipped": f"ChatGPT is over its usage limit {usage_limit()}".strip()}
        read = interpret(case["text"], config, base, case.get("origin", origin), ask=ask)
        if read is None and ask is None and usage_limit():
            return {"skipped": f"ChatGPT is over its usage limit {usage_limit()}".strip()}
        want = case.get("mode") or BRAIN_MODE[case["type"]]
        ok = read is not None and read["mode"] == want and (
            want != "act" or matches(read.get("commands"), case["commands"]))
        got = read and (read["mode"] + (f" {_short(read.get('commands'))}" if read["mode"] == "act" else ""))
        rows.append((ok, None if ok else {"text": case["text"], "got": got or "no reading",
                                          "want": want if want != "act" else _short(case["commands"])}))
    return _part(rows)


# -- a run ----------------------------------------------------------------------------------
def _vault_tools(config, state):
    """(answer, recall) for the configured vault, or None without one."""
    from .assistant import vault_answer
    from .vault import vault_for
    vault = vault_for(config, state)
    if vault is None:
        return None
    return (lambda text: vault_answer(text, config, state)), (lambda text: vault.context(text))


def _rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HOME, capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run(config, base, state, path=CASES, brain=False, size=8, seed=0, save=True):
    """Score every part; returns the report (with `previous` and `drops`), saved unless save=False."""
    meta, cases = load(path)
    origin = meta.get("origin") or (config.get("cluster") or {}).get("main_device")
    parts, notes = {}, {}
    started = time.time()
    parts["parser"] = score_parser(cases, config, origin)
    parts["registry"] = score_registry(cases, config.get("devices", {}))
    decided = score_decider(cases)
    if decided is None:
        notes["decider"] = "the embedding model isn't available"
    else:
        parts["decider"] = decided
    tools = _vault_tools(config, state)
    if tools is None:
        notes["vault"] = "no vault configured on this PC"
    else:
        parts["vault"] = score_vault(cases, *tools)
    grounded = score_grounding()
    if grounded is None:
        notes["grounding"] = "no tests/eval/grounding.jsonl"
    else:
        parts["grounding"] = grounded
    parts["judge"] = score_judge(cases)
    if brain:
        picked = sample(cases, size, seed)
        scored = score_brain(picked, config, base, origin)
        if "skipped" in scored:
            notes["brain"] = f"skipped: {scored['skipped']} (not counted as a score)"
        else:
            parts["brain"] = {**scored, "sample": size, "seed": seed}
    else:
        notes["brain"] = "not run (jarvis eval --brain uses the subscription)"
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "rev": _rev(), "cases": len(cases),
              "cases_sha": digest, "seconds": round(time.time() - started, 1),
              "parts": parts, "notes": notes}
    log = Path(state) / "eval" / "runs.jsonl"
    report["previous"], report["drops"] = compare(report, history(log))
    if save:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as out:
            out.write(json.dumps({k: v for k, v in report.items() if k not in ("previous", "drops")},
                                 ensure_ascii=False) + "\n")
        report["saved"] = str(log)
    return report


def history(log):
    """Earlier runs, oldest first."""
    try:
        lines = Path(log).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    runs = []
    for line in lines:
        try:
            runs.append(json.loads(line))
        except ValueError:
            continue
    return runs


def compare(report, runs):
    """({part: {"score", "at", "same_cases"}}, [parts that dropped]): each part against
    the latest earlier run that scored it. A brain sample with another size or seed
    isn't comparable, so it's skipped."""
    previous, drops = {}, []
    for name, part in report["parts"].items():
        for old in reversed(runs):
            before = (old.get("parts") or {}).get(name)
            if not before or before.get("score") is None:
                continue
            if name == "brain" and (before.get("sample"), before.get("seed")) != (part.get("sample"), part.get("seed")):
                continue
            previous[name] = {"score": before["score"], "at": old.get("at"),
                              "same_cases": old.get("cases_sha") == report["cases_sha"]}
            if part["score"] is not None and part["score"] < before["score"] - 1e-9:
                drops.append(name)
            break
    return previous, drops


def decide_target():
    from .decide import TARGET
    return TARGET


def render(report, show=SHOW):
    """The scorecard as text: one line per part, then the failures."""
    lines = [f"JARVIS scorecard: {report['cases']} cases, rev {report.get('rev') or '?'}, "
             f"{report['seconds']}s"]
    for name in PARTS:
        part = report["parts"].get(name)
        if part is None:
            lines.append(f"  {name:<9} -      {report['notes'].get(name, '')}")
            continue
        old = report["previous"].get(name)
        trend = ""
        if old and part["score"] is not None and old.get("score") is not None:
            delta = part["score"] - old["score"]
            mark = "DROP" if name in report["drops"] else "up" if delta > 1e-9 else "same"
            trend = f"  {mark} {delta:+.3f} vs {old['at']}" + ("" if old["same_cases"] else " (cases changed)")
        extra = "".join(f"  {k} {part[k]}" for k in ("wrong", "missed", "false_answers", "noisy_recall")
                        if k in part)
        if "ready" in part:
            extra += (f"  at {decide_target():.0%} precision: coverage {part['coverage']:.0%}"
                      + (f" (confidence >= {part['threshold']})" if part["threshold"] else "")
                      + ("  READY" if part["ready"] else "  stays in shadow"))
        score = "-    " if part["score"] is None else f"{part['score']:.3f}"
        lines.append(f"  {name:<9} {score}  {part['correct']}/{part['n']}{extra}{trend}")
    for name in PARTS:
        fails = (report["parts"].get(name) or {}).get("fails") or []
        if fails and show:
            lines.append(f"\n{name} failures ({len(fails)}):")
            for fail in fails[:show]:
                want = f"  want {json.dumps(fail['want'], ensure_ascii=False)}" if "want" in fail else ""
                lines.append(f"  - {fail['text'][:80]!r}{want}  got {json.dumps(fail['got'], ensure_ascii=False)[:160]}")
            if len(fails) > show:
                lines.append(f"  ... {len(fails) - show} more (--show {len(fails)})")
    if report["drops"]:
        lines.append(f"\nDROPPED: {', '.join(report['drops'])}")
    if report.get("saved"):
        lines.append(f"\nSaved to {report['saved']}")
    return "\n".join(lines)
