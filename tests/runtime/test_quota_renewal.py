"""Quota checks across Claude session renewal; fake renewals only, never a real CLI."""
import json
import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from jarvis import credits as mod

USAGE = {"five_hour": {"utilization": 6, "resets_at": "2030-01-01T01:00:00Z"},
         "seven_day": {"utilization": 14}}


class QuotaRenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        for patcher in (patch.object(mod, "HOME", self.home), patch.dict(os.environ)):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("CODEX_HOME", None)
        self.path = self.home / ".claude" / ".credentials.json"
        self.path.parent.mkdir(parents=True)
        self.credits = mod.Credits(self.home)

    def write(self, token="private-old-token", expires_in=1000, refresh=True):
        auth = {"accessToken": token, "expiresAt": (time.time() + expires_in) * 1000,
                "subscriptionType": "pro"}
        if refresh:
            auth["refreshToken"] = "private-refresh-token"
        self.path.write_text(json.dumps({"claudeAiOauth": auth}))

    def renewal(self, **fresh):
        """A fake CLI renewal that rewrites credentials the way Claude Code would."""
        def renew():
            self.write(**{"token": "private-new-token", **fresh})
        return patch.object(mod, "renew_claude_session", side_effect=renew)

    def reading(self):
        return {"facts": [], "meters": [], "headline": None, "note": "",
                "source": "none", "status": "unconfigured"}

    def tokens(self, call):
        return [c.args[1]["Authorization"] for c in call.call_args_list]

    def test_expired_token_is_renewed_before_the_quota_request(self):
        self.write(expires_in=-60)
        reading = self.reading()
        with self.renewal() as renew, patch.object(mod, "_subscription_json", return_value=USAGE) as call:
            self.credits._claude(reading, ())
        renew.assert_called_once()
        self.assertEqual(self.tokens(call), ["Bearer private-new-token"])
        self.assertEqual(reading["source"], "live")
        self.assertEqual(reading["headline"]["value"], 94)
        self.assertNotIn("private", json.dumps(reading))

    def test_renewal_failures_fall_back_with_sanitized_note_and_cooldown(self):
        for message in ("Claude session renewal timed out",
                        "Claude Code is unavailable for session renewal",
                        "Claude session renewal did not complete; sign-in may need attention"):
            with self.subTest(message=message):
                self.credits._usage_cache.clear()
                self.write(expires_in=-60)
                with patch.object(mod, "renew_claude_session", side_effect=RuntimeError(message)) as renew, \
                        patch.object(mod, "_subscription_json") as call:
                    first, second = self.reading(), self.reading()
                    self.credits._claude(first, ())
                    self.credits._claude(second, ())
                renew.assert_called_once()
                call.assert_not_called()
                self.assertEqual(first["meters"], [])
                self.assertIn(message, first["note"])
                self.assertEqual(first["note"], second["note"])

    def test_renewal_that_leaves_an_expired_token_is_reported(self):
        self.write(expires_in=-60)
        reading = self.reading()
        with self.renewal(expires_in=-30) as renew, patch.object(mod, "_subscription_json") as call:
            self.credits._claude(reading, ())
            # The CLI's own rewrite must not look like a concurrent sign-in change
            # and defeat the cache, which would retry renewal on every refresh.
            self.credits._claude(self.reading(), ())
        renew.assert_called_once()
        call.assert_not_called()
        self.assertIn("could not be renewed; run claude auth login", reading["note"])

    def test_missing_refresh_token_never_starts_the_cli(self):
        self.write(expires_in=-60, refresh=False)
        reading = self.reading()
        with patch.object(mod, "renew_claude_session") as renew, \
                patch.object(mod, "_subscription_json") as call:
            self.credits._claude(reading, ())
        renew.assert_not_called()
        call.assert_not_called()
        self.assertIn("run claude auth login", reading["note"])

    def test_rejected_unexpired_token_gets_exactly_one_renewal(self):
        self.write()
        reading = self.reading()
        with self.renewal(), patch.object(mod, "_subscription_json",
                side_effect=[mod.UsageEndpointError(401), USAGE]) as call:
            self.credits._claude(reading, ())
        self.assertEqual(self.tokens(call), ["Bearer private-old-token", "Bearer private-new-token"])
        self.assertEqual(reading["source"], "live")

    def test_revoked_sign_in_does_not_loop(self):
        self.write()
        reading = self.reading()
        # The CLI exits cleanly but the saved token is unchanged: a revoked sign-in.
        with patch.object(mod, "renew_claude_session") as renew, patch.object(
                mod, "_subscription_json", side_effect=mod.UsageEndpointError(401)) as call:
            self.credits._claude(reading, ())
        renew.assert_called_once()
        call.assert_called_once()
        self.assertIn("sign-in was rejected", reading["note"])

    def test_rejection_after_renewal_is_not_renewed_again(self):
        self.write(expires_in=-60)
        reading = self.reading()
        with self.renewal() as renew, patch.object(
                mod, "_subscription_json", side_effect=mod.UsageEndpointError(401)) as call:
            self.credits._claude(reading, ())
        renew.assert_called_once()
        call.assert_called_once()
        self.assertIn("HTTP 401", reading["note"])

    def test_external_sign_in_invalidates_cached_failure(self):
        self.write(expires_in=-60)
        with patch.object(mod, "renew_claude_session", side_effect=RuntimeError("Claude session renewal timed out")):
            self.credits._claude(self.reading(), ())
        # The user signs in again elsewhere; the next check must not reuse the failure.
        self.write(token="private-relogin-token-longer")
        reading = self.reading()
        with patch.object(mod, "_subscription_json", return_value=USAGE) as call:
            self.credits._claude(reading, ())
        self.assertEqual(self.tokens(call), ["Bearer private-relogin-token-longer"])
        self.assertEqual(reading["source"], "live")

    def test_cached_quota_keeps_fetch_time_and_expires_at_reset(self):
        self.write()
        start = mod._now()
        usage = {"five_hour": {"utilization": 90, "resets_at": (start + timedelta(minutes=5)).isoformat()}}
        with patch.object(mod, "_subscription_json", return_value=usage) as call:
            first = self.reading()
            self.credits._claude(first, ())
            with patch.object(mod, "_now", return_value=start + timedelta(seconds=30)):
                cached = self.reading()
                self.credits._claude(cached, ())
            self.assertEqual(call.call_count, 1)
            self.assertEqual(cached["checked_at"], first["checked_at"])
            with patch.object(mod, "_now", return_value=start + timedelta(minutes=6)):
                after = self.reading()
                self.credits._claude(after, ())
        # Crossing the reset forces a fresh reading instead of the old 10% allowance.
        self.assertEqual(call.call_count, 2)
        self.assertEqual(after["meters"], [])

    def test_panel_cache_follows_credential_changes(self):
        self.write()
        with patch.object(mod, "_subscription_json", return_value=USAGE) as call:
            self.credits.all()
            self.credits.all()
            self.assertEqual(call.call_count, 1)
            self.write(token="private-other-account-token")
            claude = next(r for r in self.credits.all() if r["id"] == "claude")
        self.assertEqual(call.call_count, 2)
        self.assertEqual(claude["source"], "live")


if __name__ == "__main__":
    unittest.main()
