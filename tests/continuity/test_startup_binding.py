"""Startup binding (`jarvis.continuity.startup`) against the isolated fleet.

A managed PC binds itself at startup; an unmanaged one is untouched. On a node, the
authority's grant starts the node's own console + phone server (fakes here), and losing
the lease stops them again.
"""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from jarvis.continuity import integration, participant as rpc
from jarvis.continuity.authority import LEASE_SECONDS, SAFETY_MARGIN
from jarvis.continuity.integration import IsolatedFleet
from jarvis.continuity.renewal import LeaseRenewer, start_bound
from jarvis.continuity.startup import (NodeMainServices, acting_main_config, bind_managed,
                                       checkpoint_id, startup_ready)
from tests.continuity._fleet import Clock


def _config(local, *, operator=True):
    config = {"cluster": {"enabled": True, "main_device": "zenbook", "local_device": local},
              "devices": {"zenbook": {}, "dell": {}}}
    if operator:
        config["continuity"] = {"operator_config": "operator.json"}
    return config


class FakeServer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class StartupBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.fleet = IsolatedFleet(self.root / "fleet", self.clock)
        self.addCleanup(self.fleet.close)
        self.fleet.cutover()
        self.states = {}
        for device in ("zenbook", "dell"):
            state = self.root / device / ".jarvis"
            state.mkdir(parents=True)
            self.states[device] = state
            self.addCleanup(rpc.unbind, state)
            self.addCleanup(integration.clear_production, state)

    def factory(self, device, *, enrollment=True, fleet_id="home"):
        client = self.fleet.nodes[device].client
        data = {"private_key_path": str(self.fleet._keys[device][1])}
        if fleet_id is not None:
            data["fleet_id"] = fleet_id
        if enrollment:
            data["enrollment"] = {
                name: {"kind": "pc", "installation_id": name + "-inst", "public_key": self.fleet.public(name)}
                for name in ("zenbook", "dell")}
        return lambda path: (client, data)

    def manage(self, device):
        integration._write_marker(self.states[device])

    def bind(self, device, services=None, **factory_args):
        participant = bind_managed(_config(device), self.states[device], self.root / device,
                                   client_factory=self.factory(device, **factory_args),
                                   services=services)
        if participant is not None and getattr(participant, "guard", None) is not None:
            self.addCleanup(participant.guard.close)
        if participant is not None:
            deadline = time.monotonic() + 5
            while participant.runtime.state().get("mode") is None and time.monotonic() < deadline:
                time.sleep(0.01)
        return participant

    def issue(self, device, target):
        issuer = integration.production_binding(self.states[device])["issuer"]
        return issuer(target, "op-transfer", "at-transfer", "sha256:" + "ab" * 32, "ordinary")

    def test_unmanaged_state_is_untouched(self):
        self.assertIsNone(self.bind("dell"))
        self.assertIsNone(rpc.lookup(self.states["dell"]))
        self.assertIsNone(integration.production_binding(self.states["dell"]))
        self.assertIsNone(start_bound(self.states["dell"], _config("dell"), self.root / "dell"))
        self.assertEqual(integration.continuity_state(self.states["dell"]), "legacy")

    def test_managed_without_operator_config_stays_unbound(self):
        self.manage("dell")
        result = bind_managed(_config("dell", operator=False), self.states["dell"],
                              client_factory=self.factory("dell"))
        self.assertIsNone(result)
        self.assertIsNone(rpc.lookup(self.states["dell"]))

    def test_identity_mismatch_stays_unbound(self):
        self.manage("dell")
        result = bind_managed(_config("dell"), self.states["dell"],
                              client_factory=self.factory("zenbook"))
        self.assertIsNone(result)

    def test_managed_pc_binds_and_start_bound_runs_renewer(self):
        self.manage("zenbook")
        self.assertIsNotNone(self.bind("zenbook"))
        renewer = start_bound(self.states["zenbook"], _config("zenbook"), self.root / "zenbook")
        self.assertIsNotNone(renewer)
        renewer.close()
        bound = rpc.lookup(self.states["zenbook"])
        self.assertIsNotNone(bound)
        binding = integration.production_binding(self.states["zenbook"])
        self.assertIs(binding["runtime"], bound.runtime)
        self.assertIs(binding["executor"], bound.guard)
        self.assertTrue(callable(bound.guard.accept))
        self.assertTrue(callable(binding["issuer"]))
        self.assertTrue(startup_ready(self.states["zenbook"]))
        # A runtime object without a Hub listener is not a MAIN canary.
        self.assertIsNone(getattr(bound, "services", None))
        self.assertIs(bound.canary(), False)

    def test_missing_adapters_do_not_bind(self):
        self.manage("dell")
        self.assertIsNone(self.bind("dell", enrollment=False))
        self.assertIsNone(rpc.lookup(self.states["dell"]))
        self.assertIsNone(integration.production_binding(self.states["dell"]))
        self.assertFalse(startup_ready(self.states["dell"]))
        self.assertEqual(integration.continuity_state(self.states["dell"]), "managed")

    def test_bad_successor_grant_does_not_bind(self):
        self.manage("dell")
        grant = self.states["dell"] / "continuity-successor-grant.json"
        grant.write_text("{}", encoding="utf-8")
        self.assertIsNone(self.bind("dell"))
        self.assertIsNone(integration.production_binding(self.states["dell"]))
        self.assertIsNone(rpc.lookup(self.states["dell"]))
        self.assertTrue(grant.is_file())
        self.assertFalse(startup_ready(self.states["dell"]))

    def test_register_failure_clears_the_runtime(self):
        self.manage("zenbook")
        with patch("jarvis.continuity.participant.bind", side_effect=RuntimeError("register failed")):
            result = bind_managed(_config("zenbook"), self.states["zenbook"], self.root / "zenbook",
                                  client_factory=self.factory("zenbook"))
        self.assertIsNone(result)
        self.assertIsNone(integration.production_binding(self.states["zenbook"]))
        self.assertIsNone(rpc.lookup(self.states["zenbook"]))
        self.assertFalse(startup_ready(self.states["zenbook"]))

    def test_node_without_successor_route_does_not_promote_or_start_services(self):
        started = []
        def phone_start(*args):
            started.append(FakeServer())
            return started[-1]
        services = NodeMainServices(_config("dell"), self.root / "dell", self.states["dell"],
                                    console_factory=lambda *a: FakeServer(), phone_start=phone_start)
        for device in ("zenbook", "dell"):
            self.manage(device)
        dell = self.bind("dell", services=services)
        self.assertIsNotNone(dell)
        renewers = {"dell": LeaseRenewer(dell)}
        for _ in range(5):  # no provisioned candidate listener means recovery must fail closed
            self.clock.tick(1)
            renewers["dell"].tick()
        self.assertIsNone(self.fleet.status()["main"])
        self.assertFalse(self.fleet.status()["main_ready"])
        self.assertFalse(services.running())
        self.assertEqual(started, [])

    def test_missing_successor_route_refuses_before_phone_start(self):
        services = NodeMainServices(_config("dell"), self.root / "dell", self.states["dell"],
                                    console_factory=lambda *a: FakeServer(),
                                    phone_start=lambda *a: None)
        self.manage("dell")
        dell = self.bind("dell", services=services)
        renewer = LeaseRenewer(dell)
        for _ in range(5):
            self.clock.tick(1)
            renewer.tick()
        self.assertNotEqual(self.fleet.status()["main"], "dell")
        self.assertTrue(services.healthy())
        self.assertFalse(services.running())

    def test_acting_main_config_is_a_copy(self):
        config = _config("dell")
        view = acting_main_config(config)
        self.assertEqual(view["cluster"]["main_device"], "dell")
        self.assertEqual(config["cluster"]["main_device"], "zenbook")

    def test_service_sync_preserves_not_ready_grant_until_cancelled(self):
        services = NodeMainServices(_config("dell"), self.root / "dell", self.states["dell"],
                                   console_factory=lambda *_: FakeServer(),
                                   phone_start=lambda *_: FakeServer())
        self.manage("dell")
        candidate = self.bind("dell", services=services)
        transition, lease = self.fleet.promote("dell", until="grant")
        candidate.runtime.accept_lease(lease)
        phone, console = FakeServer(), FakeServer()
        services.phone, services.console = phone, console
        with patch.object(candidate.production_controller, "close") as close:
            services.sync(candidate.runtime)
            self.assertEqual(candidate.runtime.lease_snapshot(),
                             {key: lease[key] for key in ("holder", "lease_id", "incarnation", "epoch")})
            self.assertFalse(phone.closed)
            self.assertFalse(console.closed)
            close.assert_not_called()
            self.fleet.owner.step(transition, "cancel")
            services.sync(candidate.runtime)
            self.assertTrue(phone.closed)
            self.assertTrue(console.closed)
            close.assert_called_once()

    def test_checkpoint_changes_with_memory(self):
        state = self.states["dell"]
        empty = checkpoint_id(state)
        (state / "memory.sqlite3").write_bytes(b"x" * 10)
        self.assertNotEqual(checkpoint_id(state), empty)
        self.assertRegex(checkpoint_id(state), r"^cp-[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
