"""Provider credit and usage readings for the console.

Every reading says where it came from, because the sources differ in kind:

- live: the provider's own balance/quota endpoint, called with your key.
- local: read from logs the tool already writes on this laptop (Codex records its
  rate-limit windows; Claude Code records token usage per message).
- manual: a figure you entered, with the date you entered it. Used for providers
  that publish no balance endpoint.
- none: nothing configured yet; the reading says what to set.

Keys come from the environment first, then .jarvis/secrets.json. They are sent only
to the provider they belong to. Nothing here estimates or invents a balance.
"""

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .claude_auth import renew_claude_session
from .fsio import atomic_write

try:
    HOME = Path.home()
except RuntimeError:
    # Service or isolated test environments may have no home variables. Keep
    # credential lookup unavailable without breaking unrelated credit helpers.
    HOME = Path(__file__).resolve().parent / "__home_unavailable__"
TIMEOUT = 8

# id, display name, group, key names (first found wins), short description
PROVIDERS = (
    ("claude", "Claude", "Coding agents", (), "Claude subscription usage windows"),
    ("codex", "Codex", "Coding agents", (), "OpenAI Codex CLI plan windows"),
    ("deepseek", "DeepSeek", "Model APIs", ("DEEPSEEK_API_KEY",), "API balance"),
    ("glm", "GLM", "Model APIs", ("GLM_API_KEY", "ZHIPUAI_API_KEY"), "Zhipu BigModel"),
    ("zai", "Z.AI", "Model APIs", ("ZAI_API_KEY",), "Z.AI platform / coding plan"),
    ("xai", "Grok", "Model APIs", ("XAI_API_KEY",), "xAI API / Grokbot"),
    ("openrouter", "OpenRouter", "Model APIs", ("OPENROUTER_API_KEY",), "Prepaid credits"),
    ("elevenlabs", "ElevenLabs", "Voice", ("ELEVENLABS_API_KEY",), "Character quota"),
    ("muse", "Muse", "Personal agents", (), "Zen browser agent"),
)


# Where each provider's own numbers live, and how JARVIS gets them.
SETUP = {
    "deepseek": ("key", "https://platform.deepseek.com/usage"),
    "openrouter": ("key", "https://openrouter.ai/settings/credits"),
    "elevenlabs": ("key", "https://elevenlabs.io/app/settings/api-keys"),
    "glm": ("manual", "https://open.bigmodel.cn"),
    "zai": ("manual", "https://z.ai"),
    "xai": ("manual", "https://console.x.ai"),
    "muse": ("manual", None),
    "codex": ("local", "https://chatgpt.com/codex/settings/usage"),
    "claude": ("local", "https://claude.ai/settings/usage"),
}


def _now():
    return datetime.now(timezone.utc)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib forwards Authorization and key headers on redirect; only follow to the same origin.
        old, new = urllib.parse.urlparse(req.full_url), urllib.parse.urlparse(newurl)
        if (old.scheme, old.netloc) != (new.scheme, new.netloc):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SameOriginRedirect())


def _get_json(url, headers):
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "jarvis-console", **headers})
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:
            return json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as exc:
        exc.close()  # the error object holds the response; drop its socket now
        raise RuntimeError(f"HTTP {exc.code} from {urllib.parse.urlparse(url).netloc}"
                           + (" — check the key" if exc.code in (401, 403) else "")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Couldn't reach {urllib.parse.urlparse(url).netloc}: "
                           f"{getattr(exc, 'reason', exc)}") from None


def _meter(label, used, limit, resets_at=None, unit=""):
    pct = None
    if limit:
        pct = max(0.0, min(100.0, 100.0 * used / limit))
    return {"label": label, "used": used, "limit": limit, "pct": pct, "unit": unit,
            "resets_at": resets_at}


class _NoUsageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Subscription credentials must never be forwarded to a redirect target.
        return None


class UsageEndpointError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Usage endpoint returned HTTP {status}")


def _subscription_json(url, headers):
    """Read a fixed first-party endpoint without redirects, retries or auth refresh."""
    request = urllib.request.Request(url, headers={"Accept": "application/json",
        "User-Agent": "jarvis-console", **headers})
    try:
        with urllib.request.build_opener(_NoUsageRedirect()).open(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read(2_000_000))
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return data
    except urllib.error.HTTPError as exc:
        exc.close()
        raise UsageEndpointError(exc.code) from None
    except Exception:
        # Exception text and response bodies can contain credentials; never surface them.
        raise RuntimeError("Usage endpoint unavailable or returned invalid data") from None


def _percentage(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 100 else None


def _object(value):
    return value if isinstance(value, dict) else {}


def _reset_iso(value):
    try:
        if isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _iso(value)
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _window_label(minutes, fallback):
    if minutes == 10080:
        return "Weekly window"
    if isinstance(minutes, (int, float)) and minutes > 0:
        return f"{minutes / 60:g}-hour window" if minutes % 60 == 0 else f"{minutes:g}-minute window"
    return fallback


def owner_only(path):
    """Keys live in this file; keep it readable only by its owner."""
    try:
        os.chmod(path, 0o600)
    except OSError:  # a filesystem without POSIX modes (FAT, some network shares)
        pass


def write_private(path, text):
    """Write a secrets file atomically, created 0600 so the key is never world-readable."""
    # owner_only: in case the umask or filesystem ignored the mode.
    atomic_write(path, text, mode=0o600, prepare=owner_only)


def _credential_path(provider):
    if provider == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / ".credentials.json"
    return Path(os.environ.get("CODEX_HOME") or HOME / ".codex") / "auth.json"


def _credential_signature(provider):
    path = _credential_path(provider)
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino
    except OSError:
        return str(path), None


def _reset_passed(value):
    stamp = _reset_iso(value)
    return bool(stamp and datetime.fromisoformat(stamp) <= _now())


def _windows_expired(data, provider, checked_at):
    if not data:
        return False
    def crossed(reset):
        stamp = _reset_iso(reset)
        return bool(stamp and checked_at and
                    datetime.fromisoformat(checked_at) < datetime.fromisoformat(stamp) <= _now())
    if provider == "claude":
        return any(crossed(_object(data.get(key)).get("resets_at"))
                   for key in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"))
    limits = _object(data.get("rate_limit"))
    return any(crossed(_object(limits.get(key)).get("reset_at"))
               for key in ("primary_window", "secondary_window"))


class Credits:
    def __init__(self, state_dir, config=None):
        self.state = Path(state_dir).resolve()
        self.config = config or {}
        self.manual_path = self.state / "credits-manual.json"
        self._cache, self._lock = {}, threading.Lock()
        self._claude_files = {}  # path -> (mtime, size, [(ts, tokens_in, tokens_out)])
        self._claude_log_lock = threading.RLock()
        self._usage_cache, self._usage_lock = {}, threading.Lock()
        self._usage_checked = {}

    def _subscription(self, provider):
        """Read quota; let Claude's CLI renew its own login, never rewrite credentials here."""
        with self._usage_lock:
            signature = _credential_signature(provider)
            hit = self._usage_cache.get(provider)
            if (hit and hit[3] == signature and time.monotonic() - hit[0] < 60
                    and not _windows_expired(hit[1], provider, self._usage_checked.get(provider))):
                return hit[1], hit[2]
            data, error = None, None
            try:
                if provider == "claude":
                    path = _credential_path(provider)
                    if not path.is_file():
                        self._usage_cache.pop(provider, None)
                        return None, None
                    auth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
                    expires = auth.get("expiresAt")
                    renewed = False
                    if isinstance(expires, (int, float)) and expires <= time.time() * 1000:
                        # The CLI may rewrite credentials even when renewal fails; that
                        # change is ours, not a concurrent sign-in, so track it either way.
                        try:
                            auth = self._renew_claude(path, auth)
                        finally:
                            signature = _credential_signature(provider)
                        renewed = True
                    token = auth.get("accessToken")
                    if token:
                        try:
                            data = self._claude_usage(token)
                        except UsageEndpointError as exc:
                            if exc.status != 401 or renewed:
                                raise
                            # A token can be rejected before its saved expiry. Permit
                            # one CLI renewal, but never loop on a revoked sign-in.
                            try:
                                auth = self._renew_claude(path, auth)
                            finally:
                                signature = _credential_signature(provider)
                            if auth.get("accessToken") == token:
                                raise RuntimeError("Claude sign-in was rejected; run claude auth login") from None
                            data = self._claude_usage(auth["accessToken"])
                        data = {**data, "_plan": auth.get("subscriptionType")}
                else:
                    path = _credential_path(provider)
                    if not path.is_file():
                        self._usage_cache.pop(provider, None)
                        return None, None
                    auth = json.loads(path.read_text(encoding="utf-8"))
                    tokens = auth.get("tokens") or {}
                    token = tokens.get("access_token")
                    if auth.get("auth_mode") in (None, "chatgpt") and token:
                        headers = {"Authorization": f"Bearer {token}"}
                        if tokens.get("account_id"):
                            headers["ChatGPT-Account-Id"] = tokens["account_id"]
                        data = _subscription_json("https://chatgpt.com/backend-api/wham/usage", headers)
            except RuntimeError as exc:
                # Only the deliberately sanitized errors above are user facing.
                error = str(exc)
            except Exception:
                error = "Existing sign-in data could not be read"
            if signature != _credential_signature(provider):
                data, error = None, "Sign-in changed during the quota check; check again"
            self._usage_checked[provider] = _now().isoformat()
            self._usage_cache[provider] = (time.monotonic(), data, error,
                                           signature)
            return data, error

    @staticmethod
    def _claude_usage(token):
        return _subscription_json("https://api.anthropic.com/api/oauth/usage",
            {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})

    @staticmethod
    def _renew_claude(path, auth):
        if not auth.get("refreshToken"):
            raise RuntimeError("Claude sign-in expired or rejected; run claude auth login")
        renew_claude_session()
        # CLI exit success alone does not establish that a usable login exists.
        fresh = _object(json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth"))
        expires = fresh.get("expiresAt")
        if (not fresh.get("accessToken") or not isinstance(expires, (int, float))
                or not math.isfinite(expires) or expires <= time.time() * 1000):
            raise RuntimeError("Claude sign-in could not be renewed; run claude auth login")
        return fresh

    def _quota(self, reading, windows, plan=None, credits=None):
        for label, used, reset in windows:
            used = _percentage(used)
            if used is not None and not _reset_passed(reset):
                reading["meters"].append(_meter(label, used, 100, _reset_iso(reset), "%"))
        if not reading["meters"]:
            return False
        reading.update(source="live", status="ok")
        first = reading["meters"][0]
        reading["headline"] = {"value": round(100 - first["used"], 1), "unit": "%",
                               "label": f"left in {first['label'].lower()}"}
        if plan:
            reading["facts"].append(["Plan", str(plan)])
        if isinstance(credits, dict):
            if credits.get("unlimited"):
                reading["facts"].append(["Credits", "unlimited"])
            elif credits.get("balance") is not None:
                reading["facts"].append(["Credits", str(credits["balance"])])
        return True

    # ---- keys and manual figures -------------------------------------------------
    def key(self, names):
        for name in names:
            if os.environ.get(name):
                return os.environ[name]
        try:
            secrets = json.loads((self.state / "secrets.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return next((secrets[n] for n in names if secrets.get(n)), None)

    def manual(self):
        try:
            return json.loads(self.manual_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def set_manual(self, provider, remaining, total=None, unit="USD", note=""):
        if provider not in {p[0] for p in PROVIDERS}:
            raise ValueError(f"Unknown provider: {provider}")
        data = self.manual()
        if remaining in (None, ""):
            data.pop(provider, None)
        else:
            data[provider] = {"remaining": float(remaining),
                              "total": float(total) if total not in (None, "") else None,
                              "unit": str(unit)[:12] or "USD", "note": str(note)[:200],
                              "updated_at": _now().isoformat()}
        self.manual_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        with self._lock:
            self._cache.pop(provider, None)
        return data.get(provider)

    # ---- collection --------------------------------------------------------------
    def all(self, refresh=False, max_age=120):
        out = []
        for pid, name, group, keys, blurb in PROVIDERS:
            signature = _credential_signature(pid) if pid in ("claude", "codex") else None
            with self._lock:
                hit = self._cache.get(pid)
            if (hit and not refresh and time.monotonic() - hit[0] < max_age
                    and hit[2] == signature
                    and not any(_reset_passed(m.get("resets_at")) for m in hit[1]["meters"])):
                out.append(hit[1])
                continue
            how, dashboard = SETUP.get(pid, ("manual", None))
            reading = {"id": pid, "name": name, "group": group, "blurb": blurb,
                       "source": "none", "status": "unconfigured", "headline": None,
                       "meters": [], "facts": [], "note": "", "checked_at": _now().isoformat(),
                       "setup": {"how": how, "keys": list(keys), "dashboard": dashboard,
                                 "secrets_file": str(self.state / "secrets.json")}}
            try:
                getattr(self, f"_{pid}")(reading, keys)
            except Exception as exc:  # one provider's odd reply must not blank the panel
                reading.update(status="error", note=str(exc) or type(exc).__name__)
            self._apply_manual(reading)
            with self._lock:
                usage = self._usage_cache.get(pid)
                self._cache[pid] = (time.monotonic(), reading,
                                    usage[3] if usage else signature)
            out.append(reading)
        return out

    def _apply_manual(self, reading):
        entry = self.manual().get(reading["id"])
        if not entry or (reading["source"] == "live" and reading["status"] == "ok"):
            return
        reading.update(source="manual", status="ok")
        reading["headline"] = {"value": entry["remaining"], "unit": entry["unit"],
                               "label": "remaining (entered)"}
        if entry.get("total"):
            reading["meters"].insert(0, _meter("Spent of entered total",
                                               entry["total"] - entry["remaining"],
                                               entry["total"], unit=entry["unit"]))
        reading["facts"].append(["Entered", entry["updated_at"][:10]])
        if entry.get("note"):
            reading["facts"].append(["Note", entry["note"]])

    def _keyed(self, reading, keys, hint):
        key = self.key(keys)
        reading["facts"].append(["Key", "set" if key else f"not set ({keys[0]})"])
        if not key:
            reading["note"] = hint
        return key

    # Codex writes `rate_limits` into each session's token_count events.
    def _codex(self, reading, _keys):
        data, error = self._subscription("codex")
        if data:
            limits = _object(data.get("rate_limit"))
            windows = []
            for key, fallback in (("primary_window", "Primary window"),
                                  ("secondary_window", "Secondary window")):
                window = _object(limits.get(key))
                seconds = window.get("limit_window_seconds")
                minutes = seconds / 60 if isinstance(seconds, (int, float)) else None
                windows.append((_window_label(minutes, fallback), window.get("used_percent"),
                                window.get("reset_at")))
            if self._quota(reading, windows, data.get("plan_type"), data.get("credits")):
                reading["checked_at"] = self._usage_checked.get("codex", _now().isoformat())
                reading["note"] = "Provider-reported subscription windows; credits are not USD."
                return
            error = "Usage endpoint returned no supported quota windows"
        self._codex_local(reading, _keys)
        if error:
            reading["note"] = f"{error}. {reading.get('note', '')}"

    def _codex_local(self, reading, _keys):
        root = _credential_path("codex").parent / "sessions"
        files = []
        if root.is_dir():
            for path in root.rglob("*.jsonl"):
                try:
                    files.append((path.stat().st_mtime, path))
                except OSError:
                    continue
        files = [path for _, path in sorted(files, reverse=True)]
        for path in files[:6]:
            limits = codex_rate_limits(path)
            if limits:
                break
        else:
            reading["note"] = "No Codex session logs with rate limits in ~/.codex/sessions."
            return
        reading.update(source="local", status="ok")
        for key, label in (("primary", "5-hour window"), ("secondary", "Weekly window")):
            window = _object(limits.get(key))
            if window.get("used_percent") is not None:
                minutes = window.get("window_minutes")
                label = _window_label(minutes, label)
                resets = window.get("resets_at")
                used = _percentage(window["used_percent"])
                if used is None:
                    continue
                if _reset_passed(resets):
                    # Other devices may already have consumed the new window. Do not
                    # fabricate a refill, or use a null meter (the UI renders it as 0).
                    reading["facts"].append([f"Last {label.lower()}",
                                              f"{used:g}% used; reset passed, current usage unknown"])
                    continue
                meter = _meter(label, used, 100, _reset_iso(resets), "%")
                reading["meters"].append(meter)
        if reading["meters"]:
            first = reading["meters"][0]
            reading["headline"] = {"value": round(100 - first["used"]), "unit": "%",
                                   "label": f"left in {first['label'].lower()}"}
        credits = _object(limits.get("credits"))
        if credits:
            reading["facts"].append(["Credits", "unlimited" if credits.get("unlimited") else
                                     credits.get("balance", "0")])
        reading["facts"].append(["Logged", _iso(limits.get("_logged_at"))])
        reading["note"] = "Read from the newest Codex session log; updates when Codex runs."

    def _claude(self, reading, _keys):
        data, error = self._subscription("claude")
        if data:
            windows = []
            for key, label in (("five_hour", "5-hour window"), ("seven_day", "Weekly window"),
                               ("seven_day_sonnet", "Sonnet weekly window"),
                               ("seven_day_opus", "Opus weekly window")):
                window = _object(data.get(key))
                windows.append((label, window.get("utilization"), window.get("resets_at")))
            if self._quota(reading, windows, data.get("_plan")):
                reading["checked_at"] = self._usage_checked.get("claude", _now().isoformat())
                extra = _object(data.get("extra_usage"))
                if isinstance(extra.get("is_enabled"), bool):
                    reading["facts"].append(["Extra usage", "enabled" if extra["is_enabled"] else "disabled"])
                reading["note"] = ("Provider-reported subscription usage across Claude surfaces. "
                                   "Percentages are plan quotas, not token counts or API credit balances.")
                return
            error = "Usage endpoint returned no supported quota windows"
        self._claude_local(reading, _keys)
        if error:
            reading["note"] = f"{error}. {reading.get('note', '')}"

    def _claude_local(self, reading, _keys):
        with self._claude_log_lock:
            self._claude_local_unlocked(reading, _keys)

    def _claude_local_unlocked(self, reading, _keys):
        root = _credential_path("claude").parent / "projects"
        if not root.is_dir():
            self._claude_files.clear()
            reading["note"] = "No Claude Code logs in ~/.claude/projects."
            return
        cutoff = time.time() - 7 * 86400
        rows = []
        active = set()
        for path in root.rglob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                continue
            active.add(path)
            cached = self._claude_files.get(path)
            if not cached or cached[:2] != (stat.st_mtime, stat.st_size):
                cached = (stat.st_mtime, stat.st_size, claude_usage_rows(path))
                self._claude_files[path] = cached
            rows.extend(cached[2])
        self._claude_files = {p: v for p, v in self._claude_files.items() if p in active}
        now = time.time()
        seen, five, week, msgs5 = set(), [0, 0], [0, 0], 0
        for message_id, ts, tin, tout in rows:
            if message_id in seen:
                continue
            seen.add(message_id)
            if 0 <= now - ts <= 7 * 86400:
                week[0] += tin
                week[1] += tout
            if 0 <= now - ts <= 5 * 3600:
                five[0] += tin
                five[1] += tout
                msgs5 += 1
        reading.update(source="local", status="ok")
        reading["headline"] = {"value": five[1], "unit": "tokens", "label": "output, last 5 h"}
        reading["facts"] += [["Output · 7 d", f"{week[1]:,}"], ["Context read · 5 h", f"{five[0]:,}"],
                             ["Context read · 7 d", f"{week[0]:,}"], ["Replies · 5 h", f"{msgs5:,}"]]
        reading["note"] = ("Local Claude Code token tally only; current plan quota is unavailable. "
                           "Check Claude Settings > Usage or /usage in Claude Code.")

    def claude_hourly(self, hours=24):
        """Claude Code output tokens per hour for the last `hours`, oldest first."""
        hours = max(1, min(int(hours), 168))
        with self._claude_log_lock:
            self._claude_local({"facts": [], "meters": []}, ())  # no quota request
            cached_rows = list(self._claude_files.values())
        now = time.time()
        start = (int(now // 3600) - hours + 1) * 3600
        buckets, replies, seen = [0] * hours, [0] * hours, set()
        for _, _, rows in cached_rows:
            for message_id, ts, _context, out in rows:
                if ts < start or ts > now or message_id in seen:
                    continue
                seen.add(message_id)
                i = min(hours - 1, int((ts - start) // 3600))
                buckets[i] += out
                replies[i] += 1
        return {"start": start, "hours": hours, "output": buckets, "replies": replies,
                "total": sum(buckets), "peak_hour": start + 3600 * buckets.index(max(buckets))
                if any(buckets) else None}

    def _deepseek(self, reading, keys):
        key = self._keyed(reading, keys, "Set DEEPSEEK_API_KEY to read the live balance.")
        if not key:
            return
        data = _get_json("https://api.deepseek.com/user/balance",
                         {"Authorization": f"Bearer {key}"})
        infos = data.get("balance_infos") or []
        reading.update(source="live", status="ok")
        if infos:
            main = infos[0]
            reading["headline"] = {"value": float(main.get("total_balance", 0)),
                                   "unit": main.get("currency", ""), "label": "balance"}
            reading["facts"] += [["Granted", main.get("granted_balance")],
                                 ["Topped up", main.get("topped_up_balance")]]
        reading["facts"].append(["Usable", "yes" if data.get("is_available") else "no"])

    def _openrouter(self, reading, keys):
        key = self._keyed(reading, keys, "Set OPENROUTER_API_KEY to read prepaid credits.")
        if not key:
            return
        data = _get_json("https://openrouter.ai/api/v1/credits",
                         {"Authorization": f"Bearer {key}"}).get("data", {})
        total, used = float(data.get("total_credits", 0)), float(data.get("total_usage", 0))
        reading.update(source="live", status="ok")
        reading["headline"] = {"value": round(total - used, 2), "unit": "USD", "label": "left"}
        reading["meters"].append(_meter("Used of purchased", used, total, unit="USD"))

    def _elevenlabs(self, reading, keys):
        month = _now().strftime("%Y-%m")
        try:
            usage = json.loads((self.state / "voice-usage.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            usage = {}
        budget = int(self.config.get("voice", {}).get("monthly_char_budget", 30000))
        jarvis_meter = _meter("JARVIS budget this month", usage.get(month, 0), budget,
                              unit="chars")
        key = self._keyed(reading, keys, "Run: python -m jarvis voice setup")
        if not key:
            reading["meters"].append(jarvis_meter)
            return
        try:
            data = _get_json("https://api.elevenlabs.io/v1/user/subscription",
                             {"xi-api-key": key})
        except RuntimeError as exc:
            reading["meters"].append(jarvis_meter)
            if "401" in str(exc):
                raise RuntimeError("The key can speak but can't read the quota: give it the "
                                   "User → Read permission in ElevenLabs.") from None
            raise
        used, limit = int(data.get("character_count", 0)), int(data.get("character_limit", 0))
        reading.update(source="live", status="ok")
        reading["headline"] = {"value": max(limit - used, 0), "unit": "chars", "label": "left"}
        reading["meters"] += [_meter("Plan quota", used, limit,
                                     _iso(data.get("next_character_count_reset_unix")), "chars"),
                              jarvis_meter]
        reading["facts"].append(["Tier", data.get("tier", "?")])

    def _manual_only(self, reading, _keys, what):
        # A key can't read these balances, so don't suggest that setting one would help.
        reading["note"] = (f"{what} Enter the balance from its dashboard and JARVIS "
                           "will show it with the date you entered it.")

    def _glm(self, reading, keys):
        self._manual_only(reading, keys, "BigModel has no public balance endpoint.")

    def _zai(self, reading, keys):
        self._manual_only(reading, keys, "Z.AI has no public balance endpoint.")

    def _xai(self, reading, keys):
        agent = self.config.get("personal_agents", {}).get("grokbot", {})
        reading["facts"].append(["Grokbot", agent.get("integration", "unconfigured")])
        self._manual_only(reading, keys, "xAI's balance needs a separate management key.")

    def _muse(self, reading, _keys):
        agent = self.config.get("personal_agents", {}).get("muse", {})
        reading["facts"] += [["Surface", agent.get("surface", "?")],
                             ["Integration", agent.get("integration", "unconfigured")]]
        reading["note"] = ("Muse runs in your logged-in Zen browser; its interface isn't "
                           "known yet, so its usage can't be read. Enter it manually.")


def save_key(state_dir, provider, config=None, secret=None):
    """Ask for a provider's key at a hidden prompt, check it live, then store it.

    The key goes only to .jarvis/secrets.json (git-ignored, never exported to child
    processes) and to the provider's own balance endpoint. It is never printed."""
    spec = next((p for p in PROVIDERS if p[0] == provider), None)
    if not spec or not spec[3] or SETUP.get(provider, ("",))[0] != "key":
        usable = ", ".join(p[0] for p in PROVIDERS if SETUP.get(p[0], ("",))[0] == "key")
        raise ValueError(f"No balance key for {provider!r}. Providers with one: {usable}.")
    pid, name, _, keys, _ = spec
    import getpass
    key = (secret or getpass.getpass)(f"Paste your {name} API key (hidden), then Enter: ").strip()
    if not key or any(c.isspace() for c in key) or not 16 <= len(key) <= 256:
        raise ValueError("That doesn't look like an API key; nothing was saved.")
    credits = Credits(state_dir, config)
    credits.key = lambda _names: key  # check this key, not whatever is stored
    reading = {"facts": [], "meters": [], "note": "", "source": "none", "status": "unconfigured",
               "headline": None}
    try:
        getattr(credits, f"_{pid}")(reading, keys)
    except RuntimeError as exc:
        if re.search(r"HTTP 40[13]", str(exc)):
            raise ValueError(f"{name} rejected that key ({exc}); nothing was saved.") from None
        reading.update(status="unchecked", note=f"Saved without a live check: {exc}")
    path = Path(state_dir).resolve() / "secrets.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[keys[0]] = key
    path.parent.mkdir(parents=True, exist_ok=True)
    write_private(path, json.dumps(data, indent=2))
    out = {"saved": keys[0], "file": str(path), "key": f"…{key[-4:]}", "status": reading["status"]}
    if reading.get("headline"):
        h = reading["headline"]
        out["balance"] = f"{h['value']} {h['unit']} {h['label']}"
    if reading.get("note"):
        out["note"] = reading["note"]
    return out


def codex_rate_limits(path, tail_bytes=400_000):
    """Newest `rate_limits` object in a Codex session log, or None."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        limits = _find(event, "rate_limits")
        if isinstance(limits, dict) and (limits.get("primary") or limits.get("secondary")):
            stamp = event.get("timestamp")
            try:
                limits["_logged_at"] = datetime.fromisoformat(
                    stamp.replace("Z", "+00:00")).timestamp() if stamp else None
            except ValueError:
                limits["_logged_at"] = None
            return limits
    return None


def _find(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find(item, key)
            if found is not None:
                return found
    return None


def claude_usage_rows(path):
    """(message_id, unix_ts, context_tokens, output_tokens) per assistant message."""
    rows = []
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return rows
    with handle:
        for line in handle:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                event = json.loads(line)
                message = event.get("message") or {}
                usage = message.get("usage") or {}
                ts = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            try:
                context = sum(int(usage.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                output = int(usage.get("output_tokens") or 0)
            except (ValueError, TypeError, OverflowError):
                continue
            if context < 0 or output < 0:
                continue
            rows.append((message.get("id") or event.get("uuid"), ts, context,
                         output))
    return rows

