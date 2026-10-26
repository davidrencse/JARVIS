"""One entry point for text and voice: route the request, do the work, remember it."""

import re

from .adapters import call_api
from .instant import answer as instant_answer
from .knowledge import recall as kb_recall
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
        return answer(text, vault, tasks_for(vault)) if vault else None
    except Exception:
        return None


CONVERSATIONAL = ("Reply as JARVIS to the owner, David. Lead with the answer in plain "
                  "sentences; it may be read aloud, so keep it short unless detail is asked "
                  "for. Put code or long output after the spoken part.")


def decide(text, config, tier=None, conversational=False, write=False):
    """The tier a request will run on. Shared by ask() and the console's preview."""
    if tier is None and not write and grok_first(text, config) and not instant_answer(text):
        return {"tier": "medium", "reasons": ["Grok Bot orchestrates account and web work"]}
    if (tier is None and conversational and not write
            and not config.get("routing", {}).get("auto_escalate")):
        # Conversation is the main pipeline: everything goes to the brain (ChatGPT).
        # Web/orchestration tiers run only when asked for explicitly with a tier.
        return {"tier": "simple", "reasons": ["conversation (auto_escalate off)"]}
    return classify(text, tier)


def lane_label(lane, text):
    """What the brain node of the connection tree is called for this request."""
    if hasattr(lane, "pick"):
        return "ChatGPT · tools" if lane.pick(text) == "act" else "ChatGPT · talk"
    return {"FastLane": "Claude fast lane", "ChatGPTLane": "ChatGPT"}.get(
        type(lane).__name__, type(lane).__name__)


def ask(text, store, memory, config, base, tier=None, write=False, timeout=600, lane=None,
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
    confirmed = _confirmation_turn(text, memory, config, base, channel,
                                   on_start, on_sentence, on_step)
    if confirmed is not None:
        return confirmed
    if tier is None and not write and routing.get("fast_path", True):
        fast = _fast_path(text, memory, config, channel, on_start, on_sentence, on_step)
        if fast is not None:
            return fast
    agent = tier is None and not write and _needs_agent(text, config)
    route = ({"tier": "medium", "reasons": ["an action the parser can't run: Grok Bot"]}
             if agent else decide(text, config, tier, lane is not None, write))
    inbound = memory.add("message", text, source="owner", tags=["inbound", route["tier"]],
                         meta={"route": route, "channel": channel,
                               "origin_device": __import__("jarvis.topology", fromlist=["ORIGIN"]).ORIGIN.get()}, importance=0.4)
    if on_start:
        on_start({"route": route, "message": inbound["id"]})
    step = _stepper(on_step)
    # Vault notes first (what David knows and decided), then wiki claims with their
    # sources and dates, then the heap (what was said).
    notes = vault_recall(config, memory.path.parent, text)
    claims = kb_recall(config, memory.path.parent, text)
    heap = memory.context(text)
    recall = notes + claims + heap
    from .topology import ORIGIN, enabled, identity
    if enabled(config) and ORIGIN.get():
        recall.insert(0, f"This request originates on {ORIGIN.get()}. The JARVIS main PC is {identity(config)[1]}. "
                         "Here/this PC means the requesting node. Use an explicit device ID for another PC.")
    step("recall", "root", "Recall", f"{len(notes)} vault · {len(claims)} wiki · {len(heap)} heap")
    step("route", "root", f"Route · {route['tier']}", "; ".join(route["reasons"]))
    reply, detail = None, {}

    if route["tier"] == "simple" and not write and (
            quick := instant_answer(text) or vault_answer(text, config, memory.path.parent)):
        reply = quick  # the clock or the vault: no model
        detail = {"lane": "instant", "streamed": on_sentence is not None}
        step("instant", "route", "Instant", "clock or vault, no model")
        if on_sentence:
            on_sentence(quick)

    if route["tier"] == "simple" and reply is None:
        if lane is not None and not write:
            step("brain", "route", lane_label(lane, text), "thinking", "run")
            try:
                reply = _with_tools(lane, step, lambda: lane.ask(text, recall,
                                                                on_sentence=on_sentence))
                detail = {"lane": type(lane).__name__, "streamed": on_sentence is not None}
                step("brain", "route", lane_label(lane, text), "answered")
            except RuntimeError as exc:  # fall back to a normal one-shot run below
                detail = {"lane_error": str(exc)}
                step("brain", "route", lane_label(lane, text), str(exc), "fail")
        if reply is None:
            spec = {"objective": text, "context": CONVERSATIONAL, "evidence": recall,
                    "provider": routing.get("simple_provider", "claude"),
                    "power": routing.get("simple_power", "low"),
                    "role": "implementer" if write else "assistant",
                    "mode": "workspace-write" if write else "read-only"}
            label = f"{spec['provider'].title()} run · {spec['power']}"
            step("task", "route", label, spec["mode"], "run")
            result = run_task(store, store.add(spec)["id"], config, base, timeout)
            reply = result.get("answer")
            detail.update(task=result["task_id"], state=result["state"])
            step("task", "route", label, f"{result['task_id']} · {result['state']}",
                 "done" if reply else "fail")

    elif route["tier"] == "medium":
        adapter = ({"kind": "browser_agent", "name": "grokbot",
                    **config.get("personal_agents", {}).get("grokbot", {})}
                   if agent or grok_first(text, config) else pick_adapter(text, config))
        detail = {"adapter": adapter.get("name"), "kind": adapter["kind"]}
        label = f"{adapter.get('name')} · {adapter['kind'].replace('_', ' ')}"
        step("adapter", "route", label, "", "run")
        if adapter["kind"] == "api":
            reply = call_api(adapter, text, recall)["answer"]
            step("adapter", "route", label, "answered")
        elif adapter["kind"] == "browser_agent" and adapter["name"] == "grokbot" and \
                adapter.get("integration") == "window_automation":
            from .grokbot import Grokbot
            from .grok_preflight import assess
            step("grok-check", "adapter", "Grok preflight", "inference and ten checks", "run")
            bot = Grokbot(config, memory.path.parent)
            stage = "grok-check"
            try:
                from .gates import agent_risk
                if agent_risk(text) == "deny":
                    raise ValueError("I can't handle passwords, passcodes, 2FA codes or card numbers.")
                prepared = assess(text, config, base)
                step("grok-check", "adapter", "Grok preflight", prepared["decision"])
                risk = agent_risk(text, prepared)
                if risk == "deny":
                    raise ValueError("That task is refused by the risk checks.")
                if prepared["decision"] in ("forward", "confirm") and risk == "confirm":
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
                    "public pages; you cannot log in to accounts.", "evidence": recall,
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
    from .topology import ORIGIN
    step = _stepper(on_step)
    route = {"tier": "simple", "reasons": ["parsed: direct actions, no model"]}
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
        done = fast_path(text, config, memory.path.parent, step=step, origin=ORIGIN.get(),
                         on_begin=begin, channel=channel)
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
    detail = {"lane": "parser", "streamed": on_sentence is not None,
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
        outcome = {"id": command["id"], "ok": False, "state": "uncertain", "text": reply}
    history.record(uuid.uuid4().hex, command, outcome, "Confirmed agent task", origin,
                   total_ms=round((time.monotonic() - began) * 1000))
    return {"reply": reply, "commands": [command], "outcomes": [outcome]}


def _confirmation_turn(text, memory, config, base, channel, on_start, on_sentence, on_step):
    # The voice listener is the wake-word gate; this only handles its accepted turns.
    words = (wake_word(text) or text).strip().lower().replace("\u2019", "'").rstrip(".!?")
    numbers = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}
    match = re.fullmatch(r"(yes|confirm|do it|go ahead|no|cancel|don't)(?:\s+(c?\d+|" +
                         "|".join(numbers) + r"))?", words)
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


def _with_tools(lane, step, call):
    """Run call() with the lane's tool calls reported as children of the brain node."""
    if not hasattr(lane, "on_tool"):
        return call()
    names = {}

    def on_tool(key, name, ok, text):
        node = names.setdefault(key, f"tool:{len(names) + 1}")
        step(node, "brain", name or "tool", text, "run" if ok is None else "done" if ok else "fail")

    lane.on_tool = on_tool
    try:
        return call()
    finally:
        lane.on_tool = None


def wake_word(text, words=("jarvis", "jervis")):
    """Strip a leading wake word; None if absent. "Jervis" is Whisper mishearing 'Jarvis';
    'Travis' is a real name, and "Jarvis's ..." is talking about JARVIS, not to it."""
    match = re.match(r"^\W*(?:hey\s+|ok\s+|okay\s+)?(" + "|".join(words) +
                     r")\b(?!['’]s\b)[\s,.!?:-]*(.*)", text, re.I | re.S)
    return match.group(2).strip() if match else None
