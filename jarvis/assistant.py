"""One entry point for text and voice: route the request, do the work, remember it."""

import re
from pathlib import Path
import threading

from .adapters import call_api
from .instant import answer as instant_answer
from .knowledge import recall as kb_recall
from .memory import CONVERSATION
from .orchestrator import Orchestrator
from .router import classify, grok_first, pick_adapter
from .runner import run_task
from .vault import recall as vault_recall

def vault_answer(text, config, state):
    """Briefings, today's agenda and what's due, read from the vault (briefing.py).
    None when there's no vault, the question isn't one of those, or anything fails."""
    try:
        from .briefing import answer
        from .tasks import tasks_for
        from .vault import vault_for
        vault = vault_for(config, state)
        if not vault:
            return None
        try:
            from .memory import Memory
            memory = Memory(Path(state))
        except Exception:  # no memory: the briefing goes without its open loops
            memory = None
        return answer(text, vault, tasks_for(vault), memory=memory)
    except Exception:
        return None


CONVERSATIONAL = ("Reply as JARVIS to the owner, David. Lead with the answer in plain "
                  "sentences; it may be read aloud, so keep it short unless detail is asked "
                  "for. Put code or long output after the spoken part.")


def decide(text, config, tier=None, conversational=False, write=False, state=None):
    """The tier a request will run on. Shared by ask() and the console's preview.
    state: the state folder, so a question the vault answers at once ("what's on my
    calendar today") stays instant instead of going to Grok for sounding like an account."""
    if tier in (None, "simple") and not write:
        # ask() answers these with no model; "instant" names which, for the console's preview.
        if instant_answer(text):
            return {"tier": "simple", "reasons": ["instant: the clock"], "instant": "clock"}
        if state is not None and vault_answer(text, config, state):
            return {"tier": "simple", "reasons": ["instant: the vault"], "instant": "vault"}
    if tier is None and not write and grok_first(text, config):
        return {"tier": "medium", "reasons": ["Grok Bot orchestrates account and web work"]}
    if (tier is None and conversational and not write
            and not config.get("routing", {}).get("auto_escalate")):
        # Conversation is the main pipeline: everything goes to the brain (ChatGPT).
        # Web/orchestration tiers run only when asked for explicitly with a tier.
        return {"tier": "simple", "reasons": ["conversation (auto_escalate off)"]}
    return classify(text, tier)


def _situation_on(config):
    """brain.situation (default on): send the live machine snapshot (situation.py). Tests
    get it only when their config asks, so the suite never lists real windows."""
    from . import _under_tests
    setting = (config.get("brain") or {}).get("situation")
    return setting is True or (setting is None and not _under_tests())


def lane_label(lane, text):
    """What the brain node of the connection tree is called for this request."""
    if hasattr(lane, "pick"):
        return "ChatGPT · tools" if lane.pick(text) == "act" else "ChatGPT · talk"
    return {"FastLane": "Claude fast lane", "ChatGPTLane": "ChatGPT"}.get(
        type(lane).__name__, type(lane).__name__)


def ask(text, store, memory, config, base, tier=None, write=False, timeout=600, lane=None,
        on_sentence=None, on_start=None, channel="cli", on_step=None):
    """One request, metered: the result carries `usage` (tokens, API-equivalent dollars,
    share of the subscription windows; see usage.py), also saved on the reply."""
    from .usage import meter
    with meter() as m:
        result = _ask(text, store, memory, config, base, tier, write, timeout, lane,
                      on_sentence, on_start, channel, on_step)
    if isinstance(result, dict) and "usage" not in result:
        spent = m.summary()
        result = {**result, "usage": spent}
        reply = (result.get("memory") or [None, None])[-1]
        if reply and memory is not None:
            try:
                memory.merge_meta(reply, {"usage": spent})
            except Exception:  # bookkeeping must never fail the turn
                pass
    return result


def _ask(text, store, memory, config, base, tier=None, write=False, timeout=600, lane=None,
         on_sentence=None, on_start=None, channel="cli", on_step=None):
    """on_sentence: optional callback for streamed sentences (fast lane only).
    on_start({"route", "message"}): called once the request is routed and remembered.
    channel: where it came from (console, voice, cli), kept on the inbound message.
    on_step(id, parent, label, detail, state): the request's connection tree as it grows,
    rooted at "root" (the request itself); state is run, done or fail."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Say or type something for JARVIS to do.")
    text = text.strip()
    from .topology import is_node
    if is_node(config):
        from .cluster import Client
        state = getattr(lane, "state", None) or (memory.path.parent if memory is not None else None)
        if state is None:
            raise ValueError("A proxy node needs its local pairing state directory.")
        result = Client(config, state).ask(text, tier, write, timeout)
        if on_start:
            on_start({"route": {"tier": result.get("tier", "simple")},
                      "message": (result.get("memory") or [None])[0]})
        if on_sentence:
            on_sentence(result.get("reply", ""))
            result = {**result, "streamed": True}
        return {**result, "via": "main"}
    routing = config.get("routing", {})
    _note_correction(text, memory)
    confirmed = _confirmation_turn(text, memory, config, base, channel,
                                   on_start, on_sentence, on_step)
    if confirmed is not None:
        return confirmed
    if tier is None and not write and routing.get("fast_path", True):
        fast = _fast_path(text, memory, config, channel, on_start, on_sentence, on_step)
        if fast is not None:
            _shadow(config, memory, text, "act")
            return fast
    # The clock or the vault answers it with no model: never hand it to Grok or the
    # interpreter for sounding like web or account work ("what's on my calendar today").
    quick = (instant_answer(text) or vault_answer(text, config, memory.path.parent)
             if tier in (None, "simple") and not write else None)
    agent = tier is None and not write and not quick and _needs_agent(text, config)
    build = False
    if agent and routing.get("interpreter"):
        # The parser saw an action it couldn't resolve: ask the interpreter before Grok.
        read = _interpret(text, memory, config, base, channel, on_start, on_sentence, on_step)
        if isinstance(read, dict):
            return read                                  # ran it, or asked David
        if read is None and lane is not None:
            agent = False   # no reading: the brain, with its desktop tools, before Grok
        elif read == "converse":
            agent = False                                # just a question: the brain
        elif read == "build":
            agent, build = False, True
    route = ({"tier": "simple", "reasons": ["instant: the clock or the vault"]}
             if quick else
             {"tier": "medium", "reasons": ["an action the parser can't run: Grok Bot"]}
             if agent else {"tier": "complex", "reasons": ["interpreter: software work"]}
             if build else decide(text, config, tier, lane is not None, write))
    if tier is None and not write:
        _shadow(config, memory, text, "agent" if agent or (not quick and grok_first(text, config)) else
                "build" if route["tier"] == "complex" else "converse")
    inbound = memory.add("message", text, source="owner", tags=["inbound", route["tier"]],
                         meta={"route": route, "channel": channel,
                               "origin_device": __import__("jarvis.topology", fromlist=["ORIGIN"]).ORIGIN.get()}, importance=0.4)
    if on_start:
        on_start({"route": route, "message": inbound["id"]})
    step = _stepper(on_step)
    built = []

    def recalled():
        # Built on first use: the clock, the vault's agenda and Grok Bot never read it, and
        # the vault, wiki, heap and embedding lookups cost more than those answers do.
        if built:
            return built[0]
        # Vault notes first (what David knows and decided), then wiki claims with their
        # sources and dates, then the heap (what was said).
        notes = vault_recall(config, memory.path.parent, text)
        claims = kb_recall(config, memory.path.parent, text)
        # The memory pack (open loops, consolidated facts and lessons that match) before the
        # raw heap, which then skips the lines the pack already has.
        pack = _memory_pack(memory, text)
        held = {line.split("]", 1)[0] for line in pack}
        heap = pack + [line for line in memory.context(text) if line.split("]", 1)[0] not in held]
        recall = notes + claims + heap
        talk = memory.conversation(skip={inbound["id"]}) if hasattr(memory, "conversation") else []
        if talk:  # earlier turns, so "go check" knows what it refers to
            recall.insert(0, CONVERSATION + "\n  " + "\n  ".join(talk))
        if _situation_on(config):  # what the computers are doing now ("close that")
            from .situation import lines
            recall[:0] = lines(config, memory.path.parent)
        from .topology import ORIGIN, enabled, identity
        if enabled(config) and ORIGIN.get():
            recall.insert(0, f"This request originates on {ORIGIN.get()}. The JARVIS main PC is {identity(config)[1]}. "
                             "Here/this PC means the requesting node. Use an explicit device ID for another PC.")
        step("recall", "root", "Recall", f"{len(notes)} vault · {len(claims)} wiki · {len(heap)} heap")
        built.append(recall)
        return recall
    step("route", "root", f"Route · {route['tier']}", "; ".join(route["reasons"]))
    reply, detail = None, {}

    if route["tier"] == "simple" and quick:
        reply = quick  # the clock or the vault: no model
        detail = {"lane": "instant", "streamed": on_sentence is not None}
        step("instant", "route", "Instant", "clock or vault, no model")
        if on_sentence:
            on_sentence(quick)

    if route["tier"] == "simple" and reply is None:
        if lane is not None and not write:
            step("brain", "route", lane_label(lane, text), "thinking", "run")
            try:
                reply = _with_tools(lane, step, lambda: lane.ask(text, recalled(),
                                                                on_sentence=on_sentence))
                detail = {"lane": type(lane).__name__, "streamed": on_sentence is not None,
                          "brain": "chatgpt" if "ChatGPT" in lane_label(lane, text) else
                          type(lane).__name__.lower()}
                step("brain", "route", lane_label(lane, text), "answered")
                _standin_done()
            except (RuntimeError, LookupError, TypeError, ValueError, OSError) as exc:
                # fall back to a normal one-shot run below, unless tools already ran:
                # then part of the request happened, and the reply must say so
                detail = {"lane_error": str(exc)}
                step("brain", "route", lane_label(lane, text), str(exc), "fail")
                ran = list(getattr(lane, "tool_log", None) or ())
                if ran:
                    reply = _partial_reply(ran)
                    detail.update(state="partial", tools=[[str(t[0]), bool(t[1])] for t in ran])
                elif "usage limit" in str(exc).lower() and \
                        routing.get("simple_provider", "claude") == "claude":
                    reply = _standin_reply(text, recalled(), config, base, timeout,
                                           on_sentence, step, detail)
        if reply is None:
            spec = {"objective": text, "context": CONVERSATIONAL, "evidence": recalled(),
                    "provider": routing.get("simple_provider", "claude"),
                    "power": routing.get("simple_power", "low"),
                    "role": "implementer" if write else "assistant",
                    "mode": "workspace-write" if write else "read-only"}
            label = f"{spec['provider'].title()} run · {spec['power']}"
            step("task", "route", label, spec["mode"], "run")
            result = _run_waiting(store, spec, config, base, timeout)
            if result is None:  # every ledger lane stayed busy: answer in-process instead
                reply, ok = _direct_reply(spec["provider"], text, recalled(), config, base,
                                          timeout, detail)
                detail.update(state="finished" if ok else "failed", brain=spec["provider"])
                step("task", "route", label, "ledger busy; answered directly" if ok else
                     detail["lane_error"], "done" if ok else "fail")
            else:
                reply = result.get("answer")
                detail.update(task=result["task_id"], state=result["state"], brain=spec["provider"])
                step("task", "route", label, f"{result['task_id']} · {result['state']}",
                     "done" if reply else "fail")
            reply = _failover_reply(reply, detail, spec["provider"])

    elif route["tier"] == "medium":
        adapter = ({"kind": "browser_agent", "name": "grokbot",
                    **config.get("personal_agents", {}).get("grokbot", {})}
                   if agent or grok_first(text, config) else pick_adapter(text, config))
        detail = {"adapter": adapter.get("name"), "kind": adapter["kind"]}
        label = f"{adapter.get('name')} · {adapter['kind'].replace('_', ' ')}"
        step("adapter", "route", label, "", "run")
        if adapter["kind"] == "api":
            reply = call_api(adapter, text, recalled())["answer"]
            step("adapter", "route", label, "answered")
        elif adapter["kind"] == "browser_agent" and adapter["name"] == "grokbot" and \
                adapter.get("integration") == "window_automation":
            from .grokbot import Grokbot
            from .grok_preflight import assess
            step("grok-check", "adapter", "Grok preflight", "inference and ten checks", "run")
            bot = Grokbot(config, memory.path.parent)
            stage = "grok-check"
            try:
                from .gates import agent_risk, asks as gates_ask
                if agent_risk(text) == "deny":
                    raise ValueError("I can't handle passwords, passcodes, 2FA codes or card numbers.")
                prepared = assess(text, config, base)
                _shadow(config, memory, text, prepared["decision"], job="grok_preflight", decided=False)
                step("grok-check", "adapter", "Grok preflight", prepared["decision"])
                risk = agent_risk(text, prepared)
                if risk == "deny":
                    raise ValueError("That task is refused by the risk checks.")
                if prepared["decision"] in ("forward", "confirm") and not gates_ask(config):
                    prepared = {**prepared, "decision": "forward",
                                "task": prepared.get("task") or text}
                if prepared["decision"] in ("forward", "confirm") and risk == "confirm" \
                        and gates_ask(config):
                    from .confirm import ConfirmQueue, prompt
                    from .remote import Remote
                    from .topology import ORIGIN
                    origin = ORIGIN.get() or Remote(config, memory.path.parent).here()
                    command = {"id": "c1", "intent": "agent_task", "device": origin,
                               "args": {"text": prepared.get("task") or text}, "depends_on": []}
                    item = ConfirmQueue(memory.path.parent).park(command, origin, channel)
                    prepared = {**prepared, "decision": "confirm", "reply": prompt(item)}
                    detail["confirmations"] = [item]
                    result = None
                elif prepared["decision"] == "forward":
                    step("grok-send", "adapter", "Grok Bot", "typing the checked task", "run")
                    stage = "grok-send"
                    result = bot.task(prepared["task"],
                                      timeout=min(timeout, routing.get("grok_wait", 30)),
                                      prechecked=True)
                else:
                    result = None
            except (TimeoutError, RuntimeError, ValueError, LookupError, OSError) as exc:
                reply = f"Grok request failed: {exc}"
                detail["state"] = "failed"
                step(stage, "adapter", "Grok Bot" if stage == "grok-send" else "Grok preflight",
                     str(exc), "fail")
            else:
                if result is None:
                    reply = prepared["reply"]
                    detail["preflight"] = prepared["decision"]
                else:
                    detail["grok"] = result["id"]
                    reply = f"Grok says: {result['answer']}"
                    step("grok-send", "adapter", "Grok Bot", f"answered: {result['id']}")
        elif adapter["kind"] == "browser_agent":
            raise RuntimeError(f"{adapter['name']} has no JARVIS interface yet; "
                               "see docs/architecture.md (medium tier).")
        else:
            spec = {"objective": text, "context": CONVERSATIONAL + " Use web search/fetch for "
                    "public pages; you cannot log in to accounts.", "evidence": recalled(),
                    "provider": "claude", "power": routing.get("web_power", "mid"),
                    "role": "assistant", "mode": "read-only"}
            result = run_task(store, store.add(spec)["id"], config, base, timeout)
            reply = result.get("answer")
            detail.update(task=result["task_id"], state=result["state"])
            step("adapter", "route", label, f"{result['task_id']} · {result['state']}",
                 "done" if reply else "fail")

    elif route["tier"] == "complex":
        step("plan", "route", "Planner", "writing the plan", "run")
        record = Orchestrator(store, memory, config, base).create(text, timeout=timeout)
        step("plan", "route", f"Plan {record['id']}", f"{len(record['subtasks'])} steps")
        for s in record["subtasks"]:
            step(f"plan:{s['id']}", "plan", f"{s['specialist']} · {s.get('power', 'mid')}",
                 s["objective"], "wait")
        steps = "; ".join(f"{s['id']} {s['specialist']} ({s.get('power', 'mid')}): "
                          f"{s['objective']}" for s in record["subtasks"])
        how = ("open the Run view to review and start it" if channel == "console" else
               f"run: jarvis orchestrate run {record['id']} (or use the console's Run view)")
        reply = (f"I planned that as {len(record['subtasks'])} specialist steps. "
                 f"{record['summary']} Review it, then {how}. Steps: {steps}")
        detail = {"plan": record["id"]}

    if not reply:
        reply = f"That didn't produce an answer ({detail.get('state', 'no output')})."
    outbound = memory.add("reply", reply, source="jarvis", tags=["outbound", route["tier"]],
                          meta=detail, links=[("answers", inbound["id"])], importance=0.4)
    step("memory", "root", "Remembered", outbound["id"])
    return {"tier": route["tier"], "reasons": route["reasons"], "reply": reply,
            "memory": [inbound["id"], outbound["id"]], **detail}


LANE_WAIT = 20  # seconds a fallback task waits for a free ledger lane before going direct


def _abandon(store, task_id, why):
    """Fail a task that never started, so no orphaned `queued` row is left behind."""
    try:
        store.transition(task_id, "queued", "failed", {"error_type": "NotRun", "note": why})
    except ValueError:  # already claimed or finished by someone else
        pass


def _run_waiting(store, spec, config, base, timeout, wait=None):
    """run_task for a new task, waiting briefly while every ledger lane is busy (an
    orchestrator wave, another fallback). None when no lane freed up; the task is then
    failed, not left queued. Any other error also fails the task before it propagates."""
    import time
    task_id = store.add(spec)["id"]
    deadline = time.monotonic() + (LANE_WAIT if wait is None else wait)
    while True:
        try:
            return run_task(store, task_id, config, base, timeout)
        except ValueError as exc:
            if "run lane" not in str(exc):
                _abandon(store, task_id, str(exc))
                raise
            if time.monotonic() >= deadline:
                _abandon(store, task_id, str(exc))
                return None
            time.sleep(0.25)
        except BaseException as exc:
            _abandon(store, task_id, f"{type(exc).__name__}: {exc}")
            raise


def _fastlane(config, base, timeout):
    from .fastlane import FastLane
    return FastLane(config, base, timeout=min(timeout, 90))


# While ChatGPT is over its plan's usage limit (hours, sometimes days), every request used to
# fall back to a fresh `claude --print` run: ~4 s of cold start each. One warm Claude lane
# stands in instead, and is closed again as soon as ChatGPT answers.
_STANDIN = {"lane": None, "lock": __import__("threading").Lock()}


def _standin_reply(text, recall, config, base, timeout, on_sentence, step, detail):
    """The warm Claude lane's answer, or None to fall back to a ledger run."""
    with _STANDIN["lock"]:
        lane = _STANDIN["lane"] = _STANDIN["lane"] or _fastlane(config, base, timeout)
    step("standin", "route", "Claude fast lane", "ChatGPT is over its usage limit", "run")
    try:
        reply = lane.ask(text, recall, on_sentence=on_sentence)
    except (RuntimeError, LookupError, TypeError, ValueError, OSError) as exc:
        detail["lane_error"] = f"{detail.get('lane_error', '')} {exc}".strip()
        step("standin", "route", "Claude fast lane", str(exc), "fail")
        return None
    detail.update(lane="FastLane", brain="claude", streamed=on_sentence is not None)
    step("standin", "route", "Claude fast lane", "answered")
    return reply


def _standin_done():
    with _STANDIN["lock"]:
        lane, _STANDIN["lane"] = _STANDIN["lane"], None
    if lane is not None:
        lane.close()


def _direct_reply(provider, text, recall, config, base, timeout, detail):
    """Answer without the task ledger: a one-off in-process Claude CLI lane. Returns
    (reply, ok); the reply is always something the user can read, and a failure is also
    described in detail["lane_error"]."""
    if provider != "claude":
        detail["lane_error"] = "All run lanes are busy; try again in a moment."
        return "I'm busy with other tasks right now. Ask me again in a moment.", False
    lane = None
    try:
        lane = _fastlane(config, base, timeout)
        return lane.ask(text, recall), True
    except (RuntimeError, LookupError, TypeError, ValueError, OSError) as exc:
        detail["lane_error"] = f"{detail.get('lane_error', '')} {exc}".strip()
        return f"I couldn't reach a brain: {exc}", False
    finally:
        if lane is not None:
            lane.close()


def _partial_reply(tool_log):
    """The brain failed after some tools ran: say what did and didn't happen, instead of
    claiming nothing was done (a retry would run the done ones again)."""
    done = [str(t[0]) for t in tool_log if t[1]]
    failed = [str(t[0]) for t in tool_log if not t[1]]
    parts = []
    if done:
        parts.append(f"{', '.join(dict.fromkeys(done))} ran")
    if failed:
        parts.append(f"{', '.join(dict.fromkeys(failed))} failed")
    return (f"I only got partway: {' and '.join(parts)}, then the brain stopped before "
            "finishing. Check the result before asking again, so nothing runs twice.")


LOGGED_OUT = ("not logged in", "please run /login", "invalid api key", "authentication")


def _failover_reply(reply, detail, provider):
    """When the fallback brain can't answer either, say plainly why and how to fix it,
    instead of reading out a CLI's error. Otherwise the reply as it came."""
    why = detail.get("lane_error") or ""
    text = (reply or "").strip()
    if provider == "claude" and any(m in text.lower() for m in LOGGED_OUT):
        from .chatgpt import usage_limit
        until = usage_limit()  # "until 8:00 PM", parsed when the refusal came in
        first = (f"ChatGPT is over its plan's usage limit{' ' + until if until else ''}"
                 if "usage limit" in why.lower()
                 else "ChatGPT didn't answer" if why else "I couldn't reach a brain")
        detail["brain"] = None
        return (f"{first}, and Claude isn't signed in on this PC. "
                "Run claude in a terminal and type /login once, then ask me again.")
    return reply


def _shadow(config, memory, text, label, job="mode", decided=True):
    """Log what the decider would have chosen next to what JARVIS actually did, in a
    background thread (requests never wait). decided=False logs only the label: data for a
    job the decider doesn't take yet (the Grok preflight). Off unless decisions.shadow."""
    if not (config.get("decisions") or {}).get("shadow") or memory is None:
        return
    state = memory.path.parent

    def run():
        try:
            from . import decide
            guess = decide.choose(job, text) if decided else None
            decide.record(state, job, text, guess, label=label, source="rules")
        except Exception:
            pass  # the shadow log must never affect a request
    import threading
    threading.Thread(target=run, name="jarvis-decider-shadow", daemon=True).start()


def _note_correction(text, memory):
    """"No, I meant the Dell": keep a lesson linked to the exchange it corrects."""
    try:
        if memory is not None and hasattr(memory, "note_correction"):
            memory.note_correction(text)
    except Exception:
        pass  # a lesson is never worth failing the request it came with


def _memory_pack(memory, text):
    try:
        return memory.pack(text) if hasattr(memory, "pack") else []
    except Exception:  # recall must never break a request
        return []


def _needs_agent(text, config):
    """Actions the parser recognized but can't run go to Grok Bot (behind its preflight);
    conversation stays with the brain. Off when Grok isn't set up or routing says so."""
    from .router import grok_available
    if not grok_available(config) or not config.get("routing", {}).get("agent_fallback", True):
        return False
    try:
        from .pipeline import needs_agent
        from .remote import Remote
        from .topology import ORIGIN
        return needs_agent(text, config, ORIGIN.get() or Remote(config, ".").here())
    except Exception:
        return False


def _fast_path(text, memory, config, channel, on_start, on_sentence, on_step):
    """Certain direct actions (open, play, volume...) run without a model; None sends
    the request on to the router and the brain as before (jarvis/pipeline.py)."""
    from .pipeline import fast_path

    def run(step, origin, begin):
        return fast_path(text, config, memory.path.parent, step=step, origin=origin,
                         on_begin=begin, channel=channel)
    return _direct_turn(text, memory, channel, on_start, on_sentence, on_step, run,
                        lane="parser", reason="parsed: direct actions, no model")


def _interpret(text, memory, config, base, channel, on_start, on_sentence, on_step):
    """Ask the interpreter about an action-like request the parser couldn't resolve.
    Returns a finished turn (direct actions run, or a clarifying question asked), or the
    mode ("agent", "converse", "build") for the caller to route, or None (no answer)."""
    from .interpret import interpret
    from .pipeline import run_commands
    from .topology import ORIGIN
    from .remote import Remote
    origin = ORIGIN.get() or Remote(config, memory.path.parent).here()
    try:  # David's past corrections, so "the laptop" resolves the way he taught it
        lessons = memory.context(text, limit=2, max_chars=600, kinds=("lesson",))
    except Exception:
        lessons = []
    read = interpret(text, config, base, origin, lessons=lessons)
    if read is None:
        return None
    if read["mode"] == "act":
        def run(step, origin_now, begin):
            return run_commands(text, read["commands"], config, memory.path.parent, step=step,
                                origin=origin_now or origin, on_begin=begin, channel=channel,
                                source="Interpreter", detail="ChatGPT")
        done = _direct_turn(text, memory, channel, on_start, on_sentence, on_step, run,
                            lane="interpreter", reason="interpreted: direct actions")
        return done if done is not None else "agent"   # invalid commands: Grok decides
    if read["mode"] == "clarify":
        def ask_back(step, origin_now, begin):
            begin([])
            return {"reply": read["question"], "commands": [], "outcomes": []}
        return _direct_turn(text, memory, channel, on_start, on_sentence, on_step, ask_back,
                            lane="interpreter", reason="interpreter: needs a detail")
    return read["mode"]


def _direct_turn(text, memory, channel, on_start, on_sentence, on_step, run, lane, reason):
    """A turn whose work is direct actions: run(step, origin, begin) -> result or None.
    Starts the turn on begin(), records it, speaks the reply. None: not ours."""
    from .topology import ORIGIN
    step = _stepper(on_step)
    route = {"tier": "simple", "reasons": [reason]}
    begun = {}

    def begin(commands):
        # Start the turn before any step is reported: the console drops steps it can't
        # place under a started turn, so the tree would otherwise stay empty.
        begun["inbound"] = memory.add("message", text, source="owner", tags=["inbound", "fast"],
                                      meta={"route": route, "channel": channel,
                                            "origin_device": ORIGIN.get()}, importance=0.4)
        if on_start:
            on_start({"route": route, "message": begun["inbound"]["id"]})

    try:
        done = run(step, ORIGIN.get(), begin)
    except Exception as exc:  # the fast path must never cost David his request
        if not begun:
            return None
        # The turn already started; answer it rather than run the request twice.
        done = {"reply": f"That didn't work: {exc}", "commands": [], "outcomes": []}
    if done is None:
        return None
    inbound = begun.get("inbound") or memory.add(
        "message", text, source="owner", tags=["inbound", "fast"],
        meta={"route": route, "channel": channel, "origin_device": ORIGIN.get()}, importance=0.4)
    if on_start and not begun:
        on_start({"route": route, "message": inbound["id"]})
    if on_sentence:
        on_sentence(done["reply"])
    detail = {"lane": lane, "streamed": on_sentence is not None,
              "commands": done["commands"], "outcomes": done["outcomes"],
              "confirmations": done.get("confirmations", [])}
    outbound = memory.add("reply", done["reply"], source="jarvis", tags=["outbound", "fast"],
                          meta=detail, links=[("answers", inbound["id"])], importance=0.4)
    step("memory", "root", "Remembered", outbound["id"])
    return {**route, "reply": done["reply"], "memory": [inbound["id"], outbound["id"]],
            **detail}


def confirmation_origin(config, state):
    from .topology import ORIGIN
    from .remote import Remote
    return ORIGIN.get() or Remote(config, state).here()


def confirm_pending(config, state, identifier, action, step=None, hash=None):
    """Console and spoken replies consume the same origin-bound approval."""
    from .confirm import ConfirmQueue
    from .pipeline import run_confirmed
    queue = ConfirmQueue(state)
    origin = confirmation_origin(config, state)
    if action == "reject":
        queue.reject(identifier, origin=origin)
        return {"reply": "Cancelled.", "commands": [], "outcomes": []}
    if action != "approve":
        raise ValueError("Choose approve or reject.")
    command = queue.approve(identifier, origin, hash=hash)
    if command["intent"] == "browser_action":  # browser_gate is waiting on this approval
        return {"reply": "Okay, go ahead.", "commands": [command], "outcomes": []}
    if command["intent"] != "agent_task":
        return run_confirmed(command, config, state, origin=origin, step=step)
    from . import gates
    from .history import History
    from .grokbot import Grokbot
    import time
    import uuid
    began = time.monotonic()
    verdict = gates.check(command, config.get("devices", {}), confirmed=True)
    history = History(state)
    past = history.check(command)
    try:
        if not verdict["pass"] or past["block"]:
            raise ValueError(verdict["why"] or "History checks blocked the task.")
        if gates.agent_risk(command["args"]["text"]) == "deny":
            raise ValueError("That task is refused by the risk checks.")
        from .router import grok_available
        if not grok_available(config):
            raise ValueError("Grok Bot is no longer available.")
        result = Grokbot(config, state).task(command["args"]["text"],
                    timeout=config.get("routing", {}).get("grok_wait", 30), prechecked=True)
        reply = f"Grok says: {result['answer']}"
        outcome = {"id": command["id"], "ok": True, "state": "agent_claimed", "text": reply}
    except (RuntimeError, ValueError, LookupError, OSError, TimeoutError) as exc:
        reply = f"Confirmed task failed: {exc}"
        # Checks, routing and a down tunnel fail before Grok gets the task: certainly
        # nothing happened. A timeout or an error mid-request may have reached it.
        reached = not isinstance(exc, (ValueError, LookupError, ConnectionError))
        outcome = {"id": command["id"], "ok": False,
                   "state": "uncertain" if reached else "pre_effect_failure", "text": reply}
    history.record(uuid.uuid4().hex, command, outcome, "Confirmed agent task", origin,
                   total_ms=round((time.monotonic() - began) * 1000))
    return {"reply": reply, "commands": [command], "outcomes": [outcome]}


NUMBERS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
           "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def confirmation_words(text):
    """"Yes", "cancel C3", "go ahead two"...: the words that answer a pending confirmation."""
    words = (wake_word(text) or text).strip().lower().replace("\u2019", "'").rstrip(".!?")
    return re.fullmatch(r"(yes|confirm|do it|go ahead|no|cancel|don't)(?:\s+(c?\d+|" +
                        "|".join(NUMBERS) + r"))?", words)


def _confirmation_turn(text, memory, config, base, channel, on_start, on_sentence, on_step):
    # The voice listener is the wake-word gate; this only handles its accepted turns.
    match, numbers = confirmation_words(text), NUMBERS
    if not match or memory is None:
        return None
    from .confirm import ConfirmQueue
    origin = confirmation_origin(config, memory.path.parent)
    if not ConfirmQueue(memory.path.parent).pending(origin):
        return None
    identifier = match.group(2)
    if identifier:
        identifier = "C" + numbers.get(identifier, identifier.removeprefix("c"))
    route = {"tier": "simple", "reasons": ["owner confirmation"]}
    inbound = memory.add("message", text, source="owner", tags=["inbound", "confirm"],
                         meta={"route": route, "channel": channel, "origin_device": origin}, importance=0.4)
    if on_start:
        on_start({"route": route, "message": inbound["id"]})
    try:
        done = confirm_pending(config, memory.path.parent, identifier,
                               "reject" if match.group(1) in ("no", "cancel", "don't") else "approve",
                               step=_stepper(on_step))
    except ValueError as exc:
        done = {"reply": str(exc), "commands": [], "outcomes": []}
    outbound = memory.add("reply", done["reply"], source="jarvis", tags=["outbound", "confirm"],
                          links=[("answers", inbound["id"])], importance=0.4)
    if on_sentence:
        on_sentence(done["reply"])
    return {**route, **done, "lane": "confirmation", "streamed": on_sentence is not None,
            "memory": [inbound["id"], outbound["id"]]}


def _stepper(on_step):
    def step(id, parent, label, detail="", state="done"):
        if on_step:
            try:
                on_step(id, parent, label, detail, state)
            except Exception:  # reporting must never fail the turn
                pass
    return step


_TOOL_STEPS = threading.local()


def _report_tool(key, name, ok, text):
    handler = getattr(_TOOL_STEPS, "handler", None)
    if handler:
        handler(key, name, ok, text)


def _with_tools(lane, step, call):
    """Run call() with the lane's tool calls reported as children of the brain node.
    Lanes report tools on the asking thread, so the tree is picked per thread: the console
    runs requests side by side on one lane, and a callback set on the lane itself sent one
    request's tools into the other's tree (and the first to finish switched both off)."""
    if not hasattr(lane, "on_tool"):
        return call()
    names = {}

    def on_tool(key, name, ok, text):
        node = names.setdefault(key, f"tool:{len(names) + 1}")
        step(node, "brain", name or "tool", text, "run" if ok is None else "done" if ok else "fail")

    lane.on_tool = _report_tool  # the same for every request, so setting it again is harmless
    previous, _TOOL_STEPS.handler = getattr(_TOOL_STEPS, "handler", None), on_tool
    try:
        return call()
    finally:
        _TOOL_STEPS.handler = previous


WAKE_WORDS = ("jarvis", "jervis", "jarvas", "javis", "jarvus", "jarviss", "jarves")
# Filler Whisper keeps before the name: "Hello, Jarvis, ...", "Okay so Jarvis ...".
WAKE_LEAD = r"(?:(?:hey|hi|hello|yo|ok|okay|oh|so|um|uh|and|alright)[\s,.!?]+)*"
# The name as the subject of a sentence ("Jarvis said...", "Jarvis is useless"), when no
# comma follows it. An auxiliary followed by a subject is a question to it instead:
# "Jarvis is it raining", "Jarvis can you open Spotify" (Whisper dropped the comma).
WAKE_SUBJECT = re.compile(
    r"(?:said|says|told|tells|thinks|thought|knows|knew|keeps|kept|seems|seemed|sucks|"
    r"wants|wanted|gets|got|had|needs|likes|hates|broke|messed|"
    r"(?:is|was|are|were|does|did|will|would|can|could|should|has|have|might|must|"
    r"isn['’]t|wasn['’]t|doesn['’]t|didn['’]t|won['’]t|can['’]t|couldn['’]t|"
    r"wouldn['’]t|shouldn['’]t|hasn['’]t|never|always|just|really|still|also)"
    r"(?!\s+(?:it|there|this|that|the|my|your|you|he|she|they|we|i|a|an|any|anything|"
    r"anyone|someone|something|everything)\b))\b", re.I)
# The word before a comma-less closing name that makes the name its object or complement:
# "Who is Jarvis?", "Did you ask Jarvis?", "Have you heard of Jarvis?".
WAKE_OBJECT = re.compile(
    r"\b(?:is|was|are|were|be|been|ask|asked|about|to|with|from|for|of|by|at|called|named|"
    r"call|tell|told|like|love|hate|use|using|trust|the|a|an|my|your|and|or|than|as|meet|"
    r"met|mean|meant|hear|heard|see|saw|know|knew|try|tried|said)\W*$", re.I)


def wake_word(text, words=WAKE_WORDS):
    """Strip the wake word; None if absent. "Jervis" is Whisper mishearing 'Jarvis';
    'Travis' is a real name, and "Jarvis's ..." is talking about JARVIS, not to it.
    The name may open the request after a greeting, or close it after a comma or as a
    question ("what time is it, Jarvis?" - Whisper often drops that comma); "I was talking
    to Jarvis" is neither and doesn't count."""
    names = "|".join(words)
    match = re.match(r"^\W*" + WAKE_LEAD + r"(?:" + names + r")\b(?!['’]s\b)([\s,.!?:-]*)(.*)",
                     text, re.I | re.S)
    if match:
        if not match.group(1).strip() and WAKE_SUBJECT.match(match.group(2)):
            return None  # "So Jarvis said it would rain": about it, not to it
        return match.group(2).strip()
    match = re.match(r"^\W*(.*?\w.*?)(?:\s*,\s*(?:" + names + r")\W*|\s+(?:" + names +
                     r")\s*\?\W*)$", text, re.I | re.S)
    if match and "," not in text[match.end(1):] and WAKE_OBJECT.search(match.group(1)):
        return None  # "Who is Jarvis?", "Did you ask Jarvis?": a question about it
    return match.group(1).strip() if match else None
