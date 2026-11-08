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
from urllib.parse import urlparse

WAIT = 180  # seconds the browser waits for David on a high-stakes action before declining
LOW_WAIT = 60  # ...and on a low-stakes one (an unnamed element, a plain typed field)
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
# Tools that type text: press_sequentially types too, and takes `submit` like browser_type.
TYPING = re.compile(r"type|fill|press_sequentially", re.I)
# Commit words inside raw selectors and test ids (button[data-testid=tweetButton], #sendBtn,
# .post-btn): a word start, no word end, since ids glue words together ("tweetbutton").
SELECTOR_COMMIT = re.compile(r"(?<![a-z])(send|post|tweet|reply|publish|submit|pay|buy|purchase|"
                             r"checkout|order|confirm|transfer|donate|subscribe|share|retweet|"
                             r"repost|comment|delete|remove|withdraw)", re.I)
TARGET_KEYS = ("ref", "target", "selector", "startTarget", "endTarget", "startRef", "endRef")
REFERENCE = re.compile(r"^@?[a-z]{0,3}\d+(?:[a-z]\d+)*$", re.I)  # @e12, e12, f1e3
_TYPED = {}  # server -> was the last field typed into a search box (Enter is then harmless)
_LOCK = threading.Lock()

# What the page looks like, learned from the browser's own replies (browser_proxy.Relay feeds
# these; the Codex lane never sees tool results, so there it stays empty and questions fall
# back to what the model said). server -> {"url": str, "refs": {"e12": (role, name)}}.
# Playwright MCP: `- button "Post" [ref=e12]` and `- Page URL: https://x.com/home`;
# agent-browser: the same lines, refs renumbered by each snapshot (used as @e12).
PAGES = {}
# role, optional quoted name, then the bracketed attributes: the element's own ref is in those.
# Anything after the colon is page text and is never read.
SNAP_LINE = re.compile(r'^\s*-\s*([A-Za-z][\w-]*)\s*(?:"((?:[^"\\]|\\.)*)")?\s*((?:\[[^\]]*\]\s*)*)')
REF_IN_ATTRS = re.compile(r"\bref=([A-Za-z0-9_]+)")
URL_LINE = re.compile(r"^\s*-?\s*Page URL:\s*(\S+)", re.I | re.M)
# Only these tools' results carry a snapshot of the page; get_text, read, console, evaluate...
# return page-controlled text that must never rewrite refs or the URL.
SNAPSHOT_TOOL = re.compile(r"(?:^|_)(?:snapshot|navigate|goto|open|click|type|press|select|hover|"
                           r"fill|tabs?|drag|back|forward|reload|dialog|check|scroll|wait)", re.I)
PAGE_STATE = re.compile(r"^#{1,4}\s*Page state\s*$", re.I | re.M)
FENCE = re.compile(r"^```.*$", re.M)
SHARED_SUFFIXES = ("github.io", "vercel.app", "netlify.app", "pages.dev", "herokuapp.com",
                   "web.app", "firebaseapp.com", "blogspot.com", "workers.dev", "onrender.com",
                   "glitch.me")


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


LABEL_KEYS = ("resolved", "element")


def _label(args):
    """The human-readable words naming what a call acts on: the accessible name a ref resolved
    to or the `element` description. Raw selectors (button:nth-of-type(3), div > span), bare
    refs like @e12 and option flags never count: nothing says what they commit."""
    words = []

    def collect(item):
        for key in LABEL_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip() and not REFERENCE.match(value.strip()):
                words.append(value.strip())

    collect(args)
    for item in args.get("fields") if isinstance(args.get("fields"), list) else ():
        if isinstance(item, dict):
            collect(item)
    label = " ".join(words)
    return label if re.search(r"[A-Za-z]{3,}", label) else ""


def blocked(tool, args):
    """Why this browser call is refused outright (credentials, card numbers, password
    changes), whatever rules.confirm says; None otherwise. `tool` is the MCP tool name."""
    tool, args = str(tool or ""), args if isinstance(args, dict) else {}
    if not INTERACTIVE.search(tool):
        return None
    if TYPING.search(tool):
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


def _clean(text, limit=60):
    text = re.sub(r"\s+", " ", str(text or "").replace('\\"', '"')).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _snapshot_part(text):
    """(header, body): the text to read for the URL and for refs. A "### Page state" section
    (the last one: earlier sections hold page-controlled results) is cut down to its header
    lines and the fenced snapshot; text without one is used whole by callers that know it is
    a snapshot."""
    marks = list(PAGE_STATE.finditer(text))
    if not marks:
        return None
    section = text[marks[-1].end():]
    nxt = re.search(r"^#{1,4}\s", section, re.M)
    section = section[:nxt.start()] if nxt else section
    fences = list(FENCE.finditer(section))
    if len(fences) >= 2:
        return section[:fences[0].start()], section[fences[0].end():fences[-1].start()]
    if fences:
        return section[:fences[0].start()], section[fences[0].end():]
    return section, section


def observe(server, text, tool=None):
    """Learn refs and the page URL from a tool result's text. `tool` is the tool that produced
    it: only snapshot-bearing tools count ("" = unknown: then a "Page state" section is
    required); None = a caller that already knows this is a snapshot. Page text is data: it
    only ever names things in David's question, and only a snapshot section is read."""
    if not isinstance(text, str) or not text:
        return
    if tool is not None and tool and not SNAPSHOT_TOOL.search(str(tool)):
        return
    part = _snapshot_part(text)
    if part is None:
        if tool is not None and not tool:
            return
        part = (text, text)
    header, body = part
    refs = {}
    for line in body.splitlines():
        match = SNAP_LINE.match(line)
        found = REF_IN_ATTRS.search(match.group(3)) if match else None
        if found:
            refs[found.group(1)] = (match.group(1), _clean(match.group(2)))
    url = URL_LINE.search(header)
    with _LOCK:
        page = PAGES.setdefault(server, {"url": "", "refs": {}})
        if url and url.group(1) != page["url"]:
            page["url"], page["refs"] = url.group(1), {}
        if refs:
            if "agent" in server:  # renumbered by every snapshot
                page["refs"] = refs
            else:
                page["refs"].update(refs)


def observe_result(server, result, tool=None):
    """observe() over an MCP tools/call result ({"content": [{"type": "text", ...}]})."""
    content = result.get("content") if isinstance(result, dict) else None
    for part in content if isinstance(content, list) else ():
        if isinstance(part, dict) and part.get("type") == "text":
            observe(server, part.get("text"), "" if tool is None else tool)


NAVIGATING = re.compile(r"navigate|goto|open|tab|back|forward|reload|close", re.I)


def _forget_typing(server, tool):
    """Enter is only exempt right after typing into a search box: anything else between
    (a click, a navigation, a tab switch) ends that."""
    if not TYPING.search(str(tool)) and \
            (INTERACTIVE.search(str(tool)) or NAVIGATING.search(str(tool))):
        with _LOCK:
            _TYPED[server] = False


def observe_call(server, tool, args):
    """A navigation names the next page before its result arrives; old refs are void."""
    _forget_typing(server, tool)
    url = args.get("url") if isinstance(args, dict) else None
    if re.search(r"navigate|goto|open", str(tool), re.I) and isinstance(url, str):
        with _LOCK:
            PAGES[server] = {"url": url, "refs": {}}


def site_of(url):
    """x.com for https://mobile.x.com/home; None when there is no usable host."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    parts = host.split(".")
    if len(parts) < 2 or re.fullmatch(r"[\d.]+", host):
        return host
    if any(host == suffix or host.endswith("." + suffix) for suffix in SHARED_SUFFIXES):
        return host  # tenants of a shared suffix are different sites
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "gov", "ac", "edu"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _ref_of(item):
    for key in TARGET_KEYS:
        value = item.get(key)
        if isinstance(value, str) and REFERENCE.match(value.strip()):
            return value.strip()
    return None


def _lookup(server, item):
    """(role, name) the last snapshot gave the ref `item` points at, or None."""
    with _LOCK:
        refs = dict((PAGES.get(server) or {}).get("refs") or {})
    ref = _ref_of(item) if isinstance(item, dict) else None
    return refs.get(ref.lstrip("@")) if ref else None


def _is_search(server, item):
    """Only what the snapshot shows counts: a searchbox, or a name that says search. The
    model's own description of an unresolved ref is not evidence."""
    hit = _lookup(server, item)
    return bool(hit) and (hit[0].lower() == "searchbox" or bool(SEARCH.search(hit[1] or "")))


def enrich(server, args):
    """`args` plus a "resolved" entry (`button "Post"`) wherever a ref names an element the
    last snapshot described, at the top level and in each fill_form field. blocked() and
    commits() read it like any other description of the target."""
    with _LOCK:
        refs = dict((PAGES.get(server) or {}).get("refs") or {})
    if not refs or not isinstance(args, dict):
        return args

    def resolve(item):
        ref = _ref_of(item)
        hit = refs.get(ref.lstrip("@")) if ref else None
        return f'{hit[0]} "{hit[1]}"' if hit and hit[1] else None

    out = dict(args)
    found = resolve(args)
    if found:
        out["resolved"] = found
    if isinstance(args.get("fields"), list):
        fields = []
        for item in args["fields"]:
            found = resolve(item) if isinstance(item, dict) else None
            fields.append({**item, "resolved": found} if found else item)
        out["fields"] = fields
    return out


def _selector_commit(args):
    """A commit word in a raw selector or test id the call targets (not a bare ref)."""
    for key in TARGET_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip() and not REFERENCE.match(value.strip()):
            found = SELECTOR_COMMIT.search(re.sub(r"([a-z])([A-Z])", r"\1 \2", value))
            if found:
                return found
    return None


def _verb(tool):
    if re.search(r"dialog", tool, re.I):
        return "dialog"
    if TYPING.search(tool):
        return "type"
    if re.search(r"press|key", tool, re.I):
        return "press"
    if re.search(r"select", tool, re.I):
        return "select"
    return "click" if re.search(r"click", tool, re.I) else "other"


def judge(params):
    """None when the call may run unasked; else what David is asked: {"question", "reason",
    "verb", "target", "site", "stakes": "high"|"low", "named", "checked"}. high: commit words,
    Enter, submit, a page dialog. checked: False when no snapshot showed the click target. named: something says what the element is (grants need that)."""
    server, tool = params.get("serverName") or "", _tool(params)
    args = (params.get("_meta") or {}).get("tool_params") or {}
    if not re.search(r"press|key", tool, re.I):  # press reads and clears it below
        _forget_typing(server, tool)
    if not INTERACTIVE.search(tool):
        return None
    args = enrich(server, args)
    with _LOCK:
        site = site_of((PAGES.get(server) or {}).get("url"))
    verb = _verb(tool)
    resolved = args.get("resolved")
    element = args.get("element") if isinstance(args.get("element"), str) else ""
    target = resolved or (f"“{_clean(element)}”" if element.strip() else None)
    where = f" on {site}" if site else ""
    named = bool(_label(args))

    def out(reason, stakes, question, suffix="", checked=True):
        return {"question": question + where + suffix, "reason": reason, "verb": verb,
                "target": target, "site": site, "stakes": stakes, "named": named and checked,
                "checked": checked}

    if "dialog" in tool:
        return out("accept a page dialog", "high", "accept a page dialog") \
            if args.get("accept", True) else None
    described = " ".join(s for k, v in args.items() if k not in ("text", "value", "values")
                         for s in _strings(v))
    typing = TYPING.search(tool)
    if typing:
        items = [i for i in (args.get("fields") if isinstance(args.get("fields"), list) else [args])
                 if isinstance(i, dict)]
        searching = bool(items) and all(_is_search(server, i) for i in items)
        with _LOCK:
            _TYPED[server] = searching
        field = target or "a field the page didn't name"
        if args.get("submit") and not searching:
            return out(f"type and submit into {described[:60] or 'a field'}", "high",
                       f"type into {field}", " (presses Enter)")
        if not searching and any(re.search(r"[\r\n]", text) for _, text in _typed(args)):
            return out(f"type text with a line break (presses Enter) into {described[:60] or 'a field'}",
                       "high", f"type into {field}", " (presses Enter)")
        for item in items:  # a checkbox/radio/combobox value is a choice, like a clicked option
            kind = str(item.get("type") or "").lower()
            chosen = " ".join(s for k in ("value", "values") for s in _strings(item.get(k, "")))
            found = COMMIT.search(chosen) if kind in ("checkbox", "radio", "combobox") else None
            if found:
                return out(f"set {kind} to “{found.group(0)}” ({_clean(chosen, 40)})", "high",
                           f"set {target or described[:60] or kind} to {_clean(chosen, 40)}")
        if not searching and not named:
            return out("type into a field the page didn't name", "low", f"type into {field}")
        return None
    if re.search(r"press|key", tool, re.I):
        # \n and \r are Enter to Playwright; any combo ending in Enter submits.
        with _LOCK:
            searching = _TYPED.get(server, False)
            _TYPED[server] = False
        for text in _strings(args):
            if re.search(r"[\r\n]", text):
                final = "enter"
            elif text == " ":
                final = "space"
            else:
                final = text.lower().split("+")[-1].strip()
            # Space presses a focused button just like Enter (audit 2026-11-03: type, Tab, Space
            # posted on x.com unasked).
            if final in ("enter", "return", "numpadenter", "space", "spacebar") and not searching:
                key = text.strip().lower() or "enter"
                key = "enter" if re.search(r"[\r\n]", text) else key
                return out(f"press {key} after typing", "high", f"press {key} after typing")
        return None
    name = re.sub(r"^(?:agent_)?browser_", "", tool).replace("_", " ")
    ref = _ref_of(args)
    described = " ".join(s for k, v in args.items() if k not in ("text",) for s in _strings(v))
    found = COMMIT.search(described) or _selector_commit(args)
    if found:
        return out(f"{name} “{found.group(0)}” ({described[:60]})", "high",
                   f"{name} {target or described[:60]}")
    hit = _lookup(server, args) if ref else None
    if hit is None:
        # No snapshot names this target (a raw selector, or a ref the gate never saw: the Codex
        # lane passes no tool results). The model's own `element` words are not evidence that
        # it isn't Post or Pay (audit 2026-11-04): unchecked, never grantable, and it asks
        # even under rules.confirm: never while money or messages are in rules.always_ask.
        if element.strip():
            return out(f"{name} on an element no snapshot showed ({described[:40]})", "low",
                       f"{name} {target}", " (not checked against the page)", checked=False)
        shown = f"an element the page didn't name ({ref})" if ref else \
            f"“{_clean(described, 40)}”" if described.strip() else "an element the page didn't name"
        return out(f"{name} on {described[:30] or 'an unnamed element'} (no label)", "low",
                   f"{name} {shown}", checked=False)
    if not hit[1]:  # the snapshot shows it without a name: nothing says what it commits
        return out(f"{name} on {described[:30] or 'an unnamed element'} (no label)", "low",
                   f"{name} an element the page didn't name ({ref})", checked=False)
    return None


def commits(params):
    """Why this browser tool call would commit something as David, or None."""
    found = judge(params)
    return found["reason"] if found else None


def gate(policy, config, state, wait=None, queue=None):
    """approve(params) for the Codex lane: `policy` first (actions.approve_browser), then
    David's OK for anything that commits. Blocks the tool call while it waits. `wait`
    overrides the defaults (WAIT for high stakes, LOW_WAIT for low). While David's "allow on
    this site" grant for a (site, verb) lasts, low-stakes calls of that kind pass; never
    commit-like ones, refusals, or calls where the site or the element isn't known."""
    def approve(params):
        ok, why = policy(params)
        if not ok:
            return ok, why
        server = params.get("serverName") or ""
        raw = (params.get("_meta") or {}).get("tool_params")
        refusal = blocked(_tool(params), enrich(server, raw) if isinstance(raw, dict) else raw)
        if refusal:  # not a question: refused even under rules.confirm: never
            return False, refusal
        info = judge(params)
        if not info:
            return ok, why
        from .gates import asks_now
        if not asks_now(config, state) and not always_asks(info, config):
            return ok, why  # rules.confirm: never (read live); money and messages still ask
        try:
            from .confirm import ConfirmQueue
            here = queue or ConfirmQueue(state)
            if info["stakes"] == "low" and info["site"] and info["named"] and \
                    hasattr(here, "has_grant") and here.has_grant(info["site"], info["verb"]):
                return True, f"allowed by David's {info['verb']} grant for {info['site']}"
            seconds = wait if wait is not None else (WAIT if info["stakes"] == "high" else LOW_WAIT)
            return ask(info["question"], config, state, seconds, here, info=info)
        except Exception as exc:  # can't ask David: refuse rather than wave through
            return False, f"Couldn't ask David ({type(exc).__name__}): {info['question']}"
    return approve


# Even in full-auto mode (rules.confirm: never) these wait for David: spending money and speaking
# for him to other people (agreed 2026-11-03). rules.always_ask lists the kinds; [] turns it off.
ALWAYS_ASK = {
    "money": re.compile(r"\b(buy|purchase|pay|payment|checkout|check out|place (?:your )?order|order now|"
                        r"donate|subscribe|transfer|withdraw|send money|tip|bid|book now|reserve|rent)\b", re.I),
    "messages": re.compile(r"\b(send|sent|post|posting|publish|tweet|retweet|repost|reply|comment|share|"
                           r"message|dm|email|invite)\b", re.I),
}


# Commit words that may send or pay, whichever of money/messages is on.
SEND_OR_PAY = re.compile(r"\b(submit|confirm|order|checkout)\b|“(?:send|post|tweet|reply|publish|"
                         r"submit|pay|buy|purchase|checkout|order|confirm|transfer|donate|"
                         r"subscribe|share|retweet|repost|comment|withdraw)”", re.I)


def always_asks(info, config):
    """True when this browser action waits for David even under rules.confirm: never. Judged
    by the action's stakes, not the question's wording: Enter or Ctrl+Enter in a composer,
    type-and-submit, a dialog accept or a click on an unchecked target can send or pay with no
    "send" or "pay" in sight (audit 2026-11-04). While money or messages is in
    rules.always_ask these ask: Enter/submit/dialog accept, any click or choice no snapshot
    checked, and high-stakes clicks whose words (selectors and test ids included) say send,
    post, pay, buy, submit, confirm, order... A checked "Delete draft" or "Next page" doesn't."""
    kinds = ((config or {}).get("rules") or {}).get("always_ask")
    kinds = ["money", "messages"] if kinds is None else [kinds] if isinstance(kinds, str) else kinds
    if not isinstance(kinds, (list, tuple, set)):
        kinds = list(ALWAYS_ASK)  # unreadable: fail closed
    if not any(k in ALWAYS_ASK for k in kinds):
        return False  # [] (or only unknown kinds): full auto
    if info.get("checked") is False:
        return True
    if info.get("stakes") != "high":
        return False
    if info.get("verb") in ("type", "press", "dialog"):  # Enter, submit, accept: always commits
        return True
    text = " ".join(str(info.get(k) or "") for k in ("question", "target", "reason"))
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return bool(SEND_OR_PAY.search(text)) or \
        any(ALWAYS_ASK[k].search(text) for k in kinds if k in ALWAYS_ASK)


def ask(action, config, state, wait=WAIT, queue=None, info=None, subject="the browser"):
    from .assistant import confirmation_origin
    from .confirm import ConfirmQueue, notify
    queue = queue or ConfirmQueue(state)
    origin = confirmation_origin(config, state)
    info = info or {}
    browser = {"site": info.get("site"), "verb": info.get("verb") or "other",
               "target": info.get("target"), "stakes": info.get("stakes") or "high",
               "named": bool(info.get("named")), "expires_at": round(time.time() + wait, 1)}
    item = queue.park({"intent": "browser_action", "device": origin,
                       "args": {"text": f"let {subject} {action}", "browser": browser}},
                      origin, "console")
    notify(f"let {subject} {action}")
    number, deadline = int(item["id"][1:]), time.monotonic() + wait
    timed_out = f"David didn't answer in {wait:g} s (timed out): {action}"
    answered = False
    try:
        while time.monotonic() < deadline:
            status = queue.status(number)
            if status == "approved":
                answered = True
                return True, f"David approved: {action}"
            if status == "rejected":
                answered = True
                return False, f"David declined: {action}"
            if status == "expired":
                answered = True
                return False, timed_out
            if hasattr(queue, "expire_safely"):
                queue.expire_safely()
            time.sleep(0.5)
        # Expire first, then look once more: an approval that landed after the last poll
        # (expire_item is then a no-op) is honoured, not reported as a timeout (audit 2026-11-04).
        answered = True
        try:
            if hasattr(queue, "expire_item"):
                queue.expire_item(item["id"])
            else:
                queue.reject(item["id"])
        except Exception:
            answered = False  # the finally tries once more
        try:
            if queue.status(number) == "approved":
                answered = True
                return True, f"David approved: {action}"
        except Exception:
            pass
        return False, timed_out
    finally:
        if not answered:  # timed out, or the poll itself failed: never leave it approvable
            try:
                if hasattr(queue, "expire_item"):
                    queue.expire_item(item["id"])
                else:
                    queue.reject(item["id"])
            except Exception:
                pass  # answered or expired at the last moment; still declined here
