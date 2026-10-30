"""The fast path: strict parser -> gates -> direct executor (docs/jarvis-os-architecture.md).

assistant.ask() calls fast_path() first. It returns None whenever the request isn't
certainly a set of direct actions JARVIS can run right now, and the request then takes
the normal route to the brain unchanged. So this can only make JARVIS faster, never
change what the brain would have done with anything it doesn't fully understand.

    fast_path("open chrome on arch and spotify on the dell", config, state)
    -> {"reply": "On the zenbook: Opened Chrome. Opened Spotify.", "commands": [...],
        "outcomes": [...], "checks": [...]}

Outcome states come from registry.OUTCOMES. Nothing is retried: a failure is reported.
"""

import json
import re
import threading
import time
import uuid

from . import gates
from .parse import parse_for
from .remote import Remote

# Registry intent -> (desktop action, its argument) for the direct executor.
DIRECT = {"open_app": ("open_app", "name"), "open_url": ("open_url", "url"),
          "focus_window": ("focus_window", "name"), "close_app": ("close_app", "name"),
          "media": ("media", "command"), "volume": ("volume", "level"),
          "list_apps": ("list_apps", "query"), "scroll": ("scroll", None),
          "browse": ("browse", None), "terminal": ("terminal", None)}
# spotify_play is not here on purpose: tested live on 2026-10-26, a freshly opened Spotify
# has no media session, so neither the media key nor Space starts Liked Songs; it needs a
# click on the page's Play button. That's UI judgement, so it goes to Grok Bot.
# Failure texts that mean "it may have run": never pre_effect_failure (which allows
# escalation). The cluster says "Node action <id> is uncertain; do not automatically retry."
UNCERTAIN = ("timed out", "no confirmed result", "may have run", "uncertain")
# Executors that read the state first and found nothing to do say so this way.
ALREADY = ("Already playing.", "Already paused.")
INFLIGHT = gates.InFlight()
_TOOLS, _TOOLS_LOCK = None, threading.Lock()


def _local_tools():
    """One DesktopTools per process, so its installed-app list stays cached."""
    global _TOOLS
    with _TOOLS_LOCK:
        if _TOOLS is None:
            from .actions import DesktopTools
            _TOOLS = DesktopTools()
        return _TOOLS


# Parser reasons that mean "this isn't an action at all": conversation for the brain.
CHAT_REASONS = ("no action verb", "empty or too long", "nothing left after the wake word")


def needs_agent(text, config, origin):
    """True when the request is an action the fast path can't run itself: the parser saw
    an action but wasn't certain of it ("play that song kevin sent me", "reply to sam"),
    or it parsed to an intent with no direct executor. Those go to
    Grok Bot, behind its preflight checks. Conversation and questions return False and
    stay with the brain."""
    if origin not in config.get("devices", {}):
        return False
    parsed = parse_for(text, config, origin)
    if parsed["commands"]:
        return any(c["intent"] not in DIRECT for c in parsed["commands"])
    if not parsed["reason"].startswith("no action verb"):
        return not any(parsed["reason"].startswith(r) for r in CHAT_REASONS)
    return lead_word(text) in AGENT_VERBS  # "buy...", "reply to...": work, not talk


# Verbs that ask for work the parser has no direct action for. Only used to route a
# request to Grok (after its preflight), never to run anything.
AGENT_VERBS = frozenset((
    "buy", "order", "book", "reserve", "pay", "send", "email", "message", "text", "dm",
    "reply", "respond", "post", "tweet", "share", "upload", "download", "install",
    "uninstall", "update", "subscribe", "unsubscribe", "delete", "remove", "cancel",
    "schedule", "add", "create", "write", "draft", "fill", "submit", "sign", "search",
    "find", "look", "google", "check", "watch", "listen", "call", "join", "log", "like",
    "follow", "comment", "save", "bookmark", "print", "translate", "summarize", "type",
))


def lead_word(text):
    """The first word after the wake word and politeness ("jarvis, can you buy..." -> buy)."""
    from .parse import FILLER, LEAD
    text = LEAD.sub("", str(text).replace("’", "'")).lower()
    words = re.sub(r"\s+", " ", FILLER.sub(" ", text)).strip().split()
    return re.sub(r"[^a-z]", "", words[0]) if words else ""


def warm():
    """Load the installed-app list in the background (about 1.3 s on Windows), so the
    first direct action after startup doesn't wait for it. Safe to call repeatedly."""
    def load():
        try:
            _local_tools().apps()
        except Exception:
            pass  # the gate falls back to letting the executor decide
    threading.Thread(target=load, name="jarvis-app-cache", daemon=True).start()


def fast_path(text, config, state, step=None, origin=None, origin_class="owner",
              remote=None, app_exists=None, history=None, on_begin=None, channel="cli"):
    """A finished turn for certain, runnable direct actions; None for everything else.
    step(id, parent, label, detail, state) reports the connection tree, like ask().
    on_begin(commands) is called once the request is certainly ours, before any step is
    reported, so the caller can start the turn (the console drops steps of unknown turns).
    history: a History to check (H1, H3) and record into; False turns it off."""
    started = time.monotonic()
    remote = remote or Remote(config, state, tools=None)
    origin = origin or remote.here()
    if origin not in config.get("devices", {}):
        return None  # an unconfigured PC can't resolve "here"
    commands = parse_for(text, config, origin)["commands"]
    if not commands or any(c["intent"] not in DIRECT for c in commands):
        return None  # not certain, or no direct executor: the brain or Grok handles it
    return run_commands(text, commands, config, state, step=step, origin=origin,
                        origin_class=origin_class, remote=remote, app_exists=app_exists,
                        history=history, on_begin=on_begin, channel=channel,
                        source="Parser", detail="no model", started=started)


def run_commands(text, commands, config, state, step=None, origin=None, origin_class="owner",
                 remote=None, app_exists=None, history=None, on_begin=None, channel="cli",
                 source="Parser", detail="no model", started=None):
    """Run already-understood direct commands (from the parser or the interpreter) through
    the same gates, history, executor and observe steps. None if they aren't all valid,
    runnable Registry commands, so a bad interpretation never acts."""
    from . import registry
    started = started or time.monotonic()
    step = step or (lambda *a, **k: None)
    remote = remote or Remote(config, state, tools=None)
    origin = origin or remote.here()
    devices = config.get("devices", {})
    if (origin not in devices or not commands or registry.validate(commands, devices)
            or any(c["intent"] not in DIRECT for c in commands)):
        return None
    parse_ms = _ms(started)
    if on_begin:
        on_begin(commands)
    step("parse", "root", source,
         f"{len(commands)} command{'s' * (len(commands) > 1)}, {detail} · {parse_ms} ms")
    if history is None:
        history = _history(state)
    turn = {"request": uuid.uuid4().hex, "text": text, "origin": origin,
            "origin_class": origin_class, "parse_ms": parse_ms, "history": history}
    result = _run_turn(commands, devices, origin, origin_class, remote, app_exists, step,
                       history, state=state, channel=channel, request=turn["request"],
                       rule=gates.trusted_rules(config))
    _record(turn, commands, result, started)
    return result


def _run_turn(commands, devices, origin, origin_class, remote, app_exists, step, history,
              state=None, channel="cli", confirmed=False, request=None, rule=None):
    gate_started = time.monotonic()
    skip = {c["id"]: gates.skips(c, origin, origin_class, rule) for c in commands}
    for command in commands:  # a step something waits on keeps its on-screen check:
        for dep in command.get("depends_on", []):  # it says the app is ready for the next
            skip[dep] = skip.get(dep, frozenset()) - {"observe"}

    if app_exists is None:
        def app_exists(device, name):
            if device != origin:
                return None  # the node checks its own apps when the action arrives
            try:
                return bool(_local_tools().match(name))
            except Exception:
                return None

    verdicts = []
    for command in commands:
        checked = time.monotonic()
        verdict = gates.check(command, devices, origin_class, confirmed=confirmed,
                              app_exists=None if "app_exists" in skip[command["id"]] else app_exists)
        verdicts.append(verdict)
        step(f"gate:{command['id']}", "parse", f"Gates · {command['intent']}",
             f"{'passed' if verdict['pass'] else verdict['why']} · {_ms(checked)} ms",
             "done" if verdict["pass"] else "fail")
    # H-checks: what happened to these same commands before.
    warnings, alerts = [], False
    parkable = [state is not None and not v["pass"] and v["risk"] == "confirm"
                and not c.get("depends_on") for c, v in zip(commands, verdicts)]
    if history and all(v["pass"] or p for v, p in zip(verdicts, parkable)):
        for command, verdict, parked in zip(commands, verdicts, parkable):
            if parked or "history" in skip[command["id"]]:
                continue
            try:
                past = history.check(command, origin_class)
            except Exception:  # an unreadable history never blocks a request
                continue
            verdict["checks"] += past["checks"]
            if past["block"]:
                verdict.update({"pass": False, "risk": None, "why": next(
                    c["why"] for c in past["checks"] if not c["pass"])})
                alerts = alerts or past["alert"]
            elif past["warn"]:
                warnings.append(past["warn"])
            step(f"hist:{command['id']}", f"gate:{command['id']}", "History",
                 "blocked" if past["block"] else past["warn"] or "clear",
                 "fail" if past["block"] else "done")
    gates_ms = _ms(gate_started)
    blocked = [(c, v) for c, v in zip(commands, verdicts) if not v["pass"]]
    if blocked and not all(p or v["pass"] for v, p in zip(verdicts, parkable)):
        # Something hard-failed: hold the whole request, parking only what can be parked.
        from .confirm import ConfirmQueue, prompt
        pending, replies = [], []
        for command, verdict in blocked:
            if verdict["risk"] == "confirm" and state is not None:
                if command.get("depends_on"):
                    replies.append("Please request that action separately so I can confirm it safely.")
                else:
                    item = ConfirmQueue(state).park(command, origin, channel)
                    pending.append(item)
                    replies.append(prompt(item))
            else:
                replies.append(_blocked_reply(command, verdict))
        reply = " ".join(replies)
        if alerts:
            reply = ("I stopped a command that keeps repeating from an agent, which looks "
                     "like a loop. " + reply)
        return {"reply": reply, "commands": commands, "checks": verdicts, "alert": alerts,
                "confirmations": pending,
                "gates_ms": gates_ms,
                "outcomes": [{"id": c["id"], "state": "not_sent", "ok": False,
                              "text": v["why"] or ""} for c, v in zip(commands, verdicts)]}
    if blocked:
        # Only risky commands are waiting for a yes: park those, hold what depends on
        # them, and run the rest instead of silently dropping it.
        from .confirm import ConfirmQueue, prompt
        held, notes, pending, outcomes = {c["id"] for c, _ in blocked}, [], [], {}
        for command, _ in blocked:
            item = ConfirmQueue(state).park(command, origin, channel)
            pending.append(item)
            notes.append(prompt(item))
            outcomes[command["id"]] = {"id": command["id"], "state": "not_sent", "ok": False,
                                       "text": f"Waiting for your OK ({item['id']})."}
        grew = True
        while grew:  # whatever depends on a held command stays held too
            grew = False
            for command in commands:
                if command["id"] not in held and held & set(command["depends_on"]):
                    held.add(command["id"])
                    grew = True
                    outcomes[command["id"]] = {"id": command["id"], "state": "not_sent", "ok": False,
                                               "text": "Held: it follows a step that needs your OK."}
                    notes.append(f"I held {command['intent'].replace('_', ' ')} "
                                 f"{next(iter(command['args'].values()))} until that's confirmed.")
        runnable = [c for c in commands if c["id"] not in held]
        outcomes.update(_execute(runnable, remote, step, request, skip))
        done = _reply(runnable, outcomes, origin) if runnable else ""
        reply = " ".join(filter(None, [*([" ".join(dict.fromkeys(warnings))] if warnings else []),
                                       done, *notes]))
        return {"reply": reply, "commands": commands, "checks": verdicts, "alert": alerts,
                "confirmations": pending, "gates_ms": gates_ms,
                "outcomes": [outcomes[c["id"]] for c in commands]}

    outcomes = _execute(commands, remote, step, request, skip)
    reply = _reply(commands, outcomes, origin)
    if warnings:
        reply = " ".join(dict.fromkeys(warnings)) + " " + reply
    return {"reply": reply, "commands": commands, "gates_ms": gates_ms,
            "checks": verdicts, "outcomes": [outcomes[c["id"]] for c in commands]}


def _ms(since):
    return round((time.monotonic() - since) * 1000)


def run_confirmed(command, config, state, step=None, origin=None, origin_class="owner",
                  remote=None, app_exists=None, history=None):
    """Run a consumed approval through every non-policy gate and normal recording."""
    started = time.monotonic()
    remote = remote or Remote(config, state, tools=None)
    origin = origin or remote.here()
    step = step or (lambda *a, **k: None)
    if command.get("intent") not in DIRECT or command.get("depends_on"):
        raise ValueError("This confirmed command cannot run independently.")
    history = _history(state) if history is None else history
    result = _run_turn([command], config.get("devices", {}), origin, origin_class,
                       remote, app_exists, step, history, confirmed=True)
    _record({"request": uuid.uuid4().hex, "text": "Confirmed: " + str(command),
             "origin": origin, "origin_class": origin_class, "parse_ms": 0,
             "history": history}, [command], result, started)
    return result


_HISTORIES, _HISTORIES_LOCK = {}, threading.Lock()


def _history(state):
    """One History per state directory; None (no checks, no records) if it can't open."""
    key = str(state)
    with _HISTORIES_LOCK:
        if key not in _HISTORIES:
            try:
                from .history import History
                _HISTORIES[key] = History(state)
            except Exception:
                _HISTORIES[key] = None
        return _HISTORIES[key]


def _record(turn, commands, result, started):
    """Every command, sent or not, with its hops. Recording never fails the turn."""
    history = turn["history"]
    if not history:
        return
    total = _ms(started)
    for command, outcome in zip(commands, result["outcomes"]):
        hops = [{"name": "parse", "ms": turn["parse_ms"]},
                {"name": "gates", "ms": result.get("gates_ms", 0)}]
        if outcome.get("run_ms") is not None:
            hops.append({"name": "run", "ms": outcome["run_ms"],
                         "state": "done" if outcome.get("ok") else "fail"})
        if outcome.get("observe_ms") is not None:
            hops.append({"name": "observe", "ms": outcome["observe_ms"],
                         "state": "done" if outcome["state"] == "observed" else "fail"})
        try:
            outcome["history"] = history.record(
                turn["request"], command, outcome, turn["text"], turn["origin"],
                turn["origin_class"], hops, total)
        except Exception:
            pass


def _blocked_reply(command, verdict):
    what = f"{command['intent'].replace('_', ' ')} {next(iter(command['args'].values()))}"
    if verdict["risk"] == "confirm":
        return (f"I need your OK to {what} on {command['device']}, and I can't ask for "
                "confirmations yet, so I didn't do it.")
    return verdict["why"]


def _execute(commands, remote, step, request=None, skip=None):
    """Run commands in dependency waves; independent ones run at the same time.
    skip: {command id: checks it may skip} from the trusted_local rule."""
    outcomes, lock, skip = {}, threading.Lock(), skip or {}

    def run(command):
        action, arg = DIRECT[command["intent"]]
        label = f"{command['intent']} · {command['device']}"
        skipped = skip.get(command["id"], frozenset())
        if "dedupe" not in skipped and not INFLIGHT.claim(command, request):
            outcome = {"id": command["id"], "state": "merged", "ok": True,
                       "text": "Already doing that."}
        else:
            before = None if "observe" in skipped else _snapshot(command, remote)
            step(f"run:{command['id']}", "parse", label, "sending", "run")
            sent, ok = time.monotonic(), False
            try:
                value = json.dumps(command["args"]) if arg is None else command["args"][arg]
                if action == "open_url" and command["args"].get("browser"):
                    value = json.dumps(command["args"])
                ok, text = remote.call(command["device"], action, value)
            except Exception as exc:  # the executor must never take the turn down
                ok, text = False, f"{type(exc).__name__}: {exc}"
            finally:
                if "dedupe" not in skipped:  # only what claimed the merge window holds it
                    INFLIGHT.release(command, ok, request)
            state = "acknowledged" if ok else (
                "uncertain" if any(u in text.lower() for u in UNCERTAIN) else "pre_effect_failure")
            outcome = {"id": command["id"], "state": state, "ok": ok, "text": text,
                       "run_ms": _ms(sent)}
            step(f"run:{command['id']}", "parse", label, f"{text} · {outcome['run_ms']} ms",
                 "done" if ok else "fail")
            if ok and text.startswith(ALREADY):
                # The executor read the state first and did nothing ("Already paused."):
                # true, but not a change it caused, so it never counts as observed.
                outcome["state"] = "unchanged"
            elif ok and "observe" not in skipped:
                watched = time.monotonic()
                seen = _observe(command, remote, step, label, before)
                if seen:
                    outcome.update(seen, observe_ms=_ms(watched))
                if outcome["state"] == "unconfirmed":
                    outcome["text"] += " I couldn't confirm it on screen yet."
                elif outcome["state"] == "unchanged":
                    outcome["text"] += " Nothing changed on screen, though: it already looked like that."
        with lock:
            outcomes[command["id"]] = outcome

    pending = list(commands)
    while pending:
        wave = [c for c in pending if all(d in outcomes for d in c["depends_on"])]
        for command in wave:
            pending.remove(command)
        runnable = []
        for command in wave:
            failed = [d for d in command["depends_on"] if not outcomes[d]["ok"]]
            if failed:
                outcomes[command["id"]] = {"id": command["id"], "state": "not_sent",
                                           "ok": False, "text": "Skipped: the step before it failed."}
            else:
                runnable.append(command)
        threads = [threading.Thread(target=run, args=(c,), daemon=True) for c in runnable[1:]]
        for thread in threads:
            thread.start()
        if runnable:
            run(runnable[0])
        for thread in threads:
            thread.join()
    return outcomes


def _snapshot(command, remote):
    """The acting machine's digest of the state the observer will look at, taken before
    the action runs; None when there is nothing to compare or it couldn't be read."""
    from . import registry
    from .observe import predicate_for, wants_snapshot
    predicate = predicate_for(command)
    if predicate is None or not registry.get(command["intent"]).settle \
            or not wants_snapshot(predicate):
        return None
    try:
        ok, text = remote.call(command["device"], "observe", json.dumps({"snapshot": predicate}))
        before = (json.loads(text) or {}).get("before") if ok else None
    except Exception:
        return None
    return before if isinstance(before, str) else None


def _observe(command, remote, step, label, before=None):
    """observed / unchanged / unconfirmed from the acting machine's own check; {} when
    the effect can't be seen as a window, or the check itself couldn't run (stays
    acknowledged). unchanged: the predicate holds but already did before the action.
    Never a retry: an unconfirmed action is reported, not repeated."""
    from . import registry
    from .observe import predicate_for
    predicate = predicate_for(command)
    settle = registry.get(command["intent"]).settle
    if predicate is None or not settle:
        return {}
    step(f"obs:{command['id']}", f"run:{command['id']}", "Observe", f"up to {settle:g} s", "run")
    body = {"predicate": predicate, "settle": settle}
    if before:
        body["before"] = before
    try:
        ok, text = remote.call(command["device"], "observe", json.dumps(body))
        seen = json.loads(text) if ok else None
    except Exception:
        ok, seen = False, None
    if not isinstance(seen, dict) or "match" not in seen:
        step(f"obs:{command['id']}", f"run:{command['id']}", "Observe", "couldn't check", "fail")
        return {}
    state = ("unconfirmed" if not seen["match"] else
             "unchanged" if seen.get("changed") is False else "observed")
    step(f"obs:{command['id']}", f"run:{command['id']}", "Observe",
         f"{state} after {seen.get('after_ms', '?')} ms", "done" if seen["match"] else "fail")
    return {"state": state, "observed_ms": seen.get("after_ms")}


def _reply(commands, outcomes, origin):
    """One short spoken reply, grouped by PC when more than one is involved."""
    devices = list(dict.fromkeys(c["device"] for c in commands))
    if devices == [origin]:
        return " ".join(outcomes[c["id"]]["text"] for c in commands)
    parts = []
    for device in devices:
        texts = " ".join(outcomes[c["id"]]["text"] for c in commands if c["device"] == device)
        parts.append(f"On the {device}: {texts}")
    return " ".join(parts)
