import io
import urllib.error
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from jarvis.adapters import call_api
from jarvis.assistant import ask, wake_word
from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator, extract_json, validate_plan
from jarvis.router import classify, pick_adapter, specialists
from jarvis.runner import final_message, make_plan
from jarvis.speech import Voice, spoken
from jarvis.store import Store

try:  # numpy is an optional extra ([voice]); these tests exercise the audio path
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

ROOT = Path(__file__).resolve().parents[1]
CATALOG = specialists(ROOT)


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()


class MemoryTests(Temp):
    def test_add_search_dedupe_link(self):
        mem = Memory(self.base)
        a = mem.add("note", "Lan Mouse pairs the Dell and the ASUS over UDP 4242",
                    tags=["desktop", "lan-mouse"], importance=0.9)
        self.assertRegex(a["id"], r"^note:\d{8}:[0-9a-f]{6}$")
        self.assertEqual(mem.add("note", "Lan Mouse pairs the Dell and the ASUS over UDP 4242")["id"],
                         a["id"])
        b = mem.add("note", "ElevenLabs flash model costs half the credits")
        mem.link(b["id"], a["id"], "related")
        hits = mem.search("lan mouse udp")
        self.assertEqual(hits[0]["id"], a["id"])
        self.assertEqual(mem.search("", tag="lan-mouse")[0]["id"], a["id"])
        self.assertEqual(mem.search("pairs", kind="reply"), [])
        self.assertEqual(len(mem.get(a["id"])["links"]), 1)
        self.assertEqual(mem.stats()["items"], 2)

    def test_hostile_query_and_bad_input(self):
        mem = Memory(self.base)
        mem.add("note", "quotes \" and NEAR( operators")
        self.assertEqual(len(mem.search('"); DROP TABLE items; -- NEAR(')), 1)
        for bad in ({"kind": "Bad Kind", "body": "x"}, {"kind": "note", "body": " "},
                    {"kind": "note", "body": "x", "tags": ["bad tag"]}):
            with self.assertRaises(ValueError):
                mem.add(**bad)
        with self.assertRaises(ValueError):
            mem.link("note:1:abc", "note:2:def")


class RouterTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(classify("what's 2+2")["tier"], "simple")
        self.assertEqual(classify("summarize https://example.com/post")["tier"], "medium")
        self.assertEqual(classify("check my instagram notifications")["tier"], "medium")
        self.assertEqual(classify("build a full-stack recon app with a backend")["tier"], "complex")
        self.assertEqual(classify("anything", "complex")["tier"], "complex")

    def test_owner_api_beats_browser(self):
        config = {"adapters": {"apis": [{"name": "ig", "url": "http://127.0.0.1:9/x",
                                         "sites": ["instagram"]}]}}
        self.assertEqual(pick_adapter("scan my Instagram", config)["name"], "ig")
        self.assertEqual(pick_adapter("open reddit", config)["kind"], "cli_web")

    def test_grokbot_takes_web_work_once_it_can_answer(self):
        config = {"adapters": {"browser_agents": ["grokbot"], "apis": []},
                  "personal_agents": {"grokbot": {"integration": "window_automation"}}}
        self.assertEqual(pick_adapter("open reddit", config)["name"], "grokbot")
        config["personal_agents"]["grokbot"]["integration"] = "unconfigured"
        self.assertEqual(pick_adapter("open reddit", config)["kind"], "cli_web")

    def test_specialists_have_tiers(self):
        self.assertEqual(CATALOG["scout"]["power"], "low")
        self.assertEqual(CATALOG["architect"]["power"], "high")


class RunnerTierTests(Temp):
    @patch("jarvis.runner.shutil.which", return_value=sys.executable)
    def test_power_maps_to_model_and_effort(self, _):
        config = {"workspace_roots": ["."], "models": {
            "claude": {"low": {"model": "haiku", "effort": "low"}},
            "codex": {"high": {"model": "gpt-x", "effort": "high"}}}}
        plan = make_plan({"objective": "x", "provider": "claude", "power": "low"}, config, self.base)
        self.assertEqual(plan["argv"][-4:], ["--model", "haiku", "--effort", "low"])
        plan = make_plan({"objective": "x", "provider": "codex", "power": "high"}, config, self.base)
        self.assertIn('model_reasoning_effort="high"', plan["argv"])
        self.assertEqual(plan["argv"][-1], "-")
        with self.assertRaises(ValueError):
            make_plan({"objective": "x", "power": "ultra"}, config, self.base)

    def test_final_message_from_both_providers(self):
        (self.base / "stdout.jsonl").write_text(
            '{"type":"system"}\nnot json\n{"type":"result","result":"claude says hi"}\n',
            encoding="utf-8")
        self.assertEqual(final_message("claude", self.base), "claude says hi")
        (self.base / "last-message.md").write_text("codex says hi\n", encoding="utf-8")
        self.assertEqual(final_message("codex", self.base), "codex says hi")


PLAN = {"summary": "two steps", "subtasks": [
    {"id": "s1", "specialist": "implementer", "objective": "write it", "provider": "codex",
     "power": "mid", "mode": "workspace-write", "owned_paths": ["app/"]},
    {"id": "s2", "specialist": "reviewer", "objective": "review it", "mode": "workspace-write",
     "depends_on": ["s1"]}]}


class OrchestratorTests(Temp):
    def test_validation(self):
        plan = validate_plan(json.loads(json.dumps(PLAN)), CATALOG)
        self.assertEqual(plan["subtasks"][1]["mode"], "read-only")  # reviewers can't write
        for broken in ({"subtasks": [{"id": "a", "specialist": "wizard", "objective": "x"}]},
                       {"subtasks": [{"id": "a", "specialist": "scout", "objective": "x",
                                      "depends_on": ["b"]}]},
                       {"subtasks": [{"id": "a", "specialist": "implementer", "objective": "x",
                                      "mode": "workspace-write", "owned_paths": ["f"]},
                                     {"id": "b", "specialist": "frontend", "objective": "y",
                                      "mode": "workspace-write", "owned_paths": ["f"]}]}):
            with self.assertRaises(ValueError):
                validate_plan(broken, CATALOG)
        self.assertEqual(extract_json('Sure:\n```json\n{"a": 1}\n```')["a"], 1)

    def test_runs_in_dependency_order_and_stops_on_failure(self):
        store, mem = Store(self.base / "state"), Memory(self.base / "state")
        orch = Orchestrator(store, mem, {"workspace_roots": ["."]}, ROOT)
        record = orch.create("ship it", plan=json.loads(json.dumps(PLAN)))
        calls = []

        def fake_run(store_, task_id, *_args):
            spec = store_.get(task_id)["spec"]
            calls.append(spec)
            store_.transition(task_id, "queued", "running")
            state = "finished" if len(calls) == 1 else "failed"
            store_.transition(task_id, "running", state)
            return {"task_id": task_id, "state": state, "artifacts": "", "answer": "done " + spec["objective"]}

        with patch("jarvis.orchestrator.run_task", side_effect=fake_run):
            result = orch.run(record["id"])
        self.assertEqual([c["objective"] for c in calls], ["write it", "review it"])
        self.assertTrue(calls[0]["allow_subagents"])
        self.assertEqual(calls[1]["role"], "reviewer")
        self.assertIn("Result of s1: done write it", calls[1]["evidence"][0])
        self.assertEqual(result["state"], "blocked")
        self.assertEqual(len(mem.search("write", kind="result")), 1)
        self.assertEqual(len(mem.search("", kind="result")), 2)


class AssistantTests(Temp):
    def test_simple_ask_is_remembered(self):
        store, mem = Store(self.base / "s"), Memory(self.base / "s")
        mem.add("note", "David's ASUS laptop runs Hyprland", tags=["hardware"])

        def fake_run(store_, task_id, *_args):
            spec = store_.get(task_id)["spec"]
            self.assertEqual(spec["power"], "low")
            self.assertTrue(any("Hyprland" in e for e in spec["evidence"]))
            return {"task_id": task_id, "state": "finished", "answer": "It runs Hyprland."}

        with patch("jarvis.assistant.run_task", side_effect=fake_run):
            result = ask("what does my ASUS laptop run", store, mem, {}, self.base)
        self.assertEqual(result["tier"], "simple")
        reply = mem.get(result["memory"][1])
        self.assertEqual(reply["links"][0]["rel"], "answers")

    def test_wake_word(self):
        self.assertEqual(wake_word("Hey Jarvis, what's on my calendar?"), "what's on my calendar?")
        self.assertEqual(wake_word("jervis open the plan"), "open the plan")
        self.assertIsNone(wake_word("I was telling Sam about jarvis"))
        self.assertIsNone(wake_word("Travis, did you see the game?"))  # a real name
        self.assertIsNone(wake_word("Jarvis's voice is too quiet"))  # about it, not to it
        self.assertIsNone(wake_word("Good. Thank you. Thank you."))


class SpeechTests(Temp):
    def test_spoken_summary(self):
        text = "Done. Here is code:\n```py\nprint(1)\n```\n" + "More detail here. " * 40
        line = spoken(text, 120)
        self.assertLessEqual(len(line), 120)
        self.assertNotIn("print(1)", line)

    @patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ID": "v1",
                             "ELEVENLABS_VOICE_SPEED": "1.1", "ELEVENLABS_VOICE_STABILITY": "9"})
    def test_tuning_reaches_read_aloud(self):
        voice = Voice(self.base, {"voice": {"engine": "elevenlabs"}})
        with patch("jarvis.speech.urllib.request.urlopen",
                   side_effect=lambda *a, **k: io.BytesIO(b"\x00\x01" * 10)) as call:
            tuned, _ = voice.synth("hello")
            body = json.loads(call.call_args.args[0].data)
            self.assertEqual(body["voice_settings"], {"speed": 1.1})  # 9 is out of range
            with patch.dict(os.environ, {"ELEVENLABS_VOICE_SPEED": "0.8"}):
                slower, source = voice.synth("hello")
        self.assertEqual(source, "elevenlabs")  # new tuning, new clip: not the cached one
        self.assertNotEqual(tuned, slower)

    @patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ID": "v1"})
    def test_cache_and_budget(self):
        voice = Voice(self.base, {"voice": {"engine": "elevenlabs", "monthly_char_budget": 10}})
        response = io.BytesIO(b"\x00\x01" * 100)
        with patch("jarvis.speech.urllib.request.urlopen", return_value=response) as call:
            path, source = voice.synth("hello")
            self.assertEqual(source, "elevenlabs")
            with wave.open(str(path)) as w:
                self.assertEqual(w.getnframes(), 100)
            self.assertEqual(voice.synth("hello")[1], "cache")
            self.assertEqual(voice.synth("over the limit")[1], "over-budget")
            self.assertEqual(call.call_count, 1)
            self.assertIn("pcm_22050", call.call_args.args[0].full_url)
        self.assertEqual(voice.remaining(), 5)

    def test_setup_saves_key_and_picked_voice(self):
        from jarvis import speech
        voices = io.BytesIO(json.dumps({"voices": [
            {"voice_id": "us1", "name": "Sarah", "labels": {"gender": "female", "accent": "american"}},
            {"voice_id": "uk1", "name": "Daniel", "labels": {"gender": "male", "accent": "british"}},
        ]}).encode())
        with patch.dict(os.environ, {}, clear=True), \
                patch("jarvis.speech.urllib.request.urlopen", return_value=voices), \
                patch.object(speech.Voice, "say", return_value={"source": "elevenlabs"}), \
                patch("builtins.print"):
            result = speech.setup(self.base, ask=lambda _: "", secret=lambda _: "sk_" + "a" * 40)
            self.assertEqual(result["voice_id"], "uk1")  # British male sorted first
            self.assertEqual(Voice(self.base).credential("ELEVENLABS_VOICE_ID"), "uk1")
            with self.assertRaises(ValueError):
                speech.setup(self.base, secret=lambda _: "not a key!")

    @patch.dict(os.environ, {"ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ID": "v1"})
    def test_elevenlabs_failure_falls_back_to_kokoro(self):
        from jarvis import speech
        voice = Voice(self.base, {"voice": {"engine": "elevenlabs"}})
        kokoro = self.base / "k.wav"
        error = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b""))
        with patch("jarvis.speech.urllib.request.urlopen", side_effect=error), \
                patch.object(speech.Voice, "_kokoro", return_value=(kokoro, "kokoro")):
            self.assertEqual(voice.synth("hi"), (kokoro, "kokoro (elevenlabs-error)"))
        self.assertIn("401", voice.last_error)

    def test_no_credentials_falls_back(self):
        with patch.dict(os.environ, {}, clear=True):
            voice = Voice(self.base, {"voice": {"engine": "elevenlabs"}})
            self.assertEqual(voice.synth("hi")[1], "no-credentials")

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_kokoro_is_default_local_and_cached(self):
        import numpy as np
        from jarvis import speech

        class FakeKokoro:
            calls = 0

            def create(self, text, voice, speed, lang):
                FakeKokoro.calls += 1
                self.args = (voice, lang)
                return np.zeros(2400, dtype="float32"), 24000

        fake = FakeKokoro()
        with patch.object(speech, "kokoro_model", return_value=fake), \
                patch("jarvis.speech.urllib.request.urlopen") as net:
            voice = Voice(self.base)
            path, source = voice.synth("Hello there.")
            self.assertEqual((source, fake.args), ("kokoro", ("bm_george", "en-gb")))
            with wave.open(str(path)) as w:
                self.assertEqual((w.getframerate(), w.getnframes()), (24000, 2400))
            self.assertEqual(voice.synth("Hello there.")[1], "cache")
            self.assertEqual(FakeKokoro.calls, 1)
            net.assert_not_called()  # local voice never touches the network

    def test_speech_queue_plays_in_order(self):
        from jarvis import speech
        played = []
        voice = Voice(self.base)
        with patch.object(voice, "synth", side_effect=lambda t: (Path(t), "kokoro")), \
                patch.object(speech, "play_wav", side_effect=lambda p, *a: played.append(str(p))):
            q = speech.SpeechQueue(voice)
            for s in ("One.", "Two.", "Three."):
                q.put(s)
            q.drain()
        self.assertEqual(played, ["One.", "Two.", "Three."])


FAKE_CLAUDE = r'''
import json, os, sys
turn = 0
for line in sys.stdin:
    turn += 1
    text = json.loads(line)["message"]["content"].splitlines()[-1]
    if "die" in text:
        sys.exit(1)
    for piece in ("Hello there. How", " are you? Fine"):
        print(json.dumps({"type": "stream_event", "event": {"type": "content_block_delta",
              "delta": {"type": "text_delta", "text": piece}}}), flush=True)
    print(json.dumps({"type": "assistant"}), flush=True)
    print(json.dumps({"type": "result", "result": f"{os.getpid()}:{turn}:{text}"}), flush=True)
'''


class FastLaneTests(Temp):
    def test_reuses_process_recycles_and_recovers(self):
        from jarvis.fastlane import FastLane
        script = self.base / "fake_claude.py"
        script.write_text(FAKE_CLAUDE, encoding="utf-8")
        lane = FastLane({}, self.base, max_turns=2, timeout=20,
                        command=[sys.executable, str(script)])
        self.addCleanup(lane.close)
        pid1, turn, text = lane.ask("hello", ["note"]).split(":", 2)
        self.assertEqual((turn, text), ("1", "David: hello"))
        pid2, turn, _ = lane.ask("again").split(":", 2)
        self.assertEqual((pid1, turn), (pid2, "2"))  # same warm process
        pid3, turn, _ = lane.ask("third").split(":", 2)
        self.assertNotEqual(pid3, pid1)  # recycled after max_turns
        with self.assertRaises(RuntimeError):
            lane.ask("die")
        self.assertTrue(lane.ask("back").endswith("David: back"))  # restarted

    def test_streams_sentences_as_they_arrive(self):
        from jarvis.fastlane import FastLane
        script = self.base / "fake_claude.py"
        script.write_text(FAKE_CLAUDE, encoding="utf-8")
        lane = FastLane({}, self.base, timeout=20, command=[sys.executable, str(script)])
        self.addCleanup(lane.close)
        heard = []
        lane.ask("hi", on_sentence=heard.append)
        self.assertEqual(heard, ["Hello there.", "How are you?", "Fine"])

    def test_children_never_see_api_keys(self):
        from jarvis.fastlane import subscription_env
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x", "OPENAI_API_KEY": "sk-y",
                                     "CODEX_API_KEY": "sk-z", "ANTHROPIC_BASE_URL": "u",
                                     "CLAUDE_CODE_USE_FOUNDRY": "1", "CLAUDECODE": "1",
                                     "CLAUDE_CODE_SESSION_ID": "s",
                                     "CLAUDE_CODE_OAUTH_TOKEN": "sub", "PATH": "keep"}):
            env = subscription_env()
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY",
                     "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE",
                     "CLAUDE_CODE_SESSION_ID"):
            self.assertNotIn(name, env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "sub")  # the subscription login stays
        self.assertEqual(env["PATH"], "keep")

    def test_fast_lane_answer_never_falls_through_to_planner(self):
        class Lane:
            def ask(self, *_, **__):
                return "Ulaanbaatar."

        store, mem = Store(self.base / "s"), Memory(self.base / "s")
        with patch("jarvis.assistant.Orchestrator") as planner, \
                patch("jarvis.assistant.run_task") as run:
            result = ask("what's the capital of Mongolia?", store, mem, {}, self.base, lane=Lane())
        self.assertEqual((result["tier"], result["reply"]), ("simple", "Ulaanbaatar."))
        planner.assert_not_called()
        run.assert_not_called()

    def test_ask_falls_back_when_lane_fails(self):
        class Broken:
            def ask(self, *_, **__):
                raise RuntimeError("down")

        store, mem = Store(self.base / "s"), Memory(self.base / "s")
        with patch("jarvis.assistant.run_task", return_value={
                "task_id": "t", "state": "finished", "answer": "slow path"}):
            result = ask("hi there", store, mem, {}, self.base, lane=Broken())
        self.assertEqual(result["reply"], "slow path")
        self.assertEqual(result["lane_error"], "down")


FAKE_APP_SERVER = r'''
import json, os, sys
def send(m): print(json.dumps(m), flush=True)
threads = 0
for line in sys.stdin:
    m = json.loads(line)
    meth, rid = m.get("method"), m.get("id")
    if meth == "initialize":
        send({"id": rid, "result": {"userAgent": "fake"}})
    elif meth == "thread/start":
        threads += 1
        send({"id": rid, "result": {"thread": {"id": f"t{threads}"}}})
    elif meth == "turn/start":
        text = m["params"]["input"][0]["text"].splitlines()[-1]
        # An approval request JARVIS must refuse, then deltas BEFORE the response.
        send({"id": 99, "method": "item/commandExecution/requestApproval", "params": {}})
        for piece in ("Ulaanbaatar is the capital. ", "Key leaked: ",
                      str("OPENAI_API_KEY" in os.environ) + "."):
            send({"method": "item/agentMessage/delta", "params": {"itemId": "a", "delta": piece}})
        send({"id": rid, "result": {"turn": {"id": "u1"}}})
        send({"method": "turn/completed", "params": {"threadId": m["params"]["threadId"],
              "turn": {"status": "completed"}}})
    elif rid == 99:
        sys.stderr.write("approval answered\n")
        assert "error" in m  # JARVIS refuses approvals
'''


class ChatGPTLaneTests(Temp):
    def test_streams_refuses_approvals_and_strips_api_keys(self):
        from jarvis.chatgpt import ChatGPTLane
        script = self.base / "fake_app_server.py"
        script.write_text(FAKE_APP_SERVER, encoding="utf-8")
        lane = ChatGPTLane({}, self.base, max_turns=1, timeout=20,
                           command=[sys.executable, str(script)])
        self.addCleanup(lane.close)
        heard = []
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-should-not-pass"}):
            answer = lane.ask("capital of Mongolia?", ["note"], on_sentence=heard.append)
        self.assertEqual(answer, "Ulaanbaatar is the capital. Key leaked: False.")
        self.assertEqual(heard, ["Ulaanbaatar is the capital.", "Key leaked: False."])
        self.assertEqual(lane.thread, "t1")
        lane.ask("again")  # max_turns=1: a fresh thread on the same warm process
        self.assertEqual(lane.thread, "t2")

    def test_conversation_goes_to_brain_without_escalating(self):
        class Brain:
            def ask(self, text, recall, on_sentence=None):
                return "brain says hi"

        store, mem = Store(self.base / "s"), Memory(self.base / "s")
        with patch("jarvis.assistant.Orchestrator") as orch:
            result = ask("build a production trading dashboard", store, mem, {}, self.base,
                         lane=Brain())
        orch.assert_not_called()  # spoken "build ..." no longer spawns a planner
        self.assertEqual((result["tier"], result["reply"]), ("simple", "brain says hi"))


class InstantTests(Temp):
    def test_clock_questions_answered_locally(self):
        from datetime import datetime
        from jarvis.instant import answer
        now = datetime(2026, 10, 21, 9, 5)
        for q in ("what day is it today?", "What day is it", "what is the date",
                  "whats the date today", "what’s today’s date?",
                  "what day of the week is it", "Jarvis, what's today?"):
            self.assertEqual(answer(q, now), "It's Wednesday, October 21st, 2026.", q)
        for q in ("what time is it?", "what's the time right now", "Jarvis, what time is it"):
            self.assertEqual(answer(q, now), "It's 9:05 AM.", q)
        for q in ("what time is it in Tokyo?", "what is the day after tomorrow",
                  "tell me a joke", "what date is my exam", "what day is Christmas"):
            self.assertIsNone(answer(q, now), q)

    def test_instant_answer_skips_the_brain(self):
        class Brain:
            def ask(self, *a, **k):
                raise AssertionError("brain should not be called")

        store, mem = Store(self.base / "s"), Memory(self.base / "s")
        heard = []
        result = ask("what day is it?", store, mem, {}, self.base, lane=Brain(),
                     on_sentence=heard.append)
        self.assertEqual(result["lane"], "instant")
        self.assertEqual(heard, [result["reply"]])


class DaemonTests(Temp):
    def test_single_instance_lock(self):
        from jarvis.daemon import InstanceLock
        with InstanceLock(self.base):
            with self.assertRaises(RuntimeError):
                with InstanceLock(self.base):
                    pass
        with InstanceLock(self.base):  # released after exit
            pass

    @unittest.skipUnless(os.name == "nt", "Windows Startup folder")
    def test_autostart_writes_hidden_launcher(self):
        from jarvis import daemon
        with patch.dict(os.environ, {"APPDATA": str(self.base)}):
            result = daemon.autostart(self.base / "state", True)
            text = Path(result["launcher"]).read_text(encoding="ascii")
            self.assertIn("voice start", text)
            self.assertIn(", 0, False", text)  # window style 0 = hidden
            self.assertFalse(daemon.autostart(self.base / "state", False)["autostart"])
            self.assertFalse(Path(result["launcher"]).exists())
        self.assertIsNone(daemon._pid(self.base))


class AdapterTests(unittest.TestCase):
    def test_rejects_plain_http_off_machine(self):
        with self.assertRaises(ValueError):
            call_api({"name": "x", "url": "http://example.com/api"}, "q")


class ListenTests(unittest.TestCase):
    def test_segment_finds_speech(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        from jarvis.listen import RATE, segment
        quiet = np.zeros(RATE, dtype="float32")
        loud = (0.2 * np.sin(np.arange(RATE) / 5)).astype("float32")
        audio = np.concatenate([quiet, loud, quiet, quiet, loud[: RATE // 10]])
        segs = segment(audio, noise_floor=0.001)
        self.assertEqual(len(segs), 1)  # the 0.1 s blip is below min_speech
        self.assertAlmostEqual(segs[0][0] / RATE, 1.0, delta=0.05)

    def test_speech_threshold_in_quiet_and_noisy_rooms(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed")
        from jarvis.listen import speech_threshold
        self.assertAlmostEqual(speech_threshold([0.002] * 16), 0.006)  # quiet: 3x floor
        self.assertEqual(speech_threshold([0.0] * 16), 0.004)
        # A fan at ~0.02 RMS with bursts to 0.04: normal speech (~0.07) must still count.
        noisy = speech_threshold([0.02, 0.025, 0.03, 0.04, 0.02, 0.018, 0.035, 0.022] * 2)
        self.assertGreater(noisy, 0.035)
        self.assertLess(noisy, 0.05)
        # Speaking during calibration doesn't push the threshold above the speech.
        self.assertLess(speech_threshold([0.002] * 12 + [0.08] * 4), 0.01)

    def test_collect_starts_on_speech_and_ends_on_silence(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        from jarvis.listen import _collect
        # 1 = speech block, 0 = not; each block carries its index so the cut is visible.
        marks = [0] * 10 + [1, 0, 1] + [0] * 5 + [1] * 20 + [0] * 30
        blocks = iter(np.full(10, i, dtype="float32") for i in range(len(marks)))
        out = _collect(lambda: next(blocks), lambda b: bool(marks[int(b[0])]), 10,
                       max_seconds=60, max_silence=10 * 10 / 16000, wait_seconds=60,
                       min_voiced=0, onset=3, preroll=2)
        kept = sorted({int(v) for v in out})
        self.assertEqual((kept[0], kept[-1]), (16, 37))  # 2 blocks of preroll, no tail
        noise = iter(np.zeros(10, dtype="float32") for _ in range(10_000))
        self.assertIsNone(_collect(lambda: next(noise), lambda b: False, 10, 60, 0.1,
                                   wait_seconds=0.05, min_voiced=0, onset=3))

    def test_enrollment_pinned_and_wake_word_learning(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        from jarvis.listen import VoicePrint
        rng = np.random.default_rng(1)
        me = rng.normal(size=32)
        unit = lambda v: (v / np.linalg.norm(v)).astype("float32")
        clips = [unit(me + rng.normal(scale=0.8, size=32)) for _ in range(4)]
        with tempfile.TemporaryDirectory() as d:
            vp = VoicePrint(d)
            vp.MAX_PROFILE = 6
            with patch.object(vp, "_embed", side_effect=lambda c: c):
                vp.enroll(clips)
                for _ in range(5):  # strong matches fill the profile past its cap
                    vp.verify(unit(me + rng.normal(scale=0.1, size=32)))
                profile = json.loads(vp.path.read_text())["profile"]
                self.assertEqual(len(profile), 6)
                self.assertTrue(np.allclose(profile[:4], clips))  # enrollment never rotated out
                stranger = unit(rng.normal(size=32))
                vp.verify(stranger)
                self.assertIsNone(vp.last_embedding)  # rejected clips can't be learned
                vp.learn()
                self.assertFalse(any(np.allclose(p, stranger) for p in
                                     json.loads(vp.path.read_text())["profile"]))

    def test_voiceprint_learns_threshold_and_adapts(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        from jarvis.listen import VoicePrint
        rng = np.random.default_rng(0)
        me = rng.normal(size=64)

        def voice(base, noise):
            v = base + rng.normal(scale=noise, size=64)
            return (v / np.linalg.norm(v)).astype("float32")

        clips = [voice(me, 1.1) for _ in range(8)]  # noisy, like short real clips
        _, threshold, own = VoicePrint.calibrate(clips)
        self.assertTrue(VoicePrint.FLOOR <= threshold <= VoicePrint.CEILING)
        self.assertLess(threshold, min(own))  # never stricter than his own weakest clip
        with tempfile.TemporaryDirectory() as d:
            vp = VoicePrint(d)
            with patch.object(vp, "_embed", side_effect=lambda c: c):
                size = lambda: len(json.loads(vp.path.read_text())["profile"])
                vp.enroll(clips)
                self.assertFalse(vp.verify(voice(rng.normal(size=64), 0.1))[0])  # stranger
                self.assertEqual(size(), 8)  # strangers never join the profile
                self.assertTrue(vp.verify(voice(me, 1.1))[0])
                vp.verify(voice(me, 0.2))  # confident match joins the profile
                self.assertGreater(size(), 8)


if __name__ == "__main__":
    unittest.main()
