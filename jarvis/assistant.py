"""One entry point for text and voice: route the request, do the work, remember it."""

import re
import threading

from .adapters import call_api
from .instant import answer as instant_answer
from .knowledge import recall as kb_recall
from .orchestrator import Orchestrator
from .router import classify, grok_first, pick_adapter
from .runner import run_task
from .vault import recall as vault_recall

def market_answer(text, config, state):
    """"Check the market": opens the Pro terminal (here or on the named laptop) and
    says how the market is doing (showcase.py). None for anything else."""
    try:
        from .showcase import answer
        return answer(text, config, state)
    except Exception:
        return None


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
    routing = config.get("routing", {})
    route = decide(text, config, tier, lane is not None, write)
    # Vault notes first (what David knows and decided), then wiki claims with their
    # sources and dates, then the heap (what was said).
    notes = vault_recall(config, memory.path.parent, text)
    claims = kb_recall(config, memory.path.parent, text)
    heap = memory.context(text)
    recall = notes + claims + heap
    inbound = memory.add("message", text, source="owner", tags=["inbound", route["tier"]],
                         meta={"route": route, "channel": channel}, importance=0.4)
    if on_start:
        on_start({"route": route, "message": inbound["id"]})
    step = _stepper(on_step)
    step("recall", "root", "Recall", f"{len(notes)} vault · {len(claims)} wiki · {len(heap)} heap")
    step("route", "root", f"Route · {route['tier']}", "; ".join(route["reasons"]))
    reply, detail = None, {}

    if route["tier"] == "simple" and not write and (
            quick := instant_answer(text) or market_answer(text, config, memory.path.parent)
            or vault_answer(text, config, memory.path.parent)):
        reply = quick  # the local clock, the market window or the vault: no model
        detail = {"lane": "instant", "streamed": on_sentence is not None}
        step("instant", "route", "Instant", "clock, market or vault, no model")
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
                   if grok_first(text, config) else pick_adapter(text, config))
        detail = {"adapter": adapter.get("name"), "kind": adapter["kind"]}
        label = f"{adapter.get('name')} · {adapter['kind'].replace('_', ' ')}"
        step("adapter", "route", label, "", "run")
        if adapter["kind"] == "api":
            reply = call_api(adapter, text, recall)["answer"]
            step("adapter", "route", label, "answered")
        elif adapter["kind"] == "browser_agent" and adapter["name"] == "grokbot" and \
                adapter.get("integration") == "window_automation":
            from .grokbot import Grokbot
            step("adapter", "route", "Grok Bot · agent browser", "typing the task", "run")
            bot = Grokbot(config, memory.path.parent)
            sent = bot.handoff(text)
            step("adapter", "route", "Grok Bot · agent browser",
                 f"handoff {sent['id']} · waiting for Grok", "run")
            detail["grok"] = sent["id"]
            reply = bot.wait(sent["id"], min(timeout, routing.get("grok_wait", 180)))
            if reply is None:  # keep watching; the answer is posted to JARVIS when it lands
                from .chatgpt import grok_tools
                late = grok_tools(config, memory.path.parent)
                late.late_seconds = routing.get("grok_late", 1800)
                threading.Thread(target=late._late, args=(sent["id"], text), daemon=True).start()
                reply = ("That's with Grok now. It's still working, and its answer will show "
                         "up here when it's done.")
                step("adapter", "route", "Grok Bot · agent browser",
                     f"still working · {sent['id']}", "run")
            else:
                reply = f"Grok says: {reply}"
                step("adapter", "route", "Grok Bot · agent browser", f"answered · {sent['id']}")
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
        how = ("open it in Tasks and press Run" if channel == "console" else
               f"run: jarvis orchestrate run {record['id']} (or press Run in the console's Tasks)")
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


def wake_word(text, words=("jarvis", "jervis", "travis")):
    """Strip a leading wake word; None if absent. Whisper often mishears 'Jarvis'."""
    match = re.match(r"^\W*(?:hey\s+|ok\s+|okay\s+)?(" + "|".join(words) + r")\b[\s,.!?:-]*(.*)",
                     text, re.I | re.S)
    return match.group(2).strip() if match else None
