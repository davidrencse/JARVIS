"""jarvis/usage.py: token normalisation, notional API-equivalent dollars, per-request meter,
Codex window calibration from session logs. Home is patched to a temp dir; no network, no CLI.

usage.py is bookkeeping: it never chooses or gates a lane. The rule "never spend pay-per-token
credits" is enforced by fastlane.subscription_env / runner (see tests/test_runner.py); here we
pin that usage.py itself can never reach a paid API and that its dollars are notional only."""

import ast
import contextlib
import json
import os
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

from jarvis import usage
from jarvis.usage import Meter, calibrate, calibration, dollars, meter, price, record, record_run, tokens


class Case(unittest.TestCase):
    def setUp(self):
        # usage.py:143 leaves log files for the GC to close (see report); keep output clean.
        ctx = warnings.catch_warnings()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        home = self.tmp / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.saved = (usage._CALIBRATION, usage._CALIBRATED_AT)
        self.addCleanup(lambda: setattr(usage, "_CALIBRATION", self.saved[0]))
        self.addCleanup(lambda: setattr(usage, "_CALIBRATED_AT", self.saved[1]))
        usage._CALIBRATION, usage._CALIBRATED_AT = None, 0.0


class NoPaidApiTests(Case):
    def test_module_imports_no_network_or_api_client(self):
        tree = ast.parse(Path(usage.__file__).read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots.add((node.module or "").split(".")[0] if node.level == 0 else "<relative>")
        self.assertFalse(roots & {"urllib", "http", "requests", "httpx", "socket", "openai",
                                  "anthropic", "subprocess", "aiohttp", "ssl"}, roots)
        self.assertEqual(roots - {"<relative>"}, {"contextlib", "contextvars", "json", "time", "pathlib"})

    def test_dollars_are_notional_and_do_not_need_credentials(self):
        env = {k: v for k, v in os.environ.items() if "KEY" not in k and "TOKEN" not in k}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertGreater(dollars(tokens({"input_tokens": 1_000_000}), "gpt-5"), 0)

    def test_subscription_env_strips_every_paid_api_variable(self):
        from jarvis.fastlane import API_KEY_VARS, subscription_env
        with mock.patch.dict(os.environ, {v: "x" for v in API_KEY_VARS}):
            env = subscription_env()
        self.assertEqual([v for v in API_KEY_VARS if v in env], [])


class PriceTests(Case):
    def test_model_families(self):
        self.assertEqual(price("opus"), usage.PRICES["opus"])
        self.assertEqual(price("Claude-SONNET-4"), usage.PRICES["sonnet"])
        self.assertEqual(price("haiku"), usage.PRICES["haiku"])
        self.assertEqual(price("gpt-5-codex"), usage.PRICES["codex"])  # longest key wins
        self.assertEqual(price("claude-unknown"), usage.PRICES["claude"])

    def test_full_claude_model_ids_use_their_family_price(self):
        self.assertEqual(price("claude-opus-4-1"), usage.PRICES["opus"])
        self.assertEqual(price("claude-haiku-4-5"), usage.PRICES["haiku"])

    def test_unknown_or_missing_model_falls_back_to_gpt(self):
        for model in (None, "", "llama-3", "mystery"):
            self.assertEqual(price(model), usage.PRICES["gpt"])

    def test_config_prices_override_and_extend(self):
        self.assertEqual(price("opus-x", {"opus": (1, 2, 3)}), (1, 2, 3))
        self.assertEqual(price("grok-4", {"grok": (9, 9, 9)}), (9, 9, 9))
        self.assertEqual(usage.PRICES["opus"], (15.0, 1.5, 75.0))  # module table untouched

    def test_dollar_math(self):
        tok = {"input": 1_000_000, "cached": 1_000_000, "output": 1_000_000}
        self.assertAlmostEqual(dollars(tok, "gpt"), 1.25 + 0.125 + 10.0)
        self.assertAlmostEqual(dollars(tok, "opus"), 15 + 1.5 + 75)
        self.assertEqual(dollars({"input": 0, "cached": 0, "output": 0}, "gpt"), 0)


class TokenTests(Case):
    def test_codex_style_cached_is_inside_input(self):
        t = tokens({"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7})
        self.assertEqual(t, {"input": 60, "cached": 40, "output": 7, "total": 107})

    def test_codex_camel_case(self):
        t = tokens({"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 2})
        self.assertEqual((t["input"], t["cached"], t["output"]), (6, 4, 2))

    def test_claude_style_cache_reported_separately(self):
        t = tokens({"input_tokens": 100, "cache_read_input_tokens": 40,
                    "cache_creation_input_tokens": 25, "output_tokens": 7})
        self.assertEqual(t, {"input": 125, "cached": 40, "output": 7, "total": 172})

    def test_claude_style_with_zero_cache_read_key_present(self):
        t = tokens({"input_tokens": 9, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 1})
        self.assertEqual(t["input"], 10)

    def test_cached_larger_than_input_clamped_for_codex(self):
        self.assertEqual(tokens({"input_tokens": 5, "cached_input_tokens": 50})["input"], 0)

    def test_empty_none_and_missing(self):
        zero = {"input": 0, "cached": 0, "output": 0, "total": 0}
        for raw in (None, {}, {"unrelated": 5}):
            self.assertEqual(tokens(raw), zero)

    def test_non_numeric_values_ignored(self):
        t = tokens({"input_tokens": "100", "output_tokens": None, "cached_input_tokens": [1]})
        self.assertEqual(t["total"], 0)

    def test_floats_truncate(self):
        self.assertEqual(tokens({"input_tokens": 10.9})["input"], 10)

    def test_first_numeric_alias_wins(self):
        t = tokens({"input_tokens": "bad", "inputTokens": 8})
        self.assertEqual(t["input"], 8)

    def test_bool_counts_as_number(self):
        # Pins current behaviour: bool is an int subclass.
        self.assertEqual(tokens({"output_tokens": True})["output"], 1)

    def test_negative_claude_style_not_clamped(self):
        self.assertEqual(tokens({"input_tokens": -5, "cache_read_input_tokens": 0})["input"], -5)


class MeterTests(Case):
    def test_empty_meter_is_a_local_path(self):
        self.assertEqual(Meter().summary(), {"tokens": 0, "usd": 0.0, "windows": {"5h": 0.0, "week": 0.0},
                                             "engine": "local", "calls": 0})

    def test_meter_sums_calls_and_providers(self):
        m = Meter()
        m.add("chatgpt", "gpt-5", {"input_tokens": 1_000_000, "output_tokens": 0})
        m.add("claude", "sonnet", {"input_tokens": 0, "output_tokens": 1_000_000})
        with mock.patch.object(usage, "calibration", return_value={"5h": None, "week": None}):
            s = m.summary()
        self.assertEqual(s["calls"], 2)
        self.assertEqual(s["engine"], "chatgpt+claude")
        self.assertEqual(s["tokens"], 2_000_000)
        self.assertAlmostEqual(s["usd"], 1.25 + 15.0)
        self.assertEqual((s["input"], s["cached"], s["output"]), (1_000_000, 0, 1_000_000))

    def test_explicit_usd_overrides_computed(self):
        m = Meter()
        m.add("claude", "sonnet", {"input_tokens": 1_000_000}, usd="0.25")
        self.assertEqual(m.calls[0]["usd"], 0.25)

    def test_prebuilt_tokens_not_renormalised(self):
        m = Meter()
        m.add("codex", "gpt", {"input": 3, "cached": 0, "output": 2, "total": 5})
        self.assertEqual(m.calls[0]["total"], 5)

    def test_window_share_uses_codex_dollars_only(self):
        m = Meter()
        m.add("codex", "gpt", {"input_tokens": 1_000_000}, usd=2.0)
        m.add("claude", "sonnet", {"input_tokens": 1}, usd=100.0)
        with mock.patch.object(usage, "calibration", return_value={"5h": 0.5, "week": 4.0}):
            s = m.summary()
        self.assertEqual(s["windows"], {"5h": 4.0, "week": 0.5})

    def test_window_unknown_when_uncalibrated(self):
        m = Meter()
        m.add("chatgpt", "gpt", {"input_tokens": 10}, usd=1.0)
        for cal in ({"5h": None, "week": None}, {}, {"5h": 0, "week": 0}):
            with self.subTest(cal=cal), mock.patch.object(usage, "calibration", return_value=cal):
                self.assertEqual(m.summary()["windows"], {"5h": None, "week": None})

    def test_window_zero_when_no_codex_spend(self):
        m = Meter()
        m.add("claude", "sonnet", {"input_tokens": 10})
        with mock.patch.object(usage, "calibration", return_value={"5h": None, "week": 3.0}):
            self.assertEqual(m.summary()["windows"], {"5h": 0.0, "week": 0.0})

    def test_nested_meter_joins_outer(self):
        with meter() as outer:
            with meter() as inner:
                self.assertIs(inner, outer)
                record("codex", "gpt", {"input_tokens": 5})
            record("claude", "sonnet", {"output_tokens": 5})
        self.assertEqual(len(outer.calls), 2)

    def test_meter_is_closed_after_request(self):
        with meter():
            pass
        self.assertIsNone(usage._METER.get())
        record("codex", "gpt", {"input_tokens": 5})  # no-op, no error

    def test_meter_closed_after_exception(self):
        with self.assertRaises(RuntimeError), meter():
            raise RuntimeError
        self.assertIsNone(usage._METER.get())

    def test_separate_requests_do_not_share_calls(self):
        with meter() as a:
            record("codex", "gpt", {"input_tokens": 1})
        with meter() as b:
            pass
        self.assertEqual((len(a.calls), len(b.calls)), (1, 0))

    def test_threads_do_not_leak_into_each_other(self):
        import threading
        seen = []
        with meter() as m:
            t = threading.Thread(target=lambda: seen.append(usage._METER.get()))
            t.start(); t.join()
        self.assertEqual(seen, [None])

    def test_record_ignores_empty_usage(self):
        with meter() as m:
            for raw in (None, {}, 0):
                record("codex", "gpt", raw)
        self.assertEqual(m.calls, [])

    def test_record_swallows_bad_data(self):
        with meter() as m:
            record("codex", "gpt", {"input_tokens": float("inf")})
            record("codex", "gpt", {"input_tokens": float("nan")})
            record("codex", "gpt", {"input_tokens": 1}, usd="not-a-number")
            record("codex", "gpt", "garbage")
        self.assertEqual(m.calls, [])


class RecordRunTests(Case):
    def write(self, *events, raw=None):
        d = self.tmp / "run"
        d.mkdir(exist_ok=True)
        lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
        (d / "stdout.jsonl").write_text("\n".join(lines), encoding="utf-8")
        return d

    def test_claude_result_uses_its_own_cost(self):
        d = self.write({"type": "system"}, {"type": "result", "total_cost_usd": 0.42,
                       "usage": {"input_tokens": 10, "output_tokens": 5}})
        with meter() as m:
            record_run(d, "claude", "sonnet")
        self.assertEqual(m.calls[0]["usd"], 0.42)
        self.assertEqual(m.calls[0]["provider"], "claude")
        self.assertEqual(m.calls[0]["total"], 15)

    def test_claude_result_without_cost_is_priced_notionally(self):
        d = self.write({"type": "result", "usage": {"input_tokens": 1_000_000}})
        with meter() as m:
            record_run(d, "claude", "sonnet")
        self.assertAlmostEqual(m.calls[0]["usd"], 3.0)

    def test_codex_turn_completed_last_one_wins(self):
        d = self.write({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
                       {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 10,
                                                            "output_tokens": 5}})
        with meter() as m:
            record_run(d, "codex", "gpt-5")
        self.assertEqual(len(m.calls), 1)
        self.assertEqual((m.calls[0]["input"], m.calls[0]["cached"]), (90, 10))

    def test_noop_without_meter_or_file(self):
        d = self.write({"type": "result", "usage": {"input_tokens": 1}})
        record_run(d, "claude", "x")  # no meter open
        with meter() as m:
            record_run(self.tmp / "missing", "claude", "x")
        self.assertEqual(m.calls, [])

    def test_malformed_lines_and_shapes_skipped(self):
        d = self.write("not json", "[]", "5", "null", "", '{"type":"turn.completed","usage":"x"}',
                       {"type": "result", "usage": None},
                       {"type": "turn.completed", "usage": {"output_tokens": 4}})
        with meter() as m:
            record_run(d, "codex", "gpt")
        self.assertEqual(m.calls[0]["output"], 4)

    def test_binary_garbage_does_not_raise(self):
        d = self.tmp / "run"
        d.mkdir()
        (d / "stdout.jsonl").write_bytes(b"\xff\xfe\x00garbage\n")
        with meter() as m:
            record_run(d, "claude", "x")
        self.assertEqual(m.calls, [])

    def test_usage_with_no_tokens_records_nothing(self):
        d = self.write({"type": "result", "total_cost_usd": 9.0, "usage": {}})
        with meter() as m:
            record_run(d, "claude", "x")
        self.assertEqual(m.calls, [])


def log_line(ts, total, primary, secondary=None):
    return json.dumps({"timestamp": ts, "payload": {
        "type": "token_count", "info": {"total_token_usage": total},
        "rate_limits": {"primary": primary, "secondary": secondary}}})


class CodexLogTests(Case):
    def session(self, name, *lines, age_days=0.0, base="sessions"):
        root = self.tmp / base
        d = root / "2026" / "10" / "21"
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if age_days:
            t = time.time() - age_days * 86400
            os.utime(p, (t, t))
        return root

    def test_missing_root_is_empty(self):
        self.assertEqual(usage._codex_events(root=self.tmp / "nope"), [])
        self.assertEqual(usage._codex_events(), [])  # default ~/.codex under patched HOME

    def test_default_root_is_under_home(self):
        d = Path(os.environ["HOME"]) / ".codex" / "sessions" / "2026" / "10" / "21"
        d.mkdir(parents=True)
        (d / "s.jsonl").write_text(log_line("t1", {"input_tokens": 1}, {"used_percent": 1, "resets_at": 5}) + "\n")
        self.assertEqual(len(usage._codex_events()), 1)

    def test_filters_old_files_and_irrelevant_or_broken_lines(self):
        good = log_line("2026-10-21T10:00:00Z", {"input_tokens": 1}, {"used_percent": 3, "resets_at": 1})
        root = self.session("s.jsonl", good, "garbage", '{"token_count": 1}',
                            '{"token_count":1,"rate_limits":1,', "",
                            log_line("t", {}, {"used_percent": 1, "resets_at": 1}),         # no usage
                            log_line("t", {"input_tokens": 1}, None))                       # no primary
        self.session("old.jsonl", good, age_days=30)
        events = usage._codex_events(root=root)
        self.assertEqual([e[1] for e in events], ["s.jsonl"])

    def test_events_sorted_by_timestamp(self):
        root = self.session("s.jsonl",
                            log_line("2026-10-21T12", {"input_tokens": 2}, {"used_percent": 2, "resets_at": 1}),
                            log_line("2026-10-21T09", {"input_tokens": 1}, {"used_percent": 1, "resets_at": 1}))
        self.assertEqual([e[0] for e in usage._codex_events(root=root)], ["2026-10-21T09", "2026-10-21T12"])

    def test_null_secondary_becomes_empty_dict(self):
        root = self.session("s.jsonl", log_line("t", {"input_tokens": 1}, {"used_percent": 1, "resets_at": 1}))
        self.assertEqual(usage._codex_events(root=root)[0][4], {})

    def test_binary_garbage_in_log_survives(self):
        root = self.session("s.jsonl")
        (root / "2026" / "10" / "21" / "s.jsonl").write_bytes(b"\xff\xfe\x00 token_count rate_limits\n")
        self.assertEqual(usage._codex_events(root=root), [])

    def test_non_object_payload_is_skipped_not_fatal(self):
        root = self.session("s.jsonl", '["token_count","rate_limits"]',
                            '{"payload":"token_count rate_limits"}',
                            log_line("t", {"input_tokens": 1}, {"used_percent": 1, "resets_at": 1}))
        self.assertEqual(len(usage._codex_events(root=root)), 1)


class CalibrateTests(CodexLogTests):
    def moved(self, name, p0, p1, tokens=1_000_000, week=None, reset=100, extra=(), base="sessions"):
        return self.session(name, base=base, *(
                            log_line("t1", {"input_tokens": 0}, {"used_percent": p0, "resets_at": reset}, week and week[0]),
                            log_line("t2", {"input_tokens": tokens}, {"used_percent": p1, "resets_at": reset}, week and week[1]),
                            *extra))

    def test_dollars_per_percent(self):
        root = self.moved("s.jsonl", 10, 14, week=({"used_percent": 1, "resets_at": 9}, {"used_percent": 3, "resets_at": 9}))
        cal = calibrate(root=root)
        self.assertAlmostEqual(cal["5h"], 1.25 / 4, places=6)   # $1.25 of gpt input over 4 points
        self.assertAlmostEqual(cal["week"], 1.25 / 2, places=6)

    def test_boundary_two_percent_counts_one_does_not(self):
        self.assertIsNotNone(calibrate(root=self.moved("a.jsonl", 10, 12, base="r2"))["5h"])
        self.assertIsNone(calibrate(root=self.moved("b.jsonl", 10, 11, base="r1"))["5h"])

    def test_no_movement_or_no_spend_is_none(self):
        self.assertEqual(calibrate(root=self.moved("a.jsonl", 10, 10, base="r1")), {"5h": None, "week": None})
        self.assertEqual(calibrate(root=self.moved("b.jsonl", 10, 20, tokens=0, base="r2")), {"5h": None, "week": None})

    def test_no_logs_gives_none(self):
        self.assertEqual(calibrate(root=self.tmp / "nope"), {"5h": None, "week": None})
        self.assertEqual(calibrate(), {"5h": None, "week": None})

    def test_window_without_reset_key_or_percent_ignored(self):
        root = self.session("s.jsonl",
                            log_line("t1", {"input_tokens": 0}, {"used_percent": 10}),
                            log_line("t2", {"input_tokens": 10**6}, {"used_percent": 20}),
                            log_line("t3", {"input_tokens": 2 * 10**6}, {"resets_at": 1}))
        self.assertEqual(calibrate(root=root)["5h"], None)

    def test_first_event_of_session_has_no_delta(self):
        root = self.session("s.jsonl", log_line("t1", {"input_tokens": 10**6}, {"used_percent": 1, "resets_at": 1}),
                            log_line("t2", {"input_tokens": 10**6}, {"used_percent": 9, "resets_at": 1}))
        self.assertIsNone(calibrate(root=root)["5h"])  # tokens never moved after the first sample

    def test_token_counter_reset_does_not_go_negative(self):
        root = self.session("s.jsonl",
                            log_line("t1", {"input_tokens": 5 * 10**6}, {"used_percent": 1, "resets_at": 1}),
                            log_line("t2", {"input_tokens": 10**6}, {"used_percent": 5, "resets_at": 1}))
        self.assertIsNone(calibrate(root=root)["5h"])

    def test_cached_tokens_priced_cheaper(self):
        root = self.session("s.jsonl", log_line("t1", {"input_tokens": 0}, {"used_percent": 0, "resets_at": 1}),
                            log_line("t2", {"input_tokens": 10**6, "cached_input_tokens": 10**6},
                                     {"used_percent": 10, "resets_at": 1}))
        self.assertAlmostEqual(calibrate(root=root)["5h"], 0.125 / 10, places=6)

    def test_sessions_and_windows_aggregate(self):
        root = self.moved("a.jsonl", 0, 5, reset=1)
        self.moved("b.jsonl", 0, 5, reset=2)
        self.assertAlmostEqual(calibrate(root=root)["5h"], 2.5 / 10, places=6)

    def test_model_and_price_overrides(self):
        root = self.moved("s.jsonl", 0, 10)
        self.assertAlmostEqual(calibrate(root=root, model="opus")["5h"], 15 / 10, places=6)
        self.assertAlmostEqual(calibrate(root=root, prices={"gpt": (2.0, 0, 0)})["5h"], 2 / 10, places=6)

    def test_percent_above_100_or_negative_is_just_arithmetic(self):
        self.assertAlmostEqual(calibrate(root=self.moved("s.jsonl", 90, 130))["5h"], 1.25 / 40, places=6)


class CalibrationCacheTests(Case):
    def test_cached_within_six_hours(self):
        with mock.patch.object(usage, "calibrate", return_value={"5h": 1.0, "week": 2.0}) as cal:
            self.assertEqual(calibration(), {"5h": 1.0, "week": 2.0})
            calibration()
        self.assertEqual(cal.call_count, 1)

    def test_recomputed_after_six_hours(self):
        with mock.patch.object(usage, "calibrate", return_value={"5h": 1.0, "week": 2.0}) as cal:
            calibration()
            usage._CALIBRATED_AT = time.time() - 21601
            calibration()
            usage._CALIBRATED_AT = time.time() - 21500
            calibration()
        self.assertEqual(cal.call_count, 2)

    def test_failure_degrades_to_unknown_and_is_cached(self):
        with mock.patch.object(usage, "calibrate", side_effect=RuntimeError("disk")) as cal:
            self.assertEqual(calibration(), {"5h": None, "week": None})
            self.assertEqual(calibration(), {"5h": None, "week": None})
        self.assertEqual(cal.call_count, 1)

    def test_summary_survives_broken_logs(self):
        m = Meter()
        m.add("codex", "gpt", {"input_tokens": 10}, usd=1.0)
        with mock.patch.object(usage, "calibrate", side_effect=OSError):
            self.assertEqual(m.summary()["windows"], {"5h": None, "week": None})


if __name__ == "__main__":
    unittest.main()
