"""Legacy entry points carry an opt-in continuity gate that is inert by default."""

import ast
import inspect
import tempfile
import textwrap
import unittest
from pathlib import Path

from jarvis import cluster, cluster_cli, phone, remote, rest
from jarvis.continuity.integration import (
    IntegrationError, close_unfenced, open_unfenced, phone_listener_open, refuse_unfenced,
    set_phone_listener_open,
)


def first_calls(func):
    """Names of `refuse_unfenced("<surface>")` calls in source order inside one function."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "refuse_unfenced" and node.args):
            found.append((node.lineno, node.args[0].value))
    return sorted(found)


class Hooks(unittest.TestCase):
    def tearDown(self):
        open_unfenced()

    def test_default_is_inert(self):
        for surface in ("set_main", "cluster_local", "hub_dispatch", "remote_call", "sleep"):
            refuse_unfenced(surface)
        self.assertTrue(phone_listener_open())

    def test_every_surface_has_its_gate(self):
        sources = {
            "set_main": cluster_cli.set_main, "cluster_local": cluster.execute_local,
            "hub_dispatch": cluster.Hub.dispatch, "remote_call": remote.Remote.call,
            "sleep": rest.sleep,
        }
        for surface, func in sources.items():
            self.assertIn(surface, [name for _, name in first_calls(func)], surface)

    def test_sleep_gate_precedes_pause_and_suspend(self):
        source = inspect.getsource(rest.sleep)
        self.assertLess(source.index('refuse_unfenced("sleep")'), source.index("pause(state)"))
        self.assertLess(source.index('refuse_unfenced("sleep")'), source.index("if not suspend"))

    def test_closed_surface_refuses_before_any_effect(self):
        close_unfenced("set_main", "cluster_local")
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "jarvis.config.json"
            with self.assertRaises(IntegrationError) as caught:
                cluster_cli.set_main({}, config, "dell", state=Path(folder) / "state")
            self.assertEqual(caught.exception.code, "legacy_retained")
            self.assertFalse(config.exists())
            self.assertFalse((Path(folder) / "state").exists())
        with self.assertRaises(IntegrationError):
            cluster.execute_local("desktop", {"action": "screenshot", "arg": ""}, {}, Path("/nonexistent"))

    def test_phone_listener_can_be_closed(self):
        set_phone_listener_open(False)
        self.assertFalse(phone_listener_open())
        self.assertIn("phone_listener_open", inspect.getsource(phone.PhoneHandler._handle))
        open_unfenced()
        self.assertTrue(phone_listener_open())

    def test_seen_mark_after_is_opt_in(self):
        source = inspect.getsource(cluster.NodeWorker)
        self.assertIn('node_seen_mode() == "after"', source)
        self.assertIn("_mark_seen", source)


if __name__ == "__main__":
    unittest.main()
