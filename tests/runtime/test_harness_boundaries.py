"""P1 boundary contracts: effect-sink inventory; two legacy gaps pinned in P1, closed in P2 prep.

Companion to docs/design/mobile-continuity/P1-BOUNDARIES.md. Fakes only: no model, service,
network or OS input. Part 1 are real contracts (a new sink fails until someone classifies it).
Parts 2-3 started as CHARACTERIZATION pins of legacy gaps (like test_harness_baseline.py) and
now assert the fixes (node console content routes denied; a started tool call blocks failover
replay). Nothing here grants authority.
"""

import unittest
from unittest import mock

from jarvis import cluster, pipeline, registry, remote
from jarvis.inference import Failover

# -- Part 1: effect-sink inventory ---------------------------------------------------------
# Every desktop action `Remote.call` / `Hub` can carry, classified once. A new action raises
# the failure below until it is placed here, which is the point: P2 admission needs the list.
READ_ONLY_ACTIONS = frozenset({
    "list_apps", "observe", "input_info", "screen", "system_status", "weather"})
HOST_EFFECT_ACTIONS = frozenset({
    "open_app", "open_url", "open_page", "focus_window", "close_app", "media", "volume",
    "scroll", "browse", "lan_mouse", "terminal", "clipboard", "computer", "spotify", "app_ui",
    "brightness", "screenshot", "workspace", "lock_screen", "reminder", "bluetooth"})
# Hub job kinds validate_action accepts (cluster.py), with who may start them.
HUB_KINDS = frozenset({"desktop", "inventory", "grokbot", "storage", "transfer", "remote",
                       "camera", "locate"})


class EffectSinkInventory(unittest.TestCase):
    def test_every_desktop_action_is_classified_exactly_once(self):
        self.assertEqual(READ_ONLY_ACTIONS & HOST_EFFECT_ACTIONS, frozenset())
        self.assertEqual(frozenset(remote.ACTIONS), READ_ONLY_ACTIONS | HOST_EFFECT_ACTIONS,
                         "new/removed remote.ACTIONS entry: classify it in this file and in "
                         "docs/design/mobile-continuity/P1-BOUNDARIES.md")

    def test_every_action_has_one_argument_name(self):
        self.assertEqual(frozenset(remote.ARG), frozenset(remote.ACTIONS))

    def test_strict_parser_executor_covers_exactly_the_direct_intents_but_spotify(self):
        direct = {n for n, i in registry.INTENTS.items() if i.executor == "direct"}
        self.assertEqual(direct - set(pipeline.DIRECT), {"spotify_play"})  # Grok Bot, on purpose
        self.assertEqual(set(pipeline.DIRECT) - direct, set())
        for intent, (action, arg) in pipeline.DIRECT.items():
            self.assertIn(action, remote.ACTIONS, intent)
            self.assertTrue(arg is None or arg in registry.INTENTS[intent].args, intent)

    def test_non_direct_intents_leave_the_direct_executor(self):
        others = {n: i.executor for n, i in registry.INTENTS.items() if i.executor != "direct"}
        self.assertEqual(others, {"agent_task": "grokbot", "answer": "brain",
                                  "vault_write": "vault", "build": "orchestrator"})

    def test_hub_accepts_only_the_declared_job_kinds(self):
        for kind in HUB_KINDS:
            try:
                cluster.validate_action(kind, {})
            except ValueError as exc:
                self.assertFalse(str(exc).startswith("Nodes execute only"), kind)
        with self.assertRaisesRegex(ValueError, "Nodes execute only"):
            cluster.validate_action("shell", {})
        self.assertLessEqual(set(cluster.IN_PROCESS_ONLY), HUB_KINDS)


# -- Part 2: private-content egress to nodes (closed in P2 prep) -----------------------------
class NodeConsoleReadsDenied(unittest.TestCase):
    """cluster.py leaves memory out of the node-readable console API on purpose. These routes
    used to leak request/reply TEXT and task evidence to a node with the `console` capability
    (P1 pinned that gap). They are now denied; this keeps them denied."""

    CONTENT_ROUTES = ("/api/history", "/api/history/commands", "/api/events", "/api/tasks/abc")
    DENIED = ("/api/memory", "/api/settings", "/api/files")

    def test_content_routes_are_denied_to_nodes(self):
        for path in self.CONTENT_ROUTES:
            for caps in ({"console"}, {"console", "ask"}):
                with self.assertRaises(PermissionError, msg=path):
                    cluster.node_api(path, False, caps)

    def test_memory_settings_and_files_stay_closed_to_nodes(self):
        for path in self.DENIED:
            with self.assertRaises(PermissionError, msg=path):
                cluster.node_api(path, False, {"console"})

    def test_nodes_cannot_post_without_the_ask_capability(self):
        with self.assertRaises(PermissionError):
            cluster.node_api("/api/ask", True, {"console"})
        self.assertIsNone(cluster.node_api("/api/ask", True, {"console", "ask"}))


# -- Part 3: brain failover and a tool call that started but never finished ------------------
LIMIT = RuntimeError("ChatGPT error: You've hit your usage limit. Try again at 11:34 PM.")


class _Lane:
    """Fake lane. `start` fires one on_tool(started) event like the real lanes, then fails."""
    def __init__(self, reply="ok", error=None, start=None, finished=()):
        self.reply, self.error, self.start, self._finished = reply, error, start, list(finished)
        self.tool_log, self.on_tool, self.asked = [], None, 0

    def ask(self, text, recall=(), on_sentence=None):
        self.asked += 1
        if self.start and self.on_tool:
            self.on_tool("call-1", self.start, None, "")      # item/started: ok is None
        self.tool_log = list(self._finished)                  # only FINISHED calls are logged
        if self.error:  # fresh each time: Failover annotates the exception it re-raises
            raise type(self.error)(*self.error.args)
        return self.reply

    def pick(self, text):
        return "act"

    def warm(self):
        pass

    def close(self):
        pass


class StartedToolCallBlocksFailover(unittest.TestCase):
    """P1 pinned a gap here: ChatGPTLane logs only FINISHED tools, so a usage limit between
    item/started and item/completed left tool_log empty and Failover re-ran the same turn on
    the Claude stand-in (same effect tools). Failover now records every started tool per
    request (not per lane) and treats it as "may have run". Not a lease or fence: a
    conservative no-replay rule. More cases: tests/intelligence/test_inference_started_tools.py."""

    def failover(self, primary):
        self.stand_in = _Lane("Claude answered.")
        return Failover(primary, lambda: self.stand_in)

    def test_finished_tool_blocks_the_stand_in(self):  # existing guarantee, kept
        lane = self.failover(_Lane(error=LIMIT, finished=[("open_app", True, "ok")]))
        with mock.patch("jarvis.chatgpt.usage_limit", return_value=""), \
                self.assertRaises(RuntimeError):
            lane.ask("open spotify")
        self.assertEqual(self.stand_in.asked, 0)

    def test_started_but_unfinished_tool_blocks_the_stand_in(self):
        """The limit hit after the tool began but before it reported: whether the effect
        happened is unknown, so the turn must not run again. The caller learns which tool
        via exc.tool_unfinished and must not claim it did or did not happen."""
        seen = []
        lane = self.failover(_Lane(error=LIMIT, start="jarvis.open_app"))
        lane.on_tool = lambda key, name, ok, text: seen.append((name, ok))
        with mock.patch("jarvis.chatgpt.usage_limit", return_value=""), \
                self.assertRaises(RuntimeError) as caught:
            lane.ask("open spotify")
        self.assertEqual(seen, [("jarvis.open_app", None)])  # the external callback still fires
        self.assertEqual(self.stand_in.asked, 0)             # ...and no replay
        self.assertEqual(caught.exception.tool_unfinished, ["jarvis.open_app"])


if __name__ == "__main__":
    unittest.main()
