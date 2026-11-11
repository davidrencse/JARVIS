"""knowledge._llm_answer launches `claude -p` under the same subscription-only contract as the
runner and the fast lane. subprocess is stubbed: no model runs, no quota is spent."""

import os
import stat
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.knowledge import KnowledgeBase

SOURCE = ("# Routines\n\nThe review routine runs daily at 02:00 and checks every page.\n\n"
          "The review routine sends a summary to David by email.\n\n"
          "Backups of the vault run nightly to the NAS drive.")


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(["claude"], returncode, stdout, stderr)


class LaunchPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name, "knowledge")
        self.kb = KnowledgeBase(self.root, today=date(2026, 9, 1))
        self.kb.ingest(text=SOURCE)
        self.hits = self.kb.retrieve("review routine")
        self.assertTrue(self.hits)
        self.good = f"It runs daily [{self.hits[0]['id']}]."

    def tearDown(self):
        for path in self.root.rglob("*"):  # raw/ is read-only on purpose
            if path.is_file():
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        self.tmp.cleanup()

    def launch(self, run, env=None):
        with patch("jarvis.knowledge.shutil.which", return_value="/usr/bin/claude"), \
                patch("jarvis.knowledge.subprocess.run", run), \
                patch.dict(os.environ, env or {}):
            return self.kb._llm_answer("when does the review run?", self.hits)

    def test_claims_only_answer_runs_without_tools_sessions_or_user_settings(self):
        run = MagicMock(return_value=completed(self.good))
        self.assertEqual(self.launch(run), self.good)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["/usr/bin/claude", "-p"])
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")  # not user/local
        self.assertEqual(argv[argv.index("--tools") + 1], "")                  # every tool off
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
            self.assertIn(flag, argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        # the prompt travels on stdin with the claims, and the old call shape is kept
        kwargs = run.call_args.kwargs
        self.assertIn(self.hits[0]["id"], kwargs["input"])
        self.assertEqual(kwargs["timeout"], 120)
        self.assertTrue(kwargs["capture_output"])

    def test_configured_model_is_still_honoured(self):
        self.kb.settings["llm_model"] = "sonnet"
        run = MagicMock(return_value=completed(self.good))
        self.launch(run)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_api_billing_variables_never_reach_the_child(self):
        run = MagicMock(return_value=completed(self.good))
        self.launch(run, {"ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_AUTH_TOKEN": "tok",
                          "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": "5",
                          "OPENAI_API_KEY": "sk-other", "CLAUDECODE": "1",
                          "CLAUDE_CODE_SESSION_ID": "parent"})
        env = run.call_args.kwargs["env"]
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                     "CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "OPENAI_API_KEY", "CLAUDECODE",
                     "CLAUDE_CODE_SESSION_ID"):
            self.assertNotIn(name, env)
        self.assertIn("PATH", env)

    def test_citations_are_still_enforced(self):
        for out in ("It runs hourly [cdeadbe].", "No citations at all.", ""):
            with self.subTest(out=out):
                self.assertIsNone(self.launch(MagicMock(return_value=completed(out))))

    def test_failures_return_none_so_ask_falls_back_to_the_extractive_answer(self):
        failures = {
            "missing binary": OSError("no such file"),
            "timeout": subprocess.TimeoutExpired("claude", 120),
            # an older CLI that rejects a flag prints usage to stderr and exits non-zero
            "unknown flag": completed("", 1, "error: unknown option '--setting-sources'"),
            "api error text": completed("API Error: 401", 1),
        }
        for name, outcome in failures.items():
            with self.subTest(name):
                run = MagicMock(side_effect=outcome) if isinstance(outcome, Exception) \
                    else MagicMock(return_value=outcome)
                self.assertIsNone(self.launch(run))
                with patch("jarvis.knowledge.shutil.which", return_value="/usr/bin/claude"), \
                        patch("jarvis.knowledge.subprocess.run", run):
                    result = self.kb.ask("review routine", file=False, llm="claude")
                self.assertEqual(result["answer"], self.kb._extractive(self.kb.retrieve("review routine")))
                self.assertTrue(result["claims"])

    def test_without_the_claude_binary_nothing_is_launched(self):
        run = MagicMock()
        with patch("jarvis.knowledge.shutil.which", return_value=None), \
                patch("jarvis.knowledge.subprocess.run", run):
            self.assertIsNone(self.kb._llm_answer("q", self.hits))
        run.assert_not_called()

    def test_llm_none_never_launches_a_model(self):
        run = MagicMock()
        with patch("jarvis.knowledge.subprocess.run", run):
            result = self.kb.ask("review routine", file=False, llm="none")
        run.assert_not_called()
        self.assertTrue(result["answer"])


if __name__ == "__main__":
    unittest.main()
