"""Trading strategies as data: rules over indicators, backtested on True Markets history.

A strategy is plain JSON, never code, so the terminal can create and edit them safely:

    {"name": "Golden cross", "symbols": ["BTC", "ETH"], "window": "180d",
     "resolution": "1d", "entry": ["sma(20) crosses_above sma(50)"],
     "exit": ["sma(20) crosses_below sma(50)"], "stop_loss": 8, "take_profit": 0,
     "fee_bps": 10}

Entry fires when every entry rule holds; exit when any exit rule holds, or the stop
loss / take profit (percent from entry) is hit. Long-only, all-in, one position per
symbol. A signal on a bar's close fills at the next bar's price, so there is no
look-ahead. Recon only: results and live signals are research, nothing is traded.

Rule grammar: TERM OP TERM, where TERM is a number, `price`, or an indicator call
(sma, ema, rsi, roc, zscore, high, low, vol, with an integer period) and OP is one
of > < >= <= crosses_above crosses_below.
"""

import json
import math
import re
import statistics
import threading
import time
import uuid
from pathlib import Path

OPS = (">=", "<=", ">", "<", "crosses_above", "crosses_below")
INDICATORS = ("sma", "ema", "rsi", "roc", "zscore", "high", "low", "vol")
TERM = r"(?:-?\d+(?:\.\d+)?|price|(?:%s)\(\d{1,3}\))" % "|".join(INDICATORS)
RULE = re.compile(rf"\s*({TERM})\s+({'|'.join(map(re.escape, OPS))})\s+({TERM})\s*$")
RESOLUTIONS = ("1m", "5m", "15m", "1h", "4h", "1d")
BARS_PER_YEAR = {"1m": 525600, "5m": 105120, "15m": 35040, "1h": 8760, "4h": 2190, "1d": 365}

TEMPLATES = [
    {"name": "Golden cross", "kind": "trend",
     "about": "Ride the trend when the fast average climbs over the slow one.",
     "window": "365d", "resolution": "1d", "entry": ["sma(20) crosses_above sma(50)"],
     "exit": ["sma(20) crosses_below sma(50)"], "stop_loss": 12, "take_profit": 0},
    {"name": "EMA momentum", "kind": "trend",
     "about": "Fast EMA over slow EMA with positive 12-bar momentum.",
     "window": "90d", "resolution": "4h", "entry": ["ema(12) > ema(26)", "roc(12) > 2"],
     "exit": ["ema(12) < ema(26)"], "stop_loss": 6, "take_profit": 0},
    {"name": "RSI reversion", "kind": "mean reversion",
     "about": "Buy oversold dips, sell into strength.",
     "window": "90d", "resolution": "4h", "entry": ["rsi(14) < 30"],
     "exit": ["rsi(14) > 55"], "stop_loss": 8, "take_profit": 0},
    {"name": "Z-score snapback", "kind": "mean reversion",
     "about": "Two standard deviations under the 20-bar mean, out at the mean.",
     "window": "30d", "resolution": "1h", "entry": ["zscore(20) < -2"],
     "exit": ["zscore(20) > 0"], "stop_loss": 5, "take_profit": 0},
    {"name": "Donchian breakout", "kind": "breakout",
     "about": "New 20-bar high in; lose the 10-bar low out.",
     "window": "180d", "resolution": "1d", "entry": ["price > high(20)"],
     "exit": ["price < low(10)"], "stop_loss": 10, "take_profit": 0},
]


# ---- indicators (each returns a list aligned to prices, None until it has data) ----------
def sma(p, n):
    out, total = [None] * len(p), 0.0
    for i, v in enumerate(p):
        total += v
        if i >= n:
            total -= p[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(p, n):
    out, k, value = [None] * len(p), 2 / (n + 1), None
    for i, v in enumerate(p):
        value = v if value is None else v * k + value * (1 - k)
        if i >= n - 1:
            out[i] = value
    return out


def rsi(p, n):
    out, gain, loss = [None] * len(p), 0.0, 0.0
    for i in range(1, len(p)):
        change = p[i] - p[i - 1]
        up, down = max(change, 0), max(-change, 0)
        if i <= n:
            gain, loss = gain + up / n, loss + down / n
        else:  # Wilder smoothing
            gain, loss = (gain * (n - 1) + up) / n, (loss * (n - 1) + down) / n
        if i >= n:
            out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


def roc(p, n):
    return [None if i < n or not p[i - n] else (p[i] / p[i - n] - 1) * 100
            for i in range(len(p))]


def zscore(p, n):
    out = [None] * len(p)
    for i in range(n - 1, len(p)):
        window = p[i - n + 1:i + 1]
        sd = statistics.pstdev(window)
        out[i] = 0.0 if sd == 0 else (p[i] - statistics.fmean(window)) / sd
    return out


def high(p, n):  # highest of the n bars *before* this one, so price > high(n) is a breakout
    return [None if i < n else max(p[i - n:i]) for i in range(len(p))]


def low(p, n):
    return [None if i < n else min(p[i - n:i]) for i in range(len(p))]


def vol(p, n):
    rets = [None] + [(b / a - 1) * 100 if a else 0 for a, b in zip(p, p[1:])]
    return [None if i < n else statistics.pstdev(rets[i - n + 1:i + 1]) for i in range(len(p))]


FUNCS = {"sma": sma, "ema": ema, "rsi": rsi, "roc": roc, "zscore": zscore, "high": high,
         "low": low, "vol": vol}


def parse(rule):
    m = RULE.match(rule or "")
    if not m:
        raise ValueError(f"Can't read rule {rule!r}. Use e.g. 'sma(20) crosses_above sma(50)'.")
    for term in (m.group(1), m.group(3)):
        if "(" in term and not 1 <= int(term.split("(")[1][:-1]) <= 400:
            raise ValueError(f"Period out of range in {term!r} (1-400).")
    return m.group(1), m.group(2), m.group(3)


def _series(term, prices, cache):
    if term not in cache:
        if term == "price":
            cache[term] = prices
        elif "(" in term:
            name, n = term[:-1].split("(")
            cache[term] = FUNCS[name](prices, int(n))
        else:
            cache[term] = [float(term)] * len(prices)
    return cache[term]


def evaluate(rule, prices, cache):
    """Per-bar booleans for one rule (False while indicators warm up)."""
    left, op, right = parse(rule)
    a, b = _series(left, prices, cache), _series(right, prices, cache)
    out = [False] * len(prices)
    for i in range(len(prices)):
        x, y = a[i], b[i]
        if x is None or y is None:
            continue
        if op in ("crosses_above", "crosses_below"):
            if i == 0 or a[i - 1] is None or b[i - 1] is None:
                continue
            out[i] = (a[i - 1] <= b[i - 1] and x > y) if op == "crosses_above" else \
                     (a[i - 1] >= b[i - 1] and x < y)
        else:
            out[i] = {">": x > y, "<": x < y, ">=": x >= y, "<=": x <= y}[op]
    return out


def validate(spec):
    """A clean strategy dict, or ValueError naming what's wrong."""
    if not isinstance(spec, dict):
        raise ValueError("A strategy must be an object.")
    name = str(spec.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Give the strategy a name.")
    symbols = [str(s).strip().upper() for s in spec.get("symbols") or [] if str(s).strip()]
    if not symbols or len(symbols) > 25 or not all(re.fullmatch(r"[A-Z0-9._-]{1,24}", s)
                                                    for s in symbols):
        raise ValueError("List 1-25 tickers, e.g. BTC, ETH, SOL.")
    from .markets import WINDOWS
    window = spec.get("window") or "90d"
    resolution = spec.get("resolution") or "4h"
    if window not in WINDOWS or resolution not in RESOLUTIONS:
        raise ValueError("Pick a supported window and bar size.")
    entry = [r.strip() for r in spec.get("entry") or [] if str(r).strip()]
    exit_ = [r.strip() for r in spec.get("exit") or [] if str(r).strip()]
    if not entry:
        raise ValueError("Add at least one entry rule.")
    for rule in entry + exit_:
        parse(rule)

    def pct(key, top):
        try:
            value = float(spec.get(key) or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number.") from None
        if not 0 <= value <= top:
            raise ValueError(f"{key} must be between 0 and {top}.")
        return value
    return {"id": spec.get("id") or uuid.uuid4().hex[:10], "name": name,
            "symbols": list(dict.fromkeys(symbols)), "window": window,
            "resolution": resolution, "entry": entry, "exit": exit_,
            "stop_loss": pct("stop_loss", 90), "take_profit": pct("take_profit", 1000),
            "fee_bps": pct("fee_bps", 500) if spec.get("fee_bps") is not None else 10.0,
            "notes": str(spec.get("notes") or "")[:500]}


def backtest(spec, prices, times=None):
    """Simulate one symbol. Returns metrics, trades, equity and buy-and-hold curves."""
    n = len(prices)
    if n < 10:
        raise ValueError("Not enough history to test (need 10+ bars).")
    cache = {}
    entry = [evaluate(r, prices, cache) for r in spec["entry"]]
    exits = [evaluate(r, prices, cache) for r in spec["exit"]]
    fee = spec["fee_bps"] / 10_000
    cash, units, entry_px, entry_i = 1.0, 0.0, None, None
    equity, trades, pending = [], [], None
    for i in range(n):
        px = prices[i]
        if pending == "buy" and units == 0:
            units, cash, entry_px, entry_i = cash * (1 - fee) / px, 0.0, px, i
        elif pending == "sell" and units:
            cash, ret = units * px * (1 - fee), px / entry_px - 1
            trades.append({"entry": entry_i, "exit": i, "entry_px": entry_px, "exit_px": px,
                           "ret": round(ret * 100, 3), "bars": i - entry_i,
                           **({"entry_t": times[entry_i], "exit_t": times[i]} if times else {})})
            units, entry_px = 0.0, None
        pending = None
        if units:
            move = px / entry_px - 1
            stop = spec["stop_loss"] and move <= -spec["stop_loss"] / 100
            take = spec["take_profit"] and move >= spec["take_profit"] / 100
            if stop or take or any(e[i] for e in exits):
                pending = "sell"
        elif all(e[i] for e in entry):
            pending = "buy"
        equity.append(cash + units * px)
    final = equity[-1]
    rets = [b / a - 1 for a, b in zip(equity, equity[1:]) if a]
    sd = statistics.pstdev(rets) if len(rets) > 2 else 0
    per_year = BARS_PER_YEAR[spec["resolution"]]
    peak, max_dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)
    wins = [t for t in trades if t["ret"] > 0]
    exposure = sum(t["bars"] for t in trades) + ((n - 1 - entry_i) if units else 0)
    return {
        "metrics": {
            "total_return": round((final - 1) * 100, 2),
            "buy_hold": round((prices[-1] / prices[0] - 1) * 100, 2),
            "sharpe": round(statistics.fmean(rets) / sd * math.sqrt(per_year), 2) if sd else 0,
            "max_drawdown": round(max_dd * 100, 2), "trades": len(trades),
            "win_rate": round(100 * len(wins) / len(trades), 1) if trades else None,
            "avg_trade": round(statistics.fmean(t["ret"] for t in trades), 2) if trades else None,
            "exposure": round(100 * exposure / n, 1), "bars": n},
        "position": {"open": bool(units), "entry_px": entry_px,
                     "since": times[entry_i] if units and times else None,
                     "unrealized": round((prices[-1] / entry_px - 1) * 100, 2) if units else None},
        "signal": ("BUY" if pending == "buy" else "SELL" if pending == "sell" else
                   "HOLD" if units else "FLAT"),
        "last": prices[-1],
        "trades": trades[-50:],
        "equity": _thin(equity), "hold": _thin([p / prices[0] for p in prices]),
        "times": _thin(times) if times else None,
    }


def _thin(values, points=240):
    if not values or len(values) <= points:
        return values
    step = len(values) / points
    return [values[int(i * step)] for i in range(points)] + [values[-1]]


class Book:
    """Saved strategies in .jarvis/strategies.json."""

    def __init__(self, state_dir):
        self.path = Path(state_dir).resolve() / "strategies.json"
        self.lock = threading.Lock()

    def all(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def save(self, spec):
        spec = validate(spec)
        with self.lock:
            items = [s for s in self.all() if s["id"] != spec["id"]]
            spec["updated"] = time.time()
            items.append(spec)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        return spec

    def delete(self, strategy_id):
        with self.lock:
            items = self.all()
            kept = [s for s in items if s["id"] != strategy_id]
            if len(kept) == len(items):
                raise LookupError("No such strategy.")
            self.path.write_text(json.dumps(kept, indent=2), encoding="utf-8")
        return {"deleted": strategy_id}

    def get(self, strategy_id):
        for s in self.all():
            if s["id"] == strategy_id:
                return s
        raise LookupError("No such strategy.")


def run(spec, market):
    """Backtest a strategy over each of its symbols with live True Markets history."""
    spec = validate(spec)
    from .markets import pool_map

    def one(symbol):
        try:
            pts = market.history(symbol, spec["window"], spec["resolution"])
            result = backtest(spec, [p["price"] for p in pts], [p["t"] for p in pts])
            return {"symbol": symbol, **result}
        except Exception as exc:
            return {"symbol": symbol, "error": str(exc)}
    results = pool_map(one, spec["symbols"])
    ok = [r for r in results if "metrics" in r]
    summary = None
    if ok:
        avg = lambda k: round(statistics.fmean(r["metrics"][k] for r in ok), 2)
        summary = {"total_return": avg("total_return"), "buy_hold": avg("buy_hold"),
                   "sharpe": avg("sharpe"), "max_drawdown": min(r["metrics"]["max_drawdown"]
                                                                for r in ok),
                   "trades": sum(r["metrics"]["trades"] for r in ok),
                   "signals": {r["symbol"]: r["signal"] for r in ok}}
    return {"strategy": spec, "summary": summary, "results": results, "at": time.time()}


def scan(specs, market):
    """Run every strategy now: per-symbol signals plus a backtest summary each."""
    templates = not specs
    specs = specs or [{**t, "symbols": ["BTC", "ETH", "SOL"], "id": f"tpl{i}"}
                      for i, t in enumerate(TEMPLATES)]
    from .markets import pool_map
    runs = pool_map(lambda spec: run(spec, market), specs, workers=3)
    signals = []
    for r in runs:
        for x in r["results"]:
            if "signal" in x:
                signals.append({"strategy": r["strategy"]["name"], "id": r["strategy"]["id"],
                                "symbol": x["symbol"], "signal": x["signal"],
                                "price": x["last"], "position": x["position"],
                                "return": x["metrics"]["total_return"],
                                "hold": x["metrics"]["buy_hold"]})
    counts = {k: sum(1 for s in signals if s["signal"] == k)
              for k in ("BUY", "SELL", "HOLD", "FLAT")}
    board = sorted(({"id": r["strategy"]["id"], "name": r["strategy"]["name"],
                     **(r["summary"] or {})} for r in runs if r["summary"]),
                   key=lambda r: -r["total_return"])
    return {"strategies": len(runs), "checks": len(signals), "counts": counts,
            "actionable": [s for s in signals if s["signal"] in ("BUY", "SELL")],
            "holding": [s for s in signals if s["signal"] == "HOLD"],
            "leaderboard": board, "templates": templates, "at": time.time()}
