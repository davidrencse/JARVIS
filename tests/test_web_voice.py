"""Run microphone lifecycle tests with mocked browser APIs; no microphone is opened."""

import shutil
import subprocess
import unittest
from pathlib import Path


NODE = shutil.which("node")
SCRIPT = Path(__file__).with_name("web_voice_test.cjs")


@unittest.skipUnless(NODE, "Node.js is required for the browser microphone unit tests")
class BrowserMicrophoneTests(unittest.TestCase):
    def check_scenario(self, name):
        result = subprocess.run([NODE, str(SCRIPT), name], capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS " + name, result.stdout)

    def test_startup_failures_release_resources_and_allow_retry(self):
        self.check_scenario("failures_and_retry")

    def test_concurrent_starts_share_one_microphone_request(self):
        self.check_scenario("concurrent_start")

    def test_stop_during_permission_request_releases_late_stream(self):
        self.check_scenario("cancel_permission")

    def test_stop_during_worklet_load_releases_resources(self):
        self.check_scenario("cancel_worklet")

    def test_old_startup_cannot_stop_or_replace_a_new_recording(self):
        self.check_scenario("stale_start_cannot_affect_retry")

    def test_successful_recording_keeps_audio_and_releases_resources(self):
        self.check_scenario("audio_and_cleanup")

    def test_noisy_room_speech_reaches_transcription_and_jarvis(self):
        self.check_scenario("noisy_room_mic_to_transcription")

    def test_always_on_mic_rejects_noise_and_recovers_for_speech(self):
        self.check_scenario("noisy_room_wait_does_not_submit_ambience")

    def test_silence_and_context_close_error_do_not_leak_microphone(self):
        self.check_scenario("no_speech_and_close_error")

    def test_malformed_audio_is_cleaned_before_transcription(self):
        self.check_scenario("malformed_audio_is_sanitized")

    def test_recording_memory_is_bounded(self):
        self.check_scenario("recording_length_is_capped")

    def test_throwing_ui_callback_cannot_skip_microphone_cleanup(self):
        self.check_scenario("callback_error_still_releases")

    def test_old_animation_loop_cannot_continue_after_restart(self):
        self.check_scenario("old_animation_loop_stops")

    def test_ui_can_cancel_startup_and_preserves_quick_space_tap(self):
        self.check_scenario("ui_cancels_startup_and_preserves_quick_tap")


if __name__ == "__main__":
    unittest.main()
