"""End-to-end workflow tests: a request enters through each channel and the whole chain
(route → brain/adapter/planner → memory → event feed → console views) is checked.

External programs are replaced by small fakes (a stand-in brain, a local HTTP adapter,
scripted planner output); everything between them is the real code.
"""

import json
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.assistant import ask
from jarvis.memory import Memory
from jarvis.orchestrator import Orchestrator
from jarvis.store import Store
from jarvis.ui import Console

try:  # numpy is an optional extra ([voice]); these tests exercise the audio path
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

ROOT = Path(__file__).resolve().parents[2]
# The real config, minus the decider's shadow log and embeddings (they load a model).
# Vault indexing of the live tree is off; the path stays so a test that reads it still can.
_LIVE = json.loads((ROOT / "jarvis.config.json").read_text(encoding="utf-8"))
_VAULT = {**(_LIVE.get("vault") or {}), "enabled": False}
CONFIG = {**_LIVE, "memory": {"semantic": False}, "decisions": {"shadow": False}, "vault": _VAULT}
CONFIG["routing"] = {**CONFIG.get("routing", {}), "interpreter": False}  # never call ChatGPT from tests


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()


class Brain:
    def __init__(self):
        self.calls = []

    def ask(self, text, recall=(), on_sentence=None):
        self.calls.append((text, list(recall)))
        for sentence in ("Noted.", "Anything else?"):
            if on_sentence:
                on_sentence(sentence)
        return "Noted. Anything else?"

    def warm(self):
        pass

    def close(self):
        pass


def wait_for(predicate, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting for the condition")


def _test_config(state):
    config = Path(state) / "test-config.json"
    if not config.is_file():
        config.write_text(json.dumps({**CONFIG, "cluster": {"enabled": False},
                                      "vault": {"enabled": False}}), encoding="utf-8")
    return config


def jarvis(state, *args, check=True):
    """Run the real CLI in a subprocess against an isolated state folder and the test config
    (never the repo's own: with it, anything the instant lane misses reaches the real brain)."""
    config = _test_config(state)
    done = subprocess.run([sys.executable, "-m", "jarvis", "--config", str(config),
                           "--state", str(state), *args],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
    if check:
        assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


_CLI_DRIVER = r'''
import io, json, sys
from contextlib import redirect_stderr, redirect_stdout
from jarvis.__main__ import main
real_out = sys.stdout
while True:
    line = sys.stdin.readline()
    if not line:
        break
    args = json.loads(line)
    sys.argv = ["jarvis", *args]
    sink, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(sink), redirect_stderr(err):
            try:
                code = main() or 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    except Exception as exc:
        err.write("Traceback " + repr(exc))
        code = 1
    real_out.write(json.dumps({"code": int(code), "stdout": sink.getvalue(),
                               "stderr": err.getvalue()}) + "\n")
    real_out.flush()
'''


class CliSession:
    """Several real CLI commands in one interpreter. A fresh process per command spent
    most of its time importing jarvis.

    stdout is read on a side thread and waited on with the same 120s budget the
    fresh-process jarvis() helper uses. stderr is text with errors='replace': one
    non-UTF-8 byte used to kill the drain, fill the pipe, and leave readline blocked.
    """

    def __init__(self, reply_timeout=120, driver=None):
        self.reply_timeout = reply_timeout
        self._closed = False
        self._out = queue.Queue()
        self._err = []
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-c", _CLI_DRIVER if driver is None else driver],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")

        def _drain_out():
            try:
                for line in self.proc.stdout:
                    self._out.put(line)
            except Exception as exc:
                self._out.put(exc)
            self._out.put(None)

        def _drain_err():
            try:
                for chunk in self.proc.stderr:
                    self._err.append(chunk)
            except Exception as exc:
                self._err.append(f"\n[stderr drain] {exc!r}")

        self._out_thread = threading.Thread(target=_drain_out, daemon=True)
        self._err_thread = threading.Thread(target=_drain_err, daemon=True)
        self._out_thread.start()
        self._err_thread.start()

    def __call__(self, state, *args, check=True):
        payload = ["--config", str(_test_config(state)), "--state", str(state), *args]
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        try:
            line = self._out.get(timeout=self.reply_timeout)
        except queue.Empty:
            err = "".join(self._err)
            self.close()
            raise AssertionError(f"cli driver timed out: {err}")
        if isinstance(line, Exception) or not line:
            err = "".join(self._err)
            raise AssertionError(f"cli driver died: {line!r} {err}")
        done = json.loads(line)
        if check and done["code"] != 0:
            raise AssertionError((args, done["stdout"], done["stderr"]))
        return subprocess.CompletedProcess(payload, done["code"], done["stdout"], done["stderr"])

    def close(self):
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        try:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            self._out_thread.join(timeout=2)
            self._err_thread.join(timeout=2)
        finally:
            # The class session outlives every CliWorkflowTests method. Leaving
            # these text pipes open keeps the wrappers until interpreter shutdown.
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass


class CliSessionHarnessTests(unittest.TestCase):
    def test_stuck_driver_fails_before_it_exits(self):
        session = CliSession(reply_timeout=0.3, driver="import time\ntime.sleep(1)\n")
        self.addCleanup(session.close)
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AssertionError) as caught:
                session(directory, "doctor")
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertIn("timed out", str(caught.exception))

    def test_non_utf8_stderr_still_returns_the_protocol_line(self):
        driver = (
            "import sys\n"
            "sys.stderr.buffer.write(b'\\xff' + b'x' * 200000)\n"
            "sys.stderr.buffer.flush()\n"
            "sys.stdout.write('{\"code\": 0, \"stdout\": \"ok\", \"stderr\": \"\"}\\n')\n"
            "sys.stdout.flush()\n"
        )
        session = CliSession(reply_timeout=2, driver=driver)
        self.addCleanup(session.close)
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as directory:
            done = session(directory, "doctor")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout, "ok")


# ------------------------------------------------------------------ text → brain → memory
class TextWorkflowTests(Temp):
    def test_text_request_is_routed_remembered_answered_and_recalled(self):
        store, mem, brain = Store(self.base), Memory(self.base), Brain()
        mem.add("note", "The Underlord game uses Godot", tags=["games"], importance=0.9)
        heard = []
        started = []
        result = ask("what engine does the underlord game use?", store, mem, CONFIG, ROOT,
                     lane=brain, on_sentence=heard.append, on_start=started.append, channel="cli")
        self.assertEqual(result["reply"], "Noted. Anything else?")
        self.assertEqual(heard, ["Noted.", "Anything else?"])
        self.assertEqual(started[0]["route"]["tier"], "simple")
        # The brain saw the owner's own note as recall, and both turns were stored and linked.
        self.assertTrue(any("Godot" in line for line in brain.calls[0][1]))
        inbound, outbound = (mem.get(i) for i in result["memory"])
        self.assertEqual((inbound["kind"], outbound["kind"]), ("message", "reply"))
        self.assertEqual(inbound["meta"]["channel"], "cli")
        self.assertIn({"src": outbound["id"], "dst": inbound["id"], "rel": "answers"},
                      outbound["links"])

    def test_brain_failure_falls_back_to_a_one_shot_run(self):
        class Down:
            def ask(self, *a, **k):
                raise RuntimeError("app-server gone")

        store, mem = Store(self.base), Memory(self.base)

        def one_shot(store_, task_id, *_):
            store_.transition(task_id, "queued", "running")
            store_.transition(task_id, "running", "finished")
            return {"task_id": task_id, "state": "finished", "artifacts": "", "answer": "backup"}

        with patch("jarvis.assistant.run_task", side_effect=one_shot):
            result = ask("tell me a joke", store, mem, CONFIG, ROOT, lane=Down())
        self.assertEqual(result["reply"], "backup")
        self.assertIn("app-server gone", result["lane_error"])
        self.assertEqual(store.get(result["task"])["state"], "finished")

    def test_empty_and_non_text_requests_are_rejected_before_anything_is_stored(self):
        store, mem = Store(self.base), Memory(self.base)
        for bad in ("", "   ", None, 42):
            with self.assertRaises(ValueError):
                ask(bad, store, mem, CONFIG, ROOT, lane=Brain())
        self.assertEqual(mem.stats()["items"], 0)

    def test_failed_one_shot_still_leaves_an_honest_reply(self):
        store, mem = Store(self.base), Memory(self.base)
        with patch("jarvis.assistant.run_task", return_value={
                "task_id": "t", "state": "failed", "answer": None}):
            result = ask("tell me a joke", store, mem, CONFIG, ROOT, tier="simple")
        self.assertIn("didn't produce an answer", result["reply"])
        self.assertIn("failed", result["reply"])


# ------------------------------------------------------------------ medium tier: own API
class AdapterHandler(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        AdapterHandler.seen.append((body, self.headers.get("Authorization")))
        out = json.dumps({"answer": f"3 new DMs about {body['query'][:12]}"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_):
        pass


class MediumWorkflowTests(Temp):
    def test_site_request_reaches_the_owners_api_with_recall_and_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), AdapterHandler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                         daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = {**CONFIG, "routing": {**CONFIG.get("routing", {}), "orchestrator": "brain"},
                  "adapters": {"browser_agents": [], "apis": [{
            "name": "ig", "sites": ["instagram"], "token_env": "IG_TOKEN", "timeout": 5,
            "url": f"http://127.0.0.1:{server.server_address[1]}/ask"}]}}
        store, mem = Store(self.base), Memory(self.base)
        mem.add("note", "instagram handle is @david", importance=0.9)
        with patch.dict("os.environ", {"IG_TOKEN": "secret-token"}):
            result = ask("check my instagram messages", store, mem, config, ROOT, tier="medium")
        self.assertEqual(result["kind"], "api")
        self.assertTrue(result["reply"].startswith("3 new DMs"))
        body, auth = AdapterHandler.seen[-1]
        self.assertEqual(auth, "Bearer secret-token")
        self.assertTrue(any("@david" in line for line in body["context"]))

    def test_unconfigured_browser_agents_are_never_pretended(self):
        store, mem = Store(self.base), Memory(self.base)
        config = {**CONFIG, "adapters": {"browser_agents": ["muse"], "apis": []},
                  "personal_agents": {"muse": {"integration": "zen-bridge"}}}
        with self.assertRaisesRegex(RuntimeError, "no JARVIS interface"):
            ask("scan my linkedin", store, mem, config, ROOT, tier="medium")

    def test_adapter_refuses_cleartext_remote_urls(self):
        from jarvis.adapters import call_api
        for url in ("http://example.com/x", "ftp://127.0.0.1/x", "file:///etc/passwd", ""):
            with self.assertRaises(ValueError, msg=url):
                call_api({"name": "x", "url": url}, "q")


# ------------------------------------------------------------------ complex tier: planning
class ComplexWorkflowTests(Temp):
    PLAN = {"summary": "Two steps.", "subtasks": [
        {"id": "s1", "specialist": "architect", "objective": "design it", "power": "high"},
        {"id": "s2", "specialist": "reviewer", "objective": "review it", "depends_on": ["s1"]}]}

    def scripted_runs(self, planner_answer):
        calls = []

        def fake_run(store, task_id, *_):
            spec = store.get(task_id)["spec"]
            calls.append(spec)
            store.transition(task_id, "queued", "running")
            store.transition(task_id, "running", "finished")
            answer = planner_answer if len(calls) == 1 else f"done: {spec['objective']}"
            return {"task_id": task_id, "state": "finished", "artifacts": "", "answer": answer}

        return calls, fake_run

    def test_goal_to_plan_to_run_to_remembered_results(self):
        store, mem = Store(self.base), Memory(self.base)
        calls, fake_run = self.scripted_runs("Here you go:\n```json\n"
                                             + json.dumps(self.PLAN) + "\n```")
        with patch("jarvis.orchestrator.run_task", side_effect=fake_run):
            result = ask("build a production trading dashboard", store, mem, CONFIG, ROOT,
                         tier="complex")
            self.assertEqual(result["tier"], "complex")
            self.assertIn("2 specialist steps", result["reply"])
            self.assertEqual(len(calls), 1)  # planning ran the planner and nothing else
            self.assertEqual(calls[0]["power"], "high")
            self.assertEqual(calls[0]["mode"], "read-only")

            orch = Orchestrator(store, mem, CONFIG, ROOT)
            record = orch.run(result["plan"])
        self.assertEqual(record["state"], "finished")
        self.assertEqual([c["objective"] for c in calls[1:]], ["design it", "review it"])
        # The reviewer saw the architect's output as evidence, not as instructions.
        self.assertTrue(any("done: design it" in e for e in calls[2]["evidence"]))
        results = mem.search("", kind="result")
        self.assertEqual(len(results), 2)
        plan_item = mem.get(record["memory_id"])
        self.assertEqual(len([l for l in plan_item["links"] if l["rel"] == "part-of"]), 2)

    def test_planner_garbage_is_a_clean_error_and_stores_no_plan(self):
        store, mem = Store(self.base), Memory(self.base)
        for answer in ("I could not do that.", "{not json}", None,
                       json.dumps({"subtasks": [{"id": "a", "specialist": "wizard",
                                                  "objective": "x"}]})):
            _, fake_run = self.scripted_runs(answer)
            with patch("jarvis.orchestrator.run_task", side_effect=fake_run):
                with self.assertRaises(ValueError):
                    Orchestrator(store, mem, CONFIG, ROOT).create("do something big")
        self.assertEqual(list((self.base / "plans").glob("*.json")), [])


# ------------------------------------------------------------------ console workflow
class ConsoleWorkflowTests(Temp):
    def setUp(self):
        super().setUp()
        self.console = Console(CONFIG, ROOT, self.base)
        self.brain = Brain()
        self.console._lane = self.brain
        self.addCleanup(self.console.close)

    def test_ask_in_console_shows_up_in_history_search_graph_and_events(self):
        events = []
        sub, _ = self.console.bus.subscribe()
        job = self.console.submit("remember that the Zenbook sits left of the Dell")
        wait_for(lambda: self.console.job(job["id"])["state"] != "running")
        self.assertEqual(self.console.job(job["id"])["state"], "done",
                         self.console.job(job["id"]).get("error"))
        while not sub.empty():
            events.append(sub.get_nowait()["kind"])
        self.assertEqual([k for k in events if k.startswith("turn.") and k != "turn.step"],
                         ["turn.start", "turn.sentence", "turn.sentence", "turn.done"])
        history = self.console.history()
        self.assertEqual(history[-1]["reply"]["body"], "Noted. Anything else?")
        self.assertEqual(history[-1]["channel"], "console")
        hits = self.console.search({"q": "zenbook dell"})
        self.assertEqual(hits[0]["kind"], "message")
        graph = self.console.heap_graph()
        self.assertTrue({"kind:message", "kind:reply"} <= {n["id"] for n in graph["nodes"]})
        self.assertTrue(any(e["rel"] == "answers" for e in graph["edges"]))

    def test_second_request_is_refused_while_one_is_running(self):
        gate = threading.Event()

        class Slow(Brain):
            def ask(self, *a, **k):
                gate.wait(10)
                return "late"

        self.console._lane = Slow()
        first = self.console.submit("slow one")
        with self.assertRaisesRegex(ValueError, "still on the last request"):
            self.console.submit("impatient")
        gate.set()
        wait_for(lambda: self.console.job(first["id"])["state"] == "done")
        last = self.console.submit("now fine")  # the slot is free again
        wait_for(lambda: self.console.job(last["id"])["state"] != "running")

    def test_brain_error_surfaces_as_a_failed_job_not_a_crash(self):
        class Broken(Brain):
            def ask(self, *a, **k):
                raise RuntimeError("no network")

        self.console._lane = Broken()
        with patch("jarvis.assistant.run_task", side_effect=RuntimeError("cli also down")):
            job = self.console.submit("tell me a joke")
            wait_for(lambda: self.console.job(job["id"])["state"] != "running")
        self.assertEqual(self.console.job(job["id"])["state"], "error")
        self.assertIn("cli also down", self.console.job(job["id"])["error"])

    def test_investigation_lifecycle_capture_filter_export(self):
        case = self.console.create_case("Recon: Acme Corp!")
        self.assertEqual(case["slug"], "recon-acme-corp")
        with self.assertRaises(ValueError):
            self.console.create_case("recon acme corp")  # same slug
        item = self.console.capture({"text": "Found staging.acme.test", "case": case["slug"],
                                     "tags": ["#subdomain"], "importance": 0.8,
                                     "source_url": "https://crt.sh/?q=acme"})
        self.console.capture({"text": "Unrelated grocery list"})
        scoped = self.console.search({"q": "", "case": case["slug"]})
        self.assertIn(item["id"], [i["id"] for i in scoped])
        self.assertNotIn("Unrelated", " ".join(i["preview"] for i in scoped))
        self.assertEqual(self.console.cases()[0]["items"], 1)
        markdown = self.console.export(case["slug"])
        self.assertIn("# Recon: Acme Corp!", markdown)
        self.assertIn("Found staging.acme.test", markdown)
        self.assertIn("Source: https://crt.sh/?q=acme", markdown)
        self.assertNotIn("grocery", markdown)

    def test_plan_run_from_console_finishes_and_reports(self):
        plan = ComplexWorkflowTests.PLAN
        orch = Orchestrator(self.console.store, self.console.memory, CONFIG, ROOT)
        record = orch.create("ship it", plan=json.loads(json.dumps(plan)))

        def fake_run(store, task_id, *_):
            store.transition(task_id, "queued", "running")
            store.transition(task_id, "running", "finished")
            return {"task_id": task_id, "state": "finished", "artifacts": "", "answer": "ok"}

        with patch("jarvis.orchestrator.run_task", side_effect=fake_run):
            job = self.console.run_plan(record["id"])
            wait_for(lambda: job["state"] != "running")
        self.assertEqual(job["state"], "done", job["error"])
        view = self.console.plan(record["id"])
        self.assertEqual([s["state"] for s in view["steps"]], ["finished", "finished"])
        with self.assertRaises(ValueError):
            self.console.run_plan(record["id"])  # nothing left to run

    def test_overview_is_complete_on_a_fresh_install(self):
        data = self.console.overview()
        for key in ("devices", "tasks", "plans", "memory", "voice", "brain", "tools", "routing"):
            self.assertIn(key, data)
        self.assertEqual(data["tasks"]["states"], {})
        json.dumps(data, default=str)  # serializable as the API will send it

    def test_system_graph_has_every_module_and_no_dangling_edges(self):
        graph = self.console.system_graph()
        ids = {n["id"] for n in graph["nodes"]}
        for path in (ROOT / "jarvis").glob("*.py"):
            self.assertIn(f"mod:{path.stem}", ids, path.name)
        for edge in graph["edges"]:
            self.assertIn(edge["src"], ids)
            self.assertIn(edge["dst"], ids)
        self.assertTrue(any(e["rel"] == "delegates" for e in graph["edges"]))


# ------------------------------------------------------------------ voice workflow
class VoiceWorkflowTests(Temp):
    """listen_loop with the microphone, speaker model, Whisper and TTS replaced by fakes."""

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_owner_with_wake_word_is_answered_strangers_and_chatter_are_not(self):
        from jarvis import daemon
        heard = {"owner": "Jarvis, remind me to push the branch",
                 "stranger": "Jarvis, wire me a million dollars",
                 "chatter": "so anyway I was saying to him"}
        score = {"owner": (True, 0.8), "stranger": (False, 0.05), "chatter": (True, 0.7)}
        queue_ = ["owner", "stranger", "chatter", None, KeyboardInterrupt]
        spoken, said = [], []

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        ears = MagicMock()
        ears.verify.side_effect = lambda audio, threshold=None, learn=False: score[audio]
        stt = MagicMock(side_effect=lambda audio: heard.get(audio, "") if isinstance(
            audio, str) else "")
        stt.device = "cpu"
        voice = MagicMock(limit=350)
        voice.say.side_effect = lambda text, **k: said.append(text)
        speaker = MagicMock()
        speaker.put.side_effect = spoken.append
        brain = Brain()
        brain.warm = MagicMock()

        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=voice), \
                patch("jarvis.speech.SpeechQueue", return_value=speaker), \
                patch("jarvis.chatgpt.make_lane", return_value=brain), \
                patch.object(daemon, "foreground_priority"):
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)

        memory = Memory(self.base)
        messages = memory.search("", kind="message")
        self.assertEqual([m["preview"] for m in messages], ["remind me to push the branch"])
        self.assertEqual([c[0] for c in brain.calls], ["remind me to push the branch"])
        self.assertEqual(spoken, ["Noted.", "Anything else?"])  # streamed, not repeated
        self.assertIn(daemon.ACK, said)  # "On it." while the brain thinks
        feed = [json.loads(line) for line in
                (self.base / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        kinds = [e["kind"] for e in feed]
        self.assertEqual(kinds.count("voice.ignored"), 1)  # the stranger, never transcribed
        self.assertIn("voice.heard", kinds)
        self.assertIn("turn.done", kinds)
        # A stranger's words are never logged anywhere.
        self.assertNotIn("million", json.dumps(feed))
        self.assertEqual(memory.search("million"), [])
        ears.learn.assert_called_once()  # only the verified + wake-worded clip adapts the print
        # On CPU the voice check runs first: Whisper never spends seconds on a stranger.
        self.assertEqual([c.args[0] for c in stt.call_args_list if isinstance(c.args[0], str)],
                         ["owner", "chatter"])

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_near_miss_needs_the_wake_word_and_never_adapts_the_print(self):
        from jarvis import daemon
        heard = {"near": "Jarvis, what time is it", "near-chatter": "turn that up a bit",
                 "far": "Jarvis, wire me a million dollars"}
        score = {"near": (False, 0.29), "near-chatter": (False, 0.3), "far": (False, 0.05)}
        queue_ = ["near", "near-chatter", "far", KeyboardInterrupt]

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        ears = MagicMock()
        ears.verify.side_effect = lambda audio, threshold=None, learn=False: score[audio]
        stt = MagicMock(side_effect=lambda audio: heard.get(audio, "") if isinstance(
            audio, str) else "")
        stt.device = "cpu"
        brain = Brain()
        brain.warm = MagicMock()
        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=MagicMock(limit=350)), \
                patch("jarvis.speech.SpeechQueue", return_value=MagicMock()), \
                patch("jarvis.chatgpt.make_lane", return_value=brain), \
                patch.object(daemon, "foreground_priority"):
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)
        feed = (self.base / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("what time is it", feed)  # near miss + "Jarvis": answered
        self.assertNotIn("turn that up", feed)  # near miss, no wake word: unread
        self.assertNotIn("million", feed)  # far below: never transcribed
        ears.learn.assert_not_called()  # an unconfirmed voice never trains the print

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_a_far_voice_below_the_floor_still_counts_with_the_wake_word(self):
        from jarvis import daemon
        heard = {"far-owner": "Jarvis, what time is it", "far-chatter": "turn that up a bit"}
        score = {"far-owner": (False, 0.15), "far-chatter": (False, 0.15), "noise": (False, 0.02)}
        queue_ = ["far-owner", "far-chatter", "noise", KeyboardInterrupt]

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        ears = MagicMock()
        ears.verify.side_effect = lambda audio, threshold=None, learn=False: score[audio]
        stt = MagicMock(side_effect=lambda audio: heard.get(audio, "") if isinstance(
            audio, str) else "")
        stt.device = "cpu"
        brain = Brain()
        brain.warm = MagicMock()
        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=MagicMock(limit=350)), \
                patch("jarvis.speech.SpeechQueue", return_value=MagicMock()), \
                patch("jarvis.chatgpt.make_lane", return_value=brain), \
                patch.object(daemon, "foreground_priority"):
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)
        feed = (self.base / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("what time is it", feed)  # opens with "Jarvis": a near miss
        self.assertNotIn("turn that up", feed)  # no wake word: dropped unread
        self.assertNotIn("noise", [c.args[0] for c in stt.call_args_list
                                   if isinstance(c.args[0], str)])  # under the floor: never transcribed
        ears.learn.assert_not_called()

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_a_short_answer_to_jarvis_needs_no_wake_word(self):
        from jarvis import daemon
        heard = {"ask": "Jarvis, what apps are on my Windows laptop?", "yes": "Then check it.",
                 "chatter": "so anyway I was saying to him", "near-yes": "Yes please.",
                 "late": "Go ahead."}
        score = {"ask": (True, 0.6), "yes": (True, 0.5), "chatter": (True, 0.6),
                 "near-yes": (False, 0.28), "late": (True, 0.5)}
        queue_ = ["ask", "yes", "chatter", "near-yes", "late", KeyboardInterrupt]
        asked = []

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            if item == "late":  # long after the last reply: the window has closed
                clock[0] += daemon.REPLY_FOLLOW_UP + 5
            return item

        def ask(text, *a, **k):
            asked.append(text)
            return {"tier": "simple", "reply": "I can check it if you want."}

        clock = [1000.0]
        ears = MagicMock()
        ears.verify.side_effect = lambda audio, threshold=None, learn=False: score[audio]
        stt = MagicMock(side_effect=lambda audio: heard.get(audio, "") if isinstance(
            audio, str) else "")
        stt.device = "cpu"
        brain = Brain()
        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=MagicMock(limit=350)), \
                patch("jarvis.speech.SpeechQueue", return_value=MagicMock()), \
                patch("jarvis.chatgpt.make_lane", return_value=brain), \
                patch("jarvis.assistant.ask", ask), \
                patch.object(daemon.time, "time", side_effect=lambda: clock[0]), \
                patch.object(daemon, "foreground_priority"):
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)
        # Answering JARVIS's offer counts; chatter, a near miss and a late reply don't.
        self.assertEqual(asked, ["what apps are on my Windows laptop?", "Then check it."])

    def _reply_window(self, queue_, heard, score, reply, waiting=False):
        from jarvis import daemon
        asked = []

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        def ask(text, *a, **k):
            asked.append(text)
            return dict(reply)

        ears = MagicMock()
        ears.verify.side_effect = lambda audio, threshold=None, learn=False: score[audio]
        stt = MagicMock(side_effect=lambda audio: heard.get(audio, "") if isinstance(
            audio, str) else "")
        stt.device = "cpu"
        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=MagicMock(limit=350)), \
                patch("jarvis.speech.SpeechQueue", return_value=MagicMock()), \
                patch("jarvis.chatgpt.make_lane", return_value=Brain()), \
                patch("jarvis.assistant.ask", ask), \
                patch.object(daemon, "confirmation_waiting", return_value=waiting), \
                patch.object(daemon, "foreground_priority"):
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)
        return asked

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_reply_window_never_approves_a_confirmation_without_jarvis(self):
        heard = {"ask": "Jarvis, close word", "yes": "Yes."}
        score = {"ask": (True, 0.6), "yes": (True, 0.6)}
        offer = {"tier": "simple", "reply": "I need your OK to close word. Say 'Jarvis, yes'."}
        self.assertEqual(self._reply_window(["ask", "yes", KeyboardInterrupt], heard, score,
                                            offer, waiting=True), ["close word"])
        # With nothing pending, a bare "Yes." still answers JARVIS.
        self.assertEqual(self._reply_window(["ask", "yes", KeyboardInterrupt], heard, score,
                                            offer, waiting=False), ["close word", "Yes."])

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_a_near_miss_jarvis_yes_never_approves_a_confirmation(self):
        # Audit 2026-11-07: a TV or roommate saying "Jarvis, yes" scored as a near miss.
        heard = {"tv": "Jarvis, yes."}
        score = {"tv": (False, 0.2)}
        offer = {"tier": "simple", "reply": "ok"}
        self.assertEqual(self._reply_window(["tv", KeyboardInterrupt], heard, score, offer,
                                            waiting=True), [])
        # Nothing waiting: a near miss with the wake word still gets its request through.
        self.assertEqual(self._reply_window(["tv", KeyboardInterrupt], heard, score, offer,
                                            waiting=False), ["yes."])

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_a_near_miss_bare_jarvis_opens_no_window(self):
        # A stranger's bare "Jarvis" must not let David's next words in without the name.
        heard = {"tv": "Jarvis.", "david": "close word"}
        score = {"tv": (False, 0.2), "david": (True, 0.7)}
        offer = {"tier": "simple", "reply": "ok"}
        self.assertEqual(self._reply_window(["tv", "david", KeyboardInterrupt], heard, score,
                                            offer), [])

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_follow_up_window_never_approves_a_confirmation(self):
        heard = {"wake": "Jarvis.", "yes": "Yes."}
        score = {"wake": (True, 0.7), "yes": (True, 0.7)}
        offer = {"tier": "simple", "reply": "ok"}
        self.assertEqual(self._reply_window(["wake", "yes", KeyboardInterrupt], heard, score,
                                            offer, waiting=True), [])
        self.assertEqual(self._reply_window(["wake", "yes", KeyboardInterrupt], heard, score,
                                            offer, waiting=False), ["Yes."])

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_a_near_miss_must_open_with_jarvis(self):
        heard = {"tv": "Stop it, Jarvis.", "near": "Jarvis, stop it."}
        score = {"tv": (False, 0.2), "near": (False, 0.2)}
        offer = {"tier": "simple", "reply": "ok"}
        self.assertEqual(self._reply_window(["tv", "near", KeyboardInterrupt], heard, score,
                                            offer), ["stop it."])

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_reply_window_survives_a_near_miss_and_skips_failures(self):
        heard = {"ask": "Jarvis, check the laptop", "tv": "turn it up", "yes": "Then check it."}
        score = {"ask": (True, 0.6), "tv": (False, 0.2), "yes": (True, 0.6)}
        offer = {"tier": "simple", "reply": "I can check it if you want."}
        self.assertEqual(self._reply_window(["ask", "tv", "yes", KeyboardInterrupt], heard,
                                            score, offer), ["check the laptop", "Then check it."])
        failed = {**offer, "outcomes": [{"ok": False, "text": "failed"}]}
        self.assertEqual(self._reply_window(["ask", "yes", KeyboardInterrupt], heard, score,
                                            failed), ["check the laptop"])

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_bare_wake_word_takes_the_next_utterance_as_the_request(self):
        from jarvis import daemon
        heard = {"wake": "Jarvis.", "rest": "open the calendar", "late": "close the door",
                 "wake2": "Jarvis", "stranger": "wire me a million dollars",
                 "near-wake": "Jarvis?", "near-rest": "delete my downloads"}
        score = {"wake": (True, 0.6), "rest": (True, 0.5), "late": (True, 0.5),
                 "wake2": (True, 0.6), "stranger": (False, 0.05),
                 "near-wake": (False, 0.28), "near-rest": (False, 0.28)}
        queue_ = ["wake", "rest", "late", "wake2", "stranger", "near-wake", "near-rest",
                  KeyboardInterrupt]
        # One clock read per utterance, at the moment it was recorded.
        clock = iter([0.0, 1.0, 2.0, 200.0, 201.0, 300.0, 301.0])

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        ears = MagicMock()
        ears.verify.side_effect = lambda audio, threshold=None, learn=False: score[audio]
        stt = MagicMock(side_effect=lambda audio: heard.get(audio, "") if isinstance(
            audio, str) else "")
        stt.device = "cpu"
        brain = Brain()
        brain.warm = MagicMock()
        real_monotonic = time.monotonic
        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=MagicMock(limit=350)), \
                patch("jarvis.speech.SpeechQueue", return_value=MagicMock()), \
                patch("jarvis.chatgpt.make_lane", return_value=brain), \
                patch("jarvis.listen.open_mic", lambda *a, **k: MagicMock()), \
                patch.object(daemon.time, "monotonic",
                             side_effect=lambda: next(clock, None) or real_monotonic()), \
                patch.object(daemon, "foreground_priority"):
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)
        feed = (self.base / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("open the calendar", feed)  # within the follow-up window: answered
        self.assertNotIn("close the door", feed)  # the window is one request only
        self.assertNotIn("million", feed)  # a stranger never fills the window
        self.assertNotIn("downloads", feed)  # two near misses never make a request

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_a_cut_off_command_is_asked_for_and_joined(self):
        # Phase 6: "Jarvis, open up" (heard 2026-10-24) went to the brain as it was.
        from jarvis import daemon
        heard = {"cut": "Jarvis, open up", "rest": "Spotify", "mumble": "Jarvis, Alel."}
        queue_ = ["cut", "rest", "mumble", KeyboardInterrupt]

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        ears = MagicMock()
        ears.verify.side_effect = lambda audio, threshold=None, learn=False: (True, 0.7)
        stt = MagicMock(side_effect=lambda audio: heard.get(audio, "") if isinstance(audio, str) else "")
        stt.stats.return_value = None
        stt.device = "cpu"
        voice = MagicMock(limit=350)
        brain = Brain()
        brain.warm = MagicMock()
        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=voice), \
                patch("jarvis.speech.SpeechQueue", return_value=MagicMock()), \
                patch("jarvis.chatgpt.make_lane", return_value=brain), \
                patch("jarvis.actions.launch_desktop_entry", return_value=(True, "Opened Spotify.")) as launch, \
                patch("jarvis.remote.Remote.call", return_value=(True, "Opened Spotify.")) as acted, \
                patch.object(daemon, "foreground_priority"):
            # Never the real app (2026-11-01: this test launched it on every suite run).
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)
        self.assertTrue(launch.called or any("Spotify" in str(c) for c in acted.call_args_list))
        said = [c.args[0] for c in voice.say.call_args_list]
        self.assertIn("Open what?", said)
        self.assertIn(daemon.UNCLEAR, said)  # "Alel." is asked again, not answered
        feed = (self.base / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("open up Spotify", feed)  # the two halves, as one request
        self.assertNotIn("Alel", feed)

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_misheard_request_is_asked_again_not_answered(self):
        from jarvis import daemon
        heard = {"mumble": ("Jarvis, for the day.", {"logprob": -1.4, "no_speech": 0.2}),
                 "repeat": ("remind me to push the branch", {"logprob": -0.2, "no_speech": 0.0})}
        queue_ = ["mumble", "repeat", KeyboardInterrupt]

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        def transcribe(audio):
            if not isinstance(audio, str):  # the warm-up clip
                return ""
            text, stats = heard.get(audio, ("", None))
            confidence[audio] = stats
            return text

        confidence = {}

        ears = MagicMock()
        ears.verify.side_effect = lambda audio, threshold=None, learn=False: (True, 0.7)
        stt = MagicMock(side_effect=transcribe)
        stt.stats.side_effect = lambda audio: confidence.pop(audio, None)
        stt.device = "cpu"
        voice = MagicMock(limit=350)
        brain = Brain()
        brain.warm = MagicMock()
        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=voice), \
                patch("jarvis.speech.SpeechQueue", return_value=MagicMock()), \
                patch("jarvis.chatgpt.make_lane", return_value=brain), \
                patch.object(daemon, "foreground_priority"):
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)
        said = [c.args[0] for c in voice.say.call_args_list]
        self.assertIn(daemon.UNCLEAR, said)
        self.assertEqual([c[0] for c in brain.calls], ["remind me to push the branch"])
        feed = (self.base / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("for the day", feed)  # the guess is never answered
        self.assertIn("voice.unclear", feed)

    def test_unsure_thresholds(self):
        from jarvis.listen import unsure
        self.assertTrue(unsure({"logprob": -1.3, "no_speech": 0.1}))
        self.assertTrue(unsure({"logprob": -0.8, "no_speech": 0.7}))  # mostly silence
        self.assertFalse(unsure({"logprob": -0.3, "no_speech": 0.7}))  # quiet but clear
        self.assertFalse(unsure({"logprob": -0.4, "no_speech": 0.1}))
        for nothing in (None, {}, {"logprob": None}, MagicMock()):
            self.assertFalse(unsure(nothing))

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_live_mic_keeps_audio_between_reads_and_drops_the_oldest(self):
        import numpy as np
        from jarvis import listen
        stream = MagicMock()
        with patch.object(listen, "_vad_model", return_value=None):
            mic = listen.LiveMic(backlog=2 * listen.FRAME / listen.RATE, stream=stream)
        stream.start.assert_called_once()
        for n in (1, 2, 3):
            mic._callback(np.full((listen.FRAME, 1), n, dtype="float32"), listen.FRAME, None, None)
        self.assertEqual([mic.read()[0], mic.read()[0]], [2, 3])  # oldest dropped, order kept
        mic._callback(np.zeros((listen.FRAME, 1), dtype="float32"), listen.FRAME, None, None)
        mic.flush()
        self.assertTrue(mic.blocks.empty())

    @unittest.skipUnless(HAVE_NUMPY, "needs the [voice] extra: pip install numpy")
    def test_listener_survives_a_failed_turn_and_keeps_listening(self):
        from jarvis import daemon
        queue_ = ["a", "b", KeyboardInterrupt]

        def record_utterance(**_):
            item = queue_.pop(0)
            if item is KeyboardInterrupt:
                raise KeyboardInterrupt
            return item

        ears = MagicMock()
        ears.verify.return_value = (True, 0.9)
        stt = MagicMock(side_effect=lambda audio: "Jarvis, tell me a joke" if isinstance(audio, str) else "")
        stt.device = "cpu"
        voice = MagicMock(limit=350)
        brain = Brain()
        calls = []

        def flaky(text, recall=(), on_sentence=None):
            calls.append(text)
            if len(calls) == 1:
                raise ValueError("boom")
            return "recovered"

        brain.ask = flaky
        with patch("jarvis.listen.record_utterance", record_utterance), \
                patch("jarvis.listen.LiveMic"), \
                patch("jarvis.listen.VoicePrint", return_value=ears), \
                patch("jarvis.listen.Transcriber", return_value=stt), \
                patch("jarvis.speech.Voice", return_value=voice), \
                patch("jarvis.speech.SpeechQueue", return_value=MagicMock()), \
                patch("jarvis.chatgpt.make_lane", return_value=brain), \
                patch("jarvis.assistant.run_task", side_effect=RuntimeError("no fallback")), \
                patch.object(daemon, "foreground_priority"), \
                patch.object(daemon.time, "sleep"):
            with self.assertRaises(KeyboardInterrupt):
                daemon.listen_loop(str(self.base), CONFIG, ROOT)
        self.assertEqual(calls, ["tell me a joke", "tell me a joke"])  # the second clip was still handled

    def test_second_listener_cannot_take_the_microphone(self):
        from jarvis.daemon import InstanceLock
        with InstanceLock(self.base):
            with self.assertRaisesRegex(RuntimeError, "already listening"):
                InstanceLock(self.base).__enter__()
        with InstanceLock(self.base):  # released on exit
            pass


# ------------------------------------------------------------------ the real CLI
class CliWorkflowTests(Temp):
    @classmethod
    def setUpClass(cls):
        cls._session = CliSession()

    @classmethod
    def tearDownClass(cls):
        cls._session.close()

    def setUp(self):
        super().setUp()
        self.cli = lambda *args, **kwargs: self._session(self.base, *args, **kwargs)

    def test_memory_loop_through_the_real_cli(self):
        added = json.loads(self.cli("mem", "add", "Dell runs the Kokoro voice",
                                  "--tag", "voice", "--importance", "0.9").stdout)
        other = json.loads(self.cli( "mem", "add", "second note").stdout)
        hits = json.loads(self.cli( "mem", "search", "kokoro").stdout)
        self.assertEqual(hits[0]["id"], added["id"])
        linked = json.loads(self.cli( "mem", "link", other["id"], added["id"],
                                   "related").stdout)
        self.assertEqual(linked["links"][0]["dst"], added["id"])
        stats = json.loads(self.cli( "mem", "stats").stdout)
        self.assertEqual((stats["items"], stats["links"]), (2, 1))

    def test_instant_question_is_answered_remembered_and_visible_to_the_console(self):
        out = json.loads(self.cli( "ask", "what day is it").stdout)
        self.assertEqual(out["lane"], "instant")
        self.assertRegex(out["reply"], r"^It's \w+day, \w+ \d+\w\w, \d{4}\.$")
        feed = (self.base / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn("turn.start", feed)
        self.assertIn("turn.done", feed)
        history = Console(CONFIG, ROOT, self.base).history()
        self.assertEqual(history[-1]["channel"], "cli")
        self.assertEqual(history[-1]["reply"]["body"], out["reply"])

    def test_route_is_a_dry_run(self):
        out = json.loads(self.cli( "route", "build a full-stack app").stdout)
        self.assertIn(out["tier"], ("simple", "complex"))
        self.assertEqual(Store(self.base).list(), [])
        self.assertEqual(Memory(self.base).stats()["items"], 0)

    def test_errors_are_one_line_with_exit_code_1(self):
        for args in (("mem", "get", "note:none"), ("show", "deadbeef"), ("run", "deadbeef"),
                     ("orchestrate", "show", "plan-ffffffff"), ("add", str(self.base / "nope.json")),
                     ("ask", "   ")):
            done = self.cli( *args, check=False)
            self.assertEqual(done.returncode, 1, args)
            self.assertTrue(done.stderr.startswith("JARVIS:"), (args, done.stderr))
            self.assertNotIn("Traceback", done.stderr, args)

    def test_task_spec_validation_through_add(self):
        bad = self.base / "bad.json"
        for spec in ({"objective": "x", "workspace": "../.."}, {"objective": "x", "mode": "root"},
                     {"objective": ""}, {"objective": "x", "provider": "gemini"}):
            bad.write_text(json.dumps(spec), encoding="utf-8")
            done = self.cli( "add", str(bad), check=False)
            self.assertEqual(done.returncode, 1, spec)
            self.assertNotIn("Traceback", done.stderr, spec)
        good = self.base / "good.json"
        good.write_text(json.dumps({"objective": "Check the code", "workspace": "."}),
                        encoding="utf-8")
        task = json.loads(self.cli( "add", str(good)).stdout)
        self.assertEqual(task["state"], "queued")
        self.assertEqual(json.loads(self.cli( "runs").stdout)[0]["id"], task["id"])
        self.assertIn("prompt-engineering subagent", self.cli( "engineer", task["id"]).stdout)

    def test_doctor_reports_without_touching_anything(self):
        out = json.loads(self.cli( "doctor").stdout)
        self.assertIn("tools", out)
        self.assertIn("devices", out)


class LocalServerWorkflowTests(Temp):
    """The actual `jarvis ui` process: boots, serves, answers, shuts down."""

    def test_ui_process_serves_api_and_page(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # The repo config may make this PC the cluster main, whose live hub already holds
        # listen_port; the test's own hub must not collide with it.
        config = json.loads((Path(ROOT) / "jarvis.config.json").read_text(encoding="utf-8"))
        config.setdefault("cluster", {})["enabled"] = False
        config["memory"] = {**config.get("memory", {}), "semantic": False}
        config["decisions"] = {**config.get("decisions", {}), "shadow": False}
        config["routing"] = {**config.get("routing", {}), "interpreter": False}
        config["vault"] = {**(config.get("vault") or {}), "enabled": False}
        config_path = Path(self.base) / "jarvis.config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-m", "jarvis", "--config", str(config_path),
                                 "--state", str(self.base), "ui", "--port", str(port)], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        url = f"http://127.0.0.1:{port}"

        def up():
            try:
                return urllib.request.urlopen(url + "/api/history", timeout=2).status == 200
            except OSError:
                return False

        wait_for(up, timeout=30)
        for path in ("/", "/api/overview", "/api/graph/system", "/api/tree", "/api/cases",
                     "/api/specialists", "/api/route?text=hello"):
            with urllib.request.urlopen(url + path, timeout=30) as res:
                self.assertEqual(res.status, 200, path)
                self.assertTrue(res.read(), path)


if __name__ == "__main__":
    unittest.main()
