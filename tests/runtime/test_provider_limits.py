"""Offline quota contract tests; no real credentials, inference or provider requests."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jarvis import credits as mod


class ProviderLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.patch = patch.object(mod, "HOME", self.home)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.credits = mod.Credits(self.home)

    def auth(self, provider, **overrides):
        if provider == "claude":
            path = self.home / ".claude" / ".credentials.json"
            data = {"claudeAiOauth": {"accessToken": "private-test-token",
                "expiresAt": (time.time() + 1000) * 1000, "subscriptionType": "pro", **overrides}}
        else:
            path = self.home / ".codex" / "auth.json"
            data = {"auth_mode": "chatgpt", "tokens": {"access_token": "private-test-token",
                "account_id": "private-account"}, **overrides}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def reading(self):
        return {"facts": [], "meters": [], "headline": None, "note": "",
                "source": "none", "status": "unconfigured"}

    def test_claude_live_quota_and_disabled_extra_usage(self):
        path = self.auth("claude")
        before = path.read_bytes()
        reading = self.reading()
        response = {"five_hour": {"utilization": 97, "resets_at": "2030-01-01T01:00:00Z"},
                    "seven_day": {"utilization": 13}, "extra_usage": {"is_enabled": False},
                    "unknown_window": {"utilization": 50}}
        with patch.object(mod, "_subscription_json", return_value=response) as call:
            self.credits._claude(reading, ())
        self.assertEqual(reading["source"], "live")
        self.assertEqual(reading["headline"]["value"], 3)
        self.assertEqual([m["used"] for m in reading["meters"]], [97, 13])
        self.assertIn(["Extra usage", "disabled"], reading["facts"])
        self.assertEqual(call.call_args.args[0], "https://api.anthropic.com/api/oauth/usage")
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("private-test-token", json.dumps(reading))

    def test_codex_live_credits_are_not_dollars(self):
        self.auth("codex")
        response = {"plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 100, "limit_window_seconds": 18000,
                               "reset_at": 1893459600},
            "secondary_window": {"used_percent": 48, "limit_window_seconds": 604800}},
            "credits": {"balance": "252.592425", "unlimited": False}}
        reading = self.reading()
        with patch.object(mod, "_subscription_json", return_value=response):
            self.credits._codex(reading, ())
        self.assertEqual(reading["headline"]["value"], 0)
        self.assertEqual([m["label"] for m in reading["meters"]], ["5-hour window", "Weekly window"])
        self.assertIn(["Credits", "252.592425"], reading["facts"])
        self.assertEqual(reading["source"], "live")

    def test_cached_success_and_failure_do_not_hammer_provider(self):
        self.auth("claude")
        with patch.object(mod, "_subscription_json", return_value={"five_hour": {"utilization": 1}}) as call:
            self.credits._claude(self.reading(), ())
            self.credits._claude(self.reading(), ())
        self.assertEqual(call.call_count, 1)
        self.credits._usage_cache.clear()
        with patch.object(mod, "_subscription_json", side_effect=RuntimeError("Usage endpoint returned HTTP 429")) as call:
            self.credits._claude(self.reading(), ())
            self.credits._claude(self.reading(), ())
        self.assertEqual(call.call_count, 1)

    def test_expired_claude_credentials_never_refresh_or_request(self):
        path = self.auth("claude", expiresAt=1)
        before = path.read_bytes()
        reading = self.reading()
        with patch.object(mod, "_subscription_json") as call:
            self.credits._claude(reading, ())
        call.assert_not_called()
        self.assertIn("expired", reading["note"])
        self.assertEqual(path.read_bytes(), before)

    def test_unavailable_quota_retains_local_tally(self):
        self.auth("claude")
        path = self.home / ".claude" / "projects" / "sample.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "assistant", "timestamp": mod._now().isoformat(),
            "message": {"id": "reply", "usage": {"input_tokens": 10, "output_tokens": 20}}}) + "\n")
        with patch.object(mod, "_subscription_json", side_effect=RuntimeError("Usage endpoint returned HTTP 403")):
            reading = self.reading()
            self.credits._claude(reading, ())
        self.assertEqual(reading["headline"]["value"], 20)
        self.assertEqual(reading["source"], "local")
        self.assertEqual(reading["meters"], [])
        self.assertIn("HTTP 403", reading["note"])

    def test_hourly_chart_never_calls_quota_endpoint(self):
        self.auth("claude")
        with patch.object(mod, "_subscription_json") as call:
            self.credits.claude_hourly()
        call.assert_not_called()

    def test_malformed_window_falls_back_without_losing_provider(self):
        for provider, response in [("claude", {"five_hour": []}),
                                   ("codex", {"rate_limit": [1]})]:
            with self.subTest(provider=provider):
                self.auth(provider)
                with patch.object(mod, "_subscription_json", return_value=response):
                    reading = self.reading()
                    getattr(self.credits, "_" + provider)(reading, ())
                self.assertEqual(reading["source"], "none")
                self.assertEqual(reading["meters"], [])
                self.assertIn("no supported quota windows", reading["note"])

    def test_missing_project_directory_clears_hourly_cache(self):
        self.credits._claude_files[self.home / "deleted"] = (0, 0, [("m", time.time(), 1, 10)])
        self.assertEqual(self.credits.claude_hourly()["total"], 0)

    def test_malformed_token_row_does_not_hide_valid_usage(self):
        path = self.home / "session.jsonl"
        rows = [{"type": "assistant", "timestamp": mod._now().isoformat(),
                 "message": {"id": str(i), "usage": {"output_tokens": count}}}
                for i, count in enumerate(["junk", 25])]
        path.write_text("\n".join(json.dumps(row) for row in rows))
        self.assertEqual([row[-1] for row in mod.claude_usage_rows(path)], [25])

    def test_invalid_percentages_cannot_become_fake_balances(self):
        for value in [None, True, "95", float("nan"), float("inf"), -1, 101]:
            with self.subTest(value=value):
                reading = self.reading()
                self.assertFalse(self.credits._quota(reading, [("5-hour window", value, None)]))
                self.assertEqual(reading["meters"], [])
        self.assertEqual(mod._window_label(15, "window"), "15-minute window")

    def test_authenticated_requests_refuse_redirects_and_redact_failures(self):
        self.assertIsNone(mod._NoUsageRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))
        opener = MagicMock()
        opener.open.side_effect = OSError("private-test-token")
        with patch.object(mod.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(RuntimeError) as error:
                mod._subscription_json("https://api.anthropic.com/api/oauth/usage", {"Authorization": "Bearer private-test-token"})
        self.assertNotIn("private-test-token", str(error.exception))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], mod.TIMEOUT)

    def test_expired_log_quota_is_unknown_not_fully_available(self):
        path = self.home / ".codex" / "sessions" / "sample.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"rate_limits": {"primary": {
            "used_percent": 100, "window_minutes": 300, "resets_at": time.time() - 10}}}))
        reading = self.reading()
        self.credits._codex(reading, ())
        self.assertIsNone(reading["headline"])
        self.assertEqual(reading["meters"], [])
        self.assertIn("unknown", str(reading["facts"]))


if __name__ == "__main__":
    unittest.main()
