"""JARVIS talks out loud only on the main PC; nodes stay silent (David, 2026-11-07)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jarvis import speech
from jarvis.speech import NODE_MUTED, SpeechQueue, Voice, may_speak


def config(local, main="zenbook", **voice):
    return {"cluster": {"enabled": True, "main_device": main, "local_device": local},
            "devices": {"zenbook": {"hostname": "archmain"}, "dell": {"hostname": "WinLaptop"}},
            "voice": {"engine": "kokoro", **voice}}


class MaySpeakTests(unittest.TestCase):
    def test_main_speaks(self):
        self.assertTrue(may_speak(config("zenbook")))

    def test_node_is_silent(self):
        self.assertFalse(may_speak(config("dell")))

    def test_override_lets_a_node_talk(self):
        self.assertTrue(may_speak(config("dell", nodes_speak=True)))

    def test_cluster_off_speaks(self):
        self.assertTrue(may_speak({"voice": {}}))

    def test_unknown_identity_never_mutes_the_main(self):
        cfg = config("auto")
        with mock.patch("socket.gethostname", return_value="nobody"):
            self.assertTrue(may_speak(cfg))

    def test_switching_the_main_moves_the_voice(self):
        self.assertTrue(may_speak(config("dell", main="dell")))
        self.assertFalse(may_speak(config("zenbook", main="dell")))


class SilentNodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def test_say_on_a_node_plays_and_synthesises_nothing(self):
        voice = Voice(self.state, config("dell"))
        with mock.patch.object(Voice, "synth") as synth, \
                mock.patch.object(speech, "play_wav") as play, mock.patch.object(speech, "local_tts") as tts:
            result = voice.say("Opened Chrome.")
        self.assertEqual(result, {"spoken": "", "source": NODE_MUTED})
        synth.assert_not_called(); play.assert_not_called(); tts.assert_not_called()

    def test_say_on_the_main_still_plays(self):
        voice = Voice(self.state, config("zenbook"))
        with mock.patch.object(Voice, "synth", return_value=(self.state / "x.wav", "kokoro")), \
                mock.patch.object(speech, "play_wav") as play:
            result = voice.say("Opened Chrome.")
        play.assert_called_once()
        self.assertEqual(result["spoken"], "Opened Chrome.")

    def test_speech_queue_on_a_node_queues_nothing(self):
        queue = SpeechQueue(Voice(self.state, config("dell")))
        queue.put("Hello there.")
        self.assertTrue(queue.texts.empty())

    def test_voice_agent_refused_on_a_node(self):
        from jarvis.agent import Agent
        agent = Agent(self.state, config("dell"))
        with self.assertRaises(ValueError) as caught:
            agent.session()
        self.assertIn("main PC", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class RealConfigTests(unittest.TestCase):
    """The repo's own jarvis.config.json: the Dell (by its real hostname) must be silent."""

    def setUp(self):
        import json
        path = Path(__file__).resolve().parents[2] / "jarvis.config.json"
        if not path.exists():
            self.skipTest("no jarvis.config.json")
        self.cfg = json.loads(path.read_text(encoding="utf-8"))
        cluster = self.cfg.get("cluster") or {}
        if not cluster.get("enabled") or cluster.get("local_device", "auto") != "auto":
            self.skipTest("config doesn't pick the PC by hostname")

    def test_every_node_hostname_is_silent_and_the_main_talks(self):
        main = self.cfg["cluster"]["main_device"]
        for device, info in self.cfg["devices"].items():
            with self.subTest(device=device), \
                    mock.patch("socket.gethostname", return_value=info["hostname"]):
                self.assertEqual(may_speak(self.cfg), device == main)

    def test_node_reports_speaks_no_in_its_check_in(self):
        from jarvis.cluster import NodeWorker
        main = self.cfg["cluster"]["main_device"]
        for device, info in self.cfg["devices"].items():
            worker = NodeWorker.__new__(NodeWorker)
            worker.config = self.cfg
            with self.subTest(device=device), \
                    mock.patch("socket.gethostname", return_value=info["hostname"]):
                self.assertEqual(worker._speaks(), "yes" if device == main else "no")
