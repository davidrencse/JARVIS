import json
import shutil
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from jarvis import ui
from jarvis.credits import Credits
from jarvis.ui import Console

ROOT = Path(__file__).resolve().parents[2]
CONFIG = {**json.loads((ROOT / "jarvis.config.json").read_text(encoding="utf-8")),
          "memory": {"semantic": False}, "decisions": {"shadow": False}}
CONFIG["routing"] = {**CONFIG.get("routing", {}), "interpreter": False}


class Tmp(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)


class FailMessageTests(unittest.TestCase):
    def fail(self, exc):
        sent = []
        stub = mock.Mock(_send=lambda status, body, close=False: sent.append((status, body)))
        with mock.patch("traceback.print_exception"):
            ui.Handler._fail(stub, exc)
        return sent[0]

    def test_runtime_error_text_reaches_the_page(self):
        status, body = self.fail(RuntimeError("The other laptop didn't respond in time."))
        self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(body["error"], "The other laptop didn't respond in time.")

    def test_oserror_and_others_stay_generic(self):
        for exc in (OSError(2, "No such file", "/home/jdoe/.jarvis/secret"), KeyError("token-abc")):
            _, body = self.fail(exc)
            self.assertNotIn("secret", body["error"])
            self.assertNotIn("token-abc", body["error"])

    def test_lost_main_is_retryable_without_internal_exception_text(self):
        from jarvis.continuity.runtime import RuntimeRefusal
        from jarvis.continuity.integration import IntegrationError
        for exc in (RuntimeRefusal("not_holder"), IntegrationError("managed_unbound")):
            status, body = self.fail(exc)
            self.assertEqual(status, HTTPStatus.SERVICE_UNAVAILABLE)
            self.assertEqual(body["code"], "main_unavailable")
            self.assertNotIn(exc.code, body["error"])


class SettingsApplyTests(Tmp):
    def test_brain_change_drops_the_warm_lane_and_semantic_rebuilds_memory(self):
        config = json.loads(json.dumps(CONFIG))
        c = Console(config, ROOT, self.base)
        lane = mock.Mock()
        c._lane = lane
        old_memory = c.memory
        before = c.settings_snapshot()
        config["routing"]["brain"] = "claude" if config["routing"].get("brain") != "claude" else "chatgpt"
        config["memory"]["semantic"] = True
        with mock.patch.object(ui.Memory, "warm_vectors"):
            c.settings_applied(before)
        lane.close.assert_called_once()
        self.assertIsNone(c._lane)
        self.assertIsNot(c.memory, old_memory)

    def test_unchanged_settings_keep_everything(self):
        c = Console(json.loads(json.dumps(CONFIG)), ROOT, self.base)
        c._lane = lane = mock.Mock()
        memory = c.memory
        c.settings_applied(c.settings_snapshot())
        self.assertIs(c._lane, lane)
        self.assertIs(c.memory, memory)

    def test_restart_list_names_brain_and_semantic(self):
        from jarvis import settings
        path = self.base / "jarvis.config.json"
        config = {"routing": {"brain": "chatgpt"}, "memory": {"semantic": False}}
        path.write_text(json.dumps(config))
        out = settings.write(config, path, self.base, {"routing.brain": "claude", "memory.semantic": True})
        self.assertEqual(sorted(out["restart"]), ["memory.semantic", "routing.brain"])


class CreditsFiniteTests(Tmp):
    def test_nan_and_inf_rejected(self):
        credits = Credits(self.base, CONFIG)
        from jarvis.credits import PROVIDERS
        name = PROVIDERS[0][0]
        for bad in (float("nan"), float("inf"), "-inf", "nan"):
            with self.assertRaises(ValueError):
                credits.set_manual(name, bad)
        with self.assertRaises(ValueError):
            credits.set_manual(name, 5, total=float("inf"))
        self.assertEqual(credits.set_manual(name, 5)["remaining"], 5.0)


class KbCliTests(Tmp):
    def test_lookup_error_is_a_message_not_a_traceback(self):
        import io
        from jarvis import __main__ as cli
        config = self.base / "c.json"
        config.write_text(json.dumps(CONFIG))
        err = io.StringIO()
        argv = ["jarvis", "--config", str(config), "--state", str(self.base), "kb", "page", "nope"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(cli, "kb_command", side_effect=LookupError("No page 'nope'.")), \
                mock.patch.object(sys, "stderr", err):
            self.assertEqual(cli.main(), 1)
        self.assertIn("No page 'nope'.", err.getvalue())


if __name__ == "__main__":
    unittest.main()
