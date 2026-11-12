"""doctor() nests main_readiness. Temp dirs and a fake hub only.

No provider call, no credential body, and no live network. ``served_code`` is not a
tested release: a test fails if doctor asks for that live-tree fallback.
"""

import argparse
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from jarvis import cluster_cli, cluster_setup

SENTINEL = "CREDENTIAL-SENTINEL-do-not-leak-9f3a"
TOKEN = "0123456789abcdef"
OTHER = "fedcba9876543210"
HOST = "arch"


def _main(enabled=True):
    return {"cluster": {"enabled": enabled, "main_device": "zenbook", "local_device": "auto",
                        "tunnel": "forward", "listen_port": 7780,
                        "main_url": "http://127.0.0.1:7780"},
            "devices": {"zenbook": {"hostname": HOST, "label": "Zenbook"},
                        "dell": {"hostname": "WinLaptop", "label": "Dell", "transport": "cluster"}}}


def _node():
    return {"cluster": {"enabled": True, "main_device": "zenbook", "local_device": "dell",
                        "tunnel": "reverse", "listen_port": 7780,
                        "main_url": "http://127.0.0.1:7780"},
            "devices": {"zenbook": {"hostname": HOST, "label": "Zenbook"},
                        "dell": {"hostname": "WinLaptop", "label": "Dell"}}}


class DoctorReadinessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.home.mkdir()
        self.state.mkdir()
        secret = self.home / ".codex" / "auth.json"
        secret.parent.mkdir()
        secret.write_text(SENTINEL + "-codex", encoding="utf-8")
        (self.state / "secret-body.txt").write_text(SENTINEL + "-state", encoding="utf-8")
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home), "JARVIS_NO_MODELS": "1"},
                                    clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)

    def _release(self, code):
        folder = self.state / "code-release"
        (folder / "jarvis").mkdir(parents=True)
        (folder / "release.json").write_text(json.dumps({
            "code": code, "at": 1, "tests": "ok", "note": SENTINEL + "-release"}), encoding="utf-8")

    def _item(self, report, name):
        found = [item for item in report["main_readiness"]["checks"] if item["check"] == name]
        self.assertEqual(len(found), 1, name)
        return found[0]

    def _assert_clean(self, report):
        blob = json.dumps(report)
        self.assertNotIn(SENTINEL, blob)
        self.assertNotIn("auth.json", blob)
        self.assertNotIn("local-exec-daemon-credential.json", blob)
        self.assertNotIn(".credentials.json", blob)
        readiness = report["main_readiness"]
        self.assertIs(readiness["ready"], False)
        self.assertIs(readiness["ai_ready"], False)
        self.assertIn("checks", readiness)
        self.assertTrue(readiness["checks"])
        self.assertIn("no provider call", " ".join(readiness["limitations"]).lower())
        return blob

    def _call(self, config, fingerprint, request):
        if isinstance(request, BaseException):
            request_patch = mock.patch.object(cluster_setup.Client, "request", side_effect=request)
        else:
            request_patch = mock.patch.object(cluster_setup.Client, "request", return_value=request)
        with ExitStack() as stack:
            for item in (
                mock.patch("jarvis.fleet.legacy_topology.socket.gethostname", return_value=HOST),
                mock.patch("jarvis.topology.socket.gethostname", return_value=HOST),
                mock.patch.object(cluster_setup, "_sshd_up", return_value=True),
                mock.patch("jarvis.deploy.code_fingerprint", return_value=fingerprint),
                mock.patch("jarvis.release.served_code", side_effect=AssertionError("served_code")),
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("urlopen")),
                mock.patch("socket.create_connection", side_effect=AssertionError("connect")),
                mock.patch.object(cluster_setup.Client, "__init__", return_value=None),
                request_patch,
            ):
                stack.enter_context(item)
            report = cluster_setup.doctor(config, self.root / "c.json", self.state)
            return report, cluster_setup.Client.request.call_count

    def test_disabled_and_identity_error_include_readiness_without_network(self):
        disabled, disabled_calls = self._call(
            {"cluster": {"enabled": False}, "devices": {}}, TOKEN, AssertionError("request"))
        self._assert_clean(disabled)
        self.assertFalse(disabled["ok"])
        self.assertIn("cluster enabled", {item["check"] for item in disabled["checks"]})
        self.assertIn("not supplied", self._item(disabled, "tested code")["detail"])
        self.assertEqual(disabled_calls, 0)

        broken = {"cluster": {"enabled": True, "main_device": "zenbook", "local_device": "auto",
                              "tunnel": "forward", "main_url": "http://127.0.0.1:7780"},
                  "devices": {"zenbook": {"hostname": "no-such"}, "dell": {"hostname": "other"}}}
        failed, failed_calls = self._call(broken, TOKEN, AssertionError("request"))
        self._assert_clean(failed)
        self.assertFalse(failed["ok"])
        self.assertIn("this PC's identity", {item["check"] for item in failed["checks"]})
        self.assertIn("not supplied", self._item(failed, "tested code")["detail"])
        self.assertEqual(failed_calls, 0)

    def test_main_code_matches_mismatches_and_is_unavailable_without_served_code(self):
        nodes = {"nodes": [{"id": "dell", "online": True, "code": TOKEN, "seen": 1}]}

        self._release(TOKEN)
        matched, calls = self._call(_main(), TOKEN, nodes)
        self._assert_clean(matched)
        self.assertEqual(calls, 1)
        self.assertTrue(self._item(matched, "tested code")["ok"])
        self.assertIn("matches the supplied tested release", self._item(matched, "tested code")["detail"])
        same = {item["check"]: item for item in matched["checks"]}
        self.assertTrue(same["dell: same code"]["ok"])

        mismatched, _ = self._call(_main(), OTHER, nodes)
        self._assert_clean(mismatched)
        item = self._item(mismatched, "tested code")
        self.assertFalse(item["ok"])
        self.assertIn("does not match", item["detail"])
        self.assertTrue({c["check"]: c for c in mismatched["checks"]}["dell: same code"]["ok"])

        bare = self.state / "code-release"
        if bare.exists():
            shutil.rmtree(bare)
        missing, _ = self._call(_main(), TOKEN, nodes)
        self._assert_clean(missing)
        item = self._item(missing, "tested code")
        self.assertFalse(item["ok"])
        self.assertIn("not supplied", item["detail"])
        link = {c["check"]: c for c in missing["checks"]}
        self.assertFalse(link["dell: same code"]["ok"])
        self.assertIn("no tested release", link["dell: same code"]["detail"])

    def test_node_code_matches_mismatches_and_is_unavailable_from_one_status_call(self):
        (self.state / "cluster-node.json").write_text(
            json.dumps({"token": SENTINEL + "-token", "node_id": "dell"}), encoding="utf-8")
        status = {"main_device": "zenbook", "code": TOKEN,
                  "nodes": [{"id": "dell", "online": True, "code": TOKEN}]}
        matched, calls = self._call(_node(), TOKEN, status)
        self._assert_clean(matched)
        self.assertEqual(calls, 1)
        self.assertEqual(matched["role"], "node")
        self.assertTrue(self._item(matched, "tested code")["ok"])
        link = {item["check"]: item for item in matched["checks"]}
        self.assertTrue(link["same code as the main"]["ok"])
        self.assertTrue(link["hub answers"]["ok"])

        mismatched, calls = self._call(_node(), OTHER, status)
        self._assert_clean(mismatched)
        self.assertEqual(calls, 1)
        self.assertFalse(self._item(mismatched, "tested code")["ok"])
        self.assertIn("does not match", self._item(mismatched, "tested code")["detail"])
        self.assertFalse({c["check"]: c for c in mismatched["checks"]}["same code as the main"]["ok"])

        missing, calls = self._call(_node(), TOKEN, ConnectionError("down"))
        self._assert_clean(missing)
        self.assertEqual(calls, 1)
        self.assertIn("not supplied", self._item(missing, "tested code")["detail"])
        self.assertFalse({c["check"]: c for c in missing["checks"]}["hub answers"]["ok"])
        self.assertNotIn("same code as the main", {c["check"] for c in missing["checks"]})

    def test_tty_renders_readiness_from_the_real_doctor(self):
        """cluster_cli doctor on a TTY prints main_readiness from the real doctor.

        Link probes and network are mocked. ``doctor`` itself is not.
        """
        config = _node()
        path = self.root / "c.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        (self.state / "cluster-node.json").write_text(json.dumps({
            "node_id": "dell", "main_device": "zenbook", "token": "t" * 32,
            "code_key": TOKEN}), encoding="utf-8")
        status = {"main_device": "zenbook", "code": TOKEN,
                  "nodes": [{"id": "dell", "online": True, "code": TOKEN}]}
        args = argparse.Namespace(
            action="doctor", config=str(path), state=str(self.state), value=None,
            main_device=None, local_device=None, url=None, hostname=None, capability=None,
            output=None, device_id=None, address=[], no_autostart=True, no_start=True,
            port=7777, rollback=False, autostart=False)
        buf = io.StringIO()
        buf.isatty = lambda: True
        with ExitStack() as stack:
            for item in (
                mock.patch("jarvis.fleet.legacy_topology.socket.gethostname", return_value="WinLaptop"),
                mock.patch("jarvis.topology.socket.gethostname", return_value="WinLaptop"),
                mock.patch.object(cluster_setup.Client, "request", return_value=status),
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("urlopen")),
                mock.patch("socket.create_connection", side_effect=AssertionError("connect")),
                mock.patch("subprocess.run", side_effect=AssertionError("subprocess.run")),
                mock.patch("subprocess.Popen", side_effect=AssertionError("subprocess.Popen")),
                mock.patch("jarvis.release.served_code", side_effect=AssertionError("served_code")),
            ):
                stack.enter_context(item)
            direct = cluster_setup.doctor(config, path, self.state)
            with mock.patch("sys.stdout", buf):
                result = cluster_cli.run(args, config, self.root)
        self._assert_clean(direct)
        self.assertIn("main_readiness", direct)
        self.assertNotIn("readiness", direct)
        self.assertIsNone(result)
        text = buf.getvalue()
        self.assertIn("hub answers", text)
        self.assertIn("candidate readiness\n", text)
        self.assertIn("control_ready:", text)
        self.assertIn("ai_ready: false\n", text)
        self.assertIn("no provider call", text.lower())
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn("auth.json", text)
        self.assertNotIn("promotion authorized", text.lower())
        self.assertNotIn("verified ai", text.lower())
        shown = [item["check"] for item in direct["main_readiness"]["checks"]
                 if isinstance(item.get("check"), str)]
        self.assertTrue(shown)
        self.assertIn(shown[0], text)
