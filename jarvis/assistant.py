"""One entry point for text and voice: route the request, do the work, remember it."""

import re

from .adapters import call_api
from .instant import answer as instant_answer
from .orchestrator import Orchestrator
from .router import classify, pick_adapter
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
    if (tier is None and conversational and not write
            and not config.get("routing", {}).get("auto_escalate")):
        # Conversation is the main pipeline: everything goes to the brain (ChatGPT).
        # Web/orchestration tiers run only when asked for explicitly with a tier.
        return {"tier": "simple", "reasons": ["conversation (auto_escalate off)"]}
    return classify(text, tier)


def ask(text, store, memory, config, base, tier=None, write=False, timeout=600, lane=None,
        on_sentence=None, on_start=None, channel="cli"):
    """on_sentence: optional callback for streamed sentences (fast lane only).
    on_start({"route", "message"}): called once the request is routed and remembered.
    channel: where it came from (console, voice, cli), kept on the inbound message."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Say or type something for JARVIS to do.")
    text = text.strip()
    routing = config.get("routing", {})
    route = decide(text, config, tier, lane is not None, write)
    # Vault notes first (what David knows and decided), then the heap (what was said).
    recall = vault_recall(config, memory.path.parent, text) + memory.context(text)
    inbound = memory.add("message", text, source="owner", tags=["inbound", route["tier"]],
                         meta={"route": route, "channel": channel}, importance=0.4)
    if on_start:
        on_start({"route": route, "message": inbound["id"]})
    reply, detail = None, {}

    if route["tier"] == "simple" and not write and (
            quick := instant_answer(text) or vault_answer(text, config, memory.path.parent)):
        reply = quick  # the local clock or the vault: no network, no model
        detail = {"lane": "instant", "streamed": on_sentence is not None}
        if on_sentence:
            on_sentence(quick)

    if route["tier"] == "simple" and reply is None:
        if lane is not None and not write:
            try:
                reply = lane.ask(text, recall, on_sentence=on_sentence)
                detail = {"lane": type(lane).__name__, "streamed": on_sentence is not None}
            except RuntimeError as exc:  # fall back to a normal one-shot run below
                detail = {"lane_error": str(exc)}
        if reply is None:
            spec = {"objective": text, "context": CONVERSATIONAL, "evidence": recall,
                    "provider": routing.get("simple_provider", "claude"),
                    "power": routing.get("simple_power", "low"),
                    "role": "implementer" if write else "assistant",
                    "mode": "workspace-write" if write else "read-only"}
            result = run_task(store, store.add(spec)["id"], config, base, timeout)
            reply = result.get("answer")
            detail.update(task=result["task_id"], state=result["state"])

    elif route["tier"] == "medium":
        adapter = pick_adapter(text, config)
        detail = {"adapter": adapter.get("name"), "kind": adapter["kind"]}
        if adapter["kind"] == "api":
            reply = call_api(adapter, text, recall)["answer"]
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

    elif route["tier"] == "complex":
        record = Orchestrator(store, memory, config, base).create(text, timeout=timeout)
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
    return {"tier": route["tier"], "reasons": route["reasons"], "reply": reply,
            "memory": [inbound["id"], outbound["id"]], **detail}


def wake_word(text, words=("jarvis", "jervis", "travis")):
    """Strip a leading wake word; None if absent. Whisper often mishears 'Jarvis'."""
    match = re.match(r"^\W*(?:hey\s+|ok\s+|okay\s+)?(" + "|".join(words) + r")\b[\s,.!?:-]*(.*)",
                     text, re.I | re.S)
    return match.group(2).strip() if match else None
