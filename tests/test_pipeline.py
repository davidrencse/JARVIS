"""The fast path (strict parser -> gates -> direct executor) and assistant's hook into it."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import assistant, gates, pipeline

DEVICES = {
    "dell": {"hostname": "WinLaptop",
             "aliases": ["windows", "windows laptop", "g15", "dell g15"]},
    "zenbook": {"hostname": "archmain",
                "aliases": ["arch", "arch linux", "archlinux", "asus", "linux",
                            "linux laptop", "zen book"]},
}
# The checks themselves are under test here, so the trusted_local shortcut is off.
CONFIG = {"devices": DEVICES, "cluster": {"main_device": "dell"},
          "rules": {"trusted_local": {"enabled": False}}}


class FakeRemote:
    """Answers "observe" itself (default: the window matched) so `respond` only sees the
    action calls; `calls` holds everything, `acts` only the non-observe calls."""

    MATCH = (True, json.dumps({"match": True, "after_ms": 250, "app": "x"}))

    def __init__(self, here="dell", respond=None, observe=None):
        self._here, self.calls = here, []
        self.respond = respond or (lambda device, action, arg: (True, f"{action} {arg} ok"))
        self.observe = observe or (lambda device, arg: self.MATCH)

    @property
    def acts(self):
        return [c for c in self.calls if c[1] != "observe"]

    @property
    def observes(self):
        return [c for c in self.calls if c[1] == "observe"]

    def here(self):
        return self._here

    def call(self, device, action, arg):
        self.calls.append((device, action, arg))
        if action == "observe":
            return self.observe(device, arg)
        return self.respond(device, action, arg)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        p = mock.patch.object(pipeline, "INFLIGHT", gates.InFlight())
        p.start()
        self.addCleanup(p.stop)

    def run_fast(self, text, remote=None, app_exists=None, config=CONFIG, **kw):
        remote = remote or FakeRemote()
        app_exists = app_exists or (lambda device, name: True)
        kw.setdefault("history", False)  # history has its own tests; never write into the repo
        return pipeline.fast_path(text, config, self.state, remote=remote,
                                  app_exists=app_exists, **kw), remote


class FallThroughTests(Base):
    def test_chat_returns_none(self):
        got, remote = self.run_fast("hello")
        self.assertIsNone(got)
        self.assertEqual(remote.calls, [])

    def test_unparseable_returns_none(self):
        got, remote = self.run_fast("flibber the quux gnarlwise")
        self.assertIsNone(got)
        self.assertEqual(remote.calls, [])

    def test_spotify_play_is_not_direct_and_touches_nothing(self):
        # Tested live: a fresh Spotify has no media session, so neither the media key
        # nor Space starts Liked Songs. It goes to Grok Bot instead (needs_agent).
        self.assertNotIn("spotify_play", pipeline.DIRECT)
        got, remote = self.run_fast("play my liked songs")
        self.assertIsNone(got)
        self.assertEqual(remote.calls, [])

    def test_unknown_origin_returns_none(self):
        got, remote = self.run_fast("open chrome", remote=FakeRemote(here="mystery"))
        self.assertIsNone(got)
        self.assertEqual(remote.calls, [])


class ExecutionTests(Base):
    def test_single_local_command(self):
        remote = FakeRemote(respond=lambda d, a, r: (True, "Opened Chrome."))
        got, remote = self.run_fast("open chrome", remote=remote)
        self.assertEqual(remote.acts, [("dell", "open_app", "Chrome")])
        self.assertEqual(got["reply"], "Opened Chrome.")
        self.assertEqual(got["outcomes"][0]["state"], "observed")
        self.assertTrue(all(c["pass"] for c in got["checks"]))

    def test_two_device_request_groups_reply(self):
        def respond(device, action, arg):
            return True, f"Opened {arg}."
        got, remote = self.run_fast("open chrome on arch and spotify on the dell",
                                    remote=FakeRemote(respond=respond))
        self.assertEqual(sorted(remote.acts),
                         [("dell", "open_app", "Spotify"), ("zenbook", "open_app", "Chrome")])
        self.assertEqual(got["reply"],
                         "On the zenbook: Opened Chrome. On the dell: Opened Spotify.")

    def test_dependency_runs_open_before_media(self):
        got, remote = self.run_fast("open spotify on arch and play")
        self.assertEqual([c[1] for c in remote.acts], ["open_app", "media"])
        self.assertEqual(remote.acts[1], ("zenbook", "media", "play"))
        order = [c[1] for c in remote.calls]
        self.assertLess(order.index("open_app"), order.index("media"))
        self.assertTrue(all(o["ok"] for o in got["outcomes"]))

    def test_dependency_skipped_when_open_fails(self):
        def respond(device, action, arg):
            return (False, "no such app") if action == "open_app" else (True, "ok")
        got, remote = self.run_fast("open spotify on arch and play",
                                    remote=FakeRemote(respond=respond))
        self.assertEqual([c[1] for c in remote.calls], ["open_app"])
        self.assertEqual([o["state"] for o in got["outcomes"]],
                         ["pre_effect_failure", "not_sent"])

    def test_failed_call_is_pre_effect_failure(self):
        got, _ = self.run_fast("open chrome", remote=FakeRemote(
            respond=lambda d, a, r: (False, "Chrome refused to start.")))
        self.assertEqual(got["outcomes"][0]["state"], "pre_effect_failure")
        self.assertFalse(got["outcomes"][0]["ok"])

    def test_timeout_text_is_uncertain(self):
        got, _ = self.run_fast("open chrome", remote=FakeRemote(
            respond=lambda d, a, r: (False, "Request Timed Out after 8s")))
        self.assertEqual(got["outcomes"][0]["state"], "uncertain")

    def test_executor_exception_does_not_crash(self):
        def boom(device, action, arg):
            raise RuntimeError("link down")
        got, _ = self.run_fast("open chrome", remote=FakeRemote(respond=boom))
        self.assertEqual(got["outcomes"][0]["state"], "pre_effect_failure")
        self.assertIn("link down", got["reply"])

    def test_open_url_with_browser_sends_json_and_no_note(self):
        got, remote = self.run_fast("open chrome, go to amazon")
        device, action, value = remote.acts[0]
        self.assertEqual(action, "open_url")
        payload = json.loads(value)
        self.assertIn("url", payload)
        self.assertEqual(payload["browser"].lower(), "chrome")
        self.assertNotIn("default browser", got["reply"])
        self.assertEqual(got["reply"], f"open_url {value} ok")

    def test_duplicate_request_is_merged(self):
        first, remote = self.run_fast("open chrome")
        second, _ = self.run_fast("open chrome", remote=remote)
        self.assertEqual(first["outcomes"][0]["state"], "observed")
        self.assertEqual(second["outcomes"][0]["state"], "merged")
        self.assertEqual(len(remote.acts), 1)
        self.assertEqual(len(remote.observes), 1)

    def test_steps_are_recorded(self):
        steps = []
        self.run_fast("open chrome", step=lambda *a, **k: steps.append(a))
        ids = [s[0] for s in steps]
        self.assertIn("parse", ids)
        self.assertIn("gate:c1", ids)
        self.assertIn("run:c1", ids)


class ObserveTests(Base):
    def obs(self, payload, ok=True):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return lambda device, arg: (ok, text)

    def test_match_is_observed_with_ms(self):
        got, remote = self.run_fast("open chrome", remote=FakeRemote(
            observe=self.obs({"match": True, "after_ms": 750, "app": "chrome"})))
        out = got["outcomes"][0]
        self.assertEqual((out["state"], out["observed_ms"], out["ok"]), ("observed", 750, True))
        self.assertEqual(len(remote.observes), 1)
        body = json.loads(remote.observes[0][2])
        self.assertEqual(body["predicate"], {"kind": "window", "app": "Chrome"})
        self.assertGreater(body["settle"], 0)

    def test_no_match_is_unconfirmed_with_suffix(self):
        got, _ = self.run_fast("open chrome", remote=FakeRemote(
            observe=self.obs({"match": False, "after_ms": 5000, "app": None})))
        out = got["outcomes"][0]
        self.assertEqual(out["state"], "unconfirmed")
        self.assertTrue(out["ok"])
        self.assertTrue(out["text"].endswith(" I couldn't confirm it on screen yet."))
        self.assertIn("I couldn't confirm", got["reply"])

    def test_garbage_observe_keeps_acknowledged(self):
        for bad in (self.obs("not json at all"), self.obs("[1, 2]"), self.obs("{}"),
                    self.obs("Can't observe: x", ok=False)):
            with self.subTest(), mock.patch.object(pipeline, "INFLIGHT", gates.InFlight()):
                got, _ = self.run_fast("open chrome", remote=FakeRemote(observe=bad))
                out = got["outcomes"][0]
                self.assertEqual(out["state"], "acknowledged")
                self.assertTrue(out["ok"])
                self.assertNotIn("observed_ms", out)

    def test_media_is_observed_with_playback_predicate(self):
        got, remote = self.run_fast("pause")
        self.assertEqual([c[1] for c in remote.acts], ["media"])
        self.assertEqual(len(remote.observes), 1)
        body = json.loads(remote.observes[0][2])
        self.assertEqual(body["predicate"], {"kind": "playback", "command": "pause"})
        self.assertEqual(got["outcomes"][0]["state"], "observed")

    def test_media_unconfirmed_when_playback_does_not_match(self):
        got, _ = self.run_fast("pause", remote=FakeRemote(
            observe=self.obs({"match": False, "after_ms": 3000, "app": None})))
        self.assertEqual(got["outcomes"][0]["state"], "unconfirmed")

    def test_observe_exception_never_changes_ok_or_retries(self):
        def boom(device, arg):
            raise RuntimeError("link down")
        got, remote = self.run_fast("open chrome", remote=FakeRemote(observe=boom))
        out = got["outcomes"][0]
        self.assertEqual((out["state"], out["ok"]), ("acknowledged", True))
        self.assertEqual(len(remote.acts), 1)
        self.assertEqual(len(remote.observes), 1)

    def test_unconfirmed_never_retries_action(self):
        _, remote = self.run_fast("open chrome", remote=FakeRemote(
            observe=self.obs({"match": False, "after_ms": 1, "app": None})))
        self.assertEqual(len(remote.acts), 1)
        self.assertEqual(len(remote.observes), 1)


class BlockedTests(Base):
    def test_missing_app_blocks_without_calling(self):
        got, remote = self.run_fast("open chrome", app_exists=lambda d, n: False)
        self.assertEqual(remote.calls, [])
        self.assertIn("isn't installed", got["reply"])
        self.assertEqual(got["outcomes"][0]["state"], "not_sent")

    def test_close_non_safe_app_needs_ok(self):
        got, remote = self.run_fast("close notepad")
        self.assertEqual(remote.calls, [])
        self.assertIn("OK", got["reply"])
        self.assertEqual(got["outcomes"][0]["state"], "not_sent")

    def test_one_blocked_command_blocks_all(self):
        got, remote = self.run_fast("open chrome on arch and spotify on the dell",
                                    app_exists=lambda d, n: d != "dell")
        self.assertEqual(remote.calls, [])
        self.assertIn("isn't installed", got["reply"])


class BeginAndDetailTests(Base):
    def logged(self, text, **kw):
        log = []

        def begin(commands):
            log.append(("begin", [c["id"] for c in commands]))

        def step(id, parent, label, detail="", state="done"):
            log.append(("step", id, detail, state))
        got, remote = self.run_fast(text, on_begin=begin, step=step, **kw)
        return log, got

    def test_on_begin_once_before_first_step(self):
        log, got = self.logged("open chrome and spotify")
        begins = [i for i, e in enumerate(log) if e[0] == "begin"]
        self.assertEqual(begins, [0])
        self.assertEqual(log[0][1], [c["id"] for c in got["commands"]])
        self.assertTrue(any(e[0] == "step" for e in log))

    def test_on_begin_not_called_when_none(self):
        for text, kw in (("hello", {}), ("flibber the quux gnarlwise", {}),
                         ("open chrome", {"remote": FakeRemote(here="mystery")})):
            with self.subTest(text=text):
                log, got = self.logged(text, **kw)
                self.assertIsNone(got)
                self.assertEqual(log, [])

    def test_step_details_carry_ms(self):
        log, _ = self.logged("open chrome")
        steps = {e[1]: e for e in log if e[0] == "step" and e[3] != "run"}
        self.assertTrue(steps["parse"][2].endswith(" ms"), steps["parse"])
        gate = steps["gate:c1"][2]
        self.assertIn("passed · ", gate)
        self.assertIn(" ms", gate)
        self.assertTrue(steps["run:c1"][2].endswith(" ms"))
        self.assertEqual(steps["run:c1"][3], "done")

    def test_run_done_before_observe_starts(self):
        log, _ = self.logged("open chrome")
        seq = [(e[1], e[3]) for e in log if e[0] == "step" and e[1] in ("run:c1", "obs:c1")]
        self.assertEqual(seq, [("run:c1", "run"), ("run:c1", "done"),
                               ("obs:c1", "run"), ("obs:c1", "done")])


class FakeMemory:
    def __init__(self, path):
        self.path, self.added = path, []

    def add(self, kind, text, **kw):
        self.added.append((kind, text, kw))
        return {"id": f"{kind}-{len(self.added)}"}

    def context(self, text):
        return []


class Sentinel(Exception):
    pass


class AskHookTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.memory = FakeMemory(Path(tmp.name) / "memory.db")
        self.config = {"devices": DEVICES, "cluster": {"enabled": False},
                       "rules": {"trusted_local": {"enabled": False}}}
        self.done = {"reply": "Opened Chrome.", "commands": [{"id": "c1"}],
                     "outcomes": [{"id": "c1", "state": "acknowledged", "ok": True}],
                     "checks": []}

    def ask(self, config=None, **kw):
        return assistant.ask("open chrome", None, self.memory, config or self.config,
                             self.memory.path.parent, **kw)

    def test_fast_result_returned_with_parser_lane(self):
        spoken, started = [], []
        with mock.patch("jarvis.pipeline.fast_path", return_value=self.done) as fp, \
                mock.patch.object(assistant, "decide", side_effect=Sentinel):
            got = self.ask(on_sentence=spoken.append, on_start=started.append)
        fp.assert_called_once()
        self.assertEqual(got["lane"], "parser")
        self.assertEqual(got["reply"], "Opened Chrome.")
        self.assertTrue(got["streamed"])
        self.assertEqual(spoken, ["Opened Chrome."])
        self.assertEqual(len(started), 1)
        kinds = [(k, t) for k, t, _ in self.memory.added]
        self.assertEqual(kinds, [("message", "open chrome"), ("reply", "Opened Chrome.")])
        self.assertEqual(got["memory"], ["message-1", "reply-2"])

    def test_none_falls_through_to_normal_route(self):
        with mock.patch("jarvis.pipeline.fast_path", return_value=None), \
                mock.patch.object(assistant, "decide", side_effect=Sentinel) as decide:
            with self.assertRaises(Sentinel):
                self.ask()
        decide.assert_called_once()
        self.assertEqual(self.memory.added, [])

    def test_fast_path_exception_falls_through(self):
        with mock.patch("jarvis.pipeline.fast_path", side_effect=RuntimeError("x")), \
                mock.patch.object(assistant, "decide", side_effect=Sentinel):
            with self.assertRaises(Sentinel):
                self.ask()

    def test_routing_fast_path_false_skips_it(self):
        config = {**self.config, "routing": {"fast_path": False}}
        with mock.patch("jarvis.pipeline.fast_path", return_value=self.done) as fp, \
                mock.patch.object(assistant, "decide", side_effect=Sentinel):
            with self.assertRaises(Sentinel):
                self.ask(config=config)
        fp.assert_not_called()

    def test_explicit_tier_or_write_skips_it(self):
        for kw in ({"tier": "simple"}, {"write": True}):
            with self.subTest(kw=kw), \
                    mock.patch("jarvis.pipeline.fast_path", return_value=self.done) as fp, \
                    mock.patch.object(assistant, "decide", side_effect=Sentinel):
                with self.assertRaises(Sentinel):
                    self.ask(**kw)
                fp.assert_not_called()


class AskBeginTests(unittest.TestCase):
    setUp = AskHookTests.setUp

    def wrapper(self, events, after=None, before=None):
        real = pipeline.fast_path

        def wrapped(text, config, state, step=None, on_begin=None, **kw):
            if before:
                raise before

            def begin(commands):
                events.append("begin")
                on_begin(commands)

            def stp(*a, **k):
                events.append("step")
                step(*a, **k)
            if after:
                begin([])
                raise after
            with mock.patch.object(pipeline, "INFLIGHT", gates.InFlight()):
                return real(text, config, state, step=stp, on_begin=begin,
                            remote=FakeRemote(), app_exists=lambda d, n: True,
                            history=False)
        return wrapped

    def ask(self, **kw):
        return assistant.ask("open chrome", None, self.memory, self.config,
                             self.memory.path.parent, **kw)

    def test_start_before_steps_and_single_inbound(self):
        events = []
        with mock.patch("jarvis.pipeline.fast_path", self.wrapper(events)), \
                mock.patch.object(assistant, "decide", side_effect=Sentinel):
            got = self.ask(on_start=lambda d: events.append("start"),
                           on_step=lambda *a: events.append("on_step"))
        self.assertEqual(events[:2], ["begin", "start"])
        self.assertLess(events.index("start"), events.index("on_step"))
        self.assertEqual(events.count("start"), 1)
        self.assertEqual([k for k, _, _ in self.memory.added], ["message", "reply"])
        self.assertEqual(got["lane"], "parser")

    def test_raise_after_begin_answers_and_skips_decide(self):
        with mock.patch("jarvis.pipeline.fast_path",
                        self.wrapper([], after=RuntimeError("kaboom"))), \
                mock.patch.object(assistant, "decide", side_effect=Sentinel):
            got = self.ask()
        self.assertTrue(got["reply"].startswith("That didn't work:"), got["reply"])
        self.assertEqual([k for k, _, _ in self.memory.added], ["message", "reply"])

    def test_raise_before_begin_falls_through(self):
        with mock.patch("jarvis.pipeline.fast_path",
                        self.wrapper([], before=RuntimeError("early"))), \
                mock.patch.object(assistant, "decide", side_effect=Sentinel) as decide:
            with self.assertRaises(Sentinel):
                self.ask()
        decide.assert_called_once()


GROK = {"personal_agents": {"grokbot": {"integration": "window_automation"}}}


class NeedsAgentTests(unittest.TestCase):
    def test_unparsed_actions_go_to_the_agent_and_talk_stays(self):
        agent = ["play my liked songs", "play that song kevin sent me",
                 "jarvis can you buy me a charger on amazon", "reply to sam's last dm",
                 "check my email", "open the thing", "open spotify on my ipad",
                 "search youtube for cats"]
        talk = ["jarvis hello?", "how are you", "what's the weather like",
                "how do i open chrome", "tell me a joke", "thanks jarvis",
                "open spotify"]  # the fast path runs this one itself
        for text in agent:
            with self.subTest(text=text):
                self.assertTrue(pipeline.needs_agent(text, CONFIG, "dell"))
        for text in talk:
            with self.subTest(text=text):
                self.assertFalse(pipeline.needs_agent(text, CONFIG, "dell"))
        self.assertFalse(pipeline.needs_agent("reply to sam", CONFIG, "phone"))

    def test_lead_word_skips_wake_word_and_politeness(self):
        self.assertEqual(pipeline.lead_word("Jarvis, could you please buy milk"), "buy")
        self.assertEqual(pipeline.lead_word(""), "")


class AgentRouteTests(unittest.TestCase):
    setUp = AskHookTests.setUp

    def ask(self, text="reply to sam's last dm", config=None, **kw):
        return assistant.ask(text, None, self.memory, config or self.config,
                             self.memory.path.parent, **kw)

    def test_unparsed_action_goes_to_grok_preflight_not_the_router(self):
        config = {**self.config, **GROK}
        with mock.patch("jarvis.pipeline.fast_path", return_value=None), \
                mock.patch("jarvis.grok_preflight.assess", side_effect=Sentinel) as assess, \
                mock.patch.object(assistant, "decide") as decide:
            with self.assertRaises(Sentinel):  # reached the Grok preflight
                self.ask(config=config)
        assess.assert_called_once()
        decide.assert_not_called()
        route = self.memory.added[0][2]["meta"]["route"]
        self.assertEqual(route["tier"], "medium")

    def test_chat_and_unconfigured_grok_use_the_normal_route(self):
        for text, config in (("how are you", {**self.config, **GROK}),
                             ("reply to sam's last dm", self.config),
                             ("reply to sam's last dm",
                              {**self.config, **GROK, "routing": {"agent_fallback": False}})):
            with self.subTest(text=text, config=config), \
                    mock.patch("jarvis.pipeline.fast_path", return_value=None), \
                    mock.patch.object(assistant, "decide", side_effect=Sentinel) as decide:
                with self.assertRaises(Sentinel):
                    self.ask(text, config=config)
                decide.assert_called_once()


class UncertainRefusalTests(Base):
    def test_node_uncertain_refusal_is_uncertain(self):
        text = ("Main PC refused the request: Node action abc is uncertain; "
                "do not automatically retry.")
        got, _ = self.run_fast("open chrome", remote=FakeRemote(
            respond=lambda d, a, r: (False, text)))
        self.assertEqual(got["outcomes"][0]["state"], "uncertain")


if __name__ == "__main__":
    unittest.main()
