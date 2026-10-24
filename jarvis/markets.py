"""Market recon through True Markets: read-only prices, history and the asset universe.

Recon only, by construction: this client signs in and issues GETs against an allowlist
of market-data paths. There is no order, transfer or wallet code here at all, so a
strategy can be researched and backtested but never place a trade.

Auth follows True Markets' API-key flow: sign `{key_id}.{timestamp}` with the key's
P-256 private key (ES256, raw r||s), POST it to /v1/auth/api-key/token, and use the
returned bearer token until it expires. ECDSA is implemented here (RFC 6979
deterministic nonces) so the feature needs no extra package on either laptop.

The key file (JSON with key_id + EC private JWK, downloaded from truemarkets.co →
Settings → API Keys) is copied once into .jarvis/ by `jarvis markets setup <file>`;
TM_KEY_FILE overrides it.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = "https://api.truemarkets.co"
KEY_NAME = "truemarkets-key.json"
READ_PATHS = ("/v1/defi/market/", "/v1/cefi/market/", "/v1/conductor/assets")
WINDOWS = ("1h", "4h", "1d", "7d", "30d", "90d", "180d", "365d")
SYMBOL = re.compile(r"[A-Za-z0-9._-]{1,24}")

# ---- ES256 (ECDSA P-256 / SHA-256) --------------------------------------------------------
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
A = P - 3
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % P == 0:
        return None
    if p == q:
        lam = (3 * p[0] * p[0] + A) * pow(2 * p[1], -1, P) % P
    else:
        lam = (q[1] - p[1]) * pow(q[0] - p[0], -1, P) % P
    x = (lam * lam - p[0] - q[0]) % P
    return x, (lam * (p[0] - x) - p[1]) % P


def _mul(k, point=G):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point, k = _add(point, point), k >> 1
    return out


def _nonce(d, h1):
    """RFC 6979 §3.2 deterministic k for P-256/SHA-256."""
    x, h = d.to_bytes(32, "big"), (int.from_bytes(h1, "big") % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def es256_sign(d, message):
    """Raw 64-byte r||s signature of `message` with private scalar `d`."""
    h1 = hashlib.sha256(message).digest()
    z = int.from_bytes(h1, "big")
    k = _nonce(d, h1)
    r = _mul(k)[0] % N
    s = pow(k, -1, N) * (z + r * d) % N
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_key(path):
    bundle = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    jwk = bundle.get("private_key") or {}
    if not bundle.get("key_id") or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256" \
            or "d" not in jwk:
        raise ValueError("Expected a True Markets key file: key_id + P-256 private JWK.")
    if bundle.get("algorithm", "ES256") != "ES256":
        raise ValueError("Only ES256 True Markets keys are supported.")
    return bundle["key_id"], int.from_bytes(b64u_decode(jwk["d"]), "big"), bundle.get("label")


# ---- client ------------------------------------------------------------------------------
class TrueMarkets:
    def __init__(self, state_dir, config=None):
        cfg = (config or {}).get("markets", {})
        self.state = Path(state_dir).resolve()
        self.base = os.environ.get("TM_BASE_URL") or cfg.get("base_url") or BASE
        self.key_path = Path(os.environ.get("TM_KEY_FILE") or self.state / KEY_NAME)
        self._token, self._expires = None, 0.0
        self._cache, self._lock = {}, threading.Lock()
        self.calls = 0

    # auth
    def configured(self):
        return self.key_path.is_file()

    def status(self):
        info = {"configured": self.configured(), "read_only": True, "base": self.base}
        if info["configured"]:
            try:
                key_id, _, label = load_key(self.key_path)
                info.update(key_id=key_id[:8] + "…", label=label)
            except (OSError, ValueError) as exc:
                info.update(configured=False, error=str(exc))
        return info

    def token(self):
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
        if not self.configured():
            raise ValueError("No True Markets key yet. Run: jarvis markets setup <key.json>")
        key_id, d, _ = load_key(self.key_path)
        ts = int(time.time())
        body = {"key_id": key_id, "timestamp": ts,
                "signature": b64u(es256_sign(d, f"{key_id}.{ts}".encode()))}
        data = self._http("POST", "/v1/auth/api-key/token", body=body)
        if not data.get("access_token"):
            raise RuntimeError("True Markets didn't return an access token.")
        with self._lock:
            self._token = data["access_token"]
            self._expires = _expiry(data.get("expires_in"))
            return self._token

    def _http(self, method, path, params=None, body=None, token=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Accept": "application/json", "User-Agent": "jarvis-recon/1"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(url, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read(20_000_000) or b"null")
        except urllib.error.HTTPError as exc:
            detail = exc.read(400).decode(errors="replace")
            exc.close()
            try:
                detail = json.loads(detail).get("message", detail)
            except ValueError:
                pass
            raise RuntimeError(f"True Markets HTTP {exc.code}: {detail}") from None

    def get(self, path, params=None, ttl=30):
        """Cached, authenticated GET on the read-only allowlist."""
        if not path.startswith(READ_PATHS):
            raise ValueError("Recon mode: only market-data endpoints are allowed.")
        key = (path, tuple(sorted((params or {}).items())))
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        try:
            data = self._http("GET", path, params, token=self.token())
        except RuntimeError as exc:
            if "HTTP 401" not in str(exc):
                raise
            with self._lock:
                self._token = None
            data = self._http("GET", path, params, token=self.token())
        with self._lock:
            self._cache[key] = (time.time(), data)
            if len(self._cache) > 500:
                for old in sorted(self._cache, key=lambda k: self._cache[k][0])[:100]:
                    del self._cache[old]
        return data

    # data
    def universe(self):
        out, seen = [], set()
        for a in self.get("/v1/conductor/assets", ttl=600).get("data", []):
            if not a.get("is_active") or a.get("symbol") in seen:
                continue
            seen.add(a["symbol"])
            md = a.get("market_data") or {}
            out.append({"symbol": a["symbol"], "name": a.get("name"), "chain": a.get("chain"),
                        "venue": a.get("venue"), "stable": bool(a.get("stable")),
                        "tradeable": bool(a.get("tradeable")), "website": a.get("website"),
                        "description": (a.get("description") or "").strip()[:1200],
                        "circulating_supply": md.get("circulating_supply"),
                        "total_supply": md.get("total_supply"),
                        "max_supply": md.get("max_supply"),
                        "x": (a.get("socials") or {}).get("x_username")})
        return out

    def caps(self, limit=40):
        rows = self.get("/v1/defi/market/market-data/market-caps",
                        {"limit": int(limit)}, ttl=120).get("assets", [])
        return [{"symbol": r["symbol"], "market_cap": _num(r.get("market_cap"))} for r in rows]

    def history(self, symbol, window="1d", resolution="1h"):
        _check(symbol, window)
        data = self.get("/v1/defi/market/prices/history",
                        {"symbol": symbol, "window": window, "resolution": resolution},
                        ttl=15 if window in ("1h", "4h") else 60)
        return [{"t": p["t"], "price": _num(p["price"])} for p in data.get("points", [])
                if _num(p.get("price")) is not None]

    def batch_history(self, symbols, window="1d", resolution="1h"):
        symbols = [s for s in symbols if SYMBOL.fullmatch(s)]
        out = {}
        for i in range(0, len(symbols), 25):
            chunk = symbols[i:i + 25]
            data = self.get("/v1/defi/market/prices/history/batch",
                            {"symbols": ",".join(chunk), "window": window,
                             "resolution": resolution}, ttl=60)
            for r in data.get("results", []):
                pts = [_num(p["price"]) for p in r.get("points") or []]
                out[r.get("symbol")] = [p for p in pts if p is not None]
        return out

    def candles(self, symbol):
        _check(symbol)
        data = self.get("/v1/defi/market/prices", {"symbol": symbol}, ttl=15)
        return {c["interval"]: {k: _num(c.get(f"{k}Price")) for k in ("open", "high", "low",
                                                                       "close")}
                for c in data.get("candles", [])}

    def quote(self, symbol):
        candles = self.candles(symbol)
        asset = next((a for a in self.universe() if a["symbol"] == symbol), {})
        cap = next((c["market_cap"] for c in self.caps(100) if c["symbol"] == symbol), None)
        last = next((c["close"] for c in candles.values() if c.get("close")), None)
        changes = {k: _pct(c["open"], c["close"]) for k, c in candles.items()}
        return {"symbol": symbol, "last": last, "candles": candles, "change": changes,
                "market_cap": cap, "asset": asset, "at": time.time()}

    def monitor(self, limit=40):
        caps = self.caps(limit)
        series = self.batch_history([c["symbol"] for c in caps], "1d", "1h")
        names = {a["symbol"]: a for a in self.universe()}
        rows = []
        for c in caps:
            pts = series.get(c["symbol"]) or []
            if not pts:
                continue
            asset = names.get(c["symbol"], {})
            rows.append({"symbol": c["symbol"], "name": asset.get("name") or c["symbol"],
                         "chain": asset.get("chain"), "stable": asset.get("stable", False),
                         "last": pts[-1], "chg_1h": _pct(pts[-2] if len(pts) > 1 else None,
                                                         pts[-1]),
                         "chg_24h": _pct(pts[0], pts[-1]), "high": max(pts), "low": min(pts),
                         "vol": _vol(pts), "market_cap": c["market_cap"], "spark": pts})
        return {"rows": rows, "at": time.time()}

    def intel(self, limit=60):
        rows = [r for r in self.monitor(limit)["rows"] if not r["stable"]]
        movers = sorted((r for r in rows if r["chg_24h"] is not None),
                        key=lambda r: r["chg_24h"])
        total = sum(r["market_cap"] or 0 for r in rows)
        btc = next((r["market_cap"] for r in rows if r["symbol"] == "BTC"), 0)
        up = sum(1 for r in rows if (r["chg_24h"] or 0) > 0)
        brief = lambda r: {k: r[k] for k in ("symbol", "name", "last", "chg_24h", "chg_1h",
                                             "vol", "market_cap")}
        weighted = (sum((r["chg_24h"] or 0) * (r["market_cap"] or 0) for r in rows) / total
                    if total else None)
        return {"gainers": [brief(r) for r in movers[::-1][:6]],
                "losers": [brief(r) for r in movers[:6]],
                "volatile": [brief(r) for r in sorted(rows, key=lambda r: -(r["vol"] or 0))[:6]],
                "breadth": {"up": up, "down": len(rows) - up, "n": len(rows)},
                "btc_dominance": round(100 * btc / total, 2) if total else None,
                "cap_weighted_24h": round(weighted, 3) if weighted is not None else None,
                "tracked_cap": total, "at": time.time()}


def _expiry(value):
    """expires_in arrives as an ISO timestamp (or seconds); default 15 minutes."""
    from datetime import datetime
    try:
        return time.time() + float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 900


def _check(symbol, window=None):
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("Unknown ticker.")
    if window is not None and window not in WINDOWS:
        raise ValueError(f"window must be one of {', '.join(WINDOWS)}.")


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _pct(start, end):
    if not start or end is None:
        return None
    return round((end / start - 1) * 100, 3)


def _vol(points):
    """Std-dev of bar returns, in percent."""
    rets = [b / a - 1 for a, b in zip(points, points[1:]) if a]
    return round(statistics.pstdev(rets) * 100, 3) if len(rets) > 2 else None


def setup(state_dir, source):
    """Copy a downloaded key file into .jarvis/ (owner-only) after checking it signs in."""
    src = Path(source).expanduser()
    load_key(src)
    dest = Path(state_dir).resolve() / KEY_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    from .credits import owner_only
    owner_only(dest)
    client = TrueMarkets(state_dir)
    client.token()
    return {**client.status(), "saved": str(dest),
            "assets": len(client.universe())}


def pool_map(fn, items, workers=6):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))
