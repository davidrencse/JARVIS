import argparse
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from jarvis.config_lock import held
from jarvis.continuity import cli, hub_control


class FakeClient:
    def __init__(self): self.calls = []
    def status(self): self.calls.append(("status",)); return {"mode": "LEGACY_ACTIVE", "mode_version": 4, "epoch": 2}
    def acknowledge(self, *args): self.calls.append(("ack", *args)); return {"ok": True}
    def set_mode(self, *args): self.calls.append(("mode", *args)); return {"ok": True}
    def enroll(self, record): self.calls.append(("enroll", record)); return {"ok": True}


class CliTests(unittest.TestCase):
    def invoke(self, action, **kw):
        client = FakeClient(); out=[]
        args=SimpleNamespace(config="unused", continuity_action=action, yes=True, **kw)
        code=cli.run(args, client_factory=lambda _: (client, {"enrollment": {"node": {"device_id":"node"}}}), output=out.append)
        return code, client.calls, out

    def test_status_and_ack(self):
        self.assertEqual(self.invoke("status")[0], 0)
        code,calls,_=self.invoke("ack"); self.assertEqual(code,0); self.assertEqual(calls[-1], ("ack",4,2))

    def test_mode_edge_and_v2_active_refused(self):
        code,calls,_=self.invoke("mode", mode="PREPARING_V2", expect_version=4)
        self.assertEqual(code,0); self.assertIn(("mode","PREPARING_V2",4),calls)
        code,calls,_=self.invoke("mode", mode="V2_ACTIVE", expect_version=4)
        self.assertEqual(code,1); self.assertFalse(any(c[0]=="mode" for c in calls))

    def test_ack_next_version_requires_quiescence(self):
        code, calls, out = self.invoke("ack", next_version=True)
        self.assertEqual(code, 1)
        self.assertFalse(any(c[0] == "ack" for c in calls))
        self.assertIn("requires QUIESCED", out[0])

    def test_ack_next_version_fences_the_upcoming_mode(self):
        for mode in ("QUIESCED", "ROLLBACK_QUIESCED"):
            with self.subTest(mode=mode), mock.patch.object(
                    FakeClient, "status", return_value={"mode": mode, "mode_version": 4, "epoch": 2}):
                code, calls, _ = self.invoke("ack", next_version=True)
                self.assertEqual(code, 0)
                self.assertEqual(calls, [("ack", 5, 2)])

    def test_enroll_remove_fallback(self):
        self.assertEqual(self.invoke("enroll", device="node")[0],0)
        code,_,out=self.invoke("remove", device="node")
        self.assertEqual(code,2); self.assertIn("not available yet",out[0])

    def test_remove_uses_disable_participant(self):
        client = FakeClient(); client.disable_participant = lambda d, i: client.calls.append(("disable", d, i)) or {"ok": True}
        args = SimpleNamespace(config="unused", continuity_action="remove", yes=True, device="node", installation=None)
        code = cli.run(args, client_factory=lambda _: (client, {"enrollment": {"node": {"installation_id": "inst-1"}}}), output=[].append)
        self.assertEqual(code, 0); self.assertIn(("disable", "node", "inst-1"), client.calls)

    def test_mutation_declined_without_yes(self):
        client = FakeClient()
        args = SimpleNamespace(config="unused", continuity_action="ack", yes=False)
        code = cli.run(args, client_factory=lambda _: (client, {}), output=[].append, confirm=lambda _: "n")
        self.assertEqual(code, 1); self.assertFalse(any(c[0] == "ack" for c in client.calls))

    def test_parser_has_config(self):
        parser = argparse.ArgumentParser(); cli.add_arguments(parser)
        args = parser.parse_args(["--config", "c.json", "remove", "node"])
        self.assertEqual(args.config, "c.json")
        args = parser.parse_args(["--config", "c.json", "ack", "--next-version"])
        self.assertTrue(args.next_version)
        args = parser.parse_args(["--config", "c.json", "main", "dell",
                                   "--hub-config", "h.json", "--state", "state"])
        self.assertEqual((args.device, args.hub_config, args.recovery_state), ("dell", "h.json", "state"))

    def test_return_retire_requires_reviewed_hashes_and_explicit_confirmation(self):
        parser = argparse.ArgumentParser()
        cli.add_arguments(parser)
        command = ['--config', 'operator.json', 'return-retire', '--hub-config', 'hub.json',
                   '--state', 'state', '--incoming-memory-sha256', 'a'*64,
                   '--current-memory-sha256', 'b'*64, '--freeze-sha256', 'c'*64]
        args = parser.parse_args(command)
        client = FakeClient()
        factory = lambda _: (client, {})
        with mock.patch('jarvis.continuity.return_brain.retire_interrupted') as retire:
            code = cli.run(args, client_factory=factory, output=[].append, confirm=lambda _: 'n')
            self.assertEqual(code, 1)
            retire.assert_not_called()
        with tempfile.TemporaryDirectory() as root:
            config = Path(root)/'hub.json'
            config.write_text(json.dumps(HUB))
            args.hub_config = str(config)
            args.yes = True
            with mock.patch('jarvis.continuity.return_brain.retire_interrupted',
                            return_value={'ok': True, 'status': 'return_review_retired'}) as retire:
                code = cli.run(args, client_factory=factory, output=[].append)
                self.assertEqual(code, 0)
                retire.assert_called_once_with(HUB, 'state', client, incoming_sha256='a'*64,
                    current_sha256='b'*64, freeze_sha256='c'*64, confirmed=True)


HUB = {
    "cluster": {"enabled": True, "local_device": "zenbook", "main_device": "zenbook",
                "main_url": "http://127.0.0.1:7780"},
    "devices": {"zenbook": {}, "dell": {}},
}


class SafeMainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.hub_path = self.root / "jarvis.config.json"
        self.hub_path.write_text(json.dumps(HUB), encoding="utf-8")
        self.before = self.hub_path.read_bytes()
        self.client = FakeClient()

    def args(self, device="dell"):
        return SimpleNamespace(config="unused", continuity_action="main", yes=True, device=device,
                               hub_config=str(self.hub_path), recovery_state=str(self.state))

    def switch(self, request, device="dell", operator="zenbook", overlay=None):
        def factory(_path):
            return self.client, {"device_id": operator, "enrollment": {}}

        def effective(config, state):
            self.overlay = (config["cluster"]["main_device"], state)
            if overlay is None:
                return config
            return overlay(config, state)

        with mock.patch("jarvis.continuity.repoint.effective_config", side_effect=effective), \
                mock.patch.object(hub_control, "request_main", side_effect=request) as posted, \
                mock.patch("jarvis.cluster.Hub.dispatch", side_effect=AssertionError("dispatch")), \
                mock.patch("jarvis.config_lock.config_mutation", side_effect=AssertionError("fleet lock")):
            out = []
            code = cli.run(self.args(device), client_factory=factory, output=out.append)
        return code, posted, out

    def receipt(self):
        return json.loads((self.state / "continuity-main-receipt.json").read_text(encoding="utf-8"))

    def test_decline_does_not_post_or_write_a_receipt(self):
        args = self.args()
        args.yes = False
        with mock.patch.object(hub_control, "switch_main", side_effect=AssertionError("called")):
            code = cli.run(args, client_factory=lambda _: (self.client, {"device_id": "zenbook"}),
                           output=[].append, confirm=lambda _: "n")
        self.assertEqual(code, 1)
        self.assertFalse((self.state / "continuity-main-receipt.json").exists())
        self.assertEqual(self.client.calls, [])

    def test_success_records_a_receipt_without_touching_the_role_or_dispatch(self):
        def request(config, state, device):
            self.assertFalse(held(state))
            self.assertEqual(config["cluster"]["main_url"], "http://127.0.0.1:9")
            raw = json.loads((Path(state) / "continuity-main-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["publication"], "outcome_unknown")
            self.assertFalse(raw["ok"])
            return {"ok": True, "status": "ready", "publication": "published", "main": device,
                    "transition_id": "tr-1", "epoch": 4, "mutated": True, "lease_id": "secret-lease"}

        def overlay(config, _state):
            changed = json.loads(json.dumps(config))
            changed["cluster"]["main_url"] = "http://127.0.0.1:9"
            return changed

        code, posted, out = self.switch(request, overlay=overlay)
        self.assertEqual(code, 0)
        self.assertEqual(posted.call_count, 1)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.hub_path.read_bytes(), self.before)
        self.assertFalse((self.state / "main-handoff.json").exists())
        saved = self.receipt()
        self.assertEqual(saved["publication"], "published")
        self.assertEqual(saved["transition_id"], "tr-1")
        self.assertTrue(saved["ok"])
        self.assertNotIn("lease_id", json.dumps(saved))
        self.assertNotIn("secret-lease", json.dumps(saved))
        self.assertEqual((self.state / "continuity-main-receipt.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.overlay[0], "zenbook")
        self.assertIn("tr-1", out[0])

    def test_lost_response_retries_the_same_target_and_keeps_the_transition(self):
        calls = []

        def request(config, state, device):
            calls.append(device)
            if len(calls) == 1:
                raise ConnectionError("lost")
            return {"ok": True, "status": "ready", "publication": "published", "main": device,
                    "transition_id": "tr-old", "epoch": 2, "mutated": True}

        (self.state / "continuity-main-receipt.json").write_text(json.dumps({
            "schema": 1, "target": "dell", "publication": "pending", "ok": False,
            "status": "pending", "transition_id": "tr-old", "epoch": 2, "mutated": False,
        }), encoding="utf-8")
        code, posted, out = self.switch(request)
        self.assertEqual(code, 1)
        self.assertIn("lost", out[0])
        self.assertEqual(self.receipt()["publication"], "outcome_unknown")
        self.assertEqual(self.receipt()["transition_id"], "tr-old")
        code, posted, _out = self.switch(request)
        self.assertEqual(code, 0)
        self.assertEqual(posted.call_count, 1)
        self.assertEqual(calls, ["dell", "dell"])
        self.assertEqual(self.receipt()["publication"], "published")
        self.assertEqual(self.hub_path.read_bytes(), self.before)

    def test_open_receipt_for_another_device_does_not_post(self):
        path = self.state / "continuity-main-receipt.json"
        body = json.dumps({"schema": 1, "target": "zenbook", "publication": "pending", "ok": False,
                           "mutated": False, "transition_id": "tr-z"})
        path.write_text(body, encoding="utf-8")
        code, posted, out = self.switch(lambda *_a: (_ for _ in ()).throw(AssertionError("posted")))
        self.assertEqual(code, 1)
        self.assertEqual(posted.call_count, 0)
        self.assertIn("already recorded", out[0])
        self.assertEqual(path.read_text(encoding="utf-8"), body)
        self.assertEqual(self.hub_path.read_bytes(), self.before)

    def test_corrupt_and_symlink_receipts_are_left_alone(self):
        path = self.state / "continuity-main-receipt.json"
        path.write_text("{", encoding="utf-8")
        code, posted, out = self.switch(lambda *_a: (_ for _ in ()).throw(AssertionError("posted")))
        self.assertEqual(code, 1)
        self.assertEqual(posted.call_count, 0)
        self.assertIn("unusable", out[0])
        self.assertEqual(path.read_text(encoding="utf-8"), "{")
        path.unlink()
        other = self.root / "other.json"
        other.write_text("keep", encoding="utf-8")
        os.symlink(other, path)
        code, posted, _out = self.switch(lambda *_a: (_ for _ in ()).throw(AssertionError("posted")))
        self.assertEqual(code, 1)
        self.assertEqual(posted.call_count, 0)
        self.assertEqual(other.read_text(encoding="utf-8"), "keep")
        self.assertTrue(path.is_symlink())

    def test_operator_mismatch_refuses_before_a_receipt(self):
        code, posted, out = self.switch(lambda *_a: {"ok": True}, operator="dell")
        self.assertEqual(code, 1)
        self.assertEqual(posted.call_count, 0)
        self.assertIn("does not belong", out[0])
        self.assertFalse((self.state / "continuity-main-receipt.json").exists())

    def test_dispatch_stays_reachable_during_the_post(self):
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def request(_config, state, device):
            self.assertFalse(held(state))
            entered.set()
            self.assertTrue(release.wait(2))
            self.assertFalse(held(state))
            return {"ok": False, "status": "pending", "publication": "pending", "main": "zenbook",
                    "transition_id": "tr-live", "mutated": False}

        box = {}

        def invoke():
            box["result"] = self.switch(request)

        worker = threading.Thread(target=invoke)
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assertFalse(held(self.state))
        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        code, posted, _out = box["result"]
        self.assertEqual(code, 1)
        self.assertEqual(posted.call_count, 1)
        self.assertEqual(self.receipt()["publication"], "pending")
        self.assertEqual(self.hub_path.read_bytes(), self.before)


if __name__ == "__main__": unittest.main()
