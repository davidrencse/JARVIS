"""Managed production fences at the real effect boundaries.

Effects are fakes. State files are temporary. A fresh interpreter proves the
durable marker still refuses when the in-process binding is gone.
"""

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis.cluster import Hub, NodeWorker, execute_local, save_json
from jarvis.continuity.authority import AuthorityError
from jarvis.continuity.executor import ExecutorGuard
from jarvis.continuity.integration import (
    ENVELOPE_KEY, IntegrationError, clear_production, close_unfenced, configure_production,
    continuity_state, effect_digest, managed_node_disposition, open_unfenced,
    production_binding, production_issuer, release_for_suspend, require_production_authority,
    run_admitted_node_job, seal_job_payload,
)
from jarvis.continuity.runtime import RuntimeRefusal
from jarvis.remote import Remote
from jarvis import rest


SECRET = "super-secret-token"
PASSPHRASE = "vault-passphrase-xyz"
ROOT = Path(__file__).resolve().parents[2]

CHILD = textwrap.dedent("""
    import json, sys
    from jarvis.cluster import execute_local
    from jarvis.continuity.integration import production_binding, require_production_authority
    state, config = sys.argv[1], json.loads(sys.argv[2])
    direct = None
    try:
        require_production_authority(state, config, "remote_call", target="zenbook")
    except Exception as exc:
        direct = getattr(exc, "code", type(exc).__name__)
    ran = False
    code = None
    try:
        execute_local("inventory", {}, config, state)
        ran = True
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
    marker = open(state + "/continuity-managed.json", encoding="utf-8").read()
    json.dump({
        "direct": direct, "ran": ran, "code": code,
        "binding": production_binding(state) is not None, "marker": marker,
    }, sys.stdout)
""")


def fleet(local="zenbook", main="zenbook"):
    return {
        "cluster": {
            "enabled": True, "main_device": main, "local_device": local,
            "main_url": "http://127.0.0.1:7780",
        },
        "devices": {
            "zenbook": {"hostname": "zenbook"},
            "dell": {"hostname": "dell", "transport": "ssh", "target": "dell.example",
                     "jarvis_dir": "jarvis"},
        },
    }


class Runtime:
    def __init__(self):
        self.calls = []
        self.releases = []
        self.effect = "ok"
        self.report = {"released": True, "code": "released",
                       "admission_stopped": True, "invalidated": True}
        self.token = SECRET

    def require_authority(self, effect_class, target=None):
        self.calls.append((effect_class, target))
        if self.effect == "stale":
            raise RuntimeRefusal("stale")
        if self.effect == "not_holder":
            raise RuntimeRefusal("not_holder")
        return {"effect_class": effect_class, "target": target, "ready": True}

    def release_before_suspend(self):
        self.releases.append(1)
        if self.effect == "explode":
            raise RuntimeRefusal("partition")
        return dict(self.report)


class Handoff:
    def __init__(self):
        self.calls = 0
        self.secret = PASSPHRASE

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return args


def tickets(target, operation_id, attempt_id, digest, effect_class, *, signature="signed-for-test"):
    """Structurally complete admission. The signature is not an SSH signature."""
    ticket = {
        "admission_id": "adm-" + operation_id,
        "target": target,
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "digest": digest,
        "main": "zenbook",
        "incarnation": 1,
        "epoch": 1,
        "issued_at": 1800000000,
        "admit_until": 1800000060,
        "issuer_incarnation": 1,
    }
    signed = {
        "authority_id": "oracle-auth", "fleet_id": "home", "main": "zenbook",
        "incarnation": 1, "epoch": 1, "mode_version": 1, "policy_version": 1,
        "target": target, "operation_id": operation_id, "attempt_id": attempt_id,
        "digest": digest, "effect_class": effect_class, "issued_at": 1800000000,
        "admit_until": 1800000060, "issuer_incarnation": 1,
        "admission_id": ticket["admission_id"], "signature": signature,
    }
    return signed, ticket


def issuer(target, operation_id, attempt_id, digest, effect_class):
    return tickets(target, operation_id, attempt_id, digest, effect_class)


class Guard:
    def __init__(self, device_id="dell"):
        self.device_id = device_id
        self.accepted = []

    def accept(self, signed_operation, admission_ticket, sink, *, failpoint=None):
        self.accepted.append((signed_operation, admission_ticket, failpoint))
        sink()
        return {"ignored": True}


class ProductionFences(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.runtime = Runtime()
        self.handoff = Handoff()

    def tearDown(self):
        clear_production()
        open_unfenced()

    def marker(self):
        return self.state / "continuity-managed.json"

    def test_configure_persists_marker_without_secrets_or_authority(self):
        source = inspect.getsource(configure_production)
        self.assertNotIn("MainRuntime(", source)
        self.assertNotIn("AuthorityStore(", source)
        issued = []

        def remember(*args):
            issued.append(args)
            return None

        binding = configure_production(self.state, self.runtime, handoff=self.handoff, issuer=remember)
        self.assertIs(binding["issuer"], remember)
        self.assertEqual(issued, [])
        self.assertIs(binding["runtime"], self.runtime)
        self.assertIs(binding["handoff"], self.handoff)
        self.assertEqual(self.handoff.calls, 0)
        text = self.marker().read_text(encoding="utf-8")
        self.assertEqual(json.loads(text), {"managed": True, "schema": 1})
        self.assertNotIn(SECRET, text)
        self.assertNotIn(PASSPHRASE, text)
        self.assertEqual(self.marker().stat().st_mode & 0o777, 0o600)
        leftovers = [path.name for path in self.state.iterdir() if path.name != self.marker().name]
        self.assertEqual(leftovers, [])
        clear_production(self.state)
        self.assertIsNone(production_binding(self.state))
        self.assertTrue(self.marker().is_file())
        with self.assertRaises(IntegrationError) as caught:
            require_production_authority(self.state, fleet(), "remote_call", target="zenbook")
        self.assertEqual(caught.exception.code, "managed_unbound")

    def test_unmanaged_execute_local_is_unchanged(self):
        with patch("jarvis.inventory.collect", return_value={"fake": True}) as collect:
            result = execute_local("inventory", {}, fleet(), self.state)
        self.assertEqual(result, {"fake": True})
        self.assertEqual(collect.call_count, 1)
        self.assertFalse(self.marker().exists())
        self.assertEqual(self.runtime.calls, [])

    def test_execute_local_allows_then_refuses_stale(self):
        guard = Guard(device_id="zenbook")
        configure_production(self.state, self.runtime, executor=guard, issuer=issuer)
        with patch("jarvis.inventory.collect", return_value={"fake": True}) as collect:
            result = execute_local("inventory", {}, fleet(), self.state)
            self.runtime.effect = "stale"
            with self.assertRaises(RuntimeRefusal) as caught:
                execute_local("inventory", {}, fleet(), self.state)
        self.assertEqual(result, {"fake": True})
        self.assertEqual(caught.exception.code, "stale")
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(len(guard.accepted), 1)
        signed, ticket = guard.accepted[0][0], guard.accepted[0][1]
        digest = effect_digest("inventory", {})
        self.assertEqual(signed["digest"], digest)
        self.assertEqual(ticket["digest"], digest)
        self.assertEqual(ticket["target"], "zenbook")
        self.assertEqual(self.runtime.calls, [("ordinary", "zenbook"), ("ordinary", "zenbook")])

    def test_missing_issuer_refuses_before_the_effect(self):
        guard = Guard(device_id="zenbook")
        configure_production(self.state, self.runtime, executor=guard)
        with patch("jarvis.inventory.collect", return_value={"fake": True}) as collect:
            with self.assertRaises(IntegrationError) as caught:
                execute_local("inventory", {}, fleet(), self.state)
        self.assertEqual(caught.exception.code, "managed_unbound")
        self.assertEqual(collect.call_count, 0)
        self.assertEqual(guard.accepted, [])

    def test_issuer_must_bind_the_actual_digest(self):
        guard = Guard(device_id="zenbook")

        def wrong(target, operation_id, attempt_id, digest, effect_class):
            del digest
            return tickets(target, operation_id, attempt_id, "sha256:" + "ab" * 32, effect_class)

        configure_production(self.state, self.runtime, executor=guard, issuer=wrong)
        with patch("jarvis.inventory.collect", return_value={"fake": True}) as collect:
            with self.assertRaises(IntegrationError) as caught:
                execute_local("inventory", {}, fleet(), self.state)
        self.assertEqual(caught.exception.code, "bad_request")
        self.assertEqual(collect.call_count, 0)
        self.assertEqual(guard.accepted, [])

    def test_bridge_allows_one_local_call(self):
        guard = Guard(device_id="zenbook")
        configure_production(self.state, self.runtime, executor=guard, issuer=issuer)
        nested = []

        def collect():
            nested.append("outer")
            with self.assertRaises(IntegrationError) as different:
                execute_local("inventory", {"extra": 1}, fleet(), self.state)
            self.assertEqual(different.exception.code, "bridge_refused")
            with self.assertRaises(IntegrationError) as repeated:
                execute_local("inventory", {}, fleet(), self.state)
            self.assertEqual(repeated.exception.code, "bridge_refused")
            return {"fake": True}

        with patch("jarvis.inventory.collect", side_effect=collect):
            result = execute_local("inventory", {}, fleet(), self.state)
        self.assertEqual(result, {"fake": True})
        self.assertEqual(nested, ["outer"])
        self.assertEqual(len(guard.accepted), 1)
        self.assertEqual(self.runtime.calls, [("ordinary", "zenbook")])

    def test_marker_only_a_missing_file_is_unmanaged(self):
        self.assertEqual(continuity_state(self.state), "legacy")
        self.assertIsNone(require_production_authority(self.state, fleet(), "remote_call", target="zenbook"))

        configure_production(self.state, self.runtime)
        real = self.marker().read_bytes()
        moved = self.state / "real-marker.json"
        self.marker().rename(moved)
        self.marker().symlink_to(moved)
        with self.assertRaises(IntegrationError) as linked:
            continuity_state(self.state)
        self.assertEqual(linked.exception.code, "quarantined")
        self.marker().unlink()
        moved.rename(self.marker())

        self.marker().unlink()
        self.marker().mkdir()
        with self.assertRaises(IntegrationError) as directory:
            require_production_authority(self.state, fleet(), "remote_call", target="zenbook")
        self.assertEqual(directory.exception.code, "quarantined")
        self.marker().rmdir()

        for text in ("[]", "not-json", '{"managed": true}', '{"managed": false, "schema": 1}',
                     '{"managed": true, "schema": 1, "token": "x"}'):
            self.marker().write_text(text, encoding="utf-8")
            with self.assertRaises(IntegrationError) as malformed:
                continuity_state(self.state)
            self.assertEqual(malformed.exception.code, "quarantined", text)

        self.marker().write_bytes(real)
        os.chmod(self.marker(), 0)
        if os.access(self.marker(), os.R_OK):
            os.chmod(self.marker(), 0o600)
        else:
            with self.assertRaises(IntegrationError) as unreadable:
                continuity_state(self.state)
            self.assertEqual(unreadable.exception.code, "quarantined")
            os.chmod(self.marker(), 0o600)
        self.marker().unlink()
        self.assertEqual(continuity_state(self.state), "legacy")

    def test_directory_fsync_is_explicit_on_windows(self):
        def boom(descriptor):
            raise OSError("directory fsync failed")

        with patch("jarvis.continuity.integration._directory_sync", boom), \
                patch("jarvis.continuity.integration.sys.platform", "linux"):
            with self.assertRaises(OSError):
                configure_production(self.state, self.runtime)
        self.assertIsNone(production_binding(self.state))
        with patch("jarvis.continuity.integration._directory_sync", boom), \
                patch("jarvis.continuity.integration.sys.platform", "win32"):
            binding = configure_production(self.state, self.runtime)
        self.assertIs(binding["runtime"], self.runtime)

    def test_issuer_refuses_a_main_that_is_not_ready(self):
        class Client:
            def status(self):
                return {"mode": "V2_ONLY_NO_MAIN", "lease_valid": False, "main_ready": False,
                        "main": None}

        issue = production_issuer(self.state / "missing-key", Client(), wall=lambda: 1800000000)
        source = inspect.getsource(production_issuer)
        self.assertIn("sign_message", source)
        self.assertNotIn("return True", source)
        with self.assertRaises(IntegrationError) as caught:
            issue("zenbook", "op-1", "at-1", "sha256:" + "ab" * 32, "ordinary")
        self.assertEqual(caught.exception.code, "stale")

    def test_issuer_refuses_a_stale_main_and_signs_for_the_holder(self):
        class Client:
            def __init__(self, device_id):
                self.device_id = device_id

            def status(self):
                return {"mode": "V2_ACTIVE", "lease_valid": True, "main_ready": True,
                        "main": "dell", "lease_holder": "dell",
                        "authority_id": "oracle-auth", "fleet_id": "home",
                        "incarnation": 2, "epoch": 4, "mode_version": 3, "policy_version": 1}

        digest = "sha256:" + "ab" * 32
        stale = production_issuer(self.state / "zenbook-key", Client("zenbook"), wall=lambda: 1800000000)
        with patch("jarvis.continuity.integration.sign_message") as sign:
            with self.assertRaises(IntegrationError) as caught:
                stale("dell", "op-1", "at-1", digest, "ordinary")
        self.assertEqual(caught.exception.code, "stale")
        self.assertEqual(sign.call_count, 0)

        holder = production_issuer(self.state / "dell-key", Client("dell"), wall=lambda: 1800000000)
        with patch("jarvis.continuity.integration.sign_message", return_value="signed-by-dell") as sign:
            signed, ticket = holder("zenbook", "op-1", "at-1", digest, "ordinary")
        self.assertEqual(sign.call_count, 1)
        self.assertEqual(signed["main"], "dell")
        self.assertEqual(ticket["main"], "dell")
        self.assertEqual(ticket["target"], "zenbook")
        self.assertEqual(signed["signature"], "signed-by-dell")
        self.assertEqual(signed["epoch"], 4)

    def test_closed_surface_still_refuses_before_the_runtime(self):
        configure_production(self.state, self.runtime)
        close_unfenced("cluster_local")
        with patch("jarvis.inventory.collect", return_value={"fake": True}) as collect:
            with self.assertRaises(IntegrationError) as caught:
                execute_local("inventory", {}, fleet(), self.state)
        self.assertEqual(caught.exception.code, "legacy_retained")
        self.assertEqual(collect.call_count, 0)
        self.assertEqual(self.runtime.calls, [])

    def test_remote_call_succeeds_locally_and_stale_reaches_no_transport(self):
        guard = Guard(device_id="zenbook")
        configure_production(self.state, self.runtime, executor=guard, issuer=issuer)
        remote = Remote(fleet(), self.state)
        effects = []

        def run_here(action, arg, tools=None, **kwargs):
            effects.append(("here", action, arg))
            return True, "fake"

        def banned(*args, **kwargs):
            raise AssertionError("transport ran")

        with patch("jarvis.remote.run_here", run_here), \
                patch("jarvis.remote.subprocess.run", banned), \
                patch("jarvis.cluster.dispatch", banned):
            ok, text = remote.call("zenbook", "list_apps", "")
            self.runtime.effect = "stale"
            with self.assertRaises(RuntimeRefusal) as caught:
                remote.call("dell", "list_apps", "")
        self.assertEqual((ok, text), (True, "fake"))
        self.assertEqual(effects, [("here", "list_apps", "")])
        self.assertEqual(caught.exception.code, "stale")
        self.assertEqual(self.runtime.calls, [("ordinary", "zenbook"), ("ordinary", "dell")])
        self.assertEqual(len(guard.accepted), 1)
        self.assertEqual(guard.accepted[0][1]["target"], "zenbook")
        self.assertEqual(guard.accepted[0][1]["digest"], effect_digest("desktop", {"action": "list_apps", "arg": ""}))

    def test_hub_dispatch_enqueues_only_after_authority(self):
        guard = Guard(device_id="zenbook")
        configure_production(self.state, self.runtime, executor=guard, issuer=issuer)
        hub = Hub(fleet(), self.state, self.state)
        self.addCleanup(lambda: hub.broker.path.unlink(missing_ok=True))
        hub.broker.enroll("dell")
        hub.broker.heartbeat("dell", {"hostname": "dell"})
        enqueued = []
        original = hub.broker.enqueue

        def enqueue(node, requester, kind, payload, timeout=60, job_id=None):
            job_id = original(node, requester, kind, payload, timeout, job_id)
            with hub.broker.db() as db:
                db.execute("UPDATE jobs SET state='done', result=? WHERE id=?",
                           (json.dumps({"fake": True}), job_id))
            enqueued.append((node, payload))
            return job_id

        hub.broker.enqueue = enqueue
        with patch("jarvis.inventory.collect", return_value={"local": True}) as collect:
            queued = hub.dispatch("dell", "inventory", {})
            local = hub.dispatch("zenbook", "inventory", {})
            self.runtime.effect = "stale"
            with self.assertRaises(RuntimeRefusal) as caught:
                hub.dispatch("dell", "inventory", {})
        self.assertEqual(queued, {"fake": True})
        self.assertEqual(local, {"local": True})
        self.assertEqual([item[0] for item in enqueued], ["dell"])
        sealed = enqueued[0][1]
        self.assertEqual(set(sealed), {ENVELOPE_KEY})
        envelope = sealed[ENVELOPE_KEY]
        digest = effect_digest("inventory", {})
        self.assertEqual(envelope["kind"], "inventory")
        self.assertEqual(envelope["schema"], 1)
        self.assertEqual(envelope["admission_ticket"]["digest"], digest)
        self.assertEqual(envelope["signed_operation"]["digest"], digest)
        self.assertEqual(envelope["admission_ticket"]["target"], "dell")
        self.assertNotIn("signed_operation", {key for key in sealed if key != ENVELOPE_KEY})
        self.assertEqual(caught.exception.code, "stale")
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(len(guard.accepted), 1)
        self.assertEqual(guard.accepted[0][1]["target"], "zenbook")
        self.assertIn(("ordinary", "dell"), self.runtime.calls)
        self.assertIn(("ordinary", "zenbook"), self.runtime.calls)

    def test_sink_context_does_not_cover_the_guard_itself(self):
        node = self.state / "node"
        node.mkdir()
        guard = Guard()
        outside = []

        def accept(signed_operation, admission_ticket, sink, *, failpoint=None):
            try:
                execute_local("inventory", {}, fleet(local="dell", main="zenbook"), node)
            except RuntimeRefusal as exc:
                outside.append(exc.code)
            sink()
            return {"ignored": True}

        guard.accept = accept
        configure_production(node, self.runtime, executor=guard, issuer=issuer)
        sealed = seal_job_payload(node, "dell", "inventory", {"authorized": True, "skip_fence": True})
        self.runtime.effect = "not_holder"
        job = {"kind": "inventory", "payload": sealed}
        with patch("jarvis.inventory.collect", return_value={"fake": True}) as collect:
            result = run_admitted_node_job(
                node, job, lambda kind, payload: execute_local(
                    kind, payload, fleet(local="dell", main="zenbook"), node))
        self.assertEqual(result, {"fake": True})
        self.assertEqual(outside, ["not_holder"])
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(self.runtime.calls, [("ordinary", "dell")])

    def test_real_guard_rejects_a_bad_ticket_before_the_sink(self):
        guard = ExecutorGuard(self.state / "executor.sqlite", device_id="dell",
                              installation_id="dell-inst", fleet_id="home",
                              authority_id="oracle-auth", boot=lambda: "boot-1")
        self.addCleanup(guard.close)
        configure_production(self.state, self.runtime, executor=guard)
        ran = []
        body = {"authorized": True}
        signed, ticket = tickets("dell", "op-bad", "at-bad", effect_digest("inventory", body),
                                 "ordinary", signature="not-a-ticket")
        job = {"kind": "inventory", "payload": {
            **body,
            ENVELOPE_KEY: {"schema": 1, "kind": "inventory",
                           "signed_operation": signed, "admission_ticket": ticket},
        }}
        with self.assertRaises(AuthorityError):
            run_admitted_node_job(self.state, job, lambda kind, payload: ran.append(payload))
        self.assertEqual(ran, [])
        self.assertEqual(guard.local_view()["journal"], False)

    def test_release_and_sleep_refuse_without_pausing(self):
        calls = []

        def pause(state):
            calls.append("pause")
            return {"paused": True, "stopped": [], "failed": [], "blockers": []}

        with patch("jarvis.rest.pause", pause):
            unmanaged = rest.sleep(self.state, self.state, suspend=False)
        self.assertEqual(calls, ["pause"])
        self.assertTrue(unmanaged["paused"])
        self.assertIsNone(release_for_suspend(self.state))

        configure_production(self.state, self.runtime, handoff=self.handoff)
        calls.clear()
        with patch("jarvis.rest.pause", pause):
            managed = rest.sleep(self.state, self.state, suspend=False)
        self.assertEqual(calls, ["pause"])
        self.assertEqual(self.runtime.releases, [1])
        self.assertTrue(managed["paused"])
        self.assertEqual(self.handoff.calls, 0)

        self.runtime.report = {"released": False, "code": "no_lease",
                               "admission_stopped": True, "invalidated": True}
        calls.clear()
        with patch("jarvis.rest.pause", pause):
            rest.sleep(self.state, self.state, suspend=False)
        self.assertEqual(calls, ["pause"])

        self.runtime.report = {"released": False, "code": "partition",
                               "admission_stopped": True, "invalidated": True}
        calls.clear()
        with patch("jarvis.rest.pause", pause):
            with self.assertRaises(IntegrationError) as caught:
                rest.sleep(self.state, self.state, suspend=False)
        self.assertEqual(caught.exception.code, "partition")
        self.assertEqual(calls, [])

        self.runtime.effect = "explode"
        with patch("jarvis.rest.pause", pause):
            with self.assertRaises(RuntimeRefusal) as exploded:
                rest.sleep(self.state, self.state, suspend=False)
        self.assertEqual(exploded.exception.code, "partition")
        self.assertEqual(calls, [])

        clear_production(self.state)
        self.assertTrue(self.marker().is_file())
        with patch("jarvis.rest.pause", pause):
            with self.assertRaises(IntegrationError) as missing:
                rest.sleep(self.state, self.state, suspend=False)
        self.assertEqual(missing.exception.code, "managed_unbound")
        self.assertEqual(calls, [])

        configure_production(self.state, self.runtime)
        close_unfenced("sleep")
        with patch("jarvis.rest.pause", pause):
            with self.assertRaises(IntegrationError) as closed:
                rest.sleep(self.state, self.state, suspend=False)
        self.assertEqual(closed.exception.code, "legacy_retained")
        self.assertEqual(calls, [])

    def test_missing_binding_refuses_in_a_fresh_process(self):
        configure_production(self.state, self.runtime, handoff=self.handoff)
        env = os.environ.copy()
        env["JARVIS_NO_MODELS"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", CHILD, str(self.state), json.dumps(fleet())],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        body = json.loads(proc.stdout)
        self.assertFalse(body["binding"])
        self.assertFalse(body["ran"])
        self.assertEqual(body["direct"], "managed_unbound")
        self.assertEqual(body["code"], "managed_unbound")
        self.assertNotIn(SECRET, body["marker"])
        self.assertNotIn(PASSPHRASE, body["marker"])
        self.assertIsNotNone(production_binding(self.state))


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class NodeFence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = self.root / "main"
        self.state = self.root / "node"
        self.main.mkdir()
        self.state.mkdir()
        self.runtime = Runtime()
        self.guard = Guard()
        from jarvis import deploy
        self.deploy = deploy
        self.key = deploy.code_key(self.main)
        save_json(self.state / "cluster-node.json", {"node_id": "dell", "code_key": self.key})

    def tearDown(self):
        clear_production()
        open_unfenced()

    def job(self, payload, **extra):
        signed = self.deploy.sign_job(
            {"id": extra.pop("id", "job-1"), "kind": "inventory", "payload": payload, "remaining": 30},
            "dell", self.main)
        signed.update(extra)
        return signed

    def worker(self, job, executor):
        sent = []

        class Client:
            main = "zenbook"

            def request(self, path, body=None, timeout=45):
                if path == "/v1/poll":
                    return {"job": job}
                sent.append(body)
                return {}

        worker = NodeWorker.__new__(NodeWorker)
        worker.config = fleet(local="dell", main="zenbook")
        worker.state = self.state
        worker.client = Client()
        worker.executor = executor
        worker.code = "test-code"
        worker.outbox = self.state / "cluster-outbox.json"
        worker.stats = {"started": 0, "polls_ok": 0, "polls_failed": 0, "outages": 0,
                        "results_sent": 0, "results_resent": 0, "last_ok": None, "last_error": None}
        worker._down = False
        worker._confirmed = True
        worker._speaks = lambda: "no"
        worker._can_pull = lambda: False
        worker._record = NodeWorker._record.__get__(worker)
        return worker, sent

    def once(self, worker):
        with patch("jarvis.hostmeter.cached_sample", return_value=None):
            return worker.once()

    def seen(self):
        path = self.state / "cluster-jobs-seen.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def test_managed_job_needs_tickets_and_does_not_replay(self):
        configure_production(self.state, self.runtime, executor=self.guard, issuer=issuer)
        self.runtime.effect = "not_holder"
        ran = []

        def run(job, executor):
            worker, sent = self.worker(job, executor)
            self.assertTrue(self.once(worker))
            return sent

        nested = self.job({"authorized": True, "signed_operation": {"signature": "nested"},
                           "admission_ticket": {"admission_id": "nested"}}, id="job-nested")
        sent = run(nested, lambda kind, payload: ran.append(payload))
        self.assertEqual(ran, [])
        self.assertEqual(self.guard.accepted, [])
        self.assertIn("Not run", sent[-1]["result"]["error"])
        self.assertNotIn("job-nested", self.seen())
        self.assertEqual(managed_node_disposition(self.state, nested), "refuse")

        topped = self.job({}, id="job-top", signed_operation={"signature": "signed"},
                          admission_ticket={"admission_id": "adm-top"})
        sent = run(topped, lambda kind, payload: ran.append(payload))
        self.assertIn("Not run", sent[-1]["result"]["error"])
        self.assertNotIn("job-top", self.seen())
        self.assertEqual(self.guard.accepted, [])

        missing = self.job({"authorized": True}, id="job-1")
        sent = run(missing, lambda kind, payload: ran.append(payload))
        self.assertIn("Not run", sent[-1]["result"]["error"])
        self.assertNotIn("job-1", self.seen())

        original = seal_job_payload(self.state, "dell", "inventory", {"authorized": False})
        tampered = dict(original)
        tampered["tampered"] = True
        sent = run(self.job(tampered, id="job-tamper"), lambda kind, payload: ran.append(payload))
        self.assertIn("Not run", sent[-1]["result"]["error"])
        self.assertNotIn("job-tamper", self.seen())

        wrong_kind = dict(original)
        wrong_kind[ENVELOPE_KEY] = dict(original[ENVELOPE_KEY])
        wrong_kind[ENVELOPE_KEY]["kind"] = "desktop"
        sent = run(self.job(wrong_kind, id="job-kind"), lambda kind, payload: ran.append(payload))
        self.assertIn("Not run", sent[-1]["result"]["error"])
        self.assertNotIn("job-kind", self.seen())

        wrong_target = dict(original)
        envelope = dict(original[ENVELOPE_KEY])
        signed = dict(envelope["signed_operation"])
        ticket = dict(envelope["admission_ticket"])
        signed["target"] = ticket["target"] = "zenbook"
        envelope["signed_operation"] = signed
        envelope["admission_ticket"] = ticket
        wrong_target[ENVELOPE_KEY] = envelope
        sent = run(self.job(wrong_target, id="job-target"), lambda kind, payload: ran.append(payload))
        self.assertIn("Not run", sent[-1]["result"]["error"])
        self.assertNotIn("job-target", self.seen())
        self.assertEqual(self.guard.accepted, [])

        repaired = self.job(original, id="job-1")
        sent = run(repaired, lambda kind, payload: {"captured": payload["authorized"], "keys": sorted(payload)})
        self.assertEqual(sent[-1]["result"], {"captured": False, "keys": ["authorized"]})
        self.assertNotIn("ignored", json.dumps(sent[-1]))
        self.assertEqual(self.guard.accepted[-1][0]["signature"], "signed-for-test")
        self.assertEqual(self.guard.accepted[-1][1]["digest"], effect_digest("inventory", {"authorized": False}))
        self.assertEqual(self.guard.accepted[-1][1]["target"], "dell")
        self.assertEqual(self.runtime.calls, [])
        self.assertIn("job-1", self.seen())
        sent = run(repaired, lambda kind, payload: ran.append(payload))
        self.assertIn("replayed", sent[-1]["result"]["error"])
        self.assertEqual(len(self.guard.accepted), 1)
        self.assertEqual(ran, [])

    def test_unmanaged_node_still_runs_a_signed_job_once(self):
        ran = []
        job = self.job({"authorized": True})
        worker, sent = self.worker(job, lambda kind, payload: ran.append(payload) or {"ok": True})
        self.assertEqual(managed_node_disposition(self.state, job), "legacy")
        self.assertTrue(self.once(worker))
        self.assertEqual(ran, [{"authorized": True}])
        self.assertEqual(sent[-1]["result"], {"ok": True})
        self.assertTrue(self.once(worker))
        self.assertEqual(ran, [{"authorized": True}])
        self.assertIn("replayed", sent[-1]["result"]["error"])
        self.assertFalse((self.state / "continuity-managed.json").exists())


if __name__ == "__main__":
    unittest.main()
