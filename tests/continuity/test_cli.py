import argparse
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from jarvis.continuity import cli


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


if __name__ == "__main__": unittest.main()
