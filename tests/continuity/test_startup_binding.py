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

from jarvis.continuity import integration, participant as rpc
from jarvis.continuity.authority import LEASE_SECONDS, SAFETY_MARGIN
from jarvis.continuity.integration import IsolatedFleet
from jarvis.continuity.renewal import LeaseRenewer, start_bound
from jarvis.continuity.startup import (NodeMainServices, acting_main_config, bind_managed,
                                       checkpoint_id)
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

    def factory(self, device):
        client = self.fleet.nodes[device].client
        return lambda path: (client, {"private_key_path": f"{device}.key"})

    def manage(self, device):
        integration._write_marker(self.states[device])

    def bind(self, device, services=None):
        participant = bind_managed(_config(device), self.states[device], self.root / device,
                                   client_factory=self.factory(device), services=services)
        if participant is not None:
            deadline = time.monotonic() + 5
            while participant.runtime.state().get("mode") is None and time.monotonic() < deadline:
                time.sleep(0.01)
        return participant

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
        self.assertTrue(callable(binding["issuer"]))
        # The configured main keeps its own console: no node services are wired there.
        self.assertIsNone(getattr(bound, "services", None))

    def test_node_takes_main_starts_services_and_stops_them_on_failback(self):
        started = []

        def phone_start(view, state, console):
            self.assertEqual(view["cluster"]["main_device"], "dell")
            server = FakeServer()
            started.append(server)
            return server

        services = NodeMainServices(_config("dell"), self.root / "dell", self.states["dell"],
                                    console_factory=lambda *a: FakeServer(), phone_start=phone_start)
        for device in ("zenbook", "dell"):
            self.manage(device)
        dell = self.bind("dell", services=services)
        self.assertIsNotNone(dell)
        renewers = {"dell": LeaseRenewer(dell)}
        for _ in range(5):  # nobody is MAIN after cutover: the node may recover it
            self.clock.tick(1)
            renewers["dell"].tick()
        self.assertEqual(self.fleet.status()["main"], "dell")
        self.assertTrue(services.running())
        self.assertEqual(len(started), 1)

        arch = self.bind("zenbook")  # the preferred PC comes up
        renewers["zenbook"] = LeaseRenewer(arch)
        for _ in range(int(30 + LEASE_SECONDS + SAFETY_MARGIN + 8)):
            self.clock.tick(1)
            for renewer in renewers.values():
                renewer.tick()
            if self.fleet.status()["main"] == "zenbook" and not services.running():
                break
        self.assertEqual(self.fleet.status()["main"], "zenbook")
        self.assertFalse(services.running())
        self.assertTrue(started[0].closed)

    def test_failed_phone_start_refuses_the_grant(self):
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
        self.assertFalse(services.healthy())

    def test_acting_main_config_is_a_copy(self):
        config = _config("dell")
        view = acting_main_config(config)
        self.assertEqual(view["cluster"]["main_device"], "dell")
        self.assertEqual(config["cluster"]["main_device"], "zenbook")

    def test_checkpoint_changes_with_memory(self):
        state = self.states["dell"]
        empty = checkpoint_id(state)
        (state / "memory.sqlite3").write_bytes(b"x" * 10)
        self.assertNotEqual(checkpoint_id(state), empty)
        self.assertRegex(checkpoint_id(state), r"^cp-[0-9a-f]{16}$")


if __name__ == "__main__":
    unittest.main()
