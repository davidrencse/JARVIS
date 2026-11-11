"""app_ui: accessibility-tree tool. The agent-desktop binary is never run: cli is a fake."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import app_ui, control

CFG = {"rules": {"confirm": "never", "always_ask": ["money", "messages"]}}
OK = {"ok": True, "data": {"disposition": {"delivery": "delivered_verified", "retry": "never"}}}

SNAP = {"ok": True, "data": {"snapshot_id": "s1", "tree": {
    "role": "window", "name": "W", "children": [
        {"role": "button", "name": "Save", "ref_id": "@s1:e1", "states": []},
        {"role": "group", "states": ["hidden"],
         "children": [{"role": "button", "name": "Hidden", "ref_id": "@s1:e2"}]},
        {"role": "group", "children": [
            {"role": "textfield", "name": "Notes", "ref_id": "@s1:e3", "value": "hi",
             "states": ["focused"]}]}]}}}


def found(ref, role, name):
    return {"ok": True, "data": {"matches": [
        {"ref_id": ref, "role": role, "name": name, "path": ["window:Chat", "group"],
         "value": None, "states": []}]}}


class Fake:
    """cli stand-in: records calls, answers by command from `replies`."""

    def __init__(self, **replies):
        self.calls, self.replies = [], replies

    def __call__(self, args):
        self.calls.append(list(args))
        return self.replies.get(args[0], OK)


class Asker:
    def __init__(self, answer=(True, "")):
        self.questions, self.answer = [], answer

    def __call__(self, question):
        self.questions.append(question)
        return self.answer


class AppUiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # state sits in a subfolder so no jarvis.config.json lies next to it (asks_now falls back)
        self.state = Path(self._tmp.name) / ".jarvis"
        self.state.mkdir()
        app_ui._STATE["refs"] = {}
        app_ui._STATE["times"] = []
        app_ui._STATE["typed"] = {}

    def act(self, spec, cli, config=CFG, ask=Asker):
        return app_ui.act(spec, state=self.state, config=config, cli=cli, ask=Asker() if ask is Asker else ask)

    def learn(self, ref, role, name, app="chat"):
        fake = Fake(find=found(ref, role, name))
        ok, _ = self.act({"action": "find", "app": app, "name": name}, fake)
        self.assertTrue(ok)

    def log_text(self):
        return (self.state / "logs" / "app_ui.jsonl").read_text(encoding="utf-8")

    # -- reads ------------------------------------------------------------------------------
    def test_windows_format(self):
        env = {"ok": True, "data": [
            {"app_name": "dolphin", "title": "Files", "is_focused": True, "visible": True, "accessible": True},
            {"app_name": "foo", "title": "Bar", "is_focused": False, "visible": True, "accessible": False},
            {"app_name": "gone", "title": "X", "visible": False}]}
        ok, text = self.act({"action": "windows"}, Fake(**{"list-windows": env}))
        self.assertTrue(ok)
        self.assertEqual(text.splitlines(), ['dolphin: "Files" [focused]',
                                             'foo: "Bar" (no accessibility tree)'])

    def test_windows_empty(self):
        ok, text = self.act({"action": "windows"}, Fake(**{"list-windows": {"ok": True, "data": []}}))
        self.assertTrue(ok)
        self.assertIn("No windows", text)

    def test_snapshot_filters_and_remembers(self):
        fake = Fake(snapshot=SNAP)
        ok, text = self.act({"action": "snapshot", "app": "dolphin"}, fake)
        self.assertTrue(ok)
        self.assertEqual(fake.calls, [["snapshot", "--app", "dolphin", "-i", "--compact"]])
        self.assertIn('@s1:e1 button "Save"', text)
        self.assertNotIn("Hidden", text)
        self.assertNotIn("@s1:e2", text)
        self.assertIn('@s1:e3 textfield "Notes" = "hi" [focused]', text)
        # the nameless groups are folded: the window's children are not indented further
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)  # window, Save, Notes
        self.assertTrue(lines[2].startswith("  @s1:e3"))
        self.assertEqual(app_ui._STATE["refs"]["@s1:e1"],
                         {"role": "button", "name": "Save", "app": "dolphin"})
        self.assertIn("@s1:e3", app_ui._STATE["refs"])
        self.assertNotIn("@s1:e2", app_ui._STATE["refs"])

    def test_snapshot_root_passed(self):
        fake = Fake(snapshot=SNAP)
        self.act({"action": "snapshot", "app": "dolphin", "root": "@s1:e3"}, fake)
        self.assertEqual(fake.calls[0][-2:], ["--root", "@s1:e3"])
        self.assertIn("-i", fake.calls[0])
        self.assertIn("--compact", fake.calls[0])

    def test_snapshot_needs_app(self):
        fake = Fake()
        ok, text = self.act({"action": "snapshot"}, fake)
        self.assertFalse(ok)
        self.assertEqual(fake.calls, [])

    def test_snapshot_clipped(self):
        kids = [{"role": "button", "name": f"Button number {i} " + "x" * 40, "ref_id": f"@s1:e{i}"}
                for i in range(1, 400)]
        env = {"ok": True, "data": {"tree": {"role": "window", "name": "W", "children": kids}}}
        ok, text = self.act({"action": "snapshot", "app": "a"}, Fake(snapshot=env))
        self.assertTrue(ok)
        self.assertLessEqual(len(text), app_ui.MAX_CHARS + 200)
        self.assertRegex(text, r"\.\.\. \d+ more lines cut\.")
        self.assertIn("root=<ref>", text)

    def test_spotify_snapshot_waits_through_five_second_startup(self):
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        def cli(args):
            return SNAP if clock[0] >= 5 else {"ok": True, "data": {"tree": {"role": "window", "name": "Spotify"}}}
        with mock.patch("jarvis.app_ui.time.monotonic", side_effect=lambda: clock[0]), mock.patch("jarvis.app_ui.time.sleep", side_effect=sleep):
            ok, text = self.act({"action": "snapshot", "app": "Spotify"}, cli)
        self.assertTrue(ok)
        self.assertEqual(clock[0], 5)

    def test_ready_spotify_has_no_added_delay(self):
        with mock.patch("jarvis.app_ui.time.sleep") as sleep:
            ok, text = self.act({"action": "snapshot", "app": "Spotify"}, Fake(snapshot=SNAP))
        self.assertTrue(ok)
        sleep.assert_not_called()

    def test_startup_wait_is_bounded(self):
        clock = [0.0]
        def sleep(seconds): clock[0] += seconds
        with mock.patch("jarvis.app_ui.time.monotonic", side_effect=lambda: clock[0]), mock.patch("jarvis.app_ui.time.sleep", side_effect=sleep):
            ok, text = self.act({"action": "snapshot", "app": "Spotify", "wait_ready": 2}, Fake(snapshot={"ok": True, "data": {}}))
        self.assertFalse(ok)
        self.assertEqual(clock[0], 2)
        self.assertIn("still loading", text)

    def test_snapshot_error(self):
        err = {"ok": False, "error": {"code": "APP_NOT_FOUND", "message": "nope"}}
        ok, text = self.act({"action": "snapshot", "app": "a"}, Fake(snapshot=err))
        self.assertFalse(ok)
        self.assertIn("APP_NOT_FOUND", text)

    def test_find_remembers_and_args(self):
        fake = Fake(find=found("@s2:e1", "button", "Send"))
        ok, text = self.act({"action": "find", "app": "chat", "role": "button", "name": "Send"}, fake)
        self.assertTrue(ok)
        args = fake.calls[0]
        self.assertEqual(args[:5], ["find", "--app", "chat", "--state", "hidden=false"])
        self.assertIn("--role", args)
        self.assertEqual(args[args.index("--role") + 1], "button")
        self.assertEqual(args[args.index("--name") + 1], "Send")
        self.assertIn('@s2:e1 button "Send"', text)
        self.assertEqual(app_ui._STATE["refs"]["@s2:e1"], {"role": "button", "name": "Send", "app": "chat"})

    def test_find_nothing(self):
        ok, text = self.act({"action": "find", "app": "chat", "name": "x"},
                            Fake(find={"ok": True, "data": {"matches": []}}))
        self.assertTrue(ok)
        self.assertEqual(text, "Nothing matched.")

    def test_read(self):
        fake = Fake(get={"ok": True, "data": {"value": "hello"}})
        ok, text = self.act({"action": "read", "ref": "@s1:e3"}, fake)
        self.assertEqual((ok, text), (True, "hello"))
        self.assertEqual(fake.calls, [["get", "@s1:e3", "--property", "text"]])

    def test_read_bad_ref(self):
        fake = Fake()
        ok, _ = self.act({"action": "read", "ref": "nonsense"}, fake)
        self.assertFalse(ok)
        self.assertEqual(fake.calls, [])

    # -- clicks and confirmation --------------------------------------------------------------
    def test_click_unknown_ref_refused(self):
        fake, ask = Fake(), Asker()
        ok, text = self.act({"action": "click", "ref": "@s9:e9"}, fake, ask=ask)
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Refused"))
        self.assertEqual(fake.calls, [])

    def test_click_remembered_save_no_ask(self):
        self.act({"action": "snapshot", "app": "dolphin"}, Fake(snapshot=SNAP))
        fake, ask = Fake(), Asker()
        ok, text = self.act({"action": "click", "ref": "@s1:e1"}, fake, ask=ask)
        self.assertTrue(ok, text)
        self.assertEqual(fake.calls, [["click", "@s1:e1"]])
        self.assertEqual(ask.questions, [])
        self.assertIn('Done: click button "Save"', text)

    def test_click_send_asks_and_runs_when_yes(self):
        self.learn("@s2:e1", "button", "Send")
        fake, ask = Fake(), Asker((True, ""))
        ok, _ = self.act({"action": "click", "ref": "@s2:e1"}, fake, ask=ask)
        self.assertTrue(ok)
        self.assertEqual(len(ask.questions), 1)
        self.assertIn("Send", ask.questions[0])
        self.assertEqual(fake.calls, [["click", "@s2:e1"]])

    def test_click_send_denied_not_called(self):
        self.learn("@s2:e1", "button", "Send")
        fake, ask = Fake(), Asker((False, "no"))
        ok, text = self.act({"action": "click", "ref": "@s2:e1"}, fake, ask=ask)
        self.assertEqual((ok, text), (False, "no"))
        self.assertEqual(len(ask.questions), 1)
        self.assertEqual(fake.calls, [])

    def test_click_send_on_node_refused_without_ask(self):
        self.learn("@s2:e1", "button", "Send")
        fake, ask = Fake(), Asker()
        ok, text = self.act({"action": "click", "ref": "@s2:e1"}, fake, config=None, ask=None)
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Refused"))
        self.assertIn("David", text)
        self.assertEqual(fake.calls, [])

    def test_always_ask_empty_turns_off(self):
        self.learn("@s2:e1", "button", "Send")
        cfg = {"rules": {"confirm": "never", "always_ask": []}}
        fake, ask = Fake(), Asker()
        ok, _ = self.act({"action": "click", "ref": "@s2:e1"}, fake, config=cfg, ask=ask)
        self.assertTrue(ok)
        self.assertEqual(ask.questions, [])

    def test_destructive_asks_only_when_confirm_ask(self):
        self.learn("@s3:e1", "button", "Delete")
        fake, ask = Fake(), Asker()
        ok, _ = self.act({"action": "click", "ref": "@s3:e1"}, fake, config=CFG, ask=ask)
        self.assertTrue(ok)
        self.assertEqual(ask.questions, [])
        self.assertEqual(fake.calls, [["click", "@s3:e1"]])

        fake, ask = Fake(), Asker((False, "nope"))
        ask_cfg = {"rules": {"confirm": "ask", "always_ask": ["money", "messages"]}}
        ok, text = self.act({"action": "click", "ref": "@s3:e1"}, fake, config=ask_cfg, ask=ask)
        self.assertEqual((ok, text), (False, "nope"))
        self.assertEqual(len(ask.questions), 1)
        self.assertEqual(fake.calls, [])

    def test_cli_name_mapping(self):
        self.learn("@s4:e1", "treeitem", "Folder")
        fake = Fake()
        self.act({"action": "expand", "ref": "@s4:e1"}, fake)
        self.act({"action": "scroll_to", "ref": "@s4:e1"}, fake)
        self.assertEqual(fake.calls, [["expand", "@s4:e1"], ["scroll-to", "@s4:e1"]])

    # -- typing ---------------------------------------------------------------------------------
    def test_type_passes_text(self):
        self.act({"action": "snapshot", "app": "dolphin"}, Fake(snapshot=SNAP))
        fake = Fake()
        ok, text = self.act({"action": "type", "ref": "@s1:e3", "text": "hello world"}, fake)
        self.assertTrue(ok, text)
        self.assertEqual(fake.calls, [["type", "@s1:e3", "hello world"]])

    def test_type_needs_text(self):
        self.act({"action": "snapshot", "app": "dolphin"}, Fake(snapshot=SNAP))
        fake = Fake()
        ok, _ = self.act({"action": "type", "ref": "@s1:e3"}, fake)
        self.assertFalse(ok)
        self.assertEqual(fake.calls, [])

    def test_type_card_number_refused(self):
        self.learn("@s5:e1", "textfield", "Notes")
        fake = Fake()
        ok, text = self.act({"action": "type", "ref": "@s5:e1", "text": "4111 1111 1111 1111"}, fake)
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Refused"))
        self.assertEqual(fake.calls, [])

    def test_type_six_digit_code_refused(self):
        self.learn("@s5:e1", "textfield", "Notes")
        fake = Fake()
        ok, text = self.act({"action": "type", "ref": "@s5:e1", "text": "123456"}, fake)
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Refused"))
        self.assertEqual(fake.calls, [])

    def test_type_into_password_field_refused(self):
        self.learn("@s5:e2", "textfield", "Password")
        fake = Fake()
        ok, text = self.act({"action": "type", "ref": "@s5:e2", "text": "hunter2"}, fake)
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Refused"))
        self.assertEqual(fake.calls, [])

    def test_set_value_checked_too(self):
        self.learn("@s5:e2", "textfield", "Password")
        fake = Fake()
        ok, _ = self.act({"action": "set_value", "ref": "@s5:e2", "value": "hunter2"}, fake)
        self.assertFalse(ok)
        self.assertEqual(fake.calls, [])

    def test_type_into_terminal_hard_denied(self):
        self.learn("@s6:e1", "textfield", "Input", app="konsole")
        fake = Fake()
        ok, text = self.act({"action": "type", "ref": "@s6:e1", "text": "rm -rf ~"}, fake)
        self.assertFalse(ok)
        self.assertTrue(text.startswith("Refused"))
        self.assertEqual(fake.calls, [])

    def test_type_harmless_into_terminal_allowed(self):
        self.learn("@s6:e1", "textfield", "Input", app="konsole")
        fake = Fake()
        ok, text = self.act({"action": "type", "ref": "@s6:e1", "text": "ls"}, fake)
        self.assertTrue(ok, text)

    def test_type_into_any_terminal_app_hard_denied(self):
        self.learn("@s6:e2", "textfield", "", app="kitty")  # no "terminal" in its names
        fake = Fake()
        ok, text = self.act({"action": "type", "ref": "@s6:e2", "text": "rm -rf ~"}, fake)
        self.assertFalse(ok, text)
        self.assertTrue(text.startswith("Refused"))
        self.assertEqual(fake.calls, [])

    def test_split_typing_checked_as_one(self):
        self.learn("@s7:e1", "textfield", "Input", app="foot")
        fake = Fake()
        self.assertTrue(self.act({"action": "type", "ref": "@s7:e1", "text": "rm -rf"}, fake)[0])
        ok, text = self.act({"action": "type", "ref": "@s7:e1", "text": " ~"}, fake)
        self.assertFalse(ok, text)
        self.assertTrue(text.startswith("Refused"))
        self.learn("@s8:e1", "textfield", "Input", app="foot")  # a new snapshot, same element
        self.assertFalse(self.act({"action": "type", "ref": "@s8:e1", "text": " ~"}, fake)[0])
        self.learn("@s7:e2", "textfield", "Card", app="shop")
        self.assertTrue(self.act({"action": "type", "ref": "@s7:e2", "text": "4111 1111 11"}, fake)[0])
        self.assertFalse(self.act({"action": "type", "ref": "@s7:e2", "text": "11 1111"}, fake)[0])
        self.learn("@s7:e3", "textfield", "Code", app="shop")
        self.assertTrue(self.act({"action": "type", "ref": "@s7:e3", "text": "12"}, fake)[0])
        self.assertFalse(self.act({"action": "type", "ref": "@s7:e3", "text": "34"}, fake)[0])
        # set_value replaces what was there, and a newline submits: both start over
        self.assertTrue(self.act({"action": "set_value", "ref": "@s7:e1", "value": "echo"}, fake)[0])
        self.assertTrue(self.act({"action": "type", "ref": "@s7:e1", "text": " hi\n"}, fake)[0])
        self.assertTrue(self.act({"action": "type", "ref": "@s7:e1", "text": "ls ~"}, fake)[0])

    def test_prose_into_ordinary_apps_allowed(self):
        self.learn("@s9:e1", "textfield", "Body", app="chat")
        fake = Fake()
        for i, line in enumerate(["Meeting notes: copy my key from ~/.ssh to the new laptop\n",
                                  "function greet() { return 1; }\n",
                                  "git reset --hard is risky, remember\n", "cd -\n"]):
            ok, text = self.act({"action": "type", "ref": "@s9:e1", "text": line}, fake)
            self.assertTrue(ok, (line, text))

    def test_terminal_emulators_hard_denied(self):
        for i, app in enumerate(["ghostty", "alacritty", "wezterm", "gnome-terminal", "xterm", "tilix",
                                 "terminator", "WindowsTerminal", "Windows Terminal", "powershell",
                                 "cmd", "foot"]):
            self.learn(f"@t{i}:e1", "textfield", "x", app=app)
            ok, text = self.act({"action": "type", "ref": f"@t{i}:e1", "text": "rm -rf ~"}, Fake())
            self.assertFalse(ok, app)

    def test_terminal_overlong_line_refused(self):
        # audit 4: a line past TYPED_MAX was cut to its tail, so `rm -rf /tmp/q` + spaces + ` ~` passed.
        self.learn("@u1:e1", "textfield", "Input", app="konsole")
        fake = Fake()
        self.assertTrue(self.act({"action": "type", "ref": "@u1:e1", "text": "rm -rf /tmp/q"}, fake)[0])
        ok, text = self.act({"action": "type", "ref": "@u1:e1", "text": " " * app_ui.TYPED_MAX}, fake)
        self.assertFalse(ok, text)
        self.assertTrue(text.startswith("Refused"))

    def test_terminal_line_does_not_expire(self):
        self.learn("@u2:e1", "textfield", "Input", app="foot")
        fake = Fake()
        self.assertTrue(self.act({"action": "type", "ref": "@u2:e1", "text": "rm -rf /tmp/q"}, fake)[0])
        with mock.patch("jarvis.app_ui.time.time", return_value=time.time() + app_ui.TYPED_TTL + 5):
            self.assertFalse(self.act({"action": "type", "ref": "@u2:e1", "text": " ~"}, fake)[0])

    def test_terminal_check_uses_config_and_home(self):
        # audit 4: hard_deny got {} and cwd /tmp, so the vault was unknown.
        cfg = {**CFG, "vault": {"path": {"linux": "~/Documents/obsidian-vault"}}}
        for i, text in enumerate(("rm -rf ~/Documents/obsidian-vault", "cat ~/.grok/auth.json")):
            self.learn(f"@u3:e{i}", "textfield", "Input", app="konsole")
            ok, said = self.act({"action": "type", "ref": f"@u3:e{i}", "text": text}, Fake(), config=cfg)
            self.assertFalse(ok, text)
        self.learn("@u4:e1", "textfield", "Input", app="konsole")  # a node: its own config
        with mock.patch("jarvis.gates.local_config", return_value=cfg):
            ok, said = self.act({"action": "type", "ref": "@u4:e1", "text": "rm -rf ~/Documents/obsidian-vault"},
                                Fake(), config=None)
        self.assertFalse(ok, said)

    def test_typed_buffer_expires(self):
        self.learn("@s10:e1", "textfield", "Code", app="shop")
        fake = Fake()
        self.assertTrue(self.act({"action": "type", "ref": "@s10:e1", "text": "12"}, fake)[0])
        with mock.patch("jarvis.app_ui.time.time", return_value=time.time() + app_ui.TYPED_TTL + 5):
            self.assertTrue(self.act({"action": "type", "ref": "@s10:e1", "text": "34"}, fake)[0])

    def test_type_null_text_refused_not_typed_as_none(self):
        self.act({"action": "snapshot", "app": "dolphin"}, Fake(snapshot=SNAP))
        fake = Fake()
        ok, text = self.act({"action": "type", "ref": "@s1:e3", "text": None}, fake)
        self.assertFalse(ok, text)
        self.assertIn("needs text", text)
        ok, text = self.act({"action": "type", "ref": "@s1:e3", "value": "x"}, fake)
        self.assertFalse(ok, text)
        self.assertIn("needs text", text)
        self.assertEqual(fake.calls, [])

    def test_device_here_on_main_reaches_confirm_queue(self):
        from jarvis import remote
        cfg = {**CFG, "devices": {"arch": {}, "dell": {}}, "cluster": {"main_device": "arch"}}
        r = remote.Remote(cfg, self.state)
        seen = {}

        def fake_run(arg, config=None, state=None):
            seen.update(config=config, state=state)
            return True, "ok"
        with mock.patch("jarvis.topology.enabled", return_value=True), \
                mock.patch("jarvis.topology.identity", return_value=("arch", "arch")), \
                mock.patch("jarvis.app_ui.run", fake_run):
            self.assertEqual(r.call("arch", "app_ui", "{}"), (True, "ok"))
        self.assertIs(seen["config"], cfg)  # the main can ask David, so it gets the config
        with mock.patch("jarvis.topology.enabled", return_value=True), \
                mock.patch("jarvis.topology.identity", return_value=("dell", "arch")), \
                mock.patch("jarvis.app_ui.run", fake_run):
            r.call("dell", "app_ui", "{}")
        self.assertIsNone(seen["config"])  # a node still refuses and leaves it to David
        # and run() hands the config on, so a Send click is asked rather than refused
        self.learn("@s2:e1", "button", "Send")
        with mock.patch("jarvis.browser_gate.ask", return_value=(False, "David said no")) as asked, \
                mock.patch("jarvis.app_ui._cli", Fake()):
            ok, text = app_ui.run(json.dumps({"action": "click", "ref": "@s2:e1"}),
                                  config=CFG, state=self.state)
        self.assertEqual((ok, text), (False, "David said no"))
        asked.assert_called_once()

    # -- result notes ------------------------------------------------------------------------------
    def test_unsafe_error_may_have_landed(self):
        self.learn("@s1:e1", "button", "Save")
        err = {"ok": False, "error": {"code": "TIMEOUT", "message": "slow",
                                      "disposition": {"delivery": "unknown", "retry": "never"}}}
        ok, text = self.act({"action": "click", "ref": "@s1:e1"}, Fake(click=err))
        self.assertFalse(ok)
        self.assertIn("TIMEOUT", text)
        self.assertIn("may have landed", text)

    def test_safe_error_no_landed_note(self):
        self.learn("@s1:e1", "button", "Save")
        err = {"ok": False, "error": {"code": "STALE_REF", "message": "gone",
                                      "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
        ok, text = self.act({"action": "click", "ref": "@s1:e1"}, Fake(click=err))
        self.assertFalse(ok)
        self.assertIn("STALE_REF", text)
        self.assertNotIn("may have landed", text)

    def test_unverified_note(self):
        self.learn("@s1:e1", "button", "Save")
        env = {"ok": True, "data": {"disposition": {"delivery": "delivered_unverified", "retry": "never"}}}
        ok, text = self.act({"action": "click", "ref": "@s1:e1"}, Fake(click=env))
        self.assertTrue(ok)
        self.assertIn("Not confirmed", text)

    def test_verified_has_no_note(self):
        self.learn("@s1:e1", "button", "Save")
        ok, text = self.act({"action": "click", "ref": "@s1:e1"}, Fake())
        self.assertTrue(ok)
        self.assertNotIn("Not confirmed", text)

    # -- stop switch, rate limit, log ------------------------------------------------------------
    def test_stop_file_refuses_even_reads(self):
        control.stop(self.state)
        fake = Fake(**{"list-windows": {"ok": True, "data": []}})
        for spec in ({"action": "windows"}, {"action": "snapshot", "app": "a"},
                     {"action": "click", "ref": "@s1:e1"}):
            ok, text = self.act(spec, fake)
            self.assertFalse(ok)
            self.assertTrue(text.startswith("Refused"), text)
        self.assertEqual(fake.calls, [])

    def test_expired_stop_file_allows(self):
        (self.state / control.STOP_FILE).write_text(str(time.time() - 5), encoding="utf-8")
        ok, _ = self.act({"action": "windows"}, Fake(**{"list-windows": {"ok": True, "data": []}}))
        self.assertTrue(ok)

    def test_rate_limit(self):
        self.learn("@s1:e1", "button", "Save")
        fake = Fake()
        with mock.patch.object(app_ui, "MAX_ACTIONS", 2):
            results = [self.act({"action": "click", "ref": "@s1:e1"}, fake) for _ in range(3)]
        self.assertEqual([r[0] for r in results], [True, True, False])
        self.assertIn("2 app actions", results[2][1])
        self.assertEqual(len(fake.calls), 2)

    def test_reads_not_rate_limited(self):
        fake = Fake(**{"list-windows": {"ok": True, "data": []}})
        with mock.patch.object(app_ui, "MAX_ACTIONS", 1):
            for _ in range(3):
                self.assertTrue(self.act({"action": "windows"}, fake)[0])

    def test_log_written_without_typed_text(self):
        self.act({"action": "snapshot", "app": "dolphin"}, Fake(snapshot=SNAP))
        secret = "zebra-quartz-pineapple"
        ok, _ = self.act({"action": "type", "ref": "@s1:e3", "text": secret}, Fake())
        self.assertTrue(ok)
        self.act({"action": "type", "ref": "@s1:e3", "text": "4111 1111 1111 1111"}, Fake())
        raw = self.log_text()
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertTrue(any(r.get("action") == "type" and r.get("chars") == len(secret) for r in rows))
        self.assertNotIn(secret, raw)
        self.assertNotIn("4111", raw)

    # -- entry points ---------------------------------------------------------------------------------
    def test_run_bad_json(self):
        self.assertEqual(app_ui.run("not json")[0], False)

    def test_run_non_object(self):
        self.assertEqual(app_ui.run("[1, 2]")[0], False)

    def test_unknown_action(self):
        fake = Fake()
        ok, text = self.act({"action": "explode"}, fake)
        self.assertFalse(ok)
        self.assertIn("action must be one of", text)
        self.assertEqual(fake.calls, [])

    def test_missing_action(self):
        self.assertFalse(app_ui.act({}, state=self.state, cli=Fake())[0])

    def test_wired_into_tools_and_remote(self):
        from jarvis import remote
        from jarvis.actions import DesktopTools
        self.assertIn("app_ui", [s.get("name") for s in DesktopTools().specs()])
        self.assertIn("app_ui", remote.ACTIONS)


if __name__ == "__main__":
    unittest.main()
