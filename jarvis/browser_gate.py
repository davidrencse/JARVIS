"""David's OK before the browser commits anything from his signed-in accounts.

The JARVIS browsers keep their sign-ins, so a page's text could steer the model into
posting, sending or buying as David. approve_browser (actions.py) decides which tools may
run at all; this gate sits on top: reading, navigating, scrolling and typing stay
automatic, but a click or key that looks like it commits something (send, post, submit,
buy, delete...), Enter after typing outside a search box, a typed field with submit, or
accepting a page dialog waits for David through the normal confirmation queue (console
Confirm button, or "Jarvis, yes"). Unanswered within WAIT seconds, it's declined.

It is a keyword heuristic over the tool's own arguments: a button labelled to look
harmless can still pass. It narrows the risk; it doesn't remove it.
"""

import re
import threading
import time

WAIT = 180  # seconds the browser waits for David before the action is declined
COMMIT = re.compile(
    r"\b(send|sent|post|posting|publish|submit|tweet|retweet|repost|reply|comment|share|"
    r"buy|purchase|pay|checkout|check out|place order|order now|donate|subscribe|"
    r"unsubscribe|delete|remove|erase|trash|transfer|withdraw|confirm|accept|approve|"
    r"agree|sign up|register|invite|follow|unfollow|block|report|save changes|update "
    r"password|change password|grant|authorize|allow access)\b", re.I)
SEARCH = re.compile(r"\b(search|find|filter|query|look ?up|address bar|url bar)\b", re.I)
INTERACTIVE = re.compile(r"(click|type|fill|press|key|select|check|submit|dialog|drag|upload)",
                         re.I)
_TYPED = {}  # server -> was the last field typed into a search box (Enter is then harmless)
_LOCK = threading.Lock()


def _tool(params):
    message = params.get("message", "")
    return message.split('"')[1] if message.count('"') >= 2 else ""


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def commits(params):
    """Why this browser tool call would commit something as David, or None."""
    server, tool = params.get("serverName") or "", _tool(params)
    args = (params.get("_meta") or {}).get("tool_params") or {}
    if not INTERACTIVE.search(tool):
        return None
    if "dialog" in tool:
        return "accept a page dialog" if args.get("accept", True) else None
    # What the model says it's acting on: element descriptions, labels, selectors. Typed
    # text itself is left out; a message body saying "post" doesn't make typing a commit.
    described = " ".join(s for k, v in args.items() if k not in ("text", "value", "values")
                         for s in _strings(v))
    typing = re.search(r"type|fill", tool, re.I)
    if typing:
        with _LOCK:
            _TYPED[server] = bool(SEARCH.search(described))
        if args.get("submit") and not SEARCH.search(described):
            return f"type and submit into {described[:60] or 'a field'}"
        return None
    if re.search(r"press|key", tool, re.I):
        key = " ".join(_strings(args)).strip().lower()
        with _LOCK:
            searching = _TYPED.get(server, False)
        if key in ("enter", "return", "numpadenter", "control+enter", "meta+enter") and not searching:
            return f"press {key} after typing"
        return None
    found = COMMIT.search(described)
    return f"{tool.split('_', 1)[-1]} “{found.group(0)}” ({described[:60]})" if found else None


def gate(policy, config, state, wait=WAIT, queue=None):
    """approve(params) for the Codex lane: `policy` first (actions.approve_browser), then
    David's OK for anything that commits. Blocks the tool call while it waits."""
    def approve(params):
        ok, why = policy(params)
        if not ok:
            return ok, why
        action = commits(params)
        if not action:
            return ok, why
        from .gates import asks
        if not asks(config):  # rules.confirm: never
            return ok, why
        return ask(action, config, state, wait, queue)
    return approve


def ask(action, config, state, wait=WAIT, queue=None):
    from .assistant import confirmation_origin
    from .confirm import ConfirmQueue
    queue = queue or ConfirmQueue(state)
    origin = confirmation_origin(config, state)
    item = queue.park({"intent": "browser_action", "device": origin,
                       "args": {"text": f"let the browser {action}"}}, origin, "console")
    number, deadline = int(item["id"][1:]), time.monotonic() + wait
    while time.monotonic() < deadline:
        status = queue.status(number)
        if status == "approved":
            return True, f"David approved: {action}"
        if status in ("rejected", "expired"):
            return False, f"David didn't approve: {action}"
        time.sleep(0.5)
    try:
        queue.reject(item["id"])
    except ValueError:
        pass  # answered or expired at the last moment; still declined here
    return False, f"No answer within {wait} s: {action}"
