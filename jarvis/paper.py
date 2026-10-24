"""Paper trading: run saved strategies on live True Markets prices with practice money.

One practice account in .jarvis/paper.json, $10,000 to start. Turning a strategy on
gives it a budget (default $1,000, split evenly over its coins). Every tick JARVIS
backtests the strategy on fresh history (strategy.backtest) and makes the paper
position match: in when the strategy is in or just signalled BUY, out when it's flat or
just signalled SELL. A strategy switched on mid-trend joins where it stands. Fills are
at the latest price less the strategy's fee. Manual paper orders ("paper buy $500 of
bitcoin") sit beside the bots. Nothing here can reach a real exchange: there is no
order code, only arithmetic on this file.

Voice: answer(text, ...) handles the common spoken commands without a model; anything
else about strategies goes to the brain, which has TradingTools.
"""

import json
import re
import threading
import time
from pathlib import Path

from .locking import exclusive_lock
from .strategy import TEMPLATES, Book, backtest, validate

START_CASH = 10_000.0
BUDGET = 1_000.0
TICK_SECONDS = 300
FEE_BPS = 10.0

COINS = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "ether": "ETH", "eth": "ETH",
         "solana": "SOL", "sol": "SOL", "dogecoin": "DOGE", "doge": "DOGE", "xrp": "XRP",
         "ripple": "XRP", "cardano": "ADA", "ada": "ADA", "avalanche": "AVAX",
         "avax": "AVAX", "chainlink": "LINK", "link": "LINK", "litecoin": "LTC",
         "ltc": "LTC", "polkadot": "DOT", "dot": "DOT", "bnb": "BNB", "binance coin": "BNB",
         "tron": "TRX", "trx": "TRX", "sui": "SUI", "pepe": "PEPE", "shiba": "SHIB",
         "shib": "SHIB", "toncoin": "TON", "ton": "TON", "near": "NEAR", "uniswap": "UNI",
         "uni": "UNI", "aave": "AAVE", "hyperliquid": "HYPE", "hype": "HYPE"}


def coins_in(text):
    """Tickers named in speech, in order: 'bitcoin and ETH' -> ['BTC', 'ETH']."""
    found = []
    for word in re.findall(r"[a-z0-9]+(?:\s+coin)?", text.lower()):
        sym = COINS.get(word) or COINS.get(word.replace(" coin", ""))
        if sym and sym not in found:
            found.append(sym)
    return found


def template(name):
    """A starter template by its plain title ('buy the dip') or its rule name."""
    key = re.sub(r"[^a-z ]", "", (name or "").lower()).strip()
    for t in TEMPLATES:
        if key and key in (t["title"].lower(), t["name"].lower()):
            return t
    for t in TEMPLATES:  # looser: "the dip one", "breakout"
        words = set(t["title"].lower().split()) | set(t["name"].lower().split())
        if key and set(key.split()) & (words - {"the", "big", "buy"}):
            return t
    return None


class Paper:
    """The practice account. Every change is read-modify-write under a file lock."""

    def __init__(self, state_dir, book=None):
        self.state_dir = Path(state_dir).resolve()
        self.path = self.state_dir / "paper.json"
        self.book = book or Book(self.state_dir)
        self._lock = threading.Lock()

    # ---- storage ---------------------------------------------------------------------
    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "cash" in data:
                return data
        except (OSError, ValueError):
            pass
        return {"start": START_CASH, "cash": START_CASH, "positions": [], "bots": {},
                "trades": [], "equity": [], "marks": {}, "ticked": 0, "created": time.time()}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _change(self, fn, wait=5.0):
        """Run fn(data) under the process and file locks, save, return its result."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        end = time.monotonic() + wait
        with self._lock:
            while True:
                try:
                    with exclusive_lock(self.state_dir / "paper.lock"):
                        data = self.load()
                        out = fn(data)
                        self._save(data)
                        return out
                except ValueError as exc:  # another process holds it
                    if "busy" not in str(exc) or time.monotonic() > end:
                        raise
                    time.sleep(0.1)

    # ---- the account -----------------------------------------------------------------
    @staticmethod
    def _value(data):
        marks = data.get("marks", {})
        held = sum(p["units"] * marks.get(p["symbol"], p["entry_px"]) for p in data["positions"])
        return data["cash"] + held

    def summary(self):
        data = self.load()
        marks, value = data.get("marks", {}), self._value(data)
        positions = []
        for p in data["positions"]:
            last = marks.get(p["symbol"], p["entry_px"])
            positions.append({**p, "last": last, "value": round(p["units"] * last, 2),
                              "pnl": round(p["units"] * (last - p["entry_px"]), 2),
                              "pnl_pct": round((last / p["entry_px"] - 1) * 100, 2)})
        bots = []
        names = {s["id"]: s for s in self.book.all()}
        for sid, bot in data["bots"].items():
            spec = names.get(sid)
            mine = [p for p in positions if p["key"] == sid]
            realized = sum(t.get("pnl", 0) for t in data["trades"]
                           if t["key"] == sid and t["side"] == "sell")
            bots.append({**bot, "id": sid, "name": spec["name"] if spec else bot.get("name"),
                         "symbols": spec["symbols"] if spec else [], "missing": spec is None,
                         "holding": [p["symbol"] for p in mine],
                         "pnl": round(realized + sum(p["pnl"] for p in mine), 2)})
        return {"start": data["start"], "cash": round(data["cash"], 2),
                "value": round(value, 2), "pnl": round(value - data["start"], 2),
                "pnl_pct": round((value / data["start"] - 1) * 100, 2),
                "positions": positions, "bots": bots, "trades": data["trades"][-40:][::-1],
                "equity": data["equity"][-300:], "ticked": data["ticked"],
                "running": sum(1 for b in bots if b.get("on")), "interval": TICK_SECONDS}

    def start(self, strategy_id, budget=BUDGET):
        spec = self.book.get(strategy_id)
        budget = float(budget or BUDGET)
        if not 10 <= budget <= 1_000_000:
            raise ValueError("Budget must be between $10 and $1,000,000.")

        def go(data):
            bot = data["bots"].get(spec["id"]) or {"since": time.time()}
            bot.update(name=spec["name"], budget=budget, on=True)
            data["bots"][spec["id"]] = bot
            return bot
        self._change(go)
        return spec

    def stop(self, strategy_id, sell=True, marks=None):
        """Switch a strategy off; by default sell what it holds at the last known price."""
        def go(data):
            bot = data["bots"].get(strategy_id)
            if not bot:
                raise LookupError("That strategy isn't paper trading.")
            bot["on"] = False
            sold = []
            if sell:
                for p in [p for p in data["positions"] if p["key"] == strategy_id]:
                    px = (marks or {}).get(p["symbol"]) or data["marks"].get(p["symbol"], p["entry_px"])
                    sold.append(self._sell(data, p, px, "strategy switched off"))
            return {"stopped": strategy_id, "sold": sold}
        return self._change(go)

    def reset(self, cash=START_CASH):
        def go(data):
            fresh = {"start": float(cash), "cash": float(cash), "positions": [],
                     "bots": {k: {**b, "on": False} for k, b in data["bots"].items()},
                     "trades": [], "equity": [], "marks": data.get("marks", {}),
                     "ticked": 0, "created": time.time()}
            data.clear()
            data.update(fresh)
            return fresh
        self._change(go)
        return self.summary()

    # ---- fills -----------------------------------------------------------------------
    @staticmethod
    def _buy(data, key, symbol, usd, px, why, fee_bps=FEE_BPS):
        usd = min(usd, data["cash"])
        if usd < 1 or not px:
            return None
        units = usd * (1 - fee_bps / 10_000) / px
        data["cash"] -= usd
        data["positions"].append({"key": key, "symbol": symbol, "units": units,
                                  "entry_px": px, "cost": usd, "at": time.time()})
        trade = {"key": key, "side": "buy", "symbol": symbol, "units": units, "price": px,
                 "usd": round(usd, 2), "why": why, "at": time.time()}
        data["trades"].append(trade)
        data["trades"] = data["trades"][-500:]
        return trade

    @staticmethod
    def _sell(data, position, px, why, fee_bps=FEE_BPS):
        proceeds = position["units"] * px * (1 - fee_bps / 10_000)
        data["cash"] += proceeds
        data["positions"].remove(position)
        trade = {"key": position["key"], "side": "sell", "symbol": position["symbol"],
                 "units": position["units"], "price": px, "usd": round(proceeds, 2),
                 "pnl": round(proceeds - position["cost"], 2),
                 "ret": round((proceeds / position["cost"] - 1) * 100, 2), "why": why,
                 "at": time.time()}
        data["trades"].append(trade)
        data["trades"] = data["trades"][-500:]
        return trade

    def order(self, symbol, side, usd=None, market=None, price=None):
        """A manual paper order at the live price. Sell with no amount sells it all."""
        symbol = (symbol or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9._-]{1,24}", symbol):
            raise ValueError("Name a coin, e.g. BTC.")
        if side not in ("buy", "sell"):
            raise ValueError("Side must be buy or sell.")
        px = price or _last_price(market, symbol)

        def go(data):
            data["marks"][symbol] = px
            out = fill(data)
            data["equity"].append([round(time.time()), round(self._value(data), 2)])
            return out

        def fill(data):
            if side == "buy":
                trade = self._buy(data, "manual", symbol, float(usd or BUDGET), px, "your order")
                if not trade:
                    raise ValueError(f"Not enough practice cash (${data['cash']:,.0f} left).")
                return trade
            held = [p for p in data["positions"] if p["key"] == "manual" and p["symbol"] == symbol]
            if not held:
                raise ValueError(f"You don't hold any {symbol} on paper (outside the strategies).")
            want = float(usd) if usd else None
            sold = []
            for p in held:
                if want is not None:
                    value = p["units"] * px
                    if want <= 0:
                        break
                    if want < value * 0.999:  # partial: split the lot
                        part = dict(p, units=p["units"] * want / value, cost=p["cost"] * want / value)
                        p["units"] -= part["units"]
                        p["cost"] -= part["cost"]
                        data["positions"].append(part)
                        sold.append(self._sell(data, part, px, "your order"))
                        break
                    want -= value
                sold.append(self._sell(data, p, px, "your order"))
            return {"side": "sell", "symbol": symbol, "price": px,
                    "usd": round(sum(t["usd"] for t in sold), 2),
                    "pnl": round(sum(t["pnl"] for t in sold), 2), "fills": sold}
        return self._change(go)

    # ---- the loop --------------------------------------------------------------------
    def tick(self, market, force=False):
        """Bring every running strategy's paper positions in line with its rules now."""
        data = self.load()
        bots = {k: b for k, b in data["bots"].items() if b.get("on")}
        if not force and time.time() - data.get("ticked", 0) < TICK_SECONDS * 0.9:
            return {"skipped": True, "trades": []}
        if not bots and not data["positions"]:
            return {"skipped": True, "trades": []}
        plans, marks, errors = {}, {}, []
        for sid, bot in bots.items():  # network outside the lock
            try:
                spec = self.book.get(sid)
            except LookupError:
                errors.append(f"{bot.get('name', sid)}: strategy was deleted")
                continue
            for symbol in spec["symbols"]:
                try:
                    pts = market.history(symbol, spec["window"], spec["resolution"])
                    r = backtest(spec, [p["price"] for p in pts], [p["t"] for p in pts])
                except Exception as exc:
                    errors.append(f"{spec['name']} {symbol}: {exc}")
                    continue
                marks[symbol] = r["last"]
                want = r["signal"] == "BUY" or (r["position"]["open"] and r["signal"] != "SELL")
                plans[(sid, symbol)] = (want, spec, r["signal"])
        for p in data["positions"]:  # mark what nobody re-priced (manual lots, stopped bots)
            if p["symbol"] not in marks:
                try:
                    marks[p["symbol"]] = _last_price(market, p["symbol"])
                except Exception:
                    pass

        def go(data):
            data["marks"].update(marks)
            done = []
            for (sid, symbol), (want, spec, signal) in plans.items():
                bot = data["bots"].get(sid)
                if not bot or not bot.get("on"):
                    continue
                held = [p for p in data["positions"] if p["key"] == sid and p["symbol"] == symbol]
                px = marks[symbol]
                if want and not held:
                    slot = bot["budget"] / max(1, len(spec["symbols"]))
                    why = "entry rule fired" if signal == "BUY" else "joined the strategy's open position"
                    t = self._buy(data, sid, symbol, slot, px, why, spec["fee_bps"])
                    if t:
                        done.append({**t, "strategy": spec["name"]})
                elif not want and held:
                    for p in held:
                        done.append({**self._sell(data, p, px, "exit rule or stop fired",
                                                  spec["fee_bps"]), "strategy": spec["name"]})
            data["ticked"] = time.time()
            data["equity"].append([round(data["ticked"]), round(self._value(data), 2)])
            data["equity"] = data["equity"][-2000:]
            return done
        trades = self._change(go)
        return {"skipped": False, "trades": trades, "errors": errors}


def _last_price(market, symbol):
    if market is None:
        raise ValueError("Live prices aren't available (True Markets isn't connected).")
    pts = market.history(symbol, "1d", "15m")
    if not pts:
        raise ValueError(f"No live price for {symbol}.")
    return pts[-1]["price"]


# ---- one background loop per process -----------------------------------------------------
_ticker = {"thread": None}
_ticker_lock = threading.Lock()


def ensure_ticker(state_dir, config, market=None):
    """Start the paper-trading loop in this process once (a daemon thread)."""
    with _ticker_lock:
        if _ticker["thread"] and _ticker["thread"].is_alive():
            return
        paper = Paper(state_dir)

        def loop():
            mk = market
            while True:
                try:
                    if mk is None:
                        from .markets import TrueMarkets
                        mk = TrueMarkets(state_dir, config)
                    if mk.configured():
                        result = paper.tick(mk)
                        if result.get("trades"):
                            _announce(state_dir, result["trades"])
                except Exception:
                    pass  # try again next round
                time.sleep(60)
        _ticker["thread"] = threading.Thread(target=loop, daemon=True, name="paper-ticker")
        _ticker["thread"].start()


def _announce(state_dir, trades):
    try:
        from .events import emit
        for t in trades:
            emit(state_dir, "paper.trade", **{k: t[k] for k in ("side", "symbol", "price", "usd",
                                                                "strategy", "why") if k in t})
    except Exception:
        pass


# ---- words -------------------------------------------------------------------------------
def money(v):
    return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"


def status_line(summary):
    s = summary
    move = "up" if s["pnl"] > 0.5 else "down" if s["pnl"] < -0.5 else "even"
    line = f"Your practice account is worth {money(s['value'])}"
    line += (f", {move} {money(abs(s['pnl']))} or {abs(s['pnl_pct']):.1f} percent"
             if move != "even" else ", about where it started")
    running = [b for b in s["bots"] if b.get("on")]
    if running:
        line += f". {len(running)} {'strategy is' if len(running) == 1 else 'strategies are'} running: "
        line += ", ".join(f"{b['name']}" + (f" holding {' and '.join(b['holding'])}" if b["holding"]
                                            else " waiting") for b in running[:3])
    else:
        line += ". No strategies are paper trading"
    if s["trades"]:
        t = s["trades"][0]
        line += (f". Last trade: {'bought' if t['side'] == 'buy' else 'sold'} {t['symbol']} "
                 f"at {money(t['price'])}")
    return line + "."


UNIT = {"1m": "minute", "5m": "5-minute", "15m": "15-minute", "1h": "hour", "4h": "4-hour",
        "1d": "day"}


def plain(rule, resolution="1d"):
    """'sma(20) crosses_above sma(50)' -> 'the 20-day average crosses above the 50-day average'."""
    unit = UNIT.get(resolution, "bar")
    names = {"sma": "the {n}-{u} average", "ema": "the {n}-{u} EMA", "rsi": "RSI",
             "roc": "the {n}-{u} change", "zscore": "the {n}-{u} z-score",
             "high": "the {n}-{u} high", "low": "the {n}-{u} low", "vol": "{n}-{u} volatility"}

    def term(x):
        m = re.fullmatch(r"(\w+)\((\d+)\)", x)
        return names[m.group(1)].format(n=m.group(2), u=unit) if m else x
    words = {">": "is above", "<": "is below", ">=": "is at least", "<=": "is at most",
             "crosses_above": "crosses above", "crosses_below": "crosses below"}
    parts = rule.split()
    return f"{term(parts[0])} {words.get(parts[1], parts[1])} {term(parts[2])}" if len(parts) == 3 else rule


# ---- spoken rules: the few shapes people say out loud -> the rule grammar -----------------
_N = r"(\d+(?:\.\d+)?)"
_ABOVE = r"(?:above|over|greater\s+than|more\s+than|higher\s+than|>)"
_BELOW = r"(?:below|under|less\s+than|lower\s+than|<)"
_AVG = r"(?:(?:simple\s+|exponential\s+)?moving\s+average|average|ma|sma|ema)"


def rule_from_speech(clause):
    """'rsi drops below 30' -> 'rsi(14) < 30'; None when it isn't one of the known shapes."""
    c = clause.lower().strip(" .,!?")
    c = re.sub(r"\b(?:the|a|an|is|goes|gets|drops|falls|rises|climbs|moves|it|its)\b", " ", c)
    c = re.sub(r"(\d+)[- ]?(?:day|bar|hour|period|candle)s?\b", r"\1", c)
    c = re.sub(r"\s+", " ", c).strip()
    kind = lambda word: "ema" if "exponential" in word or "ema" in word else "sma"
    m = re.search(rf"rsi(?:\s*\(?(\d+)\)?)?\s+(?:(crosses?)\s+)?({_ABOVE}|{_BELOW})\s+{_N}", c)
    if m:
        n, cross, d, v = m.group(1) or "14", m.group(2), m.group(3), m.group(4)
        up = re.match(_ABOVE, d) is not None
        op = ("crosses_above" if up else "crosses_below") if cross else (">" if up else "<")
        return f"rsi({n}) {op} {v}"
    m = re.search(rf"(\d+)\s*({_AVG})\s+(?:(crosses?)\s+)?({_ABOVE}|{_BELOW})\s+(\d+)\s*({_AVG})", c)
    if m:
        up = re.match(_ABOVE, m.group(4)) is not None
        op = ("crosses_above" if up else "crosses_below") if m.group(3) else (">" if up else "<")
        return f"{kind(m.group(2))}({m.group(1)}) {op} {kind(m.group(6))}({m.group(5)})"
    m = re.search(rf"price\s+(?:(crosses?)\s+)?({_ABOVE}|{_BELOW})\s+(\d+)\s*({_AVG})", c)
    if m:
        up = re.match(_ABOVE, m.group(2)) is not None
        op = ("crosses_above" if up else "crosses_below") if m.group(1) else (">" if up else "<")
        return f"price {op} {kind(m.group(4))}({m.group(3)})"
    m = re.search(r"(?:new\s+)?(\d+)\s+(high|low)\b", c)
    if m:
        return f"price {'>' if m.group(2) == 'high' else '<'} {m.group(2)}({m.group(1)})"
    m = re.search(rf"(?:price\s+)?(?:(?:is\s+)?(up|down)\s+(?:by\s+)?{_N}\s*(?:%|percent)"
                  rf"(?:\s+(?:in|over)\s+(?:last\s+)?(\d+))?)", c)
    if m:
        n = m.group(3) or "1"
        return f"roc({n}) {'>' if m.group(1) == 'up' else '<'} {'-' if m.group(1) == 'down' else ''}{m.group(2)}"
    return None


def _clauses(text):
    return [rule_from_speech(part) for part in re.split(r"\s+and\s+(?!sells?\b)", text) if part.strip()]


CREATE = re.compile(
    r"^(?:create|make|build|set\s+up|write|add|new)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:new\s+)?"
    r"(?:paper\s+|trading\s+|crypto\s+)*strategy(?:\s+(?:called|named)\s+(?P<name>.+?))?"
    r"\s+(?:that|which|to|where\s+it)\s+(?P<body>.+)$", re.I | re.S)
BUYS = re.compile(r"\bbuys?\s+(?P<coins>.*?)\s*\b(?:when|if|once|whenever)\s+(?P<entry>.+?)"
                  r"(?:,?\s+(?:and\s+(?:then\s+)?)?sells?\s+(?:it\s+|them\s+)?"
                  r"(?:when|if|once|whenever)\s+(?P<exit>.+?))?"
                  r"(?:,?\s+(?:with\s+)?(?:a\s+)?(?P<stop>\d+)\s*(?:%|percent)\s+stop(?:\s*loss)?)?"
                  r"(?:,?\s+and\s+(?P<go>(?:start\s+)?paper\s*trade\s+it|start\s+it|run\s+it|turn\s+it\s+on))?$",
                  re.I | re.S)


def strategy_from_speech(text):
    """A strategy spec from 'create a strategy that buys bitcoin when rsi is below 30 and
    sells when rsi is above 70', plus whether to start it. None when it can't be read."""
    m = CREATE.match(text.strip())
    if not m:
        return None
    body = BUYS.search(m.group("body"))
    if not body:
        return None
    entry = _clauses(body.group("entry"))
    exit_ = _clauses(body.group("exit") or "")
    if not entry or None in entry or None in exit_:
        return None
    coins = coins_in(body.group("coins")) or coins_in(text) or ["BTC"]
    hourly = re.search(r"\bhours?\b|\bhourly\b", text, re.I)
    name = (m.group("name") or "").strip(" .,'\"") or \
        f"{entry[0].split()[0].split('(')[0].upper()} idea · {' '.join(coins)}"
    spec = {"name": name[:60], "symbols": coins, "window": "30d" if hourly else "365d",
            "resolution": "1h" if hourly else "1d", "entry": entry, "exit": exit_,
            "stop_loss": float(body.group("stop") or 10), "take_profit": 0,
            "fee_bps": FEE_BPS, "notes": f"Made by voice: {text[:300]}"}
    return validate(spec), bool(body.group("go"))


# ---- voice ---------------------------------------------------------------------------------
_LEAD = re.compile(r"^\W*(?:hey\s+|ok(?:ay)?\s+)?(?:jarvis\W+)?(?:can\s+you\s+|could\s+you\s+|"
                   r"would\s+you\s+|please\s+|go\s+ahead\s+and\s+|i\s+want\s+(?:you\s+)?to\s+|"
                   r"let's\s+)*", re.I)
PAPER = r"(?:paper|practice|fake[- ]money|demo)"
STATUS = re.compile(rf"^(?:how(?:'s|\s+is|\s+are|\s+am\s+i\s+doing\s+(?:with|on|in))?|what(?:'s|\s+is)|"
                    rf"check(?:\s+on)?|show(?:\s+me)?|give\s+me|status\s+of|update\s+on)\s+"
                    rf"(?:on\s+)?(?:my\s+|the\s+)?(?:{PAPER}\s*(?:trad(?:e|es|ing)|account|portfolio|money|"
                    rf"positions?|p&l|pnl)|(?:trading\s+)?strateg(?:y|ies)|(?:my\s+)?bots?)"
                    rf"(?:\s+(?:doing|looking|status|update))?(?:\s+(?:today|now|so\s+far))?\W*$", re.I)
START = re.compile(rf"^(?:(?:start|begin|turn\s+on|switch\s+on|run|launch|deploy)\s+)?"
                   rf"{PAPER}\s*trad(?:e|ing)\s+(?:with\s+|using\s+)?(?:the\s+|my\s+)?(?P<what>.+?)"
                   rf"(?:\s+(?:strategy|template))?(?:\s+(?:on|with|for|using)\s+(?P<coins>[a-z0-9 ,]+?))?"
                   rf"(?:\s+with\s+(?:a\s+)?\$?(?P<budget>[\d,]+)\s*(?:dollars?|budget)?)?\W*$", re.I)
START2 = re.compile(r"^(?:start|turn\s+on|switch\s+on|run|launch|deploy)\s+(?:the\s+|my\s+)?(?P<what>.+?)"
                    r"(?:\s+strategy)?\s+(?:on|in|with)\s+paper(?:\s+trading)?"
                    r"(?:\s+(?:on|with|for)\s+(?P<coins>[a-z0-9 ,]+?))?\W*$", re.I)
STOP = re.compile(rf"^(?:stop|pause|end|halt|turn\s+off|switch\s+off|kill)\s+(?:all\s+(?:my\s+)?|"
                  rf"(?:the\s+|my\s+))?(?:{PAPER}\s*trad(?:e|es|ing|ers?)|strateg(?:y|ies)|bots?)"
                  rf"(?:\s+(?:with\s+|for\s+|on\s+)?(?:the\s+)?(?P<what>.+?))?(?:\s+strategy)?\W*$", re.I)
ORDER = re.compile(rf"^(?:(?:place\s+a\s+)?{PAPER}\s+(?:order\s+to\s+)?)?(?P<side>buy|sell)\s+"
                   rf"(?:(?P<all>all(?:\s+of)?\s+(?:my\s+)?)|\$?(?P<amt>[\d,]+(?:\.\d+)?)\s*"
                   rf"(?:k\b|dollars?|bucks|usd)?\s*(?:worth\s+)?(?:of\s+)?)?(?P<coin>[a-z]+(?:\s+coin)?)"
                   rf"(?P<tail>\s+(?:on\s+paper|with\s+(?:paper|practice|fake)\s+money|in\s+(?:my\s+)?"
                   rf"(?:paper|practice)\s+account|in\s+practice))?\W*$", re.I)
RESET = re.compile(rf"^(?:reset|restart|wipe|start\s+over)\s+(?:with\s+)?(?:my\s+|the\s+)?{PAPER}\s*"
                   rf"(?:trad(?:e|ing)\s+)?(?:account|portfolio|money|trading)?\W*$", re.I)


def answer(text, config, state, market=None, paper=None):
    """Spoken reply for a paper-trading command, having done it; None when `text` isn't one."""
    raw = text.replace("’", "'").strip()
    t = _LEAD.sub("", raw).strip()
    order = ORDER.match(t)
    if order and not (re.match(rf"{PAPER}", t, re.I) or order.group("tail")):
        order = None  # a plain "buy bitcoin" isn't a practice order; leave it alone
    hit = (STATUS.match(t) or START.match(t) or START2.match(t) or STOP.match(t) or RESET.match(t)
           or order or CREATE.match(t))
    if not hit:
        return None
    paper = paper or Paper(state)
    if market is None:
        try:
            from .markets import TrueMarkets
            market = TrueMarkets(state, config)
            if not market.configured():
                market = None
        except Exception:
            market = None
    try:
        return _do(t, paper, market, config, state, order)
    except (ValueError, LookupError) as exc:
        return f"I couldn't do that: {exc}"


def _find(paper, what):
    """A saved strategy by its id or (part of) its name, else None."""
    key = (what or "").lower().strip(" .")
    saved = paper.book.all()
    for s in saved:
        if key in (s["name"].lower(), s["id"]):
            return s
    for s in saved:
        if key and key in s["name"].lower():
            return s
    return None


def _do(t, paper, market, config, state, order):
    if STATUS.match(t):
        if market is not None:
            paper.tick(market)
        return status_line(paper.summary())
    if RESET.match(t):
        s = paper.reset()
        return f"Practice account reset to {money(s['value'])}. All strategies are switched off."
    if m := STOP.match(t):
        what = (m.group("what") or "").strip()
        summary = paper.summary()
        running = [b for b in summary["bots"] if b.get("on")]
        if not running:
            return "No strategies are paper trading right now."
        if not what or what.lower() in ("all", "everything", "them"):
            targets = running
        else:
            targets = [b for b in running if what.lower() in (b["name"] or "").lower()]
            if not targets:
                return (f"No running strategy matches {what}. Running: "
                        f"{', '.join(b['name'] for b in running)}.")
        sold = 0
        for b in targets:
            sold += len(paper.stop(b["id"])["sold"])
        names = ", ".join(b["name"] for b in targets)
        return f"Stopped {names}." + (f" Sold {sold} paper position{'s' * (sold != 1)}." if sold else "")
    if order:
        if market is None:
            return "I can't price that: True Markets isn't connected."
        coin = COINS.get(order.group("coin").lower().replace(" coin", "")) or order.group("coin").upper()
        amt = order.group("amt")
        usd = float(amt.replace(",", "")) if amt else None
        if usd is not None and re.search(rf"{re.escape(amt)}\s*k\b", t, re.I):
            usd *= 1000
        result = paper.order(coin, order.group("side").lower(), usd, market)
        _ensure(state, config)
        if order.group("side").lower() == "buy":
            return (f"Paper bought {money(result['usd'])} of {coin} at {money(result['price'])}. "
                    f"Practice money only.")
        verb = "gain" if result["pnl"] >= 0 else "loss"
        return f"Paper sold {money(result['usd'])} of {coin}, a {verb} of {money(abs(result['pnl']))}."
    if CREATE.match(t):
        made = strategy_from_speech(t)
        if made is None:
            return None  # the brain's tools can read freer descriptions
        spec, go = made
        spec = paper.book.save(spec)
        say = lambda rules, join: f" {join} ".join(plain(r, spec["resolution"]) for r in rules)
        line = (f"Saved {spec['name']}: buy {' and '.join(spec['symbols'])} when "
                f"{say(spec['entry'], 'and')}" +
                (f", sell when {say(spec['exit'], 'or')}" if spec["exit"] else "") +
                f", {spec['stop_loss']:.0f} percent stop.")
        if go:
            paper.start(spec["id"])
            _ensure(state, config)
            if market is not None:
                paper.tick(market, force=True)
            line += " It's paper trading now with $1,000."
        else:
            line += " Say paper trade it to run it with practice money."
        return line
    m = START.match(t) or START2.match(t)
    what = (m.group("what") or "").strip()
    coins = coins_in(m.group("coins") or "")
    if what.lower() in ("it", "that", "this", "this one", "that one", "that strategy"):
        saved = sorted(paper.book.all(), key=lambda s: -s.get("updated", 0))
        if not saved:
            return "There's no saved strategy yet. Make one first."
        spec = saved[0]
    else:
        spec = _find(paper, what)
        if spec is None:
            tpl = template(what)
            if tpl is None:
                names = ", ".join(t_["title"].lower() for t_ in TEMPLATES)
                saved = ", ".join(s["name"] for s in paper.book.all()[:5])
                return (f"I don't have a strategy called {what}. Starter ideas: {names}." +
                        (f" Saved: {saved}." if saved else ""))
            coins = coins or coins_in(what) or ["BTC", "ETH", "SOL"]
            spec = paper.book.save({**{k: v for k, v in tpl.items() if k not in ("kind", "about", "title")},
                                    "name": f"{tpl['title']} · {' '.join(coins)}", "symbols": coins,
                                    "notes": f"From the {tpl['name']} template, by voice."})
    budget = float(m.group("budget").replace(",", "")) if m.groupdict().get("budget") else BUDGET
    paper.start(spec["id"], budget)
    _ensure(state, config)
    done = []
    if market is not None:
        done = paper.tick(market, force=True)["trades"]
    line = f"Paper trading {spec['name']} with {money(budget)} of practice money."
    bought = [x for x in done if x["key"] == spec["id"] and x["side"] == "buy"]
    if bought:
        line += " It bought " + " and ".join(f"{x['symbol']} at {money(x['price'])}" for x in bought) + " right away."
    else:
        line += " Nothing to buy yet; it'll act when the rules fire."
    return line


def _ensure(state, config):
    try:
        ensure_ticker(state, config)
    except Exception:
        pass


# ---- tools for the brain (ChatGPT dynamic tools) ------------------------------------------
TRADING_INSTRUCTIONS = (
    "Paper trading tools (practice money only, no real orders exist): trading_status, "
    "list_strategies, create_strategy, paper_start, paper_stop, paper_order. To make a "
    "strategy from David's words, translate them into rules of the form TERM OP TERM, "
    "where TERM is a number, price, or sma(n) ema(n) rsi(n) roc(n) zscore(n) high(n) low(n) "
    "vol(n) and OP is > < >= <= crosses_above crosses_below; e.g. 'rsi(14) < 30', "
    "'sma(20) crosses_above sma(50)', 'price > high(20)'. Entry rules must all hold; any "
    "exit rule closes. Then say what you made in one sentence and its backtest return.")


class TradingTools:
    def __init__(self, config, state, market=None):
        self.config, self.state = config, Path(state)
        self.paper = Paper(self.state)
        self._market = market

    def market(self):
        if self._market is None:
            from .markets import TrueMarkets
            self._market = TrueMarkets(self.state, self.config)
        return self._market

    def specs(self):
        obj = lambda props, req=(): {"type": "object", "additionalProperties": False,
                                     "required": list(req), "properties": props}
        name = {"type": "string", "description": "strategy name or id"}
        return [
            {"type": "function", "name": "trading_status",
             "description": "The paper (practice) account: value, P&L, positions, running strategies, recent trades.",
             "inputSchema": obj({})},
            {"type": "function", "name": "list_strategies",
             "description": "Saved strategies with their rules, and the starter templates.",
             "inputSchema": obj({})},
            {"type": "function", "name": "create_strategy",
             "description": "Save a trading strategy (rules, see instructions), backtest it, and "
                            "optionally start paper trading it right away.",
             "inputSchema": obj({
                 "name": {"type": "string"},
                 "symbols": {"type": "array", "items": {"type": "string"},
                             "description": "tickers, e.g. BTC, ETH, SOL"},
                 "entry": {"type": "array", "items": {"type": "string"}},
                 "exit": {"type": "array", "items": {"type": "string"}},
                 "stop_loss": {"type": "number", "description": "percent, 0 for none"},
                 "resolution": {"type": "string", "enum": ["1h", "4h", "1d"]},
                 "paper_trade": {"type": "boolean"}}, ("name", "symbols", "entry"))},
            {"type": "function", "name": "paper_start",
             "description": "Start paper trading a saved strategy, or a starter template by its "
                            "title (Ride big trends, Follow momentum, Buy the dip, Quick snapbacks, "
                            "Catch breakouts) on the given coins.",
             "inputSchema": obj({"strategy": name, "symbols": {"type": "array", "items": {"type": "string"}},
                                 "budget": {"type": "number"}}, ("strategy",))},
            {"type": "function", "name": "paper_stop",
             "description": "Stop paper trading a strategy (or 'all') and sell what it holds.",
             "inputSchema": obj({"strategy": name}, ("strategy",))},
            {"type": "function", "name": "paper_order",
             "description": "A manual practice order at the live price. Sell without usd sells it all.",
             "inputSchema": obj({"symbol": {"type": "string"},
                                 "side": {"type": "string", "enum": ["buy", "sell"]},
                                 "usd": {"type": "number"}}, ("symbol", "side"))},
        ]

    def call(self, name, args):
        try:
            return True, json.dumps(self._call(name, args or {}), default=str)[:6000]
        except Exception as exc:
            return False, str(exc)

    def _resolve(self, what, symbols=None):
        spec = _find(self.paper, what)
        if spec is None:
            tpl = template(what)
            if tpl is None:
                raise LookupError(f"No strategy or template called {what!r}.")
            coins = [c.upper() for c in symbols or []] or ["BTC", "ETH", "SOL"]
            spec = self.paper.book.save({**{k: v for k, v in tpl.items()
                                            if k not in ("kind", "about", "title")},
                                         "name": f"{tpl['title']} · {' '.join(coins)}",
                                         "symbols": coins})
        return spec

    def _call(self, name, a):
        if name == "trading_status":
            if self.market().configured():
                self.paper.tick(self.market())
            s = self.paper.summary()
            return {"spoken": status_line(s), **{k: s[k] for k in ("value", "cash", "pnl", "pnl_pct",
                                                                   "positions", "bots")},
                    "trades": s["trades"][:10]}
        if name == "list_strategies":
            return {"saved": self.paper.book.all(),
                    "templates": [{"title": t["title"], "entry": t["entry"], "exit": t["exit"]}
                                  for t in TEMPLATES]}
        if name == "create_strategy":
            res = a.get("resolution") or "1d"
            spec = self.paper.book.save({
                "name": a["name"], "symbols": a["symbols"], "entry": a["entry"],
                "exit": a.get("exit") or [], "stop_loss": a.get("stop_loss", 10),
                "resolution": res, "window": {"1h": "30d", "4h": "90d"}.get(res, "365d"),
                "take_profit": 0, "fee_bps": FEE_BPS})
            out = {"saved": spec}
            try:
                from .strategy import run
                out["backtest"] = run(spec, self.market())["summary"]
            except Exception as exc:
                out["backtest_error"] = str(exc)
            if a.get("paper_trade"):
                self.paper.start(spec["id"])
                _ensure(self.state, self.config)
                out["paper"] = self.paper.tick(self.market(), force=True)
            return out
        if name == "paper_start":
            spec = self._resolve(a["strategy"], a.get("symbols"))
            self.paper.start(spec["id"], a.get("budget") or BUDGET)
            _ensure(self.state, self.config)
            return {"started": spec["name"], "tick": self.paper.tick(self.market(), force=True)}
        if name == "paper_stop":
            what = a["strategy"]
            running = [b for b in self.paper.summary()["bots"] if b.get("on")]
            hits = running if what.lower() == "all" else \
                [b for b in running if what.lower() in (b["name"] or "").lower() or b["id"] == what]
            if not hits:
                raise LookupError("No running strategy matches that.")
            return [self.paper.stop(b["id"]) for b in hits]
        if name == "paper_order":
            return self.paper.order(a["symbol"], a["side"], a.get("usd"), self.market())
        raise LookupError(f"Unknown tool {name}.")


TRADING = re.compile(rf"\b(?:{PAPER}\s*(?:trad\w*|account|portfolio|money|order)|strateg(?:y|ies)|backtest)\b",
                     re.I)


def is_trading_request(text):
    return bool(TRADING.search(text or ""))
