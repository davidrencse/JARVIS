"""ElevenLabs agent: signed sessions, voice switching, agent creation, the ask bridge."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from jarvis.agent import TOOL, Agent

NODE = shutil.which("node")
SCRIPT = Path(__file__).with_name("web_agent_test.cjs")
KEY = {"ELEVENLABS_API_KEY": "sk_" + "a" * 40}


def reply(payload):
    return io.BytesIO(json.dumps(payload).encode())


class AgentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        env = patch.dict(os.environ, KEY, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_session_needs_an_agent(self):
        with patch("jarvis.agent.urllib.request.urlopen") as net:
            with self.assertRaisesRegex(ValueError, "agent-setup"):
                Agent(self.state).session()
            net.assert_not_called()

    def test_session_mints_signed_url_and_keeps_key_server_side(self):
        agent = Agent(self.state, {"voice": {"agent_id": "agent_123"}})
        signed = "wss://api.elevenlabs.io/v1/convai/conversation?agent_id=agent_123&token=t"
        with patch("jarvis.agent.urllib.request.urlopen",
                   return_value=reply({"signed_url": signed})) as net:
            session = agent.session()
        request = net.call_args.args[0]
        self.assertIn("get-signed-url?agent_id=agent_123", request.full_url)
        self.assertEqual(request.get_header("Xi-api-key"), KEY["ELEVENLABS_API_KEY"])
        self.assertEqual(session, {"signed_url": signed, "voice_id": None})
        self.assertNotIn(KEY["ELEVENLABS_API_KEY"], json.dumps(session))

    def test_session_refuses_unexpected_host(self):
        agent = Agent(self.state, {"voice": {"agent_id": "agent_123"}})
        with patch("jarvis.agent.urllib.request.urlopen",
                   return_value=reply({"signed_url": "wss://evil.example/x"})):
            with self.assertRaises(RuntimeError):
                agent.session()

    def test_http_errors_are_explained(self):
        agent = Agent(self.state, {"voice": {"agent_id": "agent_123"}})
        error = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b""))
        with patch("jarvis.agent.urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "404.*agent-setup"):
                agent.session()

    def test_voice_switch_persists_and_is_validated(self):
        agent = Agent(self.state)
        voices = reply({"voices": [{"voice_id": "uk1", "name": "Daniel",
                                    "labels": {"gender": "male", "accent": "british"}}]})
        with patch("jarvis.speech.urllib.request.urlopen", return_value=voices):
            listed = agent.voices()
        self.assertEqual(listed["voices"][0]["voice_id"], "uk1")
        self.assertEqual(agent.set_voice("uk1"), {"current": "uk1", "name": "Daniel"})
        self.assertEqual(Agent(self.state).voice_id(), "uk1")  # read back from secrets.json
        for bad in ("not/valid", "", None, "someone_else"):
            with self.assertRaises(ValueError):
                agent.set_voice(bad)

    def test_create_registers_tool_and_voice_override(self):
        agent = Agent(self.state)
        agent.set_voice("uk1")
        with patch("jarvis.agent.urllib.request.urlopen",
                   return_value=reply({"agent_id": "agent_new"})) as net:
            result = agent.create()
        body = json.loads(net.call_args.args[0].data)
        tool = body["conversation_config"]["agent"]["prompt"]["tools"][0]
        self.assertEqual((tool["type"], tool["name"], tool["expects_response"]),
                         ("client", TOOL, True))
        self.assertEqual(body["conversation_config"]["tts"]["voice_id"], "uk1")
        self.assertTrue(body["platform_settings"]["overrides"]
                        ["conversation_config_override"]["tts"]["voice_id"])
        self.assertEqual(result["agent_id"], "agent_new")
        self.assertEqual(Agent(self.state).agent_id(), "agent_new")
        self.assertTrue(Agent(self.state).status()["configured"])


class AskBridgeTests(unittest.TestCase):
    def console(self):
        from jarvis.ui import Console
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return Console({}, temp.name, Path(temp.name) / "state")

    def test_agent_ask_returns_spoken_reply(self):
        console = self.console()
        job = {"id": "j1", "state": "done", "error": None,
               "result": {"reply": "Your **next** class is at `10:00`."}}
        with patch.object(console, "submit", return_value=job) as submit:
            result = console.agent_ask("what's next")
        submit.assert_called_once_with("what's next")
        self.assertEqual(result, {"reply": "Your next class is at 10:00.", "job": "j1"})

    def test_agent_ask_reports_errors_and_slow_jobs(self):
        console = self.console()
        with patch.object(console, "submit", return_value={
                "id": "j2", "state": "error", "error": "boom"}):
            self.assertTrue(console.agent_ask("x")["error"])
        with patch.object(console, "submit", return_value={"id": "j3", "state": "running"}):
            self.assertIn("still running", console.agent_ask("x", wait=0)["reply"])


@unittest.skipUnless(NODE, "Node.js is required for the browser agent unit tests")
class BrowserAgentTests(unittest.TestCase):
    def check_scenario(self, name):
        result = subprocess.run([NODE, str(SCRIPT), name], capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS " + name, result.stdout)

    def test_call_protocol_tool_bridge_and_cleanup(self):
        self.check_scenario("protocol")

    def test_failed_start_releases_microphone(self):
        self.check_scenario("start_failure")


if __name__ == "__main__":
    unittest.main()
