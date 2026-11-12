"""TTY rendering of cluster doctor's nested main_readiness.

cluster_setup.doctor is mocked. Nothing here starts a cluster, opens a socket,
or reads a credential. Piped stdout still returns the doctor dict unchanged.
"""

import argparse
import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from jarvis import cluster_cli

SENTINEL = "WS2-DOCTOR-SENTINEL-do-not-print-7c1e"
_MISSING = object()
LIMIT_READY = (
    "ready is false: this diagnostic never authorizes promotion, failover, sleep, or cutover."
)
LIMIT_AI = (
    "ai_ready is false unless a provider session, quota, and mediated lane are "
    "independently verified."
)


def _args(path, state):
    return argparse.Namespace(
        action="doctor", config=str(path), state=str(state), value=None,
        main_device=None, local_device=None, url=None, hostname=None, capability=None,
        output=None, device_id=None, address=[], no_autostart=True, no_start=True,
        port=7777, rollback=False, autostart=False)


class DoctorCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "jarvis.config.json"
        self.state = self.root / "state"
        self.state.mkdir()
        self.path.write_text("{}\n", encoding="utf-8")
        self.config = {"cluster": {"enabled": True, "local_device": "zenbook"}}

    def _readiness(self, **overrides):
        body = {
            "ready": False,
            "control_ready": True,
            "ai_ready": False,
            "checks": [
                {
                    "check": "python runtime",
                    "ok": True,
                    "detail": "Python 3.12 satisfies requires-python",
                    "evidence": SENTINEL,
                },
                {
                    "check": "vault storage",
                    "ok": False,
                    "detail": "missing",
                    "fix": "Enable the local vault or choose another candidate.",
                    "path": {"secret": SENTINEL},
                },
            ],
            "limitations": [LIMIT_READY, LIMIT_AI],
            "credential_paths": [SENTINEL],
        }
        body.update(overrides)
        return body

    def _report(self, *, ok=True, readiness=_MISSING):
        report = {
            "ok": ok,
            "role": "node",
            "checks": [{
                "check": "hub answers",
                "ok": ok,
                "detail": "http://127.0.0.1:7777" if ok else "down",
                "trace": SENTINEL,
                **({} if ok else {"fix": "jarvis cluster run"}),
            }],
            "debug": SENTINEL,
        }
        if readiness is not _MISSING:
            report["main_readiness"] = readiness
        return report

    @contextmanager
    def _patched(self, report):
        with mock.patch("jarvis.cluster_setup.doctor", return_value=report) as doctor, \
                mock.patch("jarvis.fleet.readiness.main_readiness",
                           side_effect=AssertionError("cli computes readiness")), \
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen")):
            yield doctor

    def _run(self, report, *, tty):
        buf = io.StringIO()
        if tty:
            buf.isatty = lambda: True
        with mock.patch("sys.stdout", buf), self._patched(report) as doctor:
            result = cluster_cli.run(_args(self.path, self.state), self.config, self.root)
        return result, buf.getvalue(), doctor

    def test_tty_prints_public_readiness_without_authorizing_promotion(self):
        report = self._report(readiness=self._readiness())
        before = json.dumps(report, sort_keys=True)
        result, text, doctor = self._run(report, tty=True)
        self.assertIsNone(result)
        self.assertEqual(json.dumps(report, sort_keys=True), before)
        self.assertEqual(text, (
            "ok    hub answers: http://127.0.0.1:7777\n"
            "All good.\n"
            "\n"
            "candidate readiness\n"
            "ok    python runtime: Python 3.12 satisfies requires-python\n"
            "FAIL  vault storage: missing\n"
            "      fix: Enable the local vault or choose another candidate.\n"
            "control_ready: true\n"
            "ai_ready: false\n"
            f"limit: {LIMIT_READY}\n"
            f"limit: {LIMIT_AI}\n"
        ))
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("credential_paths", text)
        self.assertNotIn("evidence", text)
        lowered = text.lower()
        self.assertNotIn("promotion authorized", lowered)
        self.assertNotIn("authorized to promote", lowered)
        self.assertNotIn("verified ai", lowered)
        self.assertNotIn("ai verified", lowered)
        doctor.assert_called_once_with(self.config, self.path.resolve(), self.state.resolve())

    def test_true_flags_do_not_assert_authorization_or_coerce_integers(self):
        granted = self._readiness(
            ready=True, control_ready=True, ai_ready=True, limitations=["still diagnostic only"])
        text = self._run(self._report(readiness=granted), tty=True)[1]
        self.assertIn("control_ready: true\n", text)
        self.assertIn("ai_ready: true\n", text)
        self.assertIn("limit: still diagnostic only\n", text)
        self.assertNotIn("promotion authorized", text.lower())
        self.assertNotIn("authorized to promote", text.lower())
        self.assertNotIn("verified ai", text.lower())
        self.assertNotIn("ai verified", text.lower())
        self.assertNotIn("\nready:", text)

        coerced = self._readiness(control_ready=1, ai_ready=1, limitations=[])
        flags = self._run(self._report(readiness=coerced), tty=True)[1]
        self.assertIn("control_ready: false\n", flags)
        self.assertIn("ai_ready: false\n", flags)
        self.assertNotIn("limit:", flags)

    def test_piped_returns_the_doctor_report_unchanged(self):
        report = self._report(readiness=self._readiness())
        result, text, doctor = self._run(report, tty=False)
        self.assertIs(result, report)
        self.assertEqual(text, "")
        self.assertIn(SENTINEL, json.dumps(result))
        self.assertEqual(result["main_readiness"]["credential_paths"], [SENTINEL])
        doctor.assert_called_once_with(self.config, self.path.resolve(), self.state.resolve())

    def test_missing_main_readiness_keeps_the_link_checklist(self):
        failed = self._report(ok=False)
        text = self._run(failed, tty=True)[1]
        self.assertEqual(text, (
            "FAIL  hub answers: down\n"
            "      fix: jarvis cluster run\n"
            "Fix the first FAIL, then run jarvis cluster doctor again.\n"
        ))
        self.assertNotIn("candidate readiness", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("promotion authorized", text.lower())

        for label, readiness in ("absent", _MISSING), ("none", None), ("text", SENTINEL):
            with self.subTest(label=label):
                report = self._report(ok=True) if readiness is _MISSING else self._report(
                    ok=True, readiness=readiness)
                shown = self._run(report, tty=True)[1]
                self.assertEqual(shown, "ok    hub answers: http://127.0.0.1:7777\nAll good.\n")
                self.assertNotIn(SENTINEL, shown)
                self.assertNotIn("candidate readiness", shown)

    def test_link_ok_stays_independent_of_candidate_readiness(self):
        healthy_link = self._report(ok=True, readiness=self._readiness(control_ready=False))
        text = self._run(healthy_link, tty=True)[1]
        self.assertIn("All good.\n", text)
        self.assertIn("FAIL  vault storage: missing\n", text)
        self.assertIn("control_ready: false\n", text)

        broken_link = self._report(ok=False, readiness=self._readiness(
            control_ready=True, ai_ready=False, checks=[], limitations=[LIMIT_READY]))
        other = self._run(broken_link, tty=True)[1]
        self.assertIn("Fix the first FAIL, then run jarvis cluster doctor again.\n", other)
        self.assertIn("control_ready: true\n", other)
        self.assertIn(f"limit: {LIMIT_READY}\n", other)
        self.assertNotIn("promotion authorized", other.lower())

    def test_nonpublic_readiness_values_are_not_stringified(self):
        readiness = self._readiness(limitations=["public limit", {"secret": SENTINEL}, "", None])
        readiness["checks"] = [
            {"check": "python runtime", "ok": True, "detail": {"leak": SENTINEL}},
            {"ok": True, "detail": SENTINEL},
            "not-a-check",
            {"check": "sqlite", "ok": False, "detail": "unavailable", "fix": {"leak": SENTINEL}},
        ]
        text = self._run(self._report(readiness=readiness), tty=True)[1]
        self.assertIn("ok    python runtime: \n", text)
        self.assertIn("FAIL  sqlite: unavailable\n", text)
        self.assertNotIn("fix:", text)
        self.assertIn("limit: public limit\n", text)
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("not-a-check", text)
