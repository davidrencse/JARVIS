"""App UI: read and operate desktop apps through their accessibility tree (agent-desktop CLI).

The `app_ui` tool, with a JSON spec:

    {"action": "windows"}                                   -> open windows: app, title, focused
    {"action": "snapshot", "app": "dolphin"}                -> interactive elements, one per line
    {"action": "snapshot", "app": "dolphin", "root": "@s1:e3"}   -> drill into one region
    {"action": "find", "app": "dolphin", "role": "button", "name": "Save"}  (or "text")
    {"action": "read", "ref": "@s1:e7"}                     -> what the element says
    {"action": "click"|"toggle"|"expand"|"collapse"|"scroll_to", "ref": "@s1:e7"}
    {"action": "type", "ref": "@s1:e3", "text": "notes"}    (also set_value / select with "value")
    {"action": "wait", "app": "dolphin", "text": "Saved", "timeout": 5}

Unlike `computer` (pixels, the real mouse and keyboard), this never moves the pointer, takes
focus or presses keys: actions are semantic accessibility calls, so David can keep working.
--headed is never passed (no system-wide input without a fail-safe), so whatever needs the
physical pointer is refused by agent-desktop itself.

Safety, checked before every action:
- control.py's stop switch ("Jarvis, stop controlling my computer") stops this too.
- A ref must come from a snapshot or find JARVIS took, so the element's name is known.
- Clicking a control named like buy/pay/send/post waits for David's OK (rules.always_ask),
  even under rules.confirm: never; delete-like ones too when rules.confirm asks.
- Typing gets the `computer` tool's refusals: passwords, card numbers, one-time codes, and
  the terminal's hard_deny (only when the app is a terminal emulator: elsewhere it is prose).
  Text typed into one element in pieces is checked as one, for TYPED_TTL seconds.
- At most MAX_ACTIONS actions per 10 minutes. Every call is logged to logs/app_ui.jsonl.

Binary: AGENT_DESKTOP_BIN, else `agent-desktop` on PATH or in ~/.local/bin.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .control import STATE, _stopped

ACTIONS = ("windows", "snapshot", "find", "read", "wait", "click", "toggle", "expand",
           "collapse", "scroll_to", "type", "set_value", "select")
READS = ("windows", "snapshot", "find", "read", "wait")
CLI = {"scroll_to": "scroll-to", "set_value": "set-value"}
MAX_ACTIONS = 120
MAX_CHARS = 6000  # a snapshot the brain reads; past this it is told to drill in or find
MAX_FIND = 40
TIMEOUT = 20  # seconds for one CLI call
DESTRUCTIVE = re.compile(r"\b(delete|remove|discard|erase|empty trash|move to trash|format|"
                         r"uninstall|wipe|reset|overwrite|replace all)\b", re.I)
REF = re.compile(r"@[a-z0-9]+:e\d+")

_LOCK = threading.Lock()
_STATE = {"refs": {}, "times": [], "typed": {}}  # refs: qualified ref -> {"role", "name", "app"}
# typed: (app, role, name) -> text typed into that element since a newline, checked as one
TYPED_MAX = 4000
TYPED_TTL = 120  # seconds: older typed text is forgotten ("12" now and "34" later isn't a code)
TERMINAL_APP = re.compile(r"(?<![a-z])(terminal|konsole|console|shell|kitty|alacritty|foot|wezterm|"
                          r"ghostty|xterm|tilix|terminator|windowsterminal|powershell|pwsh|cmd)"
                          r"(?![a-z])", re.I)


class Refused(Exception):
    pass


def binary():
    found = os.environ.get("AGENT_DESKTOP_BIN") or shutil.which("agent-desktop")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "agent-desktop"
    return str(local) if local.is_file() else None


def _cli(args, timeout=TIMEOUT):
    """One agent-desktop call -> its JSON envelope ({"ok", "data"} or {"ok": False, "error"})."""
    exe = binary()
    if not exe:
        return {"ok": False, "error": {"code": "NOT_INSTALLED",
                                       "message": "agent-desktop isn't installed on this computer"}}
    out = subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=timeout)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": {"code": "BAD_OUTPUT",
                                       "message": (out.stderr or out.stdout).strip()[-200:] or "no output"}}


def _error(env):
    err = env.get("error") or {}
    text = f"{err.get('code', 'ERROR')}: {err.get('message', '')}".strip()
    if err.get("suggestion"):
        text += f" ({err['suggestion']})"
    return text


def _remember(ref, role, name, app):
    if ref:
        with _LOCK:
            refs = _STATE["refs"]
            refs[ref] = {"role": role or "", "name": name or "", "app": app or ""}
            if len(refs) > 4000:  # keep the newest
                for old in list(refs)[:len(refs) - 4000]:
                    refs.pop(old, None)


def _label(node):
    name = node.get("name") or node.get("placeholder") or node.get("description") or ""
    return re.sub(r"\s+", " ", str(name)).strip()[:80]


def _tree_lines(node, app, depth=0, out=None):
    """Indented lines for the nodes worth reading: hidden subtrees are dropped, nameless
    containers without a ref are folded into their children."""
    out = [] if out is None else out
    states = node.get("states") or []
    if "hidden" in states:
        return out
    ref, label = node.get("ref_id"), _label(node)
    shown = bool(ref or label)
    if shown:
        bits = [ref or "", node.get("role") or "", json.dumps(label, ensure_ascii=False) if label else ""]
        value = node.get("value")
        if value not in (None, "", label):
            bits.append("= " + json.dumps(str(value)[:80], ensure_ascii=False))
        flags = [s for s in states if s in ("disabled", "focused", "selected", "checked", "expanded")]
        if flags:
            bits.append("[" + ",".join(flags) + "]")
        if node.get("children_count") and not node.get("children"):
            bits.append(f"(+{node['children_count']} inside)")
        out.append("  " * depth + " ".join(b for b in bits if b))
        _remember(ref, node.get("role"), label, app)
    for child in node.get("children") or ():
        _tree_lines(child, app, depth + 1 if shown else depth, out)
    return out


def _clip(lines, more):
    text, kept = "", 0
    for line in lines:
        if len(text) + len(line) + 1 > MAX_CHARS:
            break
        text += line + "\n"
        kept += 1
    if kept < len(lines):
        text += f"... {len(lines) - kept} more lines cut. {more}\n"
    return text.rstrip()


def _check(state, action, spec, config):
    if _stopped(state):
        raise Refused("Desktop control is stopped (jarvis control resume, or wait).")
    if action in READS:
        return None
    now = time.time()
    with _LOCK:
        recent = [t for t in _STATE["times"] if now - t < 600]
        if len(recent) >= MAX_ACTIONS:
            raise Refused(f"More than {MAX_ACTIONS} app actions in 10 minutes; stopping here.")
        known = _STATE["refs"].get(str(spec.get("ref") or ""))
    if not known:
        raise Refused("Use a ref from a snapshot or find JARVIS just took, so it knows what it touches.")
    if action in ("type", "set_value", "select"):
        text = _text(action, spec)
        if not text:
            raise ValueError(f"{action} needs {'text' if action == 'type' else 'value'}")
        from .browser_gate import blocked
        described = f"{known['role']} {known['name']}"
        if action == "type":  # typing adds to what is there: check the pieces together
            text = _typed_before(known) + text
        terminal = TERMINAL_APP.search(f"{known['app']} {known['role']}")
        if terminal and len(text.rsplit("\n", 1)[-1]) > TYPED_MAX:  # never checked in part
            raise Refused(f"That line would pass {TYPED_MAX} characters; JARVIS can't check it. "
                          "Clear the line first.")
        refusal = blocked("browser_type", {"text": text, "element": described})
        if not refusal and re.fullmatch(r"\s*\d(?:[\s-]?\d){3,7}\s*", text):
            refusal = "JARVIS never types one-time codes or PINs"
        if not refusal and terminal:  # a terminal runs what is typed; elsewhere it is prose
            # This computer's config (vault paths) and home, where a terminal opens: that
            # terminal has no sandbox (audit 2026-11-04: cwd /tmp and {} let vault deletes by).
            from .gates import hard_deny, local_config
            refusal = hard_deny("terminal", {"command": text, "cwd": os.path.expanduser("~")},
                                config if config else local_config())
        if refusal:
            raise Refused(refusal)
    with _LOCK:
        _STATE["times"] = [t for t in _STATE["times"] if now - t < 600] + [now]
    return known


def _text(action, spec):
    """What type/set_value/select puts in: "" when it is missing or null."""
    value = spec.get("text" if action == "type" else "value")
    return "" if value is None else str(value)


def _target(known):
    return (known["app"], known["role"], known["name"])


def _typed_before(known):
    """What was typed into this element since its last newline, if within TYPED_TTL."""
    with _LOCK:
        text, at = _STATE["typed"].get(_target(known), ("", 0.0))
    if TERMINAL_APP.search(f"{known['app']} {known['role']}"):
        return text  # a shell line waits for Enter however long the pause
    return text if time.time() - at <= TYPED_TTL else ""


def _typed_after(known, action, text):
    """Remember what the element holds since its last newline (type adds, set_value replaces)."""
    before = _typed_before(known)
    with _LOCK:
        typed = _STATE["typed"]
        key = _target(known)
        now = (before + text) if action == "type" else text
        typed[key] = (now.rsplit("\n", 1)[-1][-TYPED_MAX:], time.time())
        if len(typed) > 400:  # keep the newest
            for old in list(typed)[:len(typed) - 400]:
                typed.pop(old, None)


def _needs_ok(action, known, config, state):
    """The question for David when this action commits something as him, else None."""
    if action not in ("click", "toggle", "select"):
        return None
    from .browser_gate import ALWAYS_ASK
    kinds = ((config or {}).get("rules") or {}).get("always_ask", ["money", "messages"])
    name = known["name"]
    what = f"{action} \"{name or known['role']}\" in {known['app'] or 'an app'}"
    if any(ALWAYS_ASK[k].search(name) for k in kinds if k in ALWAYS_ASK):
        return what
    from .gates import asks_now
    if DESTRUCTIVE.search(name) and asks_now(config or {}, state):
        return what
    return None


def act(spec, state=None, config=None, cli=None, ask=None):
    """Run one app_ui action. Returns (ok, text). cli(args) -> envelope and ask(question)
    -> (ok, why) are for tests."""
    state, cli = Path(state or STATE), cli or _cli
    spec = spec if isinstance(spec, dict) else {}
    action = str(spec.get("action") or "").lower()
    if action not in ACTIONS:
        return False, f"action must be one of {', '.join(ACTIONS)}"
    try:
        known = _check(state, action, spec, config)
        app = str(spec.get("app") or "").strip()
        if action == "windows":
            env = cli(["list-windows"])
            if not env.get("ok"):
                return False, _error(env)
            rows = [w for w in env.get("data") or [] if w.get("visible", True)]
            lines = [f"{w.get('app_name')}: {json.dumps(w.get('title') or '', ensure_ascii=False)}"
                     + (" [focused]" if w.get("is_focused") else "")
                     + ("" if w.get("accessible", True) else " (no accessibility tree)") for w in rows]
            return True, "\n".join(lines) or "No windows with an accessibility tree."
        if action in ("snapshot", "find", "wait") and not app:
            raise ValueError(f"{action} needs app (the name `windows` shows, e.g. dolphin)")
        if action == "snapshot":
            args = ["snapshot", "--app", app, "-i", "--compact"]
            if spec.get("root"):
                args += ["--root", str(spec["root"])]
            env = cli(args)
            if not env.get("ok"):
                return False, _error(env)
            data = env.get("data") or {}
            lines = _tree_lines(data.get("tree") or {}, app)
            _log(state, {"action": action, "app": app, "lines": len(lines)})
            return True, _clip(lines, "Snapshot a region with root=<ref>, or use find.") or \
                "No interactive elements visible."
        if action == "find":
            args = ["find", "--app", app, "--state", "hidden=false"]
            for key in ("role", "name", "text"):
                if spec.get(key):
                    args += [f"--{key}", str(spec[key])]
            env = cli(args)
            if not env.get("ok"):
                return False, _error(env)
            matches = (env.get("data") or {}).get("matches") or []
            lines = []
            for m in matches[:MAX_FIND]:
                label = re.sub(r"\s+", " ", str(m.get("name") or "")).strip()[:80]
                _remember(m.get("ref_id"), m.get("role"), label, app)
                where = " > ".join(p for p in (m.get("path") or [])[-2:] if p not in ("group",))
                value = m.get("value")
                lines.append(" ".join(b for b in (
                    m.get("ref_id") or "(no ref: not unique)", m.get("role") or "",
                    json.dumps(label, ensure_ascii=False) if label else "",
                    f"= {json.dumps(str(value)[:80], ensure_ascii=False)}" if value not in (None, "", label) else "",
                    f"in {where}" if where else "") if b))
            more = f"\n... {len(matches) - MAX_FIND} more; narrow it with role or name." \
                if len(matches) > MAX_FIND else ""
            return True, ("\n".join(lines) + more) if lines else "Nothing matched."
        if action == "read":
            ref = str(spec.get("ref") or "")
            if not REF.fullmatch(ref):
                raise ValueError("read needs a ref like @s1abc:e7")
            env = cli(["get", ref, "--property", "text"])
            if not env.get("ok"):
                return False, _error(env)
            return True, str((env.get("data") or {}).get("value") or "")[:MAX_CHARS]
        if action == "wait":
            seconds = max(1, min(int(spec.get("timeout") or 10), 30))
            args = ["wait", "--app", app, "--timeout", str(seconds * 1000)]
            if spec.get("text"):
                args += ["--text", str(spec["text"])]
            elif spec.get("window"):
                args += ["--window", str(spec["window"])]
            else:
                raise ValueError("wait needs text or window")
            env = cli(args) if cli is not _cli else _cli(args, timeout=seconds + 10)
            return (True, "It appeared.") if env.get("ok") else (False, _error(env))
        # -- actions on a ref -------------------------------------------------------------
        ref = str(spec["ref"])
        question = _needs_ok(action, known, config, state)
        if question:
            if ask is None and config is None:  # on another computer: David answers on the main
                raise Refused(f"{question} needs David's OK, which this computer can't ask for. "
                              "Tell him and let him do it.")
            if ask is None:
                from .browser_gate import ask as ask_david
                ok, why = ask_david(question, config or {}, state, subject="JARVIS")
            else:
                ok, why = ask(question)
            if not ok:
                _log(state, {"action": action, "ref": ref, "name": known["name"], "refused": why})
                return False, why
        args = [CLI.get(action, action), ref]
        if action in ("type", "set_value", "select"):
            args.append(_text(action, spec))
        env = cli(args)
        if action in ("type", "set_value"):
            _typed_after(known, action, _text(action, spec))
        entry = {"action": action, "ref": ref, "name": known["name"], "app": known["app"],
                 **({"chars": len(_text(action, spec))}
                    if action in ("type", "set_value") else {})}
        if not env.get("ok"):
            err = env.get("error") or {}
            safe = (err.get("disposition") or {}).get("retry") == "safe"
            _log(state, {**entry, "error": err.get("code")})
            tail = "" if safe else " It may have landed anyway: snapshot again before retrying."
            return False, _error(env) + tail
        delivery = ((env.get("data") or {}).get("disposition") or {}).get("delivery") or "delivered"
        _log(state, {**entry, "delivery": delivery})
        note = "" if delivery == "delivered_verified" else \
            " Not confirmed by the app: snapshot or read to check the effect."
        return True, f"Done: {action} {known['role']} \"{known['name']}\".{note}"
    except Refused as exc:
        _log(state, {"action": action, "refused": str(exc)})
        return False, f"Refused: {exc}"
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        _log(state, {"action": action, "error": str(exc)[:200]})
        return False, f"Couldn't {action}: {exc}"


def _log(state, entry):
    """logs/app_ui.jsonl, next to control.jsonl."""
    try:
        path = Path(state) / "logs" / "app_ui.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}) + "\n")
    except OSError:
        pass


def run(arg, config=None, state=None):
    """The app_ui desktop action over the cluster: arg is the JSON spec. config is given
    only where David can be asked (the main), so always_ask clicks reach his queue."""
    try:
        spec = json.loads(arg) if isinstance(arg, str) else dict(arg or {})
    except ValueError:
        return False, "app_ui takes a JSON spec"
    if not isinstance(spec, dict):
        return False, "app_ui takes a JSON object"
    return act(spec, state=state, config=config)
