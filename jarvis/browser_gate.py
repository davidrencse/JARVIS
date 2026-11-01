"""David's OK before the browser commits anything from his signed-in accounts.

The JARVIS browsers keep their sign-ins, so a page's text could steer the model into
posting, sending or buying as David. approve_browser (actions.py) decides which tools may
run at all; this gate sits on top: reading, navigating, scrolling and typing stay
automatic, but a click or key that looks like it commits something (send, post, submit,
buy, delete...), Enter after typing outside a search box, a typed field with submit, or
accepting a page dialog waits for David through the normal confirmation queue (console
Confirm button, or "Jarvis, yes"). Unanswered within WAIT seconds, it's declined.

Some things are refused whatever rules.confirm says (blocked(): card numbers, 2FA codes and
anything typed into a password/PIN/CVV field, clicks that change a password): not questions.

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
# Fields the model must never fill: credentials. Typed text is never read back or repeated.
SECRET_FIELD = re.compile(r"\b(password|passcode|passphrase|pass ?word|pin|cvv2?|cvc2?|"
                          r"security code|card verification)\b", re.I)
OTP_FIELD = re.compile(r"\b(otp|2fa|mfa|two[- ]?factor|verification|one[- ]time|"
                       r"authenticator|auth(?:entication)? code|sms code|code)\b", re.I)
NOT_OTP = re.compile(r"\b(zip|postal|post|area|country|promo|coupon|discount|voucher|gift|"
                     r"referral|invite|source|language)\s+code\b", re.I)
OTP_STRONG = re.compile(r"\b(otp|2fa|mfa|two[- ]?factor|verification|one[- ]time|authenticator)\b",
                        re.I)
CHANGE_PASSWORD = re.compile(r"\b(change|update|reset|set)\b[^.]{0,25}\bpass(?:word|code)\b|"
                             r"\bpass(?:word|code)\b[^.]{0,15}\b(change|update|reset)\b", re.I)
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


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _card_number(text):
    for match in re.finditer(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)", text):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def _typed(args):
    """[(description, text)] per field a typing tool fills: one pair for text/value, one per
    entry of a fields list (Playwright browser_fill_form)."""
    out = []
    items = args.get("fields") if isinstance(args.get("fields"), list) else None
    for item in items or [args]:
        if not isinstance(item, dict):
            continue
        text = " ".join(s for k in ("text", "value", "values") for s in _strings(item.get(k, "")))
        described = " ".join(s for k, v in item.items() if k not in ("text", "value", "values")
                             and k != "fields" for s in _strings(v))
        out.append((described, text))
    return out


def blocked(tool, args):
    """Why this browser call is refused outright (credentials, card numbers, password
    changes), whatever rules.confirm says; None otherwise. `tool` is the MCP tool name."""
    tool, args = str(tool or ""), args if isinstance(args, dict) else {}
    if not INTERACTIVE.search(tool):
        return None
    if re.search(r"type|fill", tool, re.I):
        for described, text in _typed(args):
            if SECRET_FIELD.search(described):
                return "JARVIS never types passwords, PINs or card security codes"
            if _card_number(text):
                return "JARVIS never types card numbers"
            if OTP_FIELD.search(described) and text.strip() and \
                    (OTP_STRONG.search(described) or not NOT_OTP.search(described)) and \
                    re.fullmatch(r"(?=.*\d)[A-Za-z0-9][A-Za-z0-9 -]{2,10}[A-Za-z0-9]", text.strip()):
                return "JARVIS never types 2FA or verification codes"
        return None
    if "dialog" in tool:
        return None
    described = " ".join(s for k, v in args.items() if k not in ("text", "value", "values")
                         for s in _strings(v))
    if CHANGE_PASSWORD.search(described):
        return "JARVIS never changes passwords"
    return None


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
        refusal = blocked(_tool(params), (params.get("_meta") or {}).get("tool_params"))
        if refusal:  # not a question: refused even under rules.confirm: never
            return False, refusal
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
