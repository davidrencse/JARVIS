"""What each request cost: tokens, an API-equivalent dollar figure, and the share of the
subscription windows it used.

JARVIS never pays per token (subscriptions only), so the dollars are notional: what the same
tokens would cost at API list prices, a common yardstick across lanes. The percentages are
the useful number. Codex reports its windows (5 h and weekly) only as whole percents, so
one request's share is estimated from a calibration: across recent Codex session logs, how
many API-equivalent dollars it took to move each window by 1%.

A request opens a meter (`with meter() as m`); every model call inside it records into the
meter, so nested lanes, fallbacks and agent runs all add up to one figure per request.
"""

import contextlib
import contextvars
import json
import os
import time
from pathlib import Path

# USD per 1M tokens: input, cached input, output. Overridable in config: usage.prices.
PRICES = {
    "gpt": (1.25, 0.125, 10.0),
    "codex": (1.25, 0.125, 10.0),
    "opus": (15.0, 1.5, 75.0),
    "sonnet": (3.0, 0.3, 15.0),
    "haiku": (1.0, 0.1, 5.0),
    "claude": (3.0, 0.3, 15.0),
}
_METER = contextvars.ContextVar("jarvis_usage_meter", default=None)
_CALIBRATION, _CALIBRATED_AT = None, 0.0


def price(model, prices=None):
    name = (model or "").lower()
    table = {**PRICES, **(prices or {})}
    for key in sorted(table, key=len, reverse=True):
        if key in name:
            return table[key]
    return table["gpt"]


def tokens(raw):
    """Normalize any usage dict (Codex snake/camel case, Claude) to input/cached/output."""
    raw = raw or {}
    get = lambda *keys: next((int(raw[k]) for k in keys if isinstance(raw.get(k), (int, float))), 0)
    cached = get("cached_input_tokens", "cachedInputTokens", "cache_read_input_tokens")
    created = get("cache_creation_input_tokens", "cache_write_input_tokens", "cacheWriteInputTokens")
    inp = get("input_tokens", "inputTokens")
    # Codex counts cached tokens inside input_tokens; Claude reports them separately.
    claude_style = "cache_read_input_tokens" in raw
    fresh = inp + created if claude_style else max(0, inp - cached)
    out = get("output_tokens", "outputTokens")
    return {"input": fresh, "cached": cached, "output": out, "total": fresh + cached + out}


def dollars(tok, model, prices=None):
    p_in, p_cached, p_out = price(model, prices)
    return (tok["input"] * p_in + tok["cached"] * p_cached + tok["output"] * p_out) / 1e6


class Meter:
    def __init__(self):
        self.calls = []

    def add(self, provider, model, tok, usd=None, prices=None):
        tok = tokens(tok) if "total" not in (tok or {}) else tok
        cost = dollars(tok, model, prices) if usd is None else float(usd)
        self.calls.append({"provider": provider, "model": model, **tok, "usd": cost})

    def summary(self):
        """{tokens, usd, windows: {5h, week}, calls, engine}. Zero calls = a local path."""
        if not self.calls:
            return {"tokens": 0, "usd": 0.0, "windows": {"5h": 0.0, "week": 0.0}, "engine": "local", "calls": 0}
        total = sum(c["total"] for c in self.calls)
        usd = sum(c["usd"] for c in self.calls)
        codex_usd = sum(c["usd"] for c in self.calls if c["provider"] in ("chatgpt", "codex"))
        cal = calibration()
        windows = {}
        for name in ("5h", "week"):
            per = cal.get(name)
            windows[name] = round(codex_usd / per * 1.0, 6) if per and codex_usd else (0.0 if not codex_usd else None)
        providers = sorted({c["provider"] for c in self.calls})
        return {"tokens": total, "usd": round(usd, 6), "windows": windows, "engine": "+".join(providers),
                "calls": len(self.calls), "input": sum(c["input"] for c in self.calls),
                "cached": sum(c["cached"] for c in self.calls), "output": sum(c["output"] for c in self.calls)}


@contextlib.contextmanager
def meter():
    """Open a meter for one request (or join the one already open)."""
    current = _METER.get()
    if current is not None:
        yield current
        return
    m = Meter()
    token = _METER.set(m)
    try:
        yield m
    finally:
        _METER.reset(token)


def record(provider, model, raw, usd=None, prices=None):
    """Called by a lane after a model call; a no-op outside a request's meter."""
    m = _METER.get()
    if m is not None and raw:
        try:
            m.add(provider, model, raw, usd, prices)
        except Exception:  # metering must never fail a request
            pass


def record_run(directory, provider, model):
    """Read a finished agent run's stream (runner.py's stdout.jsonl) and record its usage."""
    path = Path(directory) / "stdout.jsonl"
    if _METER.get() is None or not path.is_file():
        return
    usd, raw = None, None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":  # Claude: its own cost and usage
            usd, raw = event.get("total_cost_usd"), event.get("usage")
        elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):  # Codex exec
            raw = event["usage"]
    if raw:
        record(provider, model, raw, usd)


def _codex_events(days=14, root=None):
    """(time, session, total_token_usage, primary, secondary) from recent Codex session logs."""
    root = Path(root or Path.home() / ".codex" / "sessions")
    cutoff = time.time() - days * 86400
    out = []
    for path in root.glob("*/*/*/*.jsonl") if root.is_dir() else []:
        try:
            if path.stat().st_mtime < cutoff:
                continue
            for line in path.open(encoding="utf-8", errors="replace"):
                if '"token_count"' not in line or '"rate_limits"' not in line:
                    continue
                try:
                    payload = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                info, limits = payload.get("info") or {}, payload.get("rate_limits") or {}
                if not info.get("total_token_usage") or not limits.get("primary"):
                    continue
                out.append((json.loads(line).get("timestamp", ""), path.name, info["total_token_usage"],
                            limits.get("primary") or {}, limits.get("secondary") or {}))
        except OSError:
            continue
    out.sort()
    return out


def calibrate(root=None, model="gpt", prices=None):
    """API-equivalent dollars per 1% of each Codex window, from how far the windows moved
    while known tokens were spent. Uses windows that moved at least 2% (whole-percent steps
    are too coarse below that)."""
    events = _codex_events(root=root)
    spent = {"5h": {}, "week": {}}
    moved = {"5h": {}, "week": {}}
    last_total = {}
    for _, session, total, primary, secondary in events:
        prev = last_total.get(session)
        last_total[session] = total
        if prev is None:
            delta = None
        else:
            now, before = tokens(total), tokens(prev)
            delta = {k: max(0, now[k] - before[k]) for k in ("input", "cached", "output")}
            delta["total"] = sum(delta.values())
        for name, window in (("5h", primary), ("week", secondary)):
            key = window.get("resets_at")
            if key is None or window.get("used_percent") is None:
                continue
            lo, hi = moved[name].get(key, (window["used_percent"], window["used_percent"]))
            moved[name][key] = (min(lo, window["used_percent"]), max(hi, window["used_percent"]))
            if delta:
                spent[name][key] = spent[name].get(key, 0.0) + dollars(delta, model, prices)
    result = {}
    for name in ("5h", "week"):
        usd = pct = 0.0
        for key, (lo, hi) in moved[name].items():
            if hi - lo >= 2 and spent[name].get(key):
                usd += spent[name][key]; pct += hi - lo
        result[name] = round(usd / pct, 6) if pct else None
    return result


def calibration(state=None):
    """Cached calibration (recomputed at most every 6 hours)."""
    global _CALIBRATION, _CALIBRATED_AT
    if _CALIBRATION is None or time.time() - _CALIBRATED_AT > 21600:
        try:
            _CALIBRATION = calibrate()
        except Exception:
            _CALIBRATION = {"5h": None, "week": None}
        _CALIBRATED_AT = time.time()
    return _CALIBRATION
