"""P1 contracts for the legacy fleet facade (`jarvis.fleet.legacy_topology`).

The embedded `Original` class is a verbatim copy of `jarvis/topology.py` at HEAD (before the
facade extraction); it is the parity oracle. Everything here is fake-only: no network, no
processes, no real hostname dependence (gethostname is always patched).
"""

import contextvars
import dataclasses
import re
import socket
import threading
import unittest
from contextlib import contextmanager
from unittest import mock
from urllib.parse import urlsplit

import jarvis.topology as topology
from jarvis.fleet import legacy_topology as legacy


class Original:
    """Verbatim original implementation (git show HEAD:jarvis/topology.py), namespaced."""

    DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
    DEFAULT_PORT = 7780

    @classmethod
    def enabled(cls, config):
        return bool(config.get("cluster", {}).get("enabled", False))

    @classmethod
    def identity(cls, config):
        settings = config.get("cluster", {})
        devices = config.get("devices", {})
        main = settings.get("main_device")
        local = settings.get("local_device", "auto")
        if local == "auto":
            host = socket.gethostname().casefold()
            matches = [name for name, d in devices.items()
                       if str(d.get("hostname", "")).casefold() == host]
            if len(matches) != 1:
                raise ValueError("Set cluster.local_device to this PC's device ID; hostname is not unique or known.")
            local = matches[0]
        for label, value in (("main_device", main), ("local_device", local)):
            if not isinstance(value, str) or not cls.DEVICE_ID.fullmatch(value) or value not in devices:
                raise ValueError(f"cluster.{label} must name a configured device.")
        return local, main

    @classmethod
    def is_node(cls, config):
        return cls.enabled(config) and cls.identity(config)[0] != cls.identity(config)[1]

    @classmethod
    def endpoint(cls, config):
        value = str(config.get("cluster", {}).get("main_url", f"http://127.0.0.1:{cls.DEFAULT_PORT}")).rstrip("/")
        parts = urlsplit(value)
        if (parts.username or parts.password or parts.query or parts.fragment
                or parts.path or not parts.hostname or parts.port == 0
                or (parts.scheme != "https" and not (parts.scheme == "http" and
                        parts.hostname in ("127.0.0.1", "localhost", "::1")))):
            raise ValueError("cluster.main_url must be an HTTPS origin or loopback HTTP origin (for an SSH tunnel).")
        return value


def outcome(fn, *args):
    """('ok', value) or ('err', type, message): compares results and exceptions alike."""
    try:
        return ("ok", fn(*args))
    except Exception as exc:  # noqa: BLE001 - the exact type is part of the contract
        return ("err", type(exc), str(exc))


DEVICES = {"main": {"hostname": "Main-Host"}, "node": {"hostname": "node-host"},
           "twin-a": {"hostname": "twin"}, "twin-b": {"hostname": "TWIN"},
           "-bad": {"hostname": "bad"}, "x" * 64: {"hostname": "long64"},
           "y" * 63: {"hostname": "long63"}, "nohost": {}}


def cfg(cluster=None, devices=None, enabled=True):
    cluster = dict(cluster or {})
    if enabled is not None:
        cluster.setdefault("enabled", enabled)
    return {"devices": DEVICES if devices is None else devices, "cluster": cluster}


def with_host(host):
    return mock.patch("jarvis.topology.socket.gethostname", return_value=host)


class ObjectIdentityTests(unittest.TestCase):
    def test_context_vars_are_the_same_objects_with_unchanged_names(self):
        self.assertIs(topology.ORIGIN, legacy.ORIGIN)
        self.assertIs(topology.APPROVAL_ORIGIN, legacy.APPROVAL_ORIGIN)
        self.assertEqual(topology.ORIGIN.name, "jarvis_origin_device")
        self.assertEqual(topology.APPROVAL_ORIGIN.name, "jarvis_approval_origin")
        self.assertIsNone(topology.ORIGIN.get())
        self.assertIsNone(topology.APPROVAL_ORIGIN.get())

    def test_constants_and_functions_are_reexported_unchanged(self):
        self.assertIs(topology.DEVICE_ID, legacy.DEVICE_ID)
        self.assertEqual(topology.DEVICE_ID.pattern, Original.DEVICE_ID.pattern)
        self.assertEqual(topology.DEFAULT_PORT, 7780)
        for name in ("enabled", "identity", "endpoint", "request_from", "approvals_on", "roles", "Roles"):
            self.assertIs(getattr(topology, name), getattr(legacy, name), name)
        # is_node is the one name that stays in topology.py (so patched enabled/identity apply).
        self.assertIsNot(getattr(topology, "is_node"), getattr(legacy, "is_node", None))

    def test_topology_socket_is_the_socket_module_and_gethostname_patch_drives_auto_identity(self):
        self.assertIs(topology.socket, socket)
        config = cfg({"main_device": "main", "local_device": "auto"})
        with with_host("NODE-HOST"):
            self.assertEqual(topology.identity(config), ("node", "main"))
            self.assertTrue(topology.is_node(config))
        with with_host("main-host"):
            self.assertEqual(topology.identity(config), ("main", "main"))
            self.assertFalse(topology.is_node(config))

    def test_roles_auto_identity_also_follows_the_gethostname_patch(self):
        config = cfg({"main_device": "main"})  # local_device absent means "auto"
        with with_host("node-host"):
            self.assertEqual(topology.roles(config).local, "node")


class ParityTests(unittest.TestCase):
    """The facade must match the original: same value, or same exception type and message."""

    IDENTITY_CASES = {
        "explicit local": ({"main_device": "main", "local_device": "node"}, "x"),
        "explicit local equals main": ({"main_device": "main", "local_device": "main"}, "x"),
        "auto unique": ({"main_device": "main", "local_device": "auto"}, "node-host"),
        "auto default (absent)": ({"main_device": "main"}, "node-host"),
        "auto case-insensitive": ({"main_device": "main"}, "NoDe-HoSt"),
        "auto casefold of main": ({"main_device": "main"}, "MAIN-HOST"),
        "auto ambiguous": ({"main_device": "main", "local_device": "auto"}, "Twin"),
        "auto unknown host": ({"main_device": "main"}, "nobody"),
        "auto empty host": ({"main_device": "main"}, ""),
        "auto host matches device without hostname": ({"main_device": "main"}, "nohost"),
        "missing main": ({"local_device": "node"}, "x"),
        "main None": ({"main_device": None, "local_device": "node"}, "x"),
        "main not a string": ({"main_device": 5, "local_device": "node"}, "x"),
        "main unknown": ({"main_device": "ghost", "local_device": "node"}, "x"),
        "local unknown": ({"main_device": "main", "local_device": "ghost"}, "x"),
        "local not a string": ({"main_device": "main", "local_device": 7}, "x"),
        "local None": ({"main_device": "main", "local_device": None}, "x"),
        "main bad id": ({"main_device": "-bad", "local_device": "node"}, "x"),
        "local bad id": ({"main_device": "main", "local_device": "-bad"}, "x"),
        "auto resolves to bad id": ({"main_device": "main"}, "bad"),
        "main 64 chars": ({"main_device": "x" * 64, "local_device": "node"}, "x"),
        "local 63 chars": ({"main_device": "main", "local_device": "y" * 63}, "x"),
        "main 65 chars": ({"main_device": "x" * 65, "local_device": "node"}, "x"),
        "id with trailing newline": ({"main_device": "main\n", "local_device": "node"}, "x"),
        "id uppercase differs from key": ({"main_device": "MAIN", "local_device": "node"}, "x"),
        "id with space": ({"main_device": "ma in", "local_device": "node"}, "x"),
        "unicode id": ({"main_device": "mäin", "local_device": "node"}, "x"),
    }

    def test_identity_parity_table(self):
        for name, (cluster, host) in self.IDENTITY_CASES.items():
            with self.subTest(name), with_host(host):
                config = cfg(cluster)
                expected = outcome(Original.identity, config)
                self.assertEqual(outcome(topology.identity, config), expected)
                self.assertEqual(outcome(legacy.identity, config), expected)

    def test_identity_parity_with_no_devices_and_no_cluster(self):
        for name, config in {"empty config": {}, "no cluster": {"devices": DEVICES},
                             "no devices": {"cluster": {"main_device": "main", "local_device": "main"}},
                             "devices empty": {"devices": {}, "cluster": {"main_device": "main", "local_device": "main"}}}.items():
            with self.subTest(name), with_host("x"):
                self.assertEqual(outcome(topology.identity, config), outcome(Original.identity, config))

    def test_unconfigured_identity_keeps_the_exact_messages(self):
        with with_host("nobody"):
            self.assertEqual(outcome(topology.identity, cfg({"main_device": "main"})),
                             ("err", ValueError,
                              "Set cluster.local_device to this PC's device ID; hostname is not unique or known."))
        self.assertEqual(outcome(topology.identity, cfg({"main_device": "ghost", "local_device": "node"})),
                         ("err", ValueError, "cluster.main_device must name a configured device."))
        self.assertEqual(outcome(topology.identity, cfg({"main_device": "main", "local_device": "ghost"})),
                         ("err", ValueError, "cluster.local_device must name a configured device."))

    def test_enabled_parity_table(self):
        cases = {"absent cluster": {}, "empty cluster": {"cluster": {}}, "false": {"cluster": {"enabled": False}},
                 "true": {"cluster": {"enabled": True}}, "none": {"cluster": {"enabled": None}},
                 "zero": {"cluster": {"enabled": 0}}, "one": {"cluster": {"enabled": 1}},
                 "empty string": {"cluster": {"enabled": ""}}, "non-empty string": {"cluster": {"enabled": "false"}}}
        for name, config in cases.items():
            with self.subTest(name):
                self.assertEqual(outcome(topology.enabled, config), outcome(Original.enabled, config))
                self.assertIsInstance(topology.enabled(config), bool)

    def test_is_node_parity_table(self):
        configs = [cfg({"main_device": "main", "local_device": "node"}),
                   cfg({"main_device": "main", "local_device": "main"}),
                   cfg({"main_device": "main", "local_device": "node"}, enabled=False),
                   cfg({"main_device": "ghost", "local_device": "node"}),
                   cfg({"main_device": "ghost", "local_device": "node"}, enabled=False),
                   cfg({"main_device": "main"}), {}]
        for index, config in enumerate(configs):
            for host in ("node-host", "main-host", "twin", "nobody"):
                with self.subTest(index=index, host=host), with_host(host):
                    self.assertEqual(outcome(topology.is_node, config), outcome(Original.is_node, config))

    def test_disabled_cluster_never_resolves_identity_in_is_node(self):
        # Original short-circuits: a broken/unconfigured cluster that is off must not raise.
        config = cfg({"main_device": "ghost", "local_device": "ghost"}, enabled=False)
        self.assertIs(topology.is_node(config), False)
        with mock.patch("jarvis.topology.identity", side_effect=AssertionError("must not be called")):
            self.assertIs(topology.is_node(config), False)

    ENDPOINT_CASES = [
        None,  # key absent: default loopback
        "https://hub.example.test", "https://hub.example.test/", "https://hub.example.test:8443",
        "https://hub.example.test:8443//", "http://127.0.0.1:7780", "http://127.0.0.1:7778/",
        "http://localhost:7778", "http://[::1]:7778", "HTTP://127.0.0.1:7778", "https://[::1]:1",
        "http://localhost", "http://127.0.0.1:65535",
        # refused
        "https://user:secret@example.test", "https://user@example.test", "https://:pw@example.test",
        "https://example.test/path", "https://example.test/a/b/", "https://example.test?token=x",
        "https://example.test/?x=1", "https://example.test#frag", "https://example.test:0",
        "http://localhost:0", "http://192.168.1.2:7778", "http://example.test", "http://0.0.0.0:7778",
        "http://127.0.0.2:7778", "ftp://example.test", "ws://127.0.0.1:7778", "example.test", "",
        "///", "https://", "http://:7778", "https:///path", "http://LOCALHOST:7778", " https://example.test",
        "https://example.test:99999", "https://example.test:abc", 12345,
    ]

    def test_endpoint_parity_table(self):
        for value in self.ENDPOINT_CASES:
            config = cfg({"main_device": "main", "local_device": "main"})
            if value is not None:
                config["cluster"]["main_url"] = value
            with self.subTest(repr(value)):
                expected = outcome(Original.endpoint, config)
                self.assertEqual(outcome(topology.endpoint, config), expected)
                self.assertEqual(outcome(legacy.endpoint, config), expected)

    def test_endpoint_default_and_error_message(self):
        self.assertEqual(topology.endpoint({}), "http://127.0.0.1:7780")
        self.assertEqual(topology.endpoint({"cluster": {"main_url": "https://hub.test///"}}), "https://hub.test")
        self.assertEqual(outcome(topology.endpoint, {"cluster": {"main_url": "http://192.168.1.2:7778"}}),
                         ("err", ValueError,
                          "cluster.main_url must be an HTTPS origin or loopback HTTP origin (for an SSH tunnel)."))

    def test_device_id_regex_parity(self):
        for value in ("a", "A1", "a-b_c", "x" * 64, "x" * 65, "", "-a", "_a", "a b", "a\n", "ä", "a.b", "0"):
            with self.subTest(repr(value)):
                self.assertEqual(bool(topology.DEVICE_ID.fullmatch(value)),
                                 bool(Original.DEVICE_ID.fullmatch(value)))


class RequestContextTests(unittest.TestCase):
    def test_request_from_sets_nests_and_restores(self):
        self.assertIsNone(topology.ORIGIN.get())
        with topology.request_from("phone"):
            self.assertEqual(topology.ORIGIN.get(), "phone")
            with topology.request_from("node"):
                self.assertEqual(topology.ORIGIN.get(), "node")
                with topology.request_from(None):
                    self.assertIsNone(topology.ORIGIN.get())
                self.assertEqual(topology.ORIGIN.get(), "node")
            self.assertEqual(topology.ORIGIN.get(), "phone")
        self.assertIsNone(topology.ORIGIN.get())

    def test_approvals_on_sets_nests_and_restores(self):
        with topology.approvals_on("main"):
            self.assertEqual(topology.APPROVAL_ORIGIN.get(), "main")
            with topology.approvals_on("phone"):
                self.assertEqual(topology.APPROVAL_ORIGIN.get(), "phone")
            self.assertEqual(topology.APPROVAL_ORIGIN.get(), "main")
        self.assertIsNone(topology.APPROVAL_ORIGIN.get())

    def test_both_restore_on_exception(self):
        with self.assertRaises(RuntimeError):
            with topology.request_from("phone"), topology.approvals_on("main"):
                raise RuntimeError("boom")
        self.assertIsNone(topology.ORIGIN.get())
        self.assertIsNone(topology.APPROVAL_ORIGIN.get())
        with topology.request_from("a"):
            with self.assertRaises(KeyError):
                with topology.request_from("b"):
                    raise KeyError("x")
            self.assertEqual(topology.ORIGIN.get(), "a")

    def test_origin_and_approval_are_independent_variables(self):
        with topology.request_from("phone"):
            self.assertIsNone(topology.APPROVAL_ORIGIN.get())
            with topology.approvals_on("main"):
                self.assertEqual((topology.ORIGIN.get(), topology.APPROVAL_ORIGIN.get()), ("phone", "main"))
        with topology.approvals_on("main"):
            self.assertIsNone(topology.ORIGIN.get())

    def test_context_managers_are_reusable_per_call_not_stateful(self):
        first, second = topology.request_from("a"), topology.request_from("b")
        with first:
            with second:
                self.assertEqual(topology.ORIGIN.get(), "b")
            self.assertEqual(topology.ORIGIN.get(), "a")
        self.assertIsNone(topology.ORIGIN.get())

    def test_threads_do_not_inherit_or_leak_context(self):
        seen = {}
        inside, release = threading.Event(), threading.Event()

        def worker():
            seen["fresh"] = (topology.ORIGIN.get(), topology.APPROVAL_ORIGIN.get())
            with topology.request_from("thread"):
                inside.set()
                release.wait(5)
                seen["own"] = topology.ORIGIN.get()

        with topology.request_from("main-thread"), topology.approvals_on("approver"):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(inside.wait(5))
            self.assertEqual(topology.ORIGIN.get(), "main-thread")  # worker's set does not reach us
            release.set()
            thread.join(5)
        self.assertEqual(seen["fresh"], (None, None))  # plain threads start with a clean context
        self.assertEqual(seen["own"], "thread")

    def test_copied_context_sees_snapshot_and_cannot_change_the_parent(self):
        with topology.request_from("phone"):
            ctx = contextvars.copy_context()
        # The snapshot kept the origin even though the block has ended in this context.
        self.assertEqual(ctx.run(topology.ORIGIN.get), "phone")
        self.assertIsNone(topology.ORIGIN.get())

        def mutate():
            with topology.request_from("child"):
                return topology.ORIGIN.get()
        self.assertEqual(ctx.run(mutate), "child")
        self.assertEqual(ctx.run(topology.ORIGIN.get), "phone")
        self.assertIsNone(topology.ORIGIN.get())

    def test_copy_context_in_thread_carries_origin_like_the_phone_path(self):
        result = []
        with topology.request_from("phone"), topology.approvals_on("main"):
            ctx = contextvars.copy_context()
        thread = threading.Thread(target=ctx.run, args=(lambda: result.append(
            (topology.ORIGIN.get(), topology.APPROVAL_ORIGIN.get())),))
        thread.start()
        thread.join(5)
        self.assertEqual(result, [("phone", "main")])

    def test_two_contexts_run_isolated_from_each_other(self):
        def run(device):
            with topology.request_from(device):
                return topology.ORIGIN.get()
        a, b = contextvars.copy_context(), contextvars.copy_context()
        self.assertEqual((a.run(run, "a"), b.run(run, "b")), ("a", "b"))
        self.assertIsNone(topology.ORIGIN.get())


class PatchPointTests(unittest.TestCase):
    """Existing tests patch jarvis.topology.enabled/identity/is_node; that must keep working."""

    CONFIG = {"devices": DEVICES, "cluster": {"enabled": False, "main_device": "ghost", "local_device": "ghost"}}

    def test_patched_enabled_and_identity_drive_is_node(self):
        with mock.patch("jarvis.topology.enabled", return_value=True), \
                mock.patch("jarvis.topology.identity", return_value=("dell", "arch")):
            self.assertTrue(topology.is_node(self.CONFIG))
        with mock.patch("jarvis.topology.enabled", return_value=True), \
                mock.patch("jarvis.topology.identity", return_value=("arch", "arch")):
            self.assertFalse(topology.is_node(self.CONFIG))
        with mock.patch("jarvis.topology.enabled", return_value=False), \
                mock.patch("jarvis.topology.identity", return_value=("dell", "arch")) as ident:
            self.assertFalse(topology.is_node(self.CONFIG))
            ident.assert_not_called()

    def test_patch_is_undone_after_exit(self):
        with mock.patch("jarvis.topology.identity", return_value=("a", "b")):
            pass
        self.assertIs(topology.identity, legacy.identity)
        self.assertFalse(topology.is_node(self.CONFIG))  # real disabled config again

    def test_patched_is_node_is_what_late_importers_see(self):
        with mock.patch("jarvis.topology.is_node", return_value=True):
            from jarvis.topology import is_node  # the consumers' call-time import style
            self.assertTrue(is_node(self.CONFIG))
        from jarvis.topology import is_node
        self.assertFalse(is_node(self.CONFIG))

    def test_patching_topology_identity_does_not_leak_into_the_facade(self):
        """Documented limit: only is_node composes the topology-level names. roles() uses the
        facade's own identity, so a topology.identity patch does not (and need not) reach it."""
        config = cfg({"main_device": "main", "local_device": "node"})
        with mock.patch("jarvis.topology.identity", return_value=("x", "y")):
            self.assertEqual(topology.roles(config).local, "node")


class RolesTests(unittest.TestCase):

    def setUp(self):
        self.config = cfg({"main_device": "main", "local_device": "node"})

    def test_roles_snapshot_separates_the_four_names(self):
        roles = topology.roles(self.config)
        self.assertEqual(roles, topology.Roles(local="node", coordinator="main", origin=None, approval_station=None))
        self.assertIsNot(roles.local, None)
        with topology.request_from("phone"), topology.approvals_on("main"):
            roles = topology.roles(self.config)
        self.assertEqual((roles.local, roles.coordinator, roles.origin, roles.approval_station),
                         ("node", "main", "phone", "main"))

    def test_origin_is_not_local_and_approval_is_not_origin(self):
        with topology.request_from("phone"):
            roles = topology.roles(self.config)
        self.assertNotEqual(roles.origin, roles.local)
        self.assertIsNone(roles.approval_station)  # request_from does not imply approvals_on
        with topology.approvals_on("main"):
            roles = topology.roles(self.config)
        self.assertIsNone(roles.origin)  # approvals_on does not imply an origin
        self.assertEqual(roles.approval_station, "main")

    def test_is_coordinator(self):
        self.assertFalse(topology.roles(self.config).is_coordinator)
        self.assertTrue(topology.roles(cfg({"main_device": "main", "local_device": "main"})).is_coordinator)
        # Follows identity()/is_node exactly.
        for local in ("main", "node", "nohost"):
            config = cfg({"main_device": "main", "local_device": local})
            self.assertEqual(topology.roles(config).is_coordinator, not topology.is_node(config))

    def test_roles_is_immutable_and_slot_safe(self):
        roles = topology.roles(self.config)
        self.assertTrue(dataclasses.is_dataclass(roles))
        for field in ("local", "coordinator", "origin", "approval_station"):
            with self.assertRaises(dataclasses.FrozenInstanceError, msg=field):
                setattr(roles, field, "evil")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            roles.is_coordinator = True  # property without setter on a frozen dataclass
        hash(roles)  # frozen + eq: hashable

    def test_roles_is_a_snapshot_not_a_live_view(self):
        with topology.request_from("phone"):
            roles = topology.roles(self.config)
        self.assertEqual(roles.origin, "phone")
        self.assertIsNone(topology.ORIGIN.get())  # leaving the block does not change the snapshot
        self.assertEqual(roles.origin, "phone")

    def test_execution_target_precedence_explicit_then_origin_then_local(self):
        self.assertEqual(topology.roles(self.config).execution_target(), "node")
        self.assertEqual(topology.roles(self.config).execution_target("other"), "other")
        with topology.request_from("phone"):
            roles = topology.roles(self.config)
            self.assertEqual(roles.execution_target(), "phone")
            self.assertEqual(roles.execution_target("other"), "other")
            self.assertEqual(roles.execution_target(None), "phone")
            self.assertEqual(roles.execution_target(""), "phone")  # falsy explicit falls through like `device or ...`

    def test_execution_target_matches_the_inline_rule_in_remote_call(self):
        """remote.Remote.call: `target = device or ORIGIN.get() or local`."""
        for origin in (None, "phone", "node"):
            for explicit in (None, "", "other", "main"):
                with self.subTest(origin=origin, explicit=explicit):
                    ctx = topology.request_from(origin) if origin else topology.request_from(None)
                    with ctx:
                        inline = explicit or topology.ORIGIN.get() or "node"
                        self.assertEqual(topology.roles(self.config).execution_target(explicit), inline)

    def test_confirmation_station_precedence_approval_then_origin_then_local(self):
        self.assertEqual(topology.roles(self.config).confirmation_station(), "node")
        with topology.request_from("phone"):
            self.assertEqual(topology.roles(self.config).confirmation_station(), "phone")
            with topology.approvals_on("main"):
                self.assertEqual(topology.roles(self.config).confirmation_station(), "main")
        with topology.approvals_on("main"):  # approval without origin still wins over local
            self.assertEqual(topology.roles(self.config).confirmation_station(), "main")

    def test_confirmation_station_matches_the_inline_rule_in_pipeline(self):
        """pipeline.py / assistant.py park with `APPROVAL_ORIGIN.get() or origin`, where origin
        is ORIGIN (or, when none, the caller-supplied fallback; the facade adds `local` there)."""
        for origin in (None, "phone"):
            for approval in (None, "", "main"):
                with self.subTest(origin=origin, approval=approval):
                    with topology.request_from(origin), topology.approvals_on(approval):
                        inline = topology.APPROVAL_ORIGIN.get() or topology.ORIGIN.get()
                        station = topology.roles(self.config).confirmation_station()
                        self.assertEqual(station, inline or "node")

    def test_phone_request_targets_a_node_while_approval_stays_on_main(self):
        config = cfg({"main_device": "main", "local_device": "main"})
        with topology.request_from("phone"), topology.approvals_on("main"):
            roles = topology.roles(config)
            # The phone is the caller, a node is named explicitly, the main still approves.
            self.assertEqual(roles.origin, "phone")
            self.assertEqual(roles.execution_target("node"), "node")
            self.assertEqual(roles.confirmation_station(), "main")
            self.assertNotEqual(roles.execution_target("node"), roles.confirmation_station())
            # Without an explicit target the legacy rule runs the action on the origin itself.
            self.assertEqual(roles.execution_target(), "phone")

    def test_roles_raises_exactly_like_identity_when_unconfigured(self):
        broken = {
            "unknown auto host": (cfg({"main_device": "main"}), "nobody"),
            "ambiguous auto host": (cfg({"main_device": "main"}), "twin"),
            "main unknown": (cfg({"main_device": "ghost", "local_device": "node"}), "x"),
            "main missing": (cfg({"local_device": "node"}), "x"),
            "local unknown": (cfg({"main_device": "main", "local_device": "ghost"}), "x"),
            "no config": ({}, "x"),
        }
        for name, (config, host) in broken.items():
            with self.subTest(name), with_host(host):
                expected = outcome(topology.identity, config)
                self.assertEqual(expected[0], "err")
                self.assertEqual(outcome(topology.roles, config), expected)

    def test_roles_does_not_consult_enabled(self):
        """Callers must check `enabled` first; roles() describes config even when the cluster is off."""
        config = cfg({"main_device": "main", "local_device": "node"}, enabled=False)
        self.assertFalse(topology.enabled(config))
        self.assertEqual(topology.roles(config).coordinator, "main")

    def test_main_device_is_just_config_no_lease_no_epoch(self):
        """Legacy-only: the coordinator is whatever cluster.main_device says right now. There is no
        lease, epoch or fencing, so editing the value changes roles instantly and nothing objects."""
        config = cfg({"main_device": "main", "local_device": "node"})
        before = topology.roles(config)
        self.assertEqual((before.coordinator, before.is_coordinator), ("main", False))
        config["cluster"]["main_device"] = "node"  # a plain dict edit, no handover protocol
        after = topology.roles(config)
        self.assertEqual((after.coordinator, after.is_coordinator), ("node", True))
        config["cluster"]["main_device"] = "main"  # and flips straight back
        self.assertEqual(topology.roles(config), before)
        # Two PCs holding different configs can both believe they are the coordinator; nothing
        # in the facade can detect or prevent that (this is what later phases must add).
        pc_a = cfg({"main_device": "main", "local_device": "main"})
        pc_b = cfg({"main_device": "node", "local_device": "node"})
        self.assertTrue(topology.roles(pc_a).is_coordinator and topology.roles(pc_b).is_coordinator)
        public = {name for name in dir(topology.Roles) if not name.startswith("_")}
        self.assertFalse({"lease", "epoch", "authority", "ticket", "fence"} & {n.lower() for n in public})


if __name__ == "__main__":
    unittest.main()
