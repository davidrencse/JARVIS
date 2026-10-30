"""jarvis/runner.py: plan/command construction, env hygiene, timeouts, process-tree kill,
exit codes, output capture. Real behaviour is exercised with `sys.executable -c ...`
stand-ins; the real codex/claude CLIs are never started (the audit hook forbids it)."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis import runner
from jarvis.runner import final_message, make_plan, run_task, stop_process, workspace
from jarvis.store import Store

CONFIG = {"workspace_roots": ["."], "models": {
    "codex": {"mid": {"model": "gpt-x", "effort": "medium"}},
    "claude": {"mid": {"model": "sonnet", "effort": "high"}}}}
SPEC = {"objective": "do the thing"}


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        (self.base / "work").mkdir()
        self.store = Store(self.base / "state")

    def which(self, found=sys.executable):
        patcher = mock.patch("jarvis.runner.shutil.which", return_value=found)
        patcher.start()
        self.addCleanup(patcher.stop)

    def standin(self, code, provider="claude", model="m"):
        """Make make_plan return a harmless python child that runs `code`."""
        plan = {"provider": provider, "workspace": str(self.base / "work"), "mode": "read-only",
                "power": "mid", "model": model, "effort": "default",
                "argv": [sys.executable, "-c", code] + (["-"] if provider == "codex" else []),
                "prompt": "PROMPT-TEXT"}
        patcher = mock.patch("jarvis.runner.make_plan", return_value=plan)
        patcher.start()
        self.addCleanup(patcher.stop)
        return plan

    def submit(self, spec=SPEC):
        return self.store.add(dict(spec))["id"]

    def run_it(self, timeout=30, config=CONFIG):
        return run_task(self.store, self.submit(), config, self.base, timeout=timeout)


class PlanTests(Base):
    def test_codex_command(self):
        self.which("/usr/bin/codex")
        plan = make_plan(SPEC, CONFIG, self.base)
        argv = plan["argv"]
        self.assertEqual(argv[:3], ["/usr/bin/codex", "exec", "--json"])
        self.assertIn('forced_login_method="chatgpt"', argv)  # never API-key billing
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-x")
        self.assertIn('model_reasoning_effort="medium"', argv)
        self.assertEqual(argv[-1], "-")
        self.assertEqual(argv[argv.index("-C") + 1], str(self.base))

    def test_claude_command(self):
        self.which("/usr/bin/claude")
        plan = make_plan({**SPEC, "provider": "claude"}, CONFIG, self.base)
        argv = plan["argv"]
        self.assertEqual(argv[0], "/usr/bin/claude")
        self.assertIn("--print", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertNotIn("--dangerously-skip-permissions", argv)

    def test_workspace_write_modes(self):
        self.which("/usr/bin/claude")
        plan = make_plan({**SPEC, "provider": "claude", "mode": "workspace-write"}, CONFIG, self.base)
        self.assertEqual(plan["argv"][plan["argv"].index("--permission-mode") + 1], "acceptEdits")
        plan = make_plan({**SPEC, "mode": "workspace-write"}, CONFIG, self.base)
        self.assertEqual(plan["argv"][plan["argv"].index("--sandbox") + 1], "workspace-write")
        self.assertIn("Workspace-write", plan["prompt"])

    def test_read_only_roles_cannot_write(self):
        self.which("/usr/bin/codex")
        for role in ("reviewer", "researcher", "assistant"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                make_plan({**SPEC, "role": role, "mode": "workspace-write"}, CONFIG, self.base)

    def test_assistant_gets_lean_flags(self):
        self.which("/usr/bin/claude")
        argv = make_plan({**SPEC, "provider": "claude", "role": "assistant"}, CONFIG, self.base)["argv"]
        self.assertIn("--strict-mcp-config", argv)

    def test_prompt_carries_read_only_boundary_and_objective(self):
        self.which("/usr/bin/codex")
        prompt = make_plan(SPEC, CONFIG, self.base)["prompt"]
        self.assertTrue(prompt.startswith("Read-only execution"))
        self.assertIn("do the thing", prompt)

    def test_missing_binary(self):
        self.which(None)
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "not installed"):
                make_plan({**SPEC, "provider": provider}, CONFIG, self.base)

    def test_windows_shell_shims_rejected(self):
        for shim in ("C:/x/codex.cmd", "C:/x/codex.BAT"):
            self.which(shim)
            with self.subTest(shim=shim), self.assertRaisesRegex(ValueError, "native"):
                make_plan(SPEC, CONFIG, self.base)

    def test_invalid_fields(self):
        self.which("/usr/bin/codex")
        for bad in ({"provider": "gemini"}, {"mode": "danger-full-access"}, {"mode": "rw"},
                    {"power": "ultra"}, {"objective": ""}, {"provider": ["codex"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                make_plan({**SPEC, **bad}, CONFIG, self.base)

    def test_non_mapping_task(self):
        with self.assertRaises(ValueError):
            make_plan("do it", CONFIG, self.base)

    def test_missing_model_tier_uses_defaults(self):
        self.which("/usr/bin/codex")
        plan = make_plan(SPEC, {"workspace_roots": ["."]}, self.base)
        self.assertEqual(plan["model"], "default")
        self.assertNotIn("-m", plan["argv"])

    def test_workspace_must_be_under_roots(self):
        outside = self.base.parent
        for ws in ("..", str(outside), "missing-dir", "../.."):
            with self.subTest(ws=ws), self.assertRaises(ValueError):
                workspace({"workspace": ws}, CONFIG, self.base)
        self.assertEqual(workspace({"workspace": "work"}, CONFIG, self.base), self.base / "work")
        self.assertEqual(workspace({}, CONFIG, self.base), self.base)

    def test_workspace_non_string_and_no_roots(self):
        for ws in (5, ["a"], None, {"a": 1}):
            with self.subTest(ws=ws), self.assertRaises(ValueError):
                workspace({"workspace": ws}, CONFIG, self.base)
        with self.assertRaises(ValueError):
            workspace({"workspace": "work"}, {}, self.base)

    def test_workspace_symlink_escape_rejected(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        try:
            os.symlink(outside.name, self.base / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(ValueError):
            workspace({"workspace": "link"}, CONFIG, self.base)

    def test_workspace_sibling_prefix_not_accepted(self):
        (self.base / "workshop").mkdir()
        cfg = {"workspace_roots": ["work"]}
        with self.assertRaises(ValueError):
            workspace({"workspace": "workshop"}, cfg, self.base)


class EnvTests(Base):
    def test_popen_gets_subscription_env_without_api_keys(self):
        self.which("/usr/bin/codex")
        fake = mock.MagicMock(returncode=0)
        fake.communicate.return_value = (None, None)
        leaky = {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k", "CODEX_API_KEY": "k",
                 "ANTHROPIC_AUTH_TOKEN": "k", "ANTHROPIC_BASE_URL": "http://x",
                 "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s",
                 "CLAUDE_CODE_OAUTH_TOKEN": "keep", "CLAUDE_CONFIG_DIR": "/keep", "PATH": "/bin"}
        with mock.patch.dict(os.environ, leaky), \
                mock.patch("jarvis.runner.subprocess.Popen", return_value=fake) as popen:
            self.assertEqual(self.run_it()["state"], "finished")
        kwargs = popen.call_args.kwargs
        env = kwargs["env"]
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                     "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
            self.assertNotIn(name, env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "keep")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/keep")
        self.assertEqual(env["PATH"], "/bin")
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["cwd"], str(self.base))
        self.assertTrue(kwargs["start_new_session"])
        fake.communicate.assert_called_once()
        self.assertEqual(fake.communicate.call_args.args[0].decode()[:9], "Read-only")

    def test_lowercase_api_key_var_names_also_stripped(self):
        from jarvis.fastlane import subscription_env
        with mock.patch.dict(os.environ, {"anthropic_api_key": "k"}):
            self.assertNotIn("anthropic_api_key", subscription_env())

    def test_codex_output_file_flag_inserted_before_stdin_dash(self):
        self.which("/usr/bin/codex")
        fake = mock.MagicMock(returncode=0)
        fake.communicate.return_value = (None, None)
        with mock.patch("jarvis.runner.subprocess.Popen", return_value=fake) as popen:
            result = self.run_it()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-1], "-")
        self.assertEqual(argv[-3], "-o")
        self.assertTrue(argv[-2].endswith("last-message.md"))
        self.assertIn(result["task_id"], argv[-2])

    def test_real_child_sees_scrubbed_env(self):
        code = ("import os,json;print(json.dumps(sorted(k for k in os.environ if "
                "k.startswith(('ANTHROPIC','OPENAI','CLAUDE','CODEX')))))")
        self.standin(code)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k",
                                          "CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_CONFIG_DIR": "/c"}):
            result = self.run_it()
        out = (Path(result["artifacts"]) / "stdout.jsonl").read_text()
        self.assertEqual(json.loads(out), ["CLAUDE_CONFIG_DIR"])


class RunTests(Base):
    def test_success_captures_output_prompt_and_answer(self):
        code = ("import sys, json\np = sys.stdin.read()\n"
                "print(json.dumps({'type':'result','result':'echo:'+p}))\n"
                "sys.stderr.write('warn-line\\n')")
        self.standin(code)
        result = self.run_it()
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["answer"], "echo:PROMPT-TEXT")
        d = Path(result["artifacts"])
        self.assertEqual((d / "prompt.md").read_text(), "PROMPT-TEXT")
        self.assertEqual((d / "stderr.log").read_text(), "warn-line\n")
        self.assertEqual(self.store.get(result["task_id"])["state"], "finished")

    def test_nonzero_exit_is_failed_with_code(self):
        self.standin("import sys; print('boom'); sys.exit(3)")
        result = self.run_it()
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["exit_code"], 3)
        self.assertEqual(self.store.get(result["task_id"])["state"], "failed")
        self.assertIn("not acceptance", result["note"])

    def test_invalid_utf8_output_does_not_break_answer_parsing(self):
        code = ("import sys\nsys.stdout.buffer.write(b'\\xff\\xfe garbage\\n"
                "{\"type\":\"result\",\"result\":\"ok\"}\\n')")
        self.standin(code)
        self.assertEqual(self.run_it()["answer"], "ok")

    def test_no_output_gives_none_answer(self):
        self.standin("pass")
        result = self.run_it()
        self.assertEqual(result["state"], "finished")
        self.assertIsNone(result["answer"])

    def test_codex_last_message_file_is_the_answer(self):
        code = ("import sys,pathlib\nargs=sys.argv\n"
                "pathlib.Path(args[args.index('-o')+1]).write_text(' final answer \\n')")
        plan = self.standin("", provider="codex")
        plan["argv"] = [sys.executable, "-c", code, "-"]
        result = self.run_it()
        self.assertEqual(result["answer"], "final answer")

    def test_timeout_kills_process_and_marks_timed_out(self):
        self.standin("import time; time.sleep(60)")
        start = time.time()
        result = self.run_it(timeout=1)
        self.assertLess(time.time() - start, 20)
        self.assertEqual(result["state"], "timed_out")
        self.assertEqual(self.store.get(result["task_id"])["state"], "timed_out")

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_timeout_kills_whole_process_tree(self):
        pidfile = self.base / "grandchild.pid"
        code = ("import subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                f"open({str(pidfile)!r},'w').write(str(p.pid))\n"
                "time.sleep(60)")
        self.standin(code)
        self.assertEqual(self.run_it(timeout=1)["state"], "timed_out")
        pid = int(pidfile.read_text())
        self.addCleanup(lambda: os.path.exists(f"/proc/{pid}") and os.kill(pid, signal.SIGKILL))
        for _ in range(50):
            try:
                os.kill(pid, 0)
                state = Path(f"/proc/{pid}/stat").read_text().split()[2] if os.path.exists(f"/proc/{pid}/stat") else "gone"
                if state in ("Z", "gone"):
                    break
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("grandchild survived the timeout")

    def test_timeout_bounds(self):
        task_id = self.submit()
        for bad in (0, -1, 3601, 0.5):
            with self.subTest(timeout=bad), self.assertRaisesRegex(ValueError, "timeout"):
                run_task(self.store, task_id, CONFIG, self.base, timeout=bad)
        self.assertEqual(self.store.get(task_id)["state"], "queued")

    def test_missing_binary_on_plan_leaves_task_queued(self):
        self.which(None)
        task_id = self.submit()
        with self.assertRaisesRegex(ValueError, "not installed"):
            run_task(self.store, task_id, CONFIG, self.base)
        self.assertEqual(self.store.get(task_id)["state"], "queued")

    def test_binary_vanished_at_spawn_marks_failed_and_raises(self):
        plan = self.standin("pass")
        plan["argv"] = [str(self.base / "no-such-binary")]
        task_id = self.submit()
        with self.assertRaises(FileNotFoundError):
            run_task(self.store, task_id, CONFIG, self.base)
        self.assertEqual(self.store.get(task_id)["state"], "failed")

    def test_unknown_task_id(self):
        with self.assertRaises(ValueError):
            run_task(self.store, "nope", CONFIG, self.base)

    def test_node_refuses_to_run_models(self):
        cfg = {**CONFIG, "cluster": {"enabled": True, "main_device": "a", "local_device": "b"},
               "devices": {"a": {}, "b": {}}}
        with self.assertRaisesRegex(ValueError, "main PC"):
            run_task(self.store, "unused", cfg, self.base)

    def test_task_cannot_run_twice(self):
        self.standin("pass")
        task_id = self.submit()
        run_task(self.store, task_id, CONFIG, self.base)
        with self.assertRaises(ValueError):
            run_task(self.store, task_id, CONFIG, self.base)

    def test_full_lane_blocks_second_run_and_leaves_it_queued(self):
        self.standin("pass")
        busy = self.submit()
        self.store.transition(busy, "queued", "running")
        task_id = self.submit()
        with mock.patch.object(Store, "reap_stale", return_value=0), \
                self.assertRaisesRegex(ValueError, "busy"):
            run_task(self.store, task_id, CONFIG, self.base)
        self.assertEqual(self.store.get(task_id)["state"], "queued")
        self.assertFalse((self.store.directory / "runs" / task_id).exists())

    def test_extra_lane_allows_concurrent_claim(self):
        self.standin("pass")
        store = Store(self.base / "state2", lanes=2)
        busy = store.add(dict(SPEC))["id"]
        store.transition(busy, "queued", "running")
        task_id = store.add(dict(SPEC))["id"]
        self.assertEqual(run_task(store, task_id, CONFIG, self.base)["state"], "finished")

    def test_keyboard_interrupt_stops_child_and_marks_interrupted(self):
        self.standin("import time; time.sleep(60)")
        procs = []
        real_popen = subprocess.Popen

        def spy(*a, **k):
            p = real_popen(*a, **k)
            procs.append(p)
            p.communicate = mock.Mock(side_effect=KeyboardInterrupt)
            return p
        with mock.patch("jarvis.runner.subprocess.Popen", side_effect=spy):
            result = self.run_it()
        self.assertEqual(result["state"], "interrupted")
        self.assertIsNotNone(procs[0].poll())
        procs[0].stdin.close()

    def test_unexpected_exception_cleans_up_and_marks_failed(self):
        self.standin("pass")
        fake = mock.MagicMock()
        fake.poll.return_value = None
        fake.pid = 99999999
        fake.communicate.side_effect = RuntimeError("pipe broke")
        task_id = self.submit()
        with mock.patch("jarvis.runner.subprocess.Popen", return_value=fake), \
                mock.patch("jarvis.runner.os.killpg") as killpg, \
                self.assertRaises(RuntimeError):
            run_task(self.store, task_id, CONFIG, self.base)
        killpg.assert_called_once_with(99999999, signal.SIGKILL)
        self.assertEqual(self.store.get(task_id)["state"], "failed")

    def test_run_directory_cannot_be_reused(self):
        self.standin("pass")
        task_id = self.submit()
        (self.store.directory / "runs" / task_id).mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            run_task(self.store, task_id, CONFIG, self.base)
        self.assertEqual(self.store.get(task_id)["state"], "failed")

    def test_usage_recorded_inside_meter(self):
        from jarvis import usage
        code = ("import json;print(json.dumps({'type':'result','result':'r','total_cost_usd':0.5,"
                "'usage':{'input_tokens':10,'output_tokens':5}}))")
        self.standin(code)
        with usage.meter() as m:
            self.run_it()
        self.assertEqual(len(m.calls), 1)
        self.assertEqual(m.calls[0]["usd"], 0.5)


class AuditHookTests(Base):
    def test_runner_cannot_spawn_a_real_claude_under_tests(self):
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        claude = fake_bin / "claude"
        claude.write_text("#!/bin/sh\necho SPAWNED > %s\n" % (self.base / "spawned"))
        claude.chmod(0o755)
        self.which(str(claude))
        task_id = self.submit({**SPEC, "provider": "claude"})
        with self.assertRaises(PermissionError):
            run_task(self.store, task_id, CONFIG, self.base)
        self.assertFalse((self.base / "spawned").exists())
        self.assertEqual(self.store.get(task_id)["state"], "failed")

    def test_runner_cannot_spawn_a_real_codex_under_tests(self):
        self.which("/nonexistent/codex")
        task_id = self.submit()
        with self.assertRaises(PermissionError):
            run_task(self.store, task_id, CONFIG, self.base)


class StopProcessTests(unittest.TestCase):
    def test_already_exited_process_is_left_alone(self):
        p = mock.MagicMock()
        p.poll.return_value = 0
        with mock.patch("jarvis.runner.os.killpg") as killpg:
            stop_process(p)
        killpg.assert_not_called()
        p.kill.assert_not_called()

    def test_posix_kills_process_group_then_waits(self):
        p = mock.MagicMock(pid=4242)
        p.poll.side_effect = [None, 0]
        with mock.patch("jarvis.runner.os.killpg") as killpg:
            stop_process(p)
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        p.kill.assert_not_called()
        p.wait.assert_called_once_with(timeout=10)

    def test_group_already_gone_falls_back_cleanly(self):
        p = mock.MagicMock(pid=4242)
        p.poll.side_effect = [None, None]
        with mock.patch("jarvis.runner.os.killpg", side_effect=ProcessLookupError):
            stop_process(p)
        p.kill.assert_called_once()
        p.wait.assert_called_once()

    def test_windows_uses_taskkill_tree(self):
        p = mock.MagicMock(pid=77)
        p.poll.side_effect = [None, None]
        with mock.patch.object(runner, "os", SimpleNamespace(name="nt")), \
                mock.patch("jarvis.runner.subprocess.run") as run:
            stop_process(p)
        args = run.call_args.args[0]
        self.assertEqual(args, ["taskkill", "/PID", "77", "/T", "/F"])
        self.assertIs(run.call_args.kwargs["shell"], False)
        p.kill.assert_called_once()

    def test_wait_timeout_propagates(self):
        p = mock.MagicMock(pid=1)
        p.poll.side_effect = [None, None]
        p.wait.side_effect = subprocess.TimeoutExpired("x", 10)
        with mock.patch("jarvis.runner.os.killpg"), self.assertRaises(subprocess.TimeoutExpired):
            stop_process(p)


class FinalMessageTests(Base):
    def test_codex_prefers_last_message_file_then_stream(self):
        (self.base / "stdout.jsonl").write_text(json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": " streamed "}}) + "\n")
        self.assertEqual(final_message("codex", self.base), "streamed")
        (self.base / "last-message.md").write_text("from file\n")
        self.assertEqual(final_message("codex", self.base), "from file")

    def test_blank_last_message_file_falls_back_to_stream(self):
        (self.base / "stdout.jsonl").write_text(json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "streamed"}}) + "\n")
        (self.base / "last-message.md").write_text("   \n")
        self.assertEqual(final_message("codex", self.base), "streamed")

    def test_malformed_events_ignored(self):
        lines = ["", "[]", "5", "null", '{"type":"result","result":5}', '{"type":"item.completed","item":"x"}',
                 '{"type":"result","result":"first"}', "garbage", '{"type":"result","result":"  last  "}']
        (self.base / "stdout.jsonl").write_text("\n".join(lines))
        self.assertEqual(final_message("claude", self.base), "last")

    def test_missing_dir(self):
        self.assertIsNone(final_message("claude", self.base / "nope"))

    def test_whitespace_result_is_none(self):
        (self.base / "stdout.jsonl").write_text('{"type":"result","result":"   "}')
        self.assertIsNone(final_message("claude", self.base))


if __name__ == "__main__":
    unittest.main()
