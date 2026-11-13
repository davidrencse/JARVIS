"""Opt-in from= pin on a node key. Temp authorized_keys only; nothing under ~/.ssh."""

import tempfile
import unittest
from pathlib import Path

from jarvis import cluster_setup

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDLTmUvsc7l7vzfOy/yHk0DpdkDZ62R+t93R+78kEG+R"
OPTIONS = (
    'restrict,port-forwarding,permitopen="127.0.0.1:7780",'
    'permitlisten="127.0.0.1:1",command="echo jarvis-node-link"'
)
LINE = f"{OPTIONS} {KEY} jarvis-node:dell"
OTHER = "ssh-ed25519 AAAAexisting someone@else\n"


class FromPinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = Path(self.tmp.name) / "authorized_keys"
        self.keys.write_text(OTHER, encoding="utf-8")

    def test_default_line_is_the_historical_string(self):
        self.assertEqual(cluster_setup.restrictions(7780), OPTIONS)
        self.assertEqual(cluster_setup.restrictions(7780, None), OPTIONS)
        self.assertEqual(cluster_setup.restrictions(7780, ""), OPTIONS)
        cluster_setup.authorize("dell", KEY, 7780, self.keys)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), OTHER + LINE + "\n")

    def test_from_addr_is_prepended_to_restrict(self):
        cluster_setup.authorize("dell", KEY, 7780, self.keys, from_addr="1.2.3.4")
        line = self.keys.read_text(encoding="utf-8").splitlines()[1]
        self.assertTrue(line.startswith('from="1.2.3.4",restrict,port-forwarding,'))
        self.assertEqual(line, f'from="1.2.3.4",{LINE}')
        self.assertEqual(
            cluster_setup.restrictions(7780, "1.2.3.4,5.6.7.8"),
            f'from="1.2.3.4,5.6.7.8",{OPTIONS}')

    def test_injection_raises_and_writes_nothing(self):
        before = self.keys.read_text(encoding="utf-8")
        for bad in (
            '1.2.3.4",command="x',
            "1.2.3.4 ",
            "1.2.3.4\n5.6.7.8",
            "1.2.3.4,not a host",
            "1.2.3.4,",
            "1.2.3.4,command=x",
            'from="1.2.3.4"',
            "*",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cluster_setup.authorize("dell", KEY, 7780, self.keys, from_addr=bad)
                with self.assertRaises(ValueError):
                    cluster_setup.restrictions(7780, bad)
        self.assertEqual(self.keys.read_text(encoding="utf-8"), before)

    def test_opt_in_off_writes_no_pin(self):
        config = {"cluster": {"pin_node_address": False},
                  "devices": {"dell": {"lan_ip": "1.2.3.4", "target": "user@5.6.7.8"}}}
        self.assertIsNone(cluster_setup.pinned_from(config, "dell"))
        self.assertIsNone(cluster_setup.pinned_from(
            {"cluster": {}, "devices": {"dell": {"lan_ip": "1.2.3.4"}}}, "dell"))
        cluster_setup.authorize("dell", KEY, 7780, self.keys,
                                from_addr=cluster_setup.pinned_from(config, "dell"))
        self.assertEqual(self.keys.read_text(encoding="utf-8"), OTHER + LINE + "\n")

    def test_opt_in_on_without_a_known_address_writes_no_pin(self):
        unknown = {"cluster": {"pin_node_address": True},
                   "devices": {"dell": {"hostname": "WinLaptop", "label": "Dell"}}}
        junk = {"cluster": {"pin_node_address": True},
                "devices": {"dell": {"lan_ip": '1.2.3.4",command="x',
                                     "target": "user@not a host"}}}
        self.assertIsNone(cluster_setup.pinned_from(unknown, "dell"))
        self.assertIsNone(cluster_setup.pinned_from(junk, "dell"))
        self.assertIsNone(cluster_setup.pinned_from(
            {"cluster": {"pin_node_address": True}, "devices": {}}, "dell"))
        cluster_setup.authorize("dell", KEY, 7780, self.keys,
                                from_addr=cluster_setup.pinned_from(unknown, "dell"))
        self.assertEqual(self.keys.read_text(encoding="utf-8"), OTHER + LINE + "\n")

    def test_opt_in_uses_recorded_addresses_only(self):
        config = {"cluster": {"pin_node_address": True},
                  "devices": {"dell": {"lan_ip": "1.2.3.4",
                                       "target": "jdoe@5.6.7.8",
                                       "fallback_targets": ["jdoe@1.2.3.4", "bad host",
                                                            "127.0.0.1"]}}}
        self.assertEqual(cluster_setup.pinned_from(config, "dell"), "1.2.3.4,5.6.7.8")
        cluster_setup.authorize("dell", KEY, 7780, self.keys,
                                from_addr=cluster_setup.pinned_from(config, "dell"))
        line = self.keys.read_text(encoding="utf-8").splitlines()[1]
        self.assertTrue(line.startswith('from="1.2.3.4,5.6.7.8",restrict,port-forwarding,'))


if __name__ == "__main__":
    unittest.main()
