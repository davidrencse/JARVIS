"""P0 baseline characterization of CURRENT (legacy) role and dispatch behavior.

These pin facts that docs/design/mobile-continuity/IMPLEMENTATION-BASELINE.md relies on. They are
NOT desired behavior: each one marks a gap the mobile-continuity plan (P2+) must close, and is
expected to be changed on purpose when a durable authority guard / execution envelope lands.
All execution is faked; no model, service, network or OS input is touched.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.cluster_cli import set_main
from jarvis.remote import Remote
from jarvis.topology import identity, is_node

DEVICES = {"arch": {"label": "Arch"}, "dell": {"label": "Dell"}}


def view(local, main):
    return {"cluster": {"enabled": True, "local_device": local, "main_device": main},
            "devices": DEVICES}


class LegacyRoleAuthority(unittest.TestCase):
    def test_config_file_alone_decides_main_so_a_stale_copy_still_claims_it(self):
        """Unbound `cluster main` no longer rewrites the role. Two config documents can still
        both read as MAIN, because identity() trusts the file it was given."""
        stale_arch = view("arch", "arch")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jarvis.config.json"
            path.write_text(json.dumps(stale_arch), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaises(ValueError) as caught:
                set_main(stale_arch, path, "dell")
            self.assertIn("not changed", str(caught.exception))
            self.assertEqual(path.read_bytes(), before)
            moved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(moved["cluster"]["main_device"], "arch")
        self.assertEqual(moved["cluster"]["local_device"], "arch")
        dell_view = view("dell", "dell")
        self.assertFalse(is_node(stale_arch))   # Arch copy: still MAIN
        self.assertFalse(is_node(dell_view))    # a second document can also say it is MAIN
        self.assertEqual(identity(stale_arch)[1], "arch")
        self.assertEqual(identity(dell_view)[1], "dell")


class LegacyDispatchEnvelope(unittest.TestCase):
    def test_main_local_effect_runs_without_hub_or_authority_context(self):
        """Remote.call on the main's own device calls run_here directly: no Hub, epoch,
        operation ID or target-bound grant is consulted (the CLI/voice/console local-effect path)."""
        config = view("arch", "arch")
        with tempfile.TemporaryDirectory() as tmp, \
                patch("jarvis.remote.run_here", return_value=(True, "ran")) as run, \
                patch("jarvis.cluster.dispatch", side_effect=AssertionError("Hub used")):
            ok, text = Remote(config, tmp).call("arch", "volume", "5")
        self.assertEqual((ok, text), (True, "ran"))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[:2], ("volume", "5"))
        self.assertIs(run.call_args.kwargs.get("config"), config)

    def test_node_request_envelope_has_no_operation_id_epoch_or_digest(self):
        """A node submits {node, kind, payload} to /v1/control only: no client idempotency key,
        authority tuple or payload digest travels with it (P2 must add them)."""
        config = view("dell", "arch")
        seen = {}

        class FakeClient:
            def __init__(self, *_args, **_kw):
                pass

            def request(self, path, body=None, timeout=45):
                seen.update(path=path, body=body)
                return {"ok": True, "text": "done"}

        with tempfile.TemporaryDirectory() as tmp, patch("jarvis.cluster.Client", FakeClient):
            ok, _text = Remote(config, tmp).call("arch", "volume", "5")
        self.assertTrue(ok)
        self.assertEqual(seen["path"], "/v1/control")
        self.assertEqual(set(seen["body"]), {"node", "kind", "payload"})
        self.assertEqual(seen["body"]["payload"], {"action": "volume", "arg": "5"})

    def test_signed_job_fields_do_not_include_authority_or_original_issue_time(self):
        """The node-visible signature covers id, node, kind, payload and `issued` only; `issued`
        is stamped when the job is handed to the node (sign_job), not when it was first requested."""
        from jarvis import deploy
        body = json.loads(deploy._job_bytes({"id": "j", "kind": "desktop", "payload": {}, "issued": 1,
                                             "epoch": 9, "main": "arch"}, "dell")
                          [len(deploy.JOB_DOMAIN):])
        self.assertEqual(set(body), {"id", "node", "kind", "payload", "issued"})


if __name__ == "__main__":
    unittest.main()
