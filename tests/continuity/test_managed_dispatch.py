"""Real producer, broker, poll, and node admission. Temporary states and fake effects.

The hub and the node are separate processes. Nothing here touches a live machine,
a camera, or the package tree.
"""

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jarvis.cluster as cluster
from jarvis.cluster import Hub, NodeWorker, execute_local, save_json
from jarvis.continuity.executor import ExecutorGuard
from jarvis.continuity.integration import (
    ENVELOPE_KEY, IsolatedFleet, SignedAuthority, clear_production, configure_production,
    effect_digest, open_unfenced, production_issuer,
)
from jarvis.deploy import code_key, sign_job
from jarvis.remote import Remote
from tests.continuity._fleet import Clock

ROOT = Path(__file__).resolve().parents[2]
BOOT = "boot-dell-cluster"

CHILD = textwrap.dedent("""
    import json, os, sys, traceback
    from pathlib import Path
    os.environ["JARVIS_NO_MODELS"] = "1"
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        import jarvis.cluster as cluster
        cluster.POLL_WAIT = 0.3
        import jarvis.deploy as deploy
        def refuse_bundle(*args, **kwargs):
            raise SystemExit("refusing to install a code bundle")
        deploy.apply_bundle = refuse_bundle
        from jarvis.cluster import NodeWorker
        from jarvis.continuity.executor import ExecutorGuard
        from jarvis.continuity.gateway import AuthorityClient
        from jarvis.continuity.integration import (
            SignedAuthority, clear_production, configure_production)

        wall = int(spec["wall"])
        client = AuthorityClient(
            spec["origin"], authority_id="oracle-auth", device_id="dell",
            installation_id="dell-inst", private_key_path=spec["dell_key"],
            wall=lambda: wall)
        authority = SignedAuthority(client, {
            "zenbook": spec["zenbook_pub"], "dell": spec["dell_pub"]})
        guard = ExecutorGuard(
            spec["journal"], device_id="dell", installation_id="dell-inst",
            fleet_id="home", authority_id="oracle-auth", authority=authority,
            verifier=authority.verify, boot=lambda: "boot-dell-cluster", wall=lambda: wall)

        class Runtime:
            def require_authority(self, effect_class, target=None):
                return {"ready": True}

            def release_before_suspend(self):
                return {"released": True, "code": "released",
                        "admission_stopped": True, "invalidated": True}

        configure_production(spec["node"], Runtime(), executor=guard)
        path = Path(spec["effects"])
        effects = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

        def executor(kind, payload):
            effects.append({"kind": kind, "payload": payload})
            path.write_text(json.dumps(effects), encoding="utf-8")
            return {"ok": True, "text": "once"}

        config = json.loads(Path(spec["config"]).read_text(encoding="utf-8"))
        worker = NodeWorker(config, spec["node"], executor=executor)
        try:
            worker.once()
        finally:
            guard.close()
            clear_production(spec["node"])
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
""")


def fleet_config(port):
    return {
        "cluster": {
            "enabled": True, "main_device": "zenbook", "local_device": "zenbook",
            "main_url": "http://127.0.0.1:%d" % port, "auto_update": False,
        },
        "clipboard": {"autostart": False},
        "devices": {"zenbook": {"hostname": "zenbook"}, "dell": {"hostname": "dell"}},
    }


class Runtime:
    def __init__(self):
        self.calls = []

    def require_authority(self, effect_class, target=None):
        self.calls.append((effect_class, target))
        return {"ready": True}

    def release_before_suspend(self):
        return {"released": True, "code": "released",
                "admission_stopped": True, "invalidated": True}


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class ManagedDispatch(unittest.TestCase):
    def test_producer_poll_runs_once_and_a_fenced_epoch_refuses(self):
        cluster.POLL_WAIT = 0.3
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        clock = Clock()
        fleet = IsolatedFleet(root / "fleet", clock)
        hub = None
        guard = None
        try:
            fleet.cutover()
            fleet.promote("zenbook")
            status = fleet.status()
            self.assertEqual(status["mode"], "V2_ACTIVE")
            wall = int(fleet.clock.wall())
            issued = []
            base = production_issuer(
                fleet._keys["zenbook"][1], fleet.nodes["zenbook"].client, wall=lambda: wall)

            def issuer(*args):
                pair = base(*args)
                issued.append(pair)
                return pair

            main = root / "main"
            node = root / "node"
            main.mkdir()
            node.mkdir()
            runtime = Runtime()
            config = fleet_config(9)
            configure_production(
                main, runtime, executor=fleet.nodes["zenbook"].executor, issuer=issuer)
            remote = Remote(config, main)
            with patch("jarvis.inventory.collect", return_value={"local": True}) as collect, \
                    patch("jarvis.remote.run_here", return_value=(True, "apps")):
                local = execute_local("inventory", {}, config, main)
                ok, text = remote.call("zenbook", "list_apps", "")
            self.assertEqual(local, {"local": True})
            self.assertEqual((ok, text), (True, "apps"))
            self.assertEqual(collect.call_count, 1)
            self.assertEqual([item[1]["target"] for item in issued], ["zenbook", "zenbook"])
            for signed, ticket in issued:
                receipt = fleet.nodes["zenbook"].executor.reconcile(
                    ticket["operation_id"], ticket["attempt_id"])
                self.assertEqual(receipt["state"], "COMPLETED")
                self.assertEqual(receipt["effect"], "completed")
            issuer_source = inspect.getsource(production_issuer)
            self.assertIn("sign_message", issuer_source)
            self.assertNotIn("return True", issuer_source)

            journal = node / "cluster-executor.sqlite"
            authority = SignedAuthority(fleet.nodes["dell"].client, {
                "zenbook": fleet.public("zenbook"), "dell": fleet.public("dell")})
            guard = self.open_guard(journal, authority, wall)
            fresh = fleet.nodes["dell"].client.status()
            guard.fence(fresh["incarnation"], 0, fresh["mode_version"])
            guard.close()
            guard = None

            hub = Hub(config, main, main)
            hub.start(host="127.0.0.1", port=0)
            port = hub.server.server_address[1]
            config["cluster"]["main_url"] = "http://127.0.0.1:%d" % port
            token = hub.broker.enroll("dell")
            save_json(node / "cluster-node.json", {
                "node_id": "dell", "main_device": "zenbook", "token": token,
                "code_key": code_key(main),
            })
            node_config = dict(config)
            node_config["cluster"] = dict(config["cluster"], local_device="dell")
            config_path = root / "node-config.json"
            config_path.write_text(json.dumps(node_config), encoding="utf-8")
            effects = root / "effects.json"
            spec = {
                "origin": fleet.origin, "wall": wall, "node": str(node),
                "journal": str(journal), "effects": str(effects),
                "config": str(config_path), "dell_key": str(fleet._keys["dell"][1]),
                "zenbook_pub": fleet.public("zenbook"), "dell_pub": fleet.public("dell"),
            }
            spec_path = root / "child.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")

            hub.broker.heartbeat("dell", {"hostname": "dell"})
            box = self.dispatch(hub, "inventory", {})
            row = self.pending(hub, box)
            payload = json.loads(row["payload"])
            self.assertEqual(row["kind"], "inventory")
            self.assertEqual(set(payload), {ENVELOPE_KEY})
            envelope = payload[ENVELOPE_KEY]
            digest = effect_digest("inventory", {})
            self.assertEqual(envelope["admission_ticket"]["digest"], digest)
            self.assertEqual(envelope["signed_operation"]["digest"], digest)
            self.assertEqual(envelope["admission_ticket"]["target"], "dell")
            self.assertIn("BEGIN SSH SIGNATURE", envelope["signed_operation"]["signature"])
            self.child(spec_path)
            self.finish(box)
            self.assertEqual(box["value"], {"ok": True, "text": "once"})
            self.assertEqual(json.loads(effects.read_text(encoding="utf-8")),
                             [{"kind": "inventory", "payload": {}}])

            self.child(spec_path)
            self.assertEqual(len(json.loads(effects.read_text(encoding="utf-8"))), 1)

            (node / "cluster-jobs-seen.json").unlink()
            replayed = []
            guard = self.open_guard(journal, authority, wall)
            configure_production(node, Runtime(), executor=guard)
            job = sign_job(
                {"id": "job-replay", "kind": "inventory", "payload": payload, "remaining": 30},
                "dell", main)
            worker = self.worker(node, job, lambda kind, body: replayed.append(body))
            with patch("jarvis.hostmeter.cached_sample", return_value=None):
                self.assertTrue(worker.once())
            self.assertEqual(replayed, [])
            guard.close()
            guard = None
            clear_production(node)

            guard = self.open_guard(journal, authority, wall)
            current = fleet.nodes["dell"].client.status()
            guard.fence(current["incarnation"], current["epoch"], current["mode_version"])
            guard.close()
            guard = None
            hub.broker.heartbeat("dell", {"hostname": "dell"})
            refused = self.dispatch(hub, "inventory", {})
            self.pending(hub, refused)
            self.child(spec_path)
            self.finish(refused)
            self.assertIn("stale", str(refused["error"]))
            self.assertEqual(len(json.loads(effects.read_text(encoding="utf-8"))), 1)
        finally:
            if guard is not None:
                guard.close()
            if hub is not None:
                hub.close()
            clear_production()
            open_unfenced()
            fleet.close()

    def open_guard(self, journal, authority, wall):
        return ExecutorGuard(
            journal, device_id="dell", installation_id="dell-inst", fleet_id="home",
            authority_id="oracle-auth", authority=authority, verifier=authority.verify,
            boot=lambda: BOOT, wall=lambda: wall)

    def dispatch(self, hub, kind, payload):
        box = {}

        def run():
            try:
                box["value"] = hub.dispatch("dell", kind, payload, timeout=20)
            except Exception as exc:
                box["error"] = exc

        box["thread"] = threading.Thread(target=run)
        box["thread"].start()
        return box

    def pending(self, hub, box):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if "error" in box:
                self.fail(box["error"])
            with hub.broker.db() as db:
                rows = db.execute(
                    "SELECT id, kind, payload, state FROM jobs WHERE state='pending'").fetchall()
            if rows:
                return rows[0]
            time.sleep(0.02)
        self.finish(box)
        self.fail(box.get("error") or "job was not queued")

    def finish(self, box):
        box["thread"].join(timeout=25)
        self.assertFalse(box["thread"].is_alive(), "dispatch did not finish")

    def child(self, spec_path):
        env = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_")}
        env["JARVIS_NO_MODELS"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", CHILD, str(spec_path)], cwd=ROOT, env=env,
            capture_output=True, text=True, timeout=45, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:] + proc.stdout[-500:])

    def worker(self, state, job, executor):
        class Client:
            main = "zenbook"

            def request(self, path, body=None, timeout=45):
                if path == "/v1/poll":
                    return {"job": job}
                return {}

        worker = NodeWorker.__new__(NodeWorker)
        worker.config = fleet_config(9)
        worker.config["cluster"]["local_device"] = "dell"
        worker.state = state
        worker.client = Client()
        worker.executor = executor
        worker.code = "test-code"
        worker.outbox = state / "cluster-outbox.json"
        worker.stats = {"started": 0, "polls_ok": 0, "polls_failed": 0, "outages": 0,
                        "results_sent": 0, "results_resent": 0, "last_ok": None, "last_error": None}
        worker._down = False
        worker._confirmed = True
        worker._speaks = lambda: "no"
        worker._can_pull = lambda: False
        worker._record = NodeWorker._record.__get__(worker)
        return worker


if __name__ == "__main__":
    unittest.main()
