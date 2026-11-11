"""Audit fixes: mic device resolution, voiceprint IO, LiveMic close, systemd autostart,
and the capture self-test's guaranteed release."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

from jarvis import autostart, listen, selftest


class FakeSD:
    def __init__(self, devices, apis):
        self.devices, self.apis = devices, apis

    def query_devices(self):
        return self.devices

    def query_hostapis(self):
        return self.apis


def dev(name, api=0, ch=1):
    return {"name": name, "max_input_channels": ch, "hostapi": api}


class MicDeviceTests(unittest.TestCase):
    def test_duplicate_name_resolves_to_wasapi_index_on_windows(self):
        sd = FakeSD([dev("Mic", 0), dev("Mic", 1), dev("Mic", 2)],
                    [{"name": "MME"}, {"name": "Windows DirectSound"},
                     {"name": "Windows WASAPI"}])
        with patch.object(listen.os, "name", "nt"):
            self.assertEqual(listen._input_device(sd, "Mic"), 2)

    def test_first_match_elsewhere_and_unique_name(self):
        sd = FakeSD([dev("Out", ch=0), dev("Mic", 0), dev("Mic", 1)], [{"name": "ALSA"}] * 2)
        self.assertEqual(listen._input_device(sd, "Mic"), 1)
        self.assertEqual(listen._input_device(FakeSD([dev("Solo")], []), "Solo"), 0)

    def test_missing_mic_falls_back_as_before(self):
        sd = FakeSD([dev("pipewire")], [])
        self.assertEqual(listen._input_device(sd, "Gone"), "pipewire")


class VoiceprintIOTests(unittest.TestCase):
    def make(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return listen.VoicePrint(tmp.name)

    def test_save_is_atomic_replace(self):
        vp = self.make()
        with patch("jarvis.fsio.os.replace", wraps=os.replace) as replace:
            vp._save({"a": 1})
        replace.assert_called_once()
        self.assertEqual(json.loads(vp.path.read_text()), {"a": 1})
        self.assertEqual([p.name for p in vp.path.parent.iterdir()], ["voiceprint.json"])

    def test_load_retries_a_half_written_file_once_briefly(self):
        vp = self.make()
        vp.path.write_text("{", encoding="utf-8")

        def heal(_):
            vp.path.write_text('{"ok": true}', encoding="utf-8")

        with patch.object(listen.time, "sleep", heal):
            self.assertEqual(vp._load(), {"ok": True})


class LiveMicCloseTests(unittest.TestCase):
    def test_close_runs_even_if_stop_raises(self):
        stream = Mock()
        stream.stop.side_effect = RuntimeError("boom")
        mic = listen.LiveMic(stream=stream)
        with self.assertRaises(RuntimeError):
            mic.close()
        stream.close.assert_called_once()


@unittest.skipIf(os.name == "nt", "systemd user unit")
class SystemdInstallTests(unittest.TestCase):
    def run_install(self, enable, existing=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        unit = Path(tmp.name, "jarvis-x.service")
        if existing is not None:
            unit.write_text(existing)
        with patch.object(autostart, "system", return_value="linux"), \
                patch.object(autostart, "path", return_value=unit), \
                patch.object(autostart.subprocess, "run") as run:
            autostart.install("jarvis-x", "X", ["python", "-m", "jarvis"], enable=enable)
        return unit, [c.args[0][2:] for c in run.call_args_list]

    def test_off_when_never_on_runs_no_systemctl(self):
        unit, calls = self.run_install(False)
        self.assertEqual(calls, [])

    def test_off_disables_unlinks_then_reloads(self):
        unit, calls = self.run_install(False, existing="x")
        self.assertFalse(unit.exists())
        self.assertEqual(calls, [["disable", "--now", "jarvis-x.service"], ["daemon-reload"]])

    def test_rewriting_an_existing_unit_restarts_it(self):
        unit, calls = self.run_install(True, existing="old")
        self.assertIn(["restart", "jarvis-x.service"], calls)

    def test_fresh_install_does_not_restart(self):
        unit, calls = self.run_install(True)
        self.assertNotIn(["restart", "jarvis-x.service"], calls)


class CaptureReleaseTests(unittest.TestCase):
    def test_cursor_released_when_captured_checks_raise_and_no_typing_if_unfocused(self):
        probe = Mock()
        probe.focused.return_value = False
        moves = []
        with patch.object(selftest, "ctypes", MagicMock()), \
                patch.object(selftest, "move_mouse", lambda x, y: moves.append(x), create=True), \
                patch.object(selftest, "wait_log", return_value=True), \
                patch.object(selftest, "guard_released", return_value=False), \
                patch.object(selftest, "_user32", return_value=MagicMock(), create=True), \
                patch.object(selftest, "_captured_checks", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                selftest.check_capture(probe)
        self.assertEqual(moves.count(80), 80)  # the release loop ran

    def test_unfocused_probe_is_never_typed_into(self):
        probe = Mock()
        probe.focused.return_value = False
        out = []
        with patch.object(selftest, "_user32", return_value=MagicMock(), create=True):
            selftest._captured_checks(probe, False, out)
        probe.type.assert_not_called()
        self.assertEqual(out[0]["flag"], "WARN")


class SystemdWordTests(unittest.TestCase):
    def test_dollar_and_percent_are_escaped(self):
        self.assertEqual(autostart._word("a$HOME"), '"a$$HOME"')
        self.assertEqual(autostart._word("100%"), "100%%")
        self.assertEqual(autostart._word("plain"), "plain")


if __name__ == "__main__":
    unittest.main()
