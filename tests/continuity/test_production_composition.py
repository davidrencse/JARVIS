"""Shipped startup composition across a real Hub HTTP listener and NodeWorker RPC.

The authority is a temp loopback gateway. No workstation credentials, live service,
or hand-constructed SafeHandoff enter this test.
"""
import base64
import copy
import hashlib
import json
import multiprocessing
import socket
import sqlite3
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from jarvis import cluster, deploy
from jarvis.cluster_store import NODE_DEFAULT
from jarvis.continuity import integration, participant
from jarvis.continuity.gateway import AuthorityClient
from jarvis.continuity.integration import IsolatedFleet, SignedAuthority


class ProcessClock:
    """Wall and monotonic clocks used by separate shipped processes."""
    @staticmethod
    def mono():
        return time.monotonic()

    @staticmethod
    def wall():
        return time.time()


def _node_poll_process(config, state, config_path, stop, service_standins=False):
    """Run the shipped worker poll/RPC path in a separate process."""
    from jarvis.cluster import NodeWorker
    from jarvis.continuity.renewal import start_bound
    worker = NodeWorker(config, state, config_path=config_path)
    if service_standins:
        from jarvis.continuity.startup import NodeMainServices, bind_managed
        # The control Hub and broker are real. Only desktop console/phone
        # services are stand-ins; this test makes no phone availability claim.
        services = NodeMainServices(config, Path(config_path).parent, state,
            console_factory=lambda *_: SimpleNamespace(close=lambda: None),
            phone_start=lambda *_: SimpleNamespace(close=lambda: None))
        bind_managed(config, state, Path(config_path).parent, services=services)
    renewer = start_bound(state, config, Path(config_path).parent)
    # OS role-unit/tunnel operations are explicit test boundaries. Hubs, keys,
    # gateway operations, broker jobs, and brain bundles remain real fixtures.
    from unittest.mock import patch
    fixture_code = deploy.code_fingerprint()
    patches = [patch("jarvis.cluster.served_code", return_value=fixture_code),
               patch("jarvis.release.code_parity", return_value={"matched": True}),
               patch("jarvis.cluster_setup.role_autostart", return_value={"autostart": True}),
               patch("jarvis.linkfinder.activate_tunnel"),
               patch("jarvis.cluster_tunnel.status", return_value={"running": False}),
               patch("jarvis.cluster_tunnel.stop"), patch("jarvis.cluster_tunnel.autostart"),
               patch("jarvis.cluster_tunnel.start")]
    for item in patches:
        item.start()
    try:
        while not stop.is_set():
            try:
                worker.once()
            except (ConnectionError, OSError, TimeoutError):
                # The fixture role-unit helper is deliberately inert, so an
                # old polling thread can outlive its former Hub route. Keep the
                # promoted Hub process alive; the real unit restart is outside
                # this in-process test boundary.
                pass
            stop.wait(0.03)
    finally:
        for item in reversed(patches):
            item.stop()
        if renewer is not None:
            renewer.close()


def _stop_process(process, stop):
    stop.set()
    process.join(1)
    if process.is_alive():
        process.terminate()
        process.join(2)


def _close_test_hub(hub, state):
    try:
        hub.close()
    finally:
        bound = participant.lookup(state)
        if bound is not None and bound.guard is not None:
            bound.guard.close()
        participant.unbind(state)
        integration.clear_production(state)


class ProductionCompositionTests(unittest.TestCase):
    def _bare_hub_request(self):
        """A handler over a bare Hub with one role-transition request in flight.
        Returns (hub, handler)."""
        from email.message import Message
        from jarvis import ui
        from jarvis.cluster import Hub, make_server
        from unittest.mock import patch
        hub = object.__new__(Hub)
        hub.console = None
        hub.config = {"cluster": {"main_url": "http://127.0.0.1:7781"}}
        hub._response_context = threading.local()
        hub._active_responses = {}
        hub._active_responses_lock = threading.RLock()
        hub._orphaned_response_callbacks = []
        def server_init(server, address, handler):
            server.RequestHandlerClass = handler
        with patch.object(ui.ConsoleServer, "__init__", server_init):
            server = make_server(hub, "127.0.0.1", 0)
        handler = object.__new__(server.RequestHandlerClass)
        handler._node_path = None
        handler.headers = Message()
        request = {"callbacks": [], "written": False, "role_transition": True}
        hub._response_context.request = request
        hub._active_responses[id(request)] = request
        return hub, handler

    def test_deferred_role_callback_runs_only_after_response_write(self):
        from jarvis import ui
        from jarvis.cluster import Hub
        from unittest.mock import patch
        hub, handler = self._bare_hub_request()
        events = []
        thread = threading.Thread(target=lambda: Hub.defer_after_response(
            hub, lambda: events.append("pivot")))
        thread.start(); thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(events, [])
        with patch.object(ui.Handler, "_send", lambda *_args, **_kw: events.append("write")):
            handler._send(200, {"ok": True})
        self.assertEqual(events, ["write", "pivot"])

    def test_failed_response_write_does_not_run_deferred_role_callback(self):
        from jarvis import ui
        from jarvis.cluster import Hub
        from unittest.mock import patch
        hub, handler = self._bare_hub_request()
        events = []
        Hub.defer_after_response(hub, lambda: events.append("pivot"))
        def failed_write(*_args, **_kwargs):
            raise OSError("client disconnected before response write")
        with patch.object(ui.Handler, "_send", failed_write):
            with self.assertRaises(OSError):
                handler._send(200, {"ok": True})
        self.assertEqual(events, [])

    def test_https_tailnet_route_can_pin_external_port_separately_from_listener(self):
        from jarvis.continuity.production import _route
        route = _route({"successor_routes": {"zenbook": {
            "endpoint": "https://archmain.tailnet-example.ts.net:8443",
            "listen_host": "127.0.0.1", "listen_port": 7781, "link": None}}}, "zenbook")
        self.assertEqual(route["listen_port"], 7781)
        self.assertEqual(route["endpoint"], "https://archmain.tailnet-example.ts.net:8443")

    def test_remote_host_requires_the_exact_pinned_origin_port(self):
        from jarvis.cluster import _remote_pinned_host_match
        origin = "https://winlaptop.tailnet-example.ts.net:8443"
        self.assertIs(_remote_pinned_host_match("winlaptop.tailnet-example.ts.net:8443", origin), True)
        self.assertIs(_remote_pinned_host_match("winlaptop.tailnet-example.ts.net:443", origin), False)
        self.assertIsNone(_remote_pinned_host_match("127.0.0.1:7781", origin))

    def test_standby_hub_pins_its_advertised_host_and_local_listener(self):
        from jarvis.continuity.production import ProductionController

        class FakeHub:
            def __init__(self, config, base, state):
                self.config, self.base, self.state = config, base, state
                self.main = config["cluster"]["main_device"]
                self.started = False
            def start(self, host, port):
                self.address = (host, port)
                self.started = True
            def close(self):
                self.started = False

        from unittest.mock import patch
        controller = object.__new__(ProductionController)
        controller.hub = None
        controller.config = {"cluster": {"main_device": "dell", "local_device": "dell",
            "main_url": "http://old-main.invalid:7780"}, "devices": {"dell": {}}}
        controller.base = Path("/tmp"); controller.state = Path("/tmp/.jarvis")
        controller.local = "dell"
        controller._dispatch = None
        controller.attach_hub = lambda _hub: None
        route = {"endpoint": "https://winlaptop.tailnet-example.ts.net:8443",
                 "listen_host": "127.0.0.1", "listen_port": 7781, "link": None}
        with patch("jarvis.cluster.Hub", FakeHub):
            hub = controller._ensure_standby_hub(route)
        self.assertEqual(hub.config["cluster"]["main_url"], route["endpoint"])
        self.assertIs(hub.config["cluster"]["auto_release"], False)
        self.assertIs(hub.config["cluster"]["auto_update"], False)
        self.assertEqual(hub.address, ("127.0.0.1", 7781))

    @staticmethod
    def _provision_executor_journal(client, operator, state):
        """Create the enrolled PC's durable journal and record its current fence."""
        from jarvis.continuity.executor import ExecutorGuard
        from jarvis.continuity.clock import boot_id
        status = client.status()
        pin_map = {device: record["public_key"]
                   for device, record in operator["enrollment"].items()}
        authority = SignedAuthority(client, pin_map)
        guard = ExecutorGuard(
            state / "continuity-executor.sqlite", device_id=client.device_id,
            installation_id=client.installation_id, fleet_id=operator["fleet_id"],
            authority_id=client.authority_id, authority=authority,
            verifier=authority.verify, boot=boot_id, wall=client._wall)
        try:
            guard.fence(status["incarnation"], status["epoch"], status["mode_version"])
        finally:
            guard.close()

    def test_factory_recovery_crosses_hub_and_node_process_boundary(self):
        self.exercise_factory()

    def test_factory_completes_one_way_handoff_with_real_broker_and_brain_transport(self):
        self.exercise_factory(complete=True)

    def exercise_factory(self, complete=False):
        with TemporaryDirectory() as folder, ExitStack() as cleanup:
            root = Path(folder)
            clock = ProcessClock()
            fleet = IsolatedFleet(root / "authority", clock)
            cleanup.callback(fleet.close)
            fleet.cutover()
            if complete:
                # This control-plane drill has two PC executors. The separate
                # Oracle fixture does not execute jobs and is not driven here.
                fleet.owner.disable_participant("oracle", "oracle-inst")

            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            with socket.socket() as route_probe:
                route_probe.bind(("127.0.0.1", 0))
                dell_port = route_probe.getsockname()[1]
            origin = f"http://127.0.0.1:{port}"
            base = root / "zenbook"
            state = base / ".jarvis"
            base.mkdir()
            state.mkdir(mode=0o700)
            devices = {name: {"brain_transfer_dir": str(root / (name + "-stage"))}
                       for name in ("zenbook", "dell")}
            for record in devices.values():
                Path(record["brain_transfer_dir"]).mkdir(mode=0o700)
            zenbook_config = {
                "cluster": {"enabled": True, "main_device": "zenbook", "local_device": "zenbook",
                            "listen_host": "127.0.0.1", "listen_port": port,
                            "main_url": origin, "auto_release": False, "auto_update": False},
                "devices": devices, "clipboard": {"autostart": False},
                "continuity": {"operator_config": str(base / "operator.json")},
                "brainsync": {"enabled": complete, "handoff_scope": "jarvis"},
                "vault": {"enabled": True, "path": str(base / "vault")},
                "storage": {"zenbook": {"roots": [str(root)]}, "dell": {"roots": [str(root)]}},
            }
            dell_base = root / "dell"
            dell_state = dell_base / ".jarvis"
            dell_base.mkdir()
            dell_state.mkdir(mode=0o700)
            dell_config = {
                "cluster": {"enabled": True, "main_device": "zenbook", "local_device": "dell",
                            "listen_port": port, "main_url": origin},
                "devices": devices, "clipboard": {"autostart": False},
                "continuity": {"operator_config": str(dell_base / "operator.json")},
                "brainsync": {"enabled": complete, "handoff_scope": "jarvis"},
                "vault": {"enabled": True, "path": str(dell_base / "vault")},
                "storage": zenbook_config["storage"],
            }
            for pc_base in (base, dell_base):
                (pc_base / "vault").mkdir()
            if complete:
                with sqlite3.connect(state / "memory.sqlite3") as memory:
                    memory.execute("CREATE TABLE items(body TEXT)")
                    memory.execute("INSERT INTO items VALUES ('source memory')")
                (base / "vault" / "note.md").write_text("source note", encoding="utf-8")
            pins = {name: {"kind": "pc", "installation_id": name + "-inst",
                           "public_key": fleet.public(name)} for name in ("zenbook", "dell")}
            for name, device_state, key_name in (
                    ("zenbook", state, "zenbook"), ("dell", dell_state, "dell")):
                operator = {
                    "origin": fleet.origin, "authority_id": "oracle-auth", "fleet_id": "home",
                    "device_id": name, "installation_id": name + "-inst",
                    "private_key_path": str(fleet._keys[key_name][1]),
                    "enrollment": pins,
                }
                # Only the current authority owner holds the owner credential;
                # the enrolled successor receives pins and its own key only.
                routes = {
                    "zenbook": {"endpoint": origin, "listen_host": "127.0.0.1",
                                "listen_port": port, "link": None},
                    "dell": {"endpoint": f"http://127.0.0.1:{dell_port}",
                             "listen_host": "127.0.0.1", "listen_port": dell_port, "link": None},
                }
                if name == "zenbook":
                    operator["owner_private_key_path"] = str(fleet._keys["owner"][1])
                    operator["owner_proxy_clients"] = ["dell"]
                    operator["successor_routes"] = routes
                elif complete:
                    operator["owner_proxy_device"] = "zenbook"
                    operator["successor_routes"] = routes
                config_dir = base if name == "zenbook" else dell_base
                (config_dir / "operator.json").write_text(json.dumps(operator), encoding="utf-8")
                (config_dir / "jarvis.config.json").write_text(
                    json.dumps(zenbook_config if name == "zenbook" else dell_config), encoding="utf-8")
                integration._write_marker(device_state)
                operator = json.loads((config_dir / "operator.json").read_text(encoding="utf-8"))
                client = fleet.nodes[name].client
                self._provision_executor_journal(client, operator, device_state)

            hub = cluster.Hub(zenbook_config, base, state)
            cleanup.callback(_close_test_hub, hub, state)
            # Pin fixture auth before exercising startup's asynchronous runtime.
            self.assertEqual(fleet.nodes["zenbook"].client.status()["mode"], "V2_ONLY_NO_MAIN")
            from jarvis.continuity.cli import build_client
            startup_client, _ = build_client(base / "operator.json")
            self.assertEqual(startup_client.status()["mode"], "V2_ONLY_NO_MAIN")
            dell_client, _ = build_client(dell_base / "operator.json")
            self.assertFalse(dell_client.can_sign_owner)
            hub.start(host="127.0.0.1", port=port)
            bound = participant.lookup(state)
            self.assertIsNotNone(bound)
            binding = integration.production_binding(state)
            self.assertEqual(binding["handoff"], hub.continuity_handoff)
            self.assertIs(hub.continuity_recovery.__self__, bound.production_controller)
            self.assertIs(hub.continuity_recovery.__func__, bound.production_controller._recover.__func__)
            self.assertIs(binding["runtime"], bound.runtime)
            self.assertIs(binding["executor"], bound.guard)
            self.assertTrue(callable(binding["issuer"]))
            self.assertTrue(bound.production_controller.canary_local(), {
                "started": hub.started, "runtime_started": bound.runtime._started,
                "readiness": bound.runtime.readiness("control"),
                "guard": bound.guard.local_view(), "address": hub.server.server_address})

            token = hub.broker.enroll("dell", NODE_DEFAULT)
            dell_state.joinpath("cluster-node.json").write_text(json.dumps({
                "node_id": "dell", "main_device": "zenbook", "token": token,
                "code_key": deploy.code_key(state),
            }), encoding="utf-8")
            stop = multiprocessing.Event()
            process = multiprocessing.Process(target=_node_poll_process,
                args=(dell_config, dell_state, str(dell_base / "jarvis.config.json"), stop, complete), daemon=True)
            process.start()
            cleanup.callback(_stop_process, process, stop)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                row = next((item for item in hub.broker.nodes() if item["id"] == "dell"), None)
                if row and row.get("seen"):
                    break
                time.sleep(0.03)
            self.assertTrue(row and row.get("seen"), "separate Dell worker never heartbeated")

            owner = cluster.Client(zenbook_config, state, owner=True)
            result = owner.request("/v1/continuity/recover", {
                "device": "zenbook", "request_id": "factory-recovery-001"}, timeout=15)
            self.assertTrue(result["ok"], {"result": result, "status": bound.client.status(),
                                           "receipt": (state / "continuity-recovery-receipts.json").read_text()
                                           if (state / "continuity-recovery-receipts.json").exists() else None})
            status = bound.client.status()
            self.assertEqual(status["mode"], "V2_ACTIVE")
            self.assertEqual(status["main"], "zenbook")
            self.assertTrue(status["lease_valid"])
            self.assertTrue(status["main_ready"])
            self.assertIs(bound.production_controller.hub, hub)
            self.assertTrue(bound.production_controller.canary_local())
            # This request is a managed broker job served by the separate
            # Dell NodeWorker process, proving the remote participant boundary.
            # This temp fixture has no checked release snapshot. Isolate the
            # already-tested remote participant boundary from the production
            # code-parity gate; the process fixture is not release evidence.
            from unittest.mock import patch
            with patch("jarvis.cluster.served_code", return_value=deploy.code_fingerprint()):
                remote_probe = bound.production_controller._remote("dell").readiness()
            self.assertIs(remote_probe.get("control_ready"), True, remote_probe)
            # The controller's in-process entry point repeats the release gate
            # used by run_main and refuses before opening an authority transition.
            parity_refusal = bound.production_controller._handoff(
                "zenbook", "dell", zenbook_config, base)
            self.assertEqual(parity_refusal.get("code"), "code_mismatch", parity_refusal)
            self.assertEqual(bound.client.status()["main"], "zenbook")
            # With the release gate explicitly satisfied in this isolated
            # fixture, invoke the factory-bound SafeHandoff through its real
            # readiness and authority coordinator path. The gateway correctly
            # keeps the transition pending until the old lease's quiet period;
            # this drill does not wait out or claim a role transfer.
            with patch("jarvis.release.code_parity", return_value={"matched": True}), \
                    patch("jarvis.cluster.served_code", return_value=deploy.code_fingerprint()), \
                    patch("jarvis.cluster_tunnel.status", return_value={"running": False}), \
                    patch("jarvis.cluster_tunnel.autostart"), \
                    patch("jarvis.cluster_setup.role_autostart", return_value={"autostart": True}):
                if complete:
                    safe_handoff = owner.request('/v1/continuity/main', {'device': 'dell'}, timeout=30)
                else:
                    safe_handoff = bound.production_controller._handoff(
                        "zenbook", "dell", zenbook_config, base)
            if complete:
                self.assertTrue(safe_handoff.get("ok"), {"result": safe_handoff,
                    "journal": (state / "continuity-handoff.json").read_text()
                        if (state / "continuity-handoff.json").exists() else None})
                final = bound.client.status()
                self.assertEqual(final["main"], "dell")
                self.assertTrue(final["main_ready"])
                self.assertTrue(final["lease_valid"])
                self.assertEqual(json.loads((base / 'jarvis.config.json').read_text())['cluster']['main_device'], 'dell')
                self.assertEqual(json.loads((state / 'main-handoff.json').read_text())['publication'], 'published')
                with sqlite3.connect(dell_state / "memory.sqlite3") as memory:
                    self.assertEqual(memory.execute("SELECT body FROM items").fetchall(), [("source memory",)])
                with self.assertRaises(Exception):
                    bound.runtime.require_authority("ordinary")

                # Prepare the explicit, hash-reviewed return approval using a
                # source export while Dell is the ready MAIN. The actual return
                # below re-exports and imports over the broker transition path.
                # The former Zenbook Hub has released its role locks; the
                # hash-reviewed local preparation is deliberately offline.
                _close_test_hub(hub, state)
                from jarvis import brainsync
                from jarvis.continuity.startup import acting_main_config
                request = json.dumps({"schema": brainsync.EXPORT_SCHEMA,
                    "old_main": "dell", "new_main": "zenbook"}, sort_keys=True).encode()
                exported = brainsync.export_result(acting_main_config(dell_config), dell_base,
                                                    request)
                self.assertTrue(exported["ok"], exported)
                reviewed_bundle = base / "return-review.bundle"
                reviewed_blob = base64.b64decode(exported["payload"], validate=False)
                reviewed_bundle.write_bytes(reviewed_blob)
                header, _, incoming_memory = brainsync._unbundle(reviewed_blob)
                self.assertEqual(header["old_main"], "dell")
                self.assertEqual(header["new_main"], "zenbook")
                approval = base / ".jarvis" / "brainsync-return-approval.json"
                freeze_path = brainsync._marker_path(base)
                from jarvis.continuity.return_brain import prepare as prepare_return
                prepared = prepare_return(zenbook_config, state, startup_client, reviewed_bundle,
                    incoming_sha256=hashlib.sha256(incoming_memory).hexdigest(),
                    current_sha256=hashlib.sha256(brainsync.snapshot_memory(state / "memory.sqlite3")).hexdigest(),
                    freeze_sha256=hashlib.sha256(freeze_path.read_bytes()).hexdigest(), confirmed=True)
                self.assertTrue(prepared["ok"], prepared)
                self.assertTrue(approval.is_file())

                # The owner PC has handed its role to Dell. Stop its old Hub and
                # run the shipped node worker as a separate process; its owner
                # signer stays on Zenbook for the bounded return proxy.
                zenbook_stop = multiprocessing.Event()
                zenbook_process = multiprocessing.Process(target=_node_poll_process,
                    args=(zenbook_config, state, str(base / "jarvis.config.json"), zenbook_stop, True),
                    daemon=True)
                zenbook_process.start()
                cleanup.callback(_stop_process, zenbook_process, zenbook_stop)

                dell_main_config = copy.deepcopy(dell_config)
                dell_main_config["cluster"]["main_device"] = "dell"
                dell_main_config["cluster"]["main_url"] = routes["dell"]["endpoint"]
                dell_hub_client = cluster.Client(dell_main_config, dell_state, owner=True)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    nodes = dell_hub_client.request("/v1/status", timeout=5).get("nodes", [])
                    if any(item.get("id") == "zenbook" and item.get("seen") for item in nodes):
                        break
                    time.sleep(0.03)
                self.assertTrue(any(item.get("id") == "zenbook" and item.get("seen")
                                    for item in nodes), "separate Zenbook worker never heartbeated to Dell")

                with patch("jarvis.release.code_parity", return_value={"matched": True}), \
                        patch("jarvis.cluster.served_code", return_value=deploy.code_fingerprint()):
                    returned = dell_hub_client.request(
                        "/v1/continuity/main", {"device": "zenbook"}, timeout=60)
                self.assertTrue(returned.get("ok"), {"result": returned,
                    "status": dell_client.status(),
                    "journal": (dell_state / "continuity-handoff.json").read_text()
                        if (dell_state / "continuity-handoff.json").exists() else None})
                returned_status = dell_client.status()
                self.assertEqual(returned_status["main"], "zenbook")
                self.assertTrue(returned_status["main_ready"])
                with sqlite3.connect(f"file:{dell_state / 'continuity-executor.sqlite'}?mode=ro",
                                     uri=True) as journal:
                    fenced_incarnation, fenced_epoch = journal.execute(
                        "SELECT fenced_incarnation, fenced_epoch FROM fence").fetchone()
                self.assertEqual(fenced_incarnation, returned_status["incarnation"])
                self.assertEqual(fenced_epoch, returned_status["epoch"] - 1)
                self.assertEqual(json.loads((dell_base / "jarvis.config.json").read_text())[
                    "cluster"]["main_device"], "zenbook")
                with sqlite3.connect(state / "memory.sqlite3") as memory:
                    self.assertEqual(memory.execute("SELECT body FROM items").fetchall(),
                                     [("source memory",)])
                approval_record = json.loads(approval.read_text())
                self.assertEqual(approval_record["phase"], "activated")
                self.assertFalse(freeze_path.exists())
                return
            self.assertEqual(safe_handoff.get("code"), "not_quiet", safe_handoff)
            self.assertEqual(safe_handoff.get("status"), "pending", safe_handoff)
            after_refusal = bound.client.status()
            self.assertIn((after_refusal.get("transition") or {}).get("state"),
                          ("DRAINING", "HANDOVER"))
            # Both executors, including the candidate, must have acknowledged
            # this closing epoch. The Oracle participant is deliberately not
            # driven here, so it still accounts for the not_quiet wait.
            self.assertEqual(bound.guard.local_view()["fenced_epoch"], after_refusal["epoch"])
            with sqlite3.connect(f"file:{dell_state / 'continuity-executor.sqlite'}?mode=ro",
                                 uri=True) as journal:
                closed = journal.execute("SELECT fenced_epoch FROM fence").fetchone()
            self.assertEqual(closed[0], after_refusal["epoch"])
            stop.set()
            process.join(1)
            if process.is_alive():
                # The worker may be inside the bounded long poll; force only this
                # disposable fixture process down before closing its test Hub.
                process.terminate()
                process.join(2)
            hub.close()

    def test_owner_proof_unavailable_is_a_clear_prebind_refusal(self):
        from jarvis.continuity.production import ProductionController
        controller = object.__new__(ProductionController)
        controller.participant = SimpleNamespace(client=AuthorityClient(
            "http://127.0.0.1:1", authority_id="test", device_id="zenbook",
            installation_id="zenbook-inst"))
        with self.assertRaisesRegex(Exception, "owner_proof_unavailable"):
            controller._require_owner()

    def test_owner_proxy_refuses_wrong_tuple_or_retry_key_before_remote_signing(self):
        from jarvis.continuity.production import _OwnerProxyPort
        from jarvis.continuity.runtime import _caller_key, RuntimeRefusal
        status = {"mode": "V2_ACTIVE", "lease_valid": True, "main_ready": True,
                  "main": "dell", "incarnation": 4, "epoch": 9, "policy_version": 3,
                  "transition": None}
        remote_calls = []
        remote = SimpleNamespace(_request=lambda *args, **kwargs: remote_calls.append((args, kwargs)))
        port = object.__new__(_OwnerProxyPort)
        port.local = SimpleNamespace(status=lambda: status)
        port.remote = remote
        port.source, port.candidate = "dell", "zenbook"
        request = {"caller": "safe-handoff", "client_key": _caller_key(
            "set_preferred_main", "dell", "zenbook", status),
            "action": "set_preferred_main", "candidate": "zenbook",
            "expected_incarnation": 4, "expected_epoch": 9,
            "expected_policy": 3, "profile": "control"}
        stale = dict(status, epoch=10)
        port.local.status = lambda: stale
        with self.assertRaisesRegex(RuntimeRefusal, "stale"):
            port.transition_request(request)
        port.local.status = lambda: status
        request["client_key"] = "sh-invalid"
        with self.assertRaisesRegex(RuntimeRefusal, "forbidden"):
            port.transition_request(request)
        self.assertEqual(remote_calls, [])
