"""CLI entry (__main__) and console server hardening from the audit: no models, no services."""

import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import __main__ as cli
from jarvis.ui import SKIP, Console, Handler


class ControlStopTests(unittest.TestCase):
    def run_cli(self, argv, config_text):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp, "bad.json")
            config.write_text(config_text, encoding="utf-8")
            state = Path(tmp, "state")
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["jarvis", "--config", str(config), "--state", str(state), *argv]), \
                    mock.patch("jarvis.control._release"), redirect_stdout(out):
                code = cli.main()
            return code, out.getvalue(), list(state.iterdir())

    def test_control_stop_works_with_a_broken_config(self):
        code, out, files = self.run_cli(["control", "stop"], "{ not json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])
        self.assertTrue(files)  # the stop marker was written

    def test_control_stop_works_when_this_pc_matches_no_device(self):
        config = json.dumps({"cluster": {"enabled": True, "main_device": "a", "local_device": "nope"},
                             "devices": {"a": {"hostname": "elsewhere"}}})
        code, out, _ = self.run_cli(["control", "stop"], config)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])


class TaskCommandTests(unittest.TestCase):
    def args(self, action, **kw):
        base = dict(command="task", action=action, value=["x"], due=None, to=None, priority=None,
                    every=None, note=None, state="s")
        return SimpleNamespace(**{**base, **kw})

    def run_cmd(self, args):
        tasks = mock.Mock()
        tasks.update.return_value = {"ok": True}
        with mock.patch("jarvis.vault.vault_for", return_value=object()), \
                mock.patch("jarvis.tasks.tasks_for", return_value=tasks), redirect_stdout(io.StringIO()):
            cli.task_command(args, {})
        return tasks

    def test_due_flag_is_used_and_missing_value_never_clears(self):
        self.assertEqual(self.run_cmd(self.args("due", due="friday")).update.call_args.args[:3],
                         ("x", "due", "friday"))
        self.assertEqual(self.run_cmd(self.args("priority", priority="high")).update.call_args.args[2], "high")
        for action in ("due", "scheduled", "priority"):
            with self.assertRaises(ValueError):
                self.run_cmd(self.args(action))

    def test_none_clears_explicitly(self):
        self.assertEqual(self.run_cmd(self.args("due", to="none")).update.call_args.args[2], "none")
        self.assertEqual(self.run_cmd(self.args("priority", to="none")).update.call_args.args[2], "")


class HandlerTests(unittest.TestCase):
    def test_body_reads_time_out_but_the_event_stream_does_not(self):
        import queue
        self.assertEqual(Handler.timeout, 60)
        sub = queue.Queue()
        sub.put(None)  # overflow marker: the stream ends right after the hello
        handler = object.__new__(Handler)
        handler.connection, handler.headers, handler.wfile = mock.Mock(), {}, io.BytesIO()
        handler.console = mock.Mock()
        handler.console.bus.subscribe.return_value = (sub, [])
        handler.console.bus.seq = 0
        handler.send_response = handler.send_header = handler.end_headers = mock.Mock()
        handler._events({})
        handler.connection.settimeout.assert_called_with(None)

    def test_tree_skips_tool_and_dependency_folders(self):
        for name in (".venv", ".ruff_cache", ".playwright-mcp", ".codex", ".claude", ".learnings"):
            self.assertIn(name, SKIP)


class KbLockTests(unittest.TestCase):
    def test_concurrent_first_requests_build_one_engine(self):
        console = Console.__new__(Console)
        console._kb_lock = threading.Lock()
        console.config, console.state, console.bus = {}, "s", mock.Mock()
        built, gate = [], threading.Barrier(4)

        def slow(*a, **k):
            built.append(1)
            threading.Event().wait(0.05)
            return object()

        got = []

        def go():
            gate.wait()
            got.append(console.kb())
        with mock.patch("jarvis.knowledge.kb_for", slow):
            threads = [threading.Thread(target=go) for _ in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(len(built), 1)
        self.assertEqual(len({id(k) for k in got}), 1)


if __name__ == "__main__":
    unittest.main()
