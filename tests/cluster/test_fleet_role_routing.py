"""P1 role-wiring seam, legacy behaviour pinned (fake-backed; no desktop, network or models).

Covers the three consumers of the topology roles:
  * `Remote.call` (execution target: explicit device, else ORIGIN, else local; node forwarding),
  * `pipeline._run_turn` and `assistant._ask` (where a confirmation parks: `approvals_on`
    override, else the caller's already-resolved origin),
  * `topology.confirmation_station` / `Roles.confirmation_station`.

Real objects: Remote, ConfirmQueue (sqlite in a tempdir), gates, registry, pipeline.
Faked at the boundary only: `jarvis.remote.run_here` (the desktop), `jarvis.cluster.Client` /
`dispatch` (the hub) and the Grok Bot (`jarvis.grokbot.Grokbot`).
"""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jarvis.topology as topology
from jarvis import assistant, gates, pipeline
from jarvis.cluster import NodeOffline
from jarvis.confirm import ConfirmQueue
from jarvis.fleet import legacy_topology as legacy
from jarvis.remote import Remote
from jarvis.topology import (APPROVAL_ORIGIN, ORIGIN, Roles, approvals_on,
                             confirmation_station, request_from)

UNKNOWN_ALL = "known: main, node, other."


def cluster_config(local="main", enabled=True, **extra):
    config = {"devices": {"main": {"hostname": "main-host", "transport": "local"},
                          "node": {"hostname": "node-host", "transport": "ssh"},
                          "other": {"hostname": "other-host"}},
              "cluster": {"enabled": enabled, "main_device": "main", "local_device": local,
                          "auto_update": False, "main_url": "http://127.0.0.1:7778"},
              # The checks themselves are under test: no trusted_local shortcut.
              "rules": {"trusted_local": {"enabled": False}}}
    config.update(extra)
    return config


def standalone_config(**cluster):
    """No cluster at all (or a cluster block that is switched off)."""
    config = {"devices": {"zenbook": {"hostname": "zen-host"}, "dell": {"hostname": "dell-host"}},
              "rules": {"trusted_local": {"enabled": False}}}
    if cluster:
        config["cluster"] = cluster
    return config


class FakeRemote:
    """Stand-in for the pipeline's executor/observer: records calls, answers `observe` itself."""

    def __init__(self, here="main"):
        self._here, self.calls = here, []

    def here(self):
        return self._here

    def call(self, device, action, arg):
        self.calls.append((device, action, arg))
        if action == "observe":
            return True, json.dumps({"match": True, "after_ms": 1, "app": "x"})
        return True, f"{action} ok"


class FakeGrokbot:
    """Only `here()` is used: the standalone (cluster off) answer to "which PC is this?"."""
    name = "zenbook"

    def __init__(self, config=None, state=None):
        pass

    def here(self):
        return type(self).name


def close_command(device, app="Photoshop", cid="c1"):
    # close_app of anything but a SAFE_TO_CLOSE app is risk "confirm": it parks.
    return {"id": cid, "intent": "close_app", "device": device, "args": {"name": app}, "depends_on": []}


def open_command(device, app="Notepad", cid="c2"):
    return {"id": cid, "intent": "open_app", "device": device, "args": {"name": app}, "depends_on": []}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # A ".jarvis" child: gates.asks_now reads a jarvis.config.json next to the state folder.
        self.state = Path(tmp.name) / ".jarvis"
        self.state.mkdir()
        for name in ("INFLIGHT",):
            patcher = mock.patch.object(pipeline, name, gates.InFlight())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertIsNone(ORIGIN.get())
        self.assertIsNone(APPROVAL_ORIGIN.get())

    def tearDown(self):
        self.assertIsNone(ORIGIN.get())
        self.assertIsNone(APPROVAL_ORIGIN.get())

    def queue(self):
        return ConfirmQueue(self.state)

    def run_pipeline(self, commands, origin, config=None, remote=None):
        config = config or cluster_config()
        remote = remote or FakeRemote()
        result = pipeline.run_commands("t", commands, config, self.state, origin=origin, remote=remote,
                                       app_exists=lambda device, name: True, history=False)
        return result, remote


class RemoteBase(Base):
    """A Remote with a fake desktop: patched run_here, hub Client and dispatch."""

    def start(self, local="main"):
        self.config = cluster_config(local)
        self.remote = Remote(self.config, self.state, tools=object())
        self.run_here = self.patch("jarvis.remote.run_here", return_value=(True, "ran here"))
        self.client = self.patch("jarvis.cluster.Client")
        self.client.return_value.request.return_value = {"ok": True, "text": "forwarded"}
        self.dispatch = self.patch("jarvis.cluster.dispatch", return_value={"ok": True, "text": "dispatched"})
        return self.remote

    def patch(self, target, **kw):
        patcher = mock.patch(target, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def assert_here(self, with_config):
        self.run_here.assert_called_once()
        kwargs = self.run_here.call_args.kwargs
        if with_config:
            self.assertEqual(kwargs, {"config": self.config, "state": self.state})
        else:
            self.assertEqual(kwargs, {})
        self.client.assert_not_called()
        self.dispatch.assert_not_called()


# 1 ---------------------------------------------------------------------------------------------
class LocalExecutionTests(RemoteBase):
    def test_main_without_device_or_origin_runs_here_with_config_and_state(self):
        remote = self.start("main")
        self.assertEqual(remote.call(None, "open_app", "Spotify"), (True, "ran here"))
        self.assert_here(with_config=True)
        self.assertEqual(self.run_here.call_args.args[:2], ("open_app", "Spotify"))

    def test_main_explicit_main_or_here_words_run_here_with_config_and_state(self):
        remote = self.start("main")
        for spoken in ("main", "main pc", "here", "this one"):
            self.run_here.reset_mock()
            self.assertEqual(remote.call(spoken, "volume", "5"), (True, "ran here"), spoken)
            self.assert_here(with_config=True)

    def test_node_explicit_self_runs_here_without_config_or_state_and_never_touches_hub(self):
        remote = self.start("node")
        self.assertEqual(remote.call("node", "open_app", "Spotify"), (True, "ran here"))
        self.assert_here(with_config=False)

    def test_node_with_origin_equal_to_itself_runs_here_without_config(self):
        remote = self.start("node")
        with request_from("node"):
            self.assertEqual(remote.call(None, "volume", "5"), (True, "ran here"))
        self.assert_here(with_config=False)

    def test_unknown_action_is_refused_before_any_routing(self):
        remote = self.start("main")
        self.assertEqual(remote.call("node", "teleport", "x"), (False, "Unknown desktop action teleport."))
        self.run_here.assert_not_called()
        self.dispatch.assert_not_called()


# 2 ---------------------------------------------------------------------------------------------
class ForwardingTests(RemoteBase):
    def assert_forwarded(self, node, action="open_app", arg="Spotify"):
        self.client.return_value.request.assert_called_once()
        call = self.client.return_value.request.call_args
        self.assertEqual(call.args[0], "/v1/control")
        self.assertEqual(call.args[1], {"node": node, "kind": "desktop",
                                        "payload": {"action": action, "arg": arg}})
        self.run_here.assert_not_called()
        self.dispatch.assert_not_called()

    def test_node_with_explicit_other_target_forwards_through_the_hub(self):
        remote = self.start("node")
        self.assertEqual(remote.call("other", "open_app", "Spotify"), (True, "forwarded"))
        self.assert_forwarded("other")

    def test_node_targeting_the_main_also_forwards_it_does_not_run_there_itself(self):
        remote = self.start("node")
        self.assertEqual(remote.call("main pc", "open_app", "Spotify"), (True, "forwarded"))
        self.assert_forwarded("main")

    def test_node_with_other_origin_and_no_device_forwards_to_that_origin(self):
        remote = self.start("node")
        with request_from("other"):
            self.assertEqual(remote.call(None, "open_app", "Spotify"), (True, "forwarded"))
        self.assert_forwarded("other")

    def test_node_forward_reports_the_hub_result_flag_and_text(self):
        remote = self.start("node")
        self.client.return_value.request.return_value = {"ok": False, "text": "refused"}
        self.assertEqual(remote.call("other", "open_app", "x"), (False, "refused"))

    def test_main_targeting_a_node_uses_dispatch_not_the_client(self):
        remote = self.start("main")
        self.assertEqual(remote.call("node", "open_app", "Spotify"), (True, "dispatched"))
        self.dispatch.assert_called_once()
        self.assertEqual(self.dispatch.call_args.args[2:4], ("node", "desktop"))
        self.assertEqual(self.dispatch.call_args.args[4], {"action": "open_app", "arg": "Spotify"})
        self.client.assert_not_called()
        self.run_here.assert_not_called()

    def test_main_with_node_origin_dispatches_to_the_origin(self):
        remote = self.start("main")
        with request_from("node"):
            self.assertEqual(remote.call(None, "open_app", "Spotify"), (True, "dispatched"))
        self.assertEqual(self.dispatch.call_args.args[2:4], ("node", "desktop"))
        self.run_here.assert_not_called()


# 3 ---------------------------------------------------------------------------------------------
class TargetPrecedenceTests(RemoteBase):
    def test_explicit_target_beats_origin(self):
        remote = self.start("main")
        with request_from("node"):
            self.assertEqual(remote.call("other", "open_app", "x"), (True, "dispatched"))
        self.assertEqual(self.dispatch.call_args.args[2], "other")

    def test_origin_beats_local_when_no_device(self):
        remote = self.start("main")
        with request_from("other"):
            remote.call(None, "open_app", "x")
        self.assertEqual(self.dispatch.call_args.args[2], "other")
        self.run_here.assert_not_called()

    def test_explicit_target_equal_to_local_beats_a_foreign_origin(self):
        remote = self.start("node")
        with request_from("other"):
            self.assertEqual(remote.call("node", "open_app", "x"), (True, "ran here"))
        self.assert_here(with_config=False)

    def test_unknown_explicit_target_is_an_exact_error_and_dispatches_nothing(self):
        remote = self.start("main")
        self.assertEqual(remote.call("ghost", "open_app", "x"),
                         (False, f"Unknown node 'ghost'; {UNKNOWN_ALL}"))
        self.run_here.assert_not_called()
        self.dispatch.assert_not_called()
        self.client.assert_not_called()

    def test_unknown_origin_with_no_device_is_the_same_error(self):
        remote = self.start("main")
        with request_from("ghost"):
            self.assertEqual(remote.call(None, "open_app", "x"),
                             (False, f"Unknown node 'ghost'; {UNKNOWN_ALL}"))
        self.run_here.assert_not_called()
        self.dispatch.assert_not_called()

    def test_unknown_explicit_on_a_node_never_reaches_the_hub_either(self):
        remote = self.start("node")
        self.assertEqual(remote.call("ghost", "open_app", "x"),
                         (False, f"Unknown node 'ghost'; {UNKNOWN_ALL}"))
        self.client.assert_not_called()
        self.run_here.assert_not_called()

    def test_explicit_known_target_overrides_an_unknown_origin(self):
        remote = self.start("main")
        with request_from("ghost"):
            self.assertEqual(remote.call("node", "open_app", "x"), (True, "dispatched"))
        self.assertEqual(self.dispatch.call_args.args[2], "node")


# 4 ---------------------------------------------------------------------------------------------
class PhoneStyleFlowTests(RemoteBase):
    def test_origin_that_is_not_a_configured_device_is_an_unknown_node(self):
        """Current behaviour: a phone-like ORIGIN is not in `devices`, so with no explicit
        device nothing runs and the user gets the 'Unknown node' text."""
        remote = self.start("main")
        with request_from("phone"):
            self.assertEqual(remote.call(None, "open_app", "x"),
                             (False, f"Unknown node 'phone'; {UNKNOWN_ALL}"))
        self.run_here.assert_not_called()
        self.dispatch.assert_not_called()

    def test_phone_origin_can_still_target_a_node_explicitly(self):
        remote = self.start("main")
        with request_from("phone"):
            self.assertEqual(remote.call("node", "open_app", "x"), (True, "dispatched"))
        self.assertEqual(self.dispatch.call_args.args[2], "node")


class ConfirmationParkingTests(Base):
    def test_confirm_command_for_node_parks_under_main_when_approvals_are_on_main(self):
        with request_from("node"), approvals_on("main"):
            result, remote = self.run_pipeline([close_command("node")], origin="node")
        self.assertEqual([i["origin"] for i in self.queue().pending()], ["main"])
        self.assertEqual(len(self.queue().pending("main")), 1)
        self.assertEqual(self.queue().pending("node"), [])
        self.assertEqual(self.queue().pending("main")[0]["command"]["device"], "node")  # target kept
        self.assertEqual([i["origin"] for i in result["confirmations"]], ["main"])
        self.assertEqual(remote.calls, [])  # nothing was sent anywhere

    def test_without_approvals_on_it_parks_under_the_requests_origin(self):
        with request_from("node"):
            result, remote = self.run_pipeline([close_command("node")], origin="node")
        self.assertEqual(len(self.queue().pending("node")), 1)
        self.assertEqual(self.queue().pending("main"), [])
        self.assertEqual([i["origin"] for i in result["confirmations"]], ["node"])
        self.assertEqual(remote.calls, [])

    def test_confirm_command_without_state_is_not_parked(self):
        """state=None (no queue to park in) holds the request unparked; unchanged by the seam."""
        remote = FakeRemote()
        with approvals_on("main"):
            result = pipeline._run_turn([close_command("node")], cluster_config()["devices"], "node",
                                        "owner", remote, lambda d, n: True, lambda *a, **k: None,
                                        None, state=None)
        self.assertEqual(result["outcomes"][0]["state"], "not_sent")
        self.assertEqual(result["confirmations"], [])
        self.assertIn("can't ask for confirmations", result["reply"])
        self.assertEqual(remote.calls, [])


# 5 ---------------------------------------------------------------------------------------------
class ExplicitOriginArgumentTests(Base):
    def test_explicit_origin_argument_wins_over_the_origin_contextvar_for_parking(self):
        with request_from("node"):  # ContextVar says node; the argument says other
            result, _ = self.run_pipeline([close_command("other")], origin="other")
        self.assertEqual(len(self.queue().pending("other")), 1)
        self.assertEqual(self.queue().pending("node"), [])
        self.assertEqual([i["origin"] for i in result["confirmations"]], ["other"])

    def test_approvals_on_main_beats_the_origin_argument(self):
        with request_from("node"), approvals_on("main"):
            self.run_pipeline([close_command("other")], origin="other")
        self.assertEqual(len(self.queue().pending("main")), 1)
        self.assertEqual(self.queue().pending("other"), [])
        self.assertEqual(self.queue().pending("node"), [])

    def test_no_origin_argument_means_the_executors_own_here_not_the_contextvar(self):
        remote = FakeRemote(here="main")
        with request_from("node"):
            self.run_pipeline([close_command("node")], origin=None, remote=remote)
        self.assertEqual(len(self.queue().pending("main")), 1)
        self.assertEqual(self.queue().pending("node"), [])

    def test_partial_block_branch_parks_under_the_argument_and_under_the_override(self):
        """A runnable command plus a confirm one: the confirm one parks, the rest runs."""
        commands = [open_command("other"), close_command("other")]
        with request_from("node"):
            result, remote = self.run_pipeline(commands, origin="other")
        self.assertEqual(len(self.queue().pending("other")), 1)
        self.assertEqual(self.queue().pending("node"), [])
        self.assertEqual([c for c in remote.calls if c[1] == "open_app"][0][0], "other")  # ran
        self.assertEqual(result["outcomes"][0]["state"] != "not_sent", True)

        with approvals_on("main"):
            self.run_pipeline(commands, origin="other")
        self.assertEqual(len(self.queue().pending("main")), 1)
        self.assertEqual(len(self.queue().pending("other")), 1)  # the earlier one is untouched

    def test_hard_fail_branch_with_a_dependent_command_parks_nothing_for_the_dependent(self):
        dependent = {**close_command("other", "Gimp", cid="c2"), "depends_on": ["c1"]}
        with approvals_on("main"):
            result, _ = self.run_pipeline([close_command("other"), dependent], origin="other")
        pending = self.queue().pending()
        self.assertEqual([(i["origin"], i["command"]["id"]) for i in pending], [("main", "c1")])
        self.assertIn("request that action separately", result["reply"])


# 6 ---------------------------------------------------------------------------------------------
class ClusterDisabledTests(Base):
    def setUp(self):
        super().setUp()
        patcher = mock.patch("jarvis.grokbot.Grokbot", FakeGrokbot)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Any attempt to read cluster identity would blow up loudly.
        guard = mock.patch("jarvis.topology.identity", side_effect=AssertionError("identity read"))
        self.identity = guard.start()
        self.addCleanup(guard.stop)

    def remote(self, config):
        return Remote(config, self.state, tools=object())

    def test_no_cluster_key_runs_here_with_config_and_state(self):
        config = standalone_config()
        with mock.patch("jarvis.remote.run_here", return_value=(True, "ran here")) as run_here:
            remote = self.remote(config)
            self.assertEqual(remote.call(None, "open_app", "x"), (True, "ran here"))
            self.assertEqual(remote.call("zenbook", "open_app", "x"), (True, "ran here"))  # here()
        self.assertEqual(run_here.call_args.kwargs, {"config": config, "state": self.state})
        self.identity.assert_not_called()

    def test_no_cluster_key_unknown_laptop_message_is_the_legacy_one(self):
        remote = self.remote(standalone_config())
        self.assertEqual(remote.call("ghost", "open_app", "x"),
                         (False, "Unknown laptop 'ghost'; known: dell, zenbook."))
        self.identity.assert_not_called()

    def test_no_cluster_key_other_laptop_uses_the_direct_transport_not_the_hub(self):
        with mock.patch.object(Remote, "_tunnel", return_value=(True, "tunnel")) as tunnel, \
                mock.patch("jarvis.cluster.dispatch") as dispatch, mock.patch("jarvis.cluster.Client") as client:
            self.assertEqual(self.remote(standalone_config()).call("dell", "open_app", "x"), (True, "tunnel"))
        tunnel.assert_called_once()
        dispatch.assert_not_called()
        client.assert_not_called()

    def test_origin_is_ignored_for_targeting_when_the_cluster_is_off(self):
        with mock.patch("jarvis.remote.run_here", return_value=(True, "ran here")), \
                request_from("dell"):
            self.assertEqual(self.remote(standalone_config()).call(None, "open_app", "x"), (True, "ran here"))

    def test_parking_works_with_no_cluster_key_and_reads_no_identity(self):
        config = standalone_config()
        result, _ = self.run_pipeline([close_command("zenbook")], origin="zenbook", config=config)
        self.assertEqual(len(self.queue().pending("zenbook")), 1)
        with approvals_on("dell"):
            self.run_pipeline([close_command("zenbook")], origin="zenbook", config=config)
        self.assertEqual(len(self.queue().pending("dell")), 1)
        self.identity.assert_not_called()

    def test_malformed_but_disabled_cluster_config_does_not_raise_anywhere(self):
        bad = {"enabled": False, "main_device": 5, "local_device": None}
        config = standalone_config(**bad)
        with mock.patch("jarvis.remote.run_here", return_value=(True, "ran here")):
            self.assertEqual(self.remote(config).call(None, "open_app", "x"), (True, "ran here"))
        self.assertEqual(confirmation_station("zenbook"), "zenbook")
        self.run_pipeline([close_command("zenbook")], origin="zenbook", config=config)
        self.assertEqual(len(self.queue().pending("zenbook")), 1)
        self.identity.assert_not_called()

    def test_assistant_here_is_the_grokbot_answer_when_the_cluster_is_off(self):
        config = standalone_config()
        self.assertEqual(Remote(config, self.state).here(), "zenbook")
        self.assertEqual(assistant.confirmation_origin(config, self.state), "zenbook")


# 7 ---------------------------------------------------------------------------------------------
class ExceptionAndRestorationTests(RemoteBase):
    def test_node_offline_and_oserror_from_the_hub_become_false_text_and_restore_the_vars(self):
        remote = self.start("node")
        for error in (NodeOffline("node is offline"), OSError("link down")):
            self.client.return_value.request.side_effect = error
            with request_from("other"), approvals_on("main"):
                got = remote.call(None, "open_app", "x")
                self.assertEqual((ORIGIN.get(), APPROVAL_ORIGIN.get()), ("other", "main"))
            self.assertEqual(got, (False, str(error)))
            self.assertIsNone(ORIGIN.get())
            self.assertIsNone(APPROVAL_ORIGIN.get())

    def test_main_dispatch_offline_without_ssh_target_returns_false_text(self):
        remote = self.start("main")
        self.dispatch.side_effect = NodeOffline("node offline")
        with request_from("node"):
            self.assertEqual(remote.call(None, "open_app", "x"), (False, "node offline"))
        self.assertIsNone(ORIGIN.get())

    def test_unexpected_exception_propagates_and_both_vars_are_restored(self):
        remote = self.start("node")
        self.client.return_value.request.side_effect = ZeroDivisionError("boom")
        with self.assertRaises(ZeroDivisionError):
            with request_from("other"), approvals_on("main"):
                remote.call(None, "open_app", "x")
        self.assertIsNone(ORIGIN.get())
        self.assertIsNone(APPROVAL_ORIGIN.get())

    def test_unexpected_exception_while_parking_propagates_and_vars_are_restored(self):
        boom = mock.patch("jarvis.confirm.ConfirmQueue.park", side_effect=KeyError("disk"))
        with boom, self.assertRaises(KeyError):
            with request_from("node"), approvals_on("main"):
                self.run_pipeline([close_command("node")], origin="node")
        self.assertIsNone(ORIGIN.get())
        self.assertIsNone(APPROVAL_ORIGIN.get())
        self.assertEqual(self.queue().pending(), [])

    def test_nested_blocks_restore_each_level_even_when_the_inner_one_raises(self):
        with request_from("a"), approvals_on("x"):
            with self.assertRaises(RuntimeError):
                with request_from("b"), approvals_on("y"):
                    self.assertEqual((ORIGIN.get(), APPROVAL_ORIGIN.get()), ("b", "y"))
                    with request_from(None):
                        self.assertIsNone(ORIGIN.get())
                        self.assertEqual(confirmation_station("o"), "y")
                    self.assertEqual(ORIGIN.get(), "b")
                    raise RuntimeError("inner")
            self.assertEqual((ORIGIN.get(), APPROVAL_ORIGIN.get()), ("a", "x"))
            self.assertEqual(confirmation_station("o"), "x")
        self.assertIsNone(ORIGIN.get())
        self.assertIsNone(APPROVAL_ORIGIN.get())


# 8 ---------------------------------------------------------------------------------------------
class DenyApprovalAndAlwaysAskTests(Base):
    HARD = {"id": "c3", "intent": "terminal", "device": "node", "args": {"command": "rm -rf ~"},
            "depends_on": []}

    def test_the_chosen_command_really_is_hard_denied(self):
        self.assertTrue(gates.hard_deny("terminal", self.HARD["args"], cluster_config()))

    def test_hard_denied_command_is_refused_not_parked_even_with_approvals_on_main(self):
        for station in (None, "main"):
            with request_from("node"), approvals_on(station):
                result, remote = self.run_pipeline([self.HARD], origin="node")
            self.assertTrue(result["reply"].startswith("Refused:"), result["reply"])
            self.assertEqual(result["outcomes"][0]["state"], "not_sent")
            self.assertEqual(self.queue().pending(), [])
            self.assertEqual(remote.calls, [])

    def test_a_hard_denied_step_holds_the_whole_request_and_parks_nothing(self):
        with approvals_on("main"):
            result, remote = self.run_pipeline([close_command("node"), self.HARD], origin="node")
        self.assertEqual(self.queue().pending(), [])
        self.assertEqual({o["state"] for o in result["outcomes"]}, {"not_sent"})
        self.assertEqual(remote.calls, [])

    def test_a_parked_item_can_only_be_approved_from_the_station_it_was_parked_at(self):
        with approvals_on("main"):
            self.run_pipeline([close_command("node")], origin="node")
        queue = self.queue()
        item = queue.pending("main")[0]
        for wrong in ("node", "other"):
            with self.assertRaisesRegex(ValueError, "No pending confirmation for this device"):
                queue.approve(item["id"], wrong)
            with self.assertRaisesRegex(ValueError, "No pending confirmation for this device"):
                queue.approve(None, wrong)
            with self.assertRaisesRegex(ValueError, "No pending confirmation for this device"):
                queue.reject(item["id"], origin=wrong)
        self.assertEqual(len(queue.pending("main")), 1)  # still waiting after the wrong tries
        approved = queue.approve(item["id"], "main")
        self.assertEqual(approved["intent"], "close_app")
        self.assertEqual(approved["device"], "node")
        self.assertEqual(queue.pending(), [])
        with self.assertRaisesRegex(ValueError, "expired or was already handled"):
            queue.approve(item["id"], "main")  # single use

    def test_without_override_the_origin_is_the_only_approver(self):
        self.run_pipeline([close_command("node")], origin="node")
        queue = self.queue()
        item = queue.pending("node")[0]
        with self.assertRaises(ValueError):
            queue.approve(item["id"], "main")
        self.assertEqual(queue.approve(item["id"], "node")["intent"], "close_app")

    def test_rules_confirm_never_does_not_park_ordinary_risk_but_always_ask_still_applies(self):
        config = cluster_config(rules={"confirm": "never", "trusted_local": {"enabled": False}})
        self.assertTrue(gates.always_asks_text("buy me headphones", config))
        self.assertTrue(gates.always_asks_text("send a message to Sam", config))
        self.assertFalse(gates.always_asks_text("open notepad", config))
        off = cluster_config(rules={"confirm": "never", "always_ask": []})
        self.assertFalse(gates.always_asks_text("buy me headphones", off))

    def test_agent_task_money_or_message_parks_at_the_override_or_the_resolved_origin(self):
        """assistant._ask: a Grok task that spends money/sends a message waits for David even
        under rules.confirm never; it parks via confirmation_station(ORIGIN or here())."""
        config = standalone_config()
        config["routing"] = {"interpreter": False}
        config["rules"] = {"confirm": "never", "trusted_local": {"enabled": False}}
        config["personal_agents"] = {"grokbot": {"integration": "window_automation"}}

        class Memory:
            path = self.state / "memory.sqlite3"

            def add(self, *args, **kwargs):
                return {"id": "m1"}

            def merge_meta(self, *args, **kwargs):
                pass

        def ask(text="buy me headphones"):
            with contextlib.ExitStack() as stack:
                for patcher in (mock.patch("jarvis.grokbot.Grokbot", FakeGrokbot),
                                mock.patch.object(assistant, "_note_correction"),
                                mock.patch.object(assistant, "_confirmation_turn", return_value=None),
                                mock.patch.object(assistant, "_fast_path", return_value=None),
                                mock.patch.object(assistant, "_shadow"),
                                mock.patch.object(assistant, "_needs_agent", return_value=True),
                                mock.patch("jarvis.grok_preflight.assess", return_value={
                                    "decision": "forward", "task": text, "checks": []})):
                    stack.enter_context(patcher)
                return assistant._ask(text, None, Memory(), config, ".", channel="cli")

        result = ask()
        self.assertEqual([i["origin"] for i in result["confirmations"]], ["zenbook"])  # here()
        with request_from("dell"):
            ask()
        with request_from("dell"), approvals_on("main"):
            ask()
        self.assertEqual(sorted(i["origin"] for i in self.queue().pending()),
                         ["dell", "main", "zenbook"])
        for item in self.queue().pending():
            self.assertEqual(item["command"]["intent"], "agent_task")
            self.assertEqual(item["command"]["device"], item["origin"] if item["origin"] != "main" else "dell")

    def test_consumer_side_confirmation_origin_ignores_the_approval_override(self):
        """Pinned legacy behaviour: confirm_pending binds to ORIGIN or this PC, never to
        APPROVAL_ORIGIN (a phone-parked item is approved from the main console as `main`)."""
        config = cluster_config("main")
        with approvals_on("main"), request_from("node"):
            self.assertEqual(assistant.confirmation_origin(config, self.state), "node")
        with approvals_on("other"):
            self.assertEqual(assistant.confirmation_origin(config, self.state), "main")
        self.assertEqual(assistant.confirmation_origin(config, self.state), "main")


# 9 ---------------------------------------------------------------------------------------------
class ConfirmationStationTests(unittest.TestCase):
    def test_override_wins_else_argument_else_none(self):
        self.assertIsNone(confirmation_station(None))
        self.assertEqual(confirmation_station("zenbook"), "zenbook")
        with approvals_on("main"):
            self.assertEqual(confirmation_station("zenbook"), "main")
            self.assertEqual(confirmation_station(None), "main")
        self.assertIsNone(APPROVAL_ORIGIN.get())

    def test_falsy_override_falls_back_to_the_argument(self):
        with approvals_on(""):
            self.assertEqual(confirmation_station("zenbook"), "zenbook")
        with approvals_on(None):
            self.assertEqual(confirmation_station("zenbook"), "zenbook")

    def test_it_ignores_the_origin_contextvar_and_reads_no_cluster_identity(self):
        with mock.patch("jarvis.topology.identity", side_effect=AssertionError("identity read")), \
                mock.patch.object(legacy, "identity", side_effect=AssertionError("identity read")), \
                request_from("node"):
            self.assertEqual(confirmation_station("zenbook"), "zenbook")
            self.assertIsNone(confirmation_station(None))

    def test_topology_reexports_the_same_function(self):
        self.assertIs(topology.confirmation_station, legacy.confirmation_station)

    def test_roles_agree_with_the_function_across_the_whole_matrix(self):
        local = "local-pc"
        for approval in (None, "", "main", "node"):
            for origin in (None, "", "main", "zenbook"):
                roles = Roles(local, "main", origin, approval)
                expected = approval or origin or local
                with self.subTest(approval=approval, origin=origin):
                    self.assertEqual(roles.confirmation_station(), expected)
                    with approvals_on(approval):
                        # The function is handed the caller's already-resolved origin.
                        self.assertEqual(confirmation_station(origin or local), expected)
                        if origin:
                            self.assertEqual(confirmation_station(origin), roles.confirmation_station())

    def test_roles_execution_target_rule(self):
        for explicit, origin, expected in ((None, None, "l"), (None, "o", "o"), ("e", "o", "e"),
                                           ("e", None, "e"), ("", "o", "o")):
            with self.subTest(explicit=explicit, origin=origin):
                self.assertEqual(Roles("l", "m", origin, None).execution_target(explicit), expected)


class IdentityPatchCompatTests(RemoteBase):
    def test_patching_topology_identity_drives_the_target_computation(self):
        """Remote.call reads identity through `jarvis.topology`'s (patchable) name."""
        remote = self.start("main")  # config says: this PC is the main
        with mock.patch("jarvis.topology.identity", return_value=("node", "main")) as ident:
            self.assertEqual(remote.call("other", "open_app", "x"), (True, "forwarded"))
        ident.assert_called()
        self.client.return_value.request.assert_called_once()
        self.dispatch.assert_not_called()  # a node forwards; only the main dispatches

    def test_a_patched_identity_also_makes_a_node_run_here_without_config(self):
        remote = self.start("main")
        with mock.patch("jarvis.topology.identity", return_value=("node", "main")):
            self.assertEqual(remote.call("node", "open_app", "x"), (True, "ran here"))
        self.assert_here(with_config=False)

    def test_patched_identity_is_honoured_even_when_the_configured_one_would_raise(self):
        config = cluster_config("ghost-device")  # real identity() raises ValueError for this
        with self.assertRaises(ValueError):
            topology.identity(config)
        remote = Remote(config, self.state, tools=object())
        self.run_here = self.patch("jarvis.remote.run_here", return_value=(True, "ran here"))
        with mock.patch("jarvis.topology.identity", return_value=("main", "main")):
            self.assertEqual(remote.call(None, "open_app", "x"), (True, "ran here"))
        self.assertEqual(self.run_here.call_args.kwargs, {"config": config, "state": self.state})

    def test_unpatched_bad_identity_still_fails_closed_in_the_cluster_branch(self):
        remote = Remote(cluster_config("ghost-device"), self.state, tools=object())
        with self.assertRaisesRegex(ValueError, "local_device"):
            remote.call(None, "open_app", "x")


if __name__ == "__main__":
    unittest.main()
