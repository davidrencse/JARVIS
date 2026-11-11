"""Direct tests for cluster.validate_action: what a node will and won't execute."""

import unittest

from jarvis.cluster import validate_action
from jarvis.remote import ACTIONS


class PositiveTests(unittest.TestCase):
    def test_every_known_desktop_action_is_accepted(self):
        for action in ACTIONS:
            self.assertIsNone(validate_action("desktop", {"action": action, "arg": ""}))

    def test_desktop_arg_at_limit_and_unicode(self):
        self.assertIsNone(validate_action("desktop", {"action": "open_app", "arg": "a" * 12000}))
        self.assertIsNone(validate_action("desktop", {"action": "open_app", "arg": "ünïcödé 日本語 🙂"}))

    def test_grokbot_bounds(self):
        self.assertIsNone(validate_action("grokbot", {"text": "x"}))
        self.assertIsNone(validate_action("grokbot", {"text": "x" * 16000}))
        self.assertIsNone(validate_action("grokbot", {"text": "日本語"}))

    def test_storage_actions(self):
        for action in ("list", "resolve", "exists", "thumbnail"):
            self.assertIsNone(validate_action("storage", {"action": action, "path": "C:/x"}))

    def test_inventory_accepts_any_object(self):
        self.assertIsNone(validate_action("inventory", {}))
        self.assertIsNone(validate_action("inventory", {"anything": [1, 2]}))

    def test_extra_fields_are_ignored(self):
        self.assertIsNone(validate_action("desktop", {"action": "volume", "arg": "5", "extra": {"a": 1}}))
        self.assertIsNone(validate_action("grokbot", {"text": "hi", "command": "rm -rf /"}))


class PayloadShapeTests(unittest.TestCase):
    def test_non_object_payloads_rejected_for_every_kind(self):
        for kind in ("desktop", "grokbot", "storage", "inventory", "shell", "", None):
            for payload in (None, [], [{"action": "volume"}], "x", 0, 1, True, ("a",), set()):
                with self.subTest(kind=kind, payload=payload):
                    with self.assertRaisesRegex(ValueError, "Node payload must be an object"):
                        validate_action(kind, payload)


class KindTests(unittest.TestCase):
    def test_unknown_kinds_rejected(self):
        for kind in ("shell", "exec", "Desktop", "DESKTOP", " desktop", "desktop ", "", None, 1, ["desktop"],
                     "ask", "../desktop", "desktop\x00", "日本"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "only desktop, inventory, or Grokbot"):
                    validate_action(kind, {"action": "volume", "arg": "1", "text": "x", "path": "p"})

    def test_unhashable_kind_is_rejected_not_crashed(self):
        with self.assertRaises(ValueError):
            validate_action({"k": 1}, {})


class DesktopTests(unittest.TestCase):
    MSG = "Unknown desktop action or invalid argument"

    def bad(self, payload):
        with self.assertRaisesRegex(ValueError, self.MSG):
            validate_action("desktop", payload)

    def test_unknown_or_missing_action(self):
        for action in ("shell", "run", "Volume", "volume ", "", None, 1, ["volume"], {"a": 1}, "../volume",
                       "volume; rm -rf /", "volume\n"):
            with self.subTest(action=action):
                self.bad({"action": action, "arg": ""})
        self.bad({"arg": ""})
        self.bad({})

    def test_missing_or_non_string_arg(self):
        self.bad({"action": "volume"})
        for arg in (None, 1, 1.5, True, [], ["a"], {}, {"a": {"b": 1}}, b"bytes"):
            with self.subTest(arg=arg):
                self.bad({"action": "volume", "arg": arg})

    def test_oversized_arg(self):
        for size in (12001, 100000):
            with self.assertRaisesRegex(ValueError, "too long"):
                validate_action("desktop", {"action": "open_app", "arg": "a" * size})

    def test_length_counts_characters_not_bytes(self):
        self.assertIsNone(validate_action("desktop", {"action": "open_app", "arg": "日" * 12000}))
        with self.assertRaisesRegex(ValueError, "too long"):
            validate_action("desktop", {"action": "open_app", "arg": "日" * 12001})

    def test_shell_metacharacters_and_traversal_are_not_filtered_here(self):
        # Pinned current behavior: arguments are opaque strings; the action list is the guard.
        for arg in ("../../etc/passwd", "a; rm -rf /", "$(whoami)", "`id`", "a && b", "a|b", "\x00", "\n"):
            with self.subTest(arg=arg):
                self.assertIsNone(validate_action("desktop", {"action": "open_app", "arg": arg}))


class GrokbotTests(unittest.TestCase):
    def bad(self, payload):
        with self.assertRaisesRegex(ValueError, "Invalid Grokbot prompt"):
            validate_action("grokbot", payload)

    def test_missing_empty_wrong_type_or_oversized(self):
        self.bad({})
        self.bad({"text": ""})
        self.bad({"text": "x" * 16001})
        for text in (None, 1, [], ["hi"], {"a": 1}, True, b"hi"):
            with self.subTest(text=text):
                self.bad({"text": text})

    def test_whitespace_only_is_currently_accepted(self):
        self.assertIsNone(validate_action("grokbot", {"text": "   "}))


class StorageTests(unittest.TestCase):
    def bad(self, payload):
        with self.assertRaisesRegex(ValueError, "Invalid node storage query"):
            validate_action("storage", payload)

    def test_bad_action(self):
        for action in ("delete", "write", "List", "", None, 1, ["list"], {"a": 1}, "list;"):
            with self.subTest(action=action):
                self.bad({"action": action, "path": "/x"})
        self.bad({"path": "/x"})

    def test_bad_path(self):
        self.bad({"action": "list"})
        for path in (None, 1, [], ["/x"], {"p": 1}, b"/x"):
            with self.subTest(path=path):
                self.bad({"action": "list", "path": path})

    def test_path_content_is_not_filtered_here(self):
        # Pinned: traversal/empty/huge paths pass; Storage._local enforces roots.
        for path in ("", "../../etc/passwd", "a" * 100000, "日本語", "x\x00y"):
            with self.subTest(path=path[:20]):
                self.assertIsNone(validate_action("storage", {"action": "list", "path": path}))


if __name__ == "__main__":
    unittest.main()
