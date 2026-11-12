"""Cross-laptop storage browsing and copying: boundaries, naming, directions, HTTP."""

import http.client
import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from jarvis import transfer
from jarvis.cluster import validate_action
from jarvis.storage import REMOTE_SCRIPT, Storage
from jarvis.ui import Console, Handler

ROOT = Path(__file__).resolve().parents[2]


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.local = self.base / "dell"
        self.remote = self.base / "zen"  # stands in for the Zenbook's filesystem
        (self.local / "docs").mkdir(parents=True)
        self.remote.mkdir()
        (self.local / "a.txt").write_text("alpha")
        (self.local / "docs" / "b.txt").write_text("beta")
        (self.base / "secret.txt").write_text("outside")
        self.config = {
            "devices": {"dell": {"transport": "local", "hostname": "D"},
                        "zen": {"transport": "ssh", "hostname": "Z", "target": "jdoe@10.0.0.2"}},
            "storage": {"dell": {"roots": [str(self.local)]}, "zen": {"roots": [str(self.remote)]}}}
        self.fs = Storage(self.config)


class BoundaryTests(Temp):
    def test_listing_is_sorted_folders_first_and_stays_in_roots(self):
        out = self.fs.list("dell")
        self.assertEqual([e["name"] for e in out["entries"]], ["docs", "a.txt"])
        self.assertIsNone(out["parent"])  # at the root, there is nowhere further up
        inner = self.fs.list("dell", str(self.local / "docs"))
        self.assertEqual(inner["parent"], str(self.local))

    def test_nothing_outside_the_roots_is_reachable(self):
        for path in (str(self.base), str(self.base / "secret.txt"), str(self.local / ".." / "zen"),
                     str(self.local / "docs" / ".." / ".." / "secret.txt"), "C:\\Windows", "/etc"):
            with self.assertRaises(ValueError, msg=path):
                self.fs.list("dell", path)

    def test_symlinks_cannot_lead_out_of_the_roots(self):
        link = self.local / "escape"
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks need privileges on this Windows account")
        with self.assertRaises(ValueError):
            self.fs.list("dell", str(link))

    def test_unknown_device_and_file_listing_are_clean_errors(self):
        with self.assertRaises(ValueError):
            self.fs.list("toaster")
        with self.assertRaises(ValueError):
            self.fs.list("dell", str(self.local / "a.txt"))  # a file, not a folder

    def test_default_roots_are_just_the_home_folder(self):
        fs = Storage({"devices": self.config["devices"]})
        self.assertEqual(fs.roots("dell"), [str(Path.home())])
        self.assertEqual(fs.roots("zen"), ["~"])

    def test_remote_script_runs_and_enforces_roots_on_a_real_interpreter(self):
        """The script sent over SSH is executed here with python, as the Zenbook would."""
        def run(path, action="list", roots=None):
            req = json.dumps({"action": action, "path": path, "roots": roots or [str(self.remote)]})
            script = f"REQUEST = {json.dumps(req)}\n" + REMOTE_SCRIPT
            done = subprocess.run(["python", "-"], input=script, capture_output=True, text=True,
                                  timeout=30)
            return json.loads(done.stdout.strip().splitlines()[-1])

        (self.remote / "x y.txt").write_text("1")
        (self.remote / "dir").mkdir()
        listing = run(str(self.remote))
        self.assertEqual(sorted(e["name"] for e in listing["entries"]), ["dir", "x y.txt"])
        self.assertIn("error", run(str(self.base)))  # parent of the root
        self.assertIn("error", run(str(self.remote / ".." / "dell")))
        self.assertIn("error", run(str(self.remote / "missing")))
        self.assertIn("error", run(str(self.remote / "x y.txt"), "list"))  # not a folder
        # A hostile path is data, never code: quotes and shell syntax change nothing.
        self.assertIn("error", run(str(self.remote) + "\"; import os; os.system('x') #"))


class FakeRemote:
    """Runs scp/list against folders on this machine instead of over SSH."""

    def __init__(self, test):
        self.test, self.calls = test, []

    def remote(self, device_id, path, action):
        if action == "exists":
            candidate = Path(path)
            root = self.test.remote
            parent = candidate.parent.resolve()
            if parent != root and root not in parent.parents:
                raise ValueError("That folder is outside the allowed locations.")
            return {"exists": candidate.exists() or candidate.is_symlink()}
        path = Path(path or self.test.remote).resolve()
        root = self.test.remote
        if path != root and root not in path.parents:
            raise ValueError("That folder is outside the allowed locations.")
        if not path.exists():
            raise ValueError("No such file or folder.")
        # The Zenbook speaks POSIX paths; mirror that so name handling is exercised for real.
        res = {"path": path.as_posix(), "dir": path.is_dir(), "size": path.stat().st_size,
               "roots": [root.as_posix()]}
        if action == "list":
            res["entries"] = [{"name": p.name, "dir": p.is_dir(), "size": 0, "mtime": 0}
                              for p in path.iterdir()]
        return res

    def run(self, args, **kw):
        """Emulate scp: the remote side is `jdoe@10.0.0.2:<path>` on this machine."""
        self.calls.append((args, kw.get("cwd")))
        import shutil
        clean = list(args[args.index("--") + 1:])
        src, dst = clean
        cwd = Path(kw["cwd"])

        def path_of(arg):
            return Path(arg.split(":", 1)[1]) if arg.startswith("jdoe@10.0.0.2:") else cwd / arg
        s, d = path_of(src), path_of(dst)
        if "-r" in args:
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
        return subprocess.CompletedProcess(args, 0, "", "")


class CopyTests(Temp):
    def setUp(self):
        super().setUp()
        self.fake = FakeRemote(self)
        patcher = [patch.object(Storage, "_remote", lambda s, d, p, a: self.fake.remote(d, p, a)),
                   patch("jarvis.storage.subprocess.run", lambda args, **kw: self.fake.run(args, **kw)),
                   patch("jarvis.storage.shutil.which", lambda n: n)]
        for p in patcher:
            p.start()
            self.addCleanup(p.stop)

    def test_file_goes_dell_to_zenbook_and_never_overwrites(self):
        one = self.fs.copy("dell", str(self.local / "a.txt"), "zen", str(self.remote))
        two = self.fs.copy("dell", str(self.local / "a.txt"), "zen", str(self.remote))
        self.assertEqual((one["as"], two["as"]), ("a.txt", "a (2).txt"))
        self.assertEqual((self.remote / "a (2).txt").read_text(), "alpha")
        self.assertEqual(one["bytes"], 5)

    def test_folder_goes_both_ways_and_into_a_chosen_subfolder(self):
        (self.remote / "inbox").mkdir()
        out = self.fs.copy("dell", str(self.local / "docs"), "zen", str(self.remote / "inbox"))
        self.assertTrue(out["dir"])
        self.assertEqual((self.remote / "inbox" / "docs" / "b.txt").read_text(), "beta")
        back = self.fs.copy("zen", str(self.remote / "inbox" / "docs"), "dell", str(self.local))
        self.assertEqual(back["as"], "docs (2)")  # docs already exists on the Dell
        self.assertEqual((self.local / "docs (2)" / "b.txt").read_text(), "beta")

    def test_local_arguments_are_bare_names_so_drive_letters_cannot_become_hosts(self):
        self.fs.copy("dell", str(self.local / "a.txt"), "zen", str(self.remote))
        args, cwd = self.fake.calls[-1]
        self.assertEqual(cwd, str(self.local))
        self.assertEqual(args[args.index("--") + 1], "a.txt")
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)

    def test_bad_copies_are_refused_before_anything_runs(self):
        cases = (("dell", str(self.local / "a.txt"), "dell", str(self.local)),     # same machine
                 ("zen", str(self.remote), "zen", str(self.remote)),
                 ("dell", str(self.base / "secret.txt"), "zen", str(self.remote)),  # outside roots
                 ("dell", str(self.local / "a.txt"), "zen", str(self.base)),
                 ("dell", str(self.local / "a.txt"), "zen", str(self.remote / "nope")))
        for case in cases:
            with self.assertRaises(ValueError, msg=str(case)):
                self.fs.copy(*case)
        self.assertEqual(self.fake.calls, [])

    def test_destination_must_be_a_folder(self):
        (self.remote / "f.txt").write_text("x")
        with self.assertRaisesRegex(ValueError, "must be a folder"):
            self.fs.copy("dell", str(self.local / "a.txt"), "zen", str(self.remote / "f.txt"))


class ClusterCopyTests(Temp):
    """The Dell is a hub-only node (no SSH target): copies ride chunked transfer jobs."""

    def setUp(self):
        super().setUp()
        self.node = self.base / "node"  # the Dell's storage root, on this machine
        self.node.mkdir()
        self.state = self.base / "state"
        self.state.mkdir()
        self.config = {
            "devices": {"main": {"transport": "local", "hostname": "M"},
                        "dell": {"transport": "cluster", "hostname": "D", "label": "Dell G15"}},
            "cluster": {"enabled": True, "main_device": "main", "local_device": "main"},
            "storage": {"main": {"roots": [str(self.local)]}, "dell": {"roots": [str(self.node)]}}}
        self.node_config = {**self.config, "cluster": {**self.config["cluster"],
                                                       "local_device": "dell"}}
        self.fs = Storage(self.config, self.state)
        self.calls = []

        def fake_call(config, state, device, payload, timeout=120):
            """What the hub does: JSON across, validated, run on the node with ITS config."""
            self.calls.append(payload)
            payload = json.loads(json.dumps(payload))
            validate_action("transfer", payload)
            return json.loads(json.dumps(transfer.run_here(payload, self.state, self.node_config)))

        def fake_dispatch(config, state, node, kind, payload, timeout=45):
            self.assertEqual((node, kind), ("dell", "storage"))
            return Storage(self.node_config)._local("dell", payload["path"], payload["action"])

        for target in (patch("jarvis.transfer.call", fake_call),
                       patch("jarvis.cluster.dispatch", fake_dispatch)):
            target.start()
            self.addCleanup(target.stop)

    def test_file_goes_main_to_node_and_never_overwrites(self):
        (self.node / "inbox").mkdir()
        with patch.object(transfer, "CHUNK", 2):
            one = self.fs.copy("main", str(self.local / "a.txt"), "dell", str(self.node / "inbox"))
        two = self.fs.copy("main", str(self.local / "a.txt"), "dell", str(self.node / "inbox"))
        self.assertEqual((one["as"], two["as"]), ("a.txt", "a (2).txt"))
        self.assertEqual((self.node / "inbox" / "a.txt").read_text(), "alpha")
        self.assertEqual((self.node / "inbox" / "a (2).txt").read_text(), "alpha")
        self.assertEqual((one["copied"], one["dir"], one["bytes"]), ("a.txt", False, 5))
        self.assertEqual(one["to"], f"dell:{self.node / 'inbox'}")
        self.assertEqual([c["last"] for c in self.calls if c["action"] == "put"][:3],
                         [False, False, True])
        self.assertEqual(list((self.node / "inbox").glob(".jarvis-upload-*")), [])

    def test_file_comes_node_to_main_in_chunks_without_overwriting(self):
        (self.node / "n.bin").write_bytes(bytes(range(11)))
        (self.local / "n.bin").write_bytes(b"mine")
        with patch.object(transfer, "CHUNK", 4):
            out = self.fs.copy("dell", str(self.node / "n.bin"), "main", str(self.local))
        self.assertEqual(out["as"], "n (2).bin")
        self.assertEqual((self.local / "n (2).bin").read_bytes(), bytes(range(11)))
        self.assertEqual((self.local / "n.bin").read_bytes(), b"mine")
        self.assertEqual(sum(c["action"] == "get" for c in self.calls), 3)
        self.assertEqual(list(self.local.glob(".jarvis-upload-*")), [])

    def test_folder_goes_both_ways_with_subfolders_and_empty_ones(self):
        (self.local / "docs" / "sub" / "empty").mkdir(parents=True)
        (self.local / "docs" / "sub" / "c.txt").write_text("gamma")
        (self.local / "docs" / "zero.txt").write_text("")
        (self.node / "inbox").mkdir()
        out = self.fs.copy("main", str(self.local / "docs"), "dell", str(self.node / "inbox"))
        self.assertTrue(out["dir"])
        self.assertIsNone(out["bytes"])
        landed = self.node / "inbox" / "docs"
        self.assertEqual((landed / "b.txt").read_text(), "beta")
        self.assertEqual((landed / "sub" / "c.txt").read_text(), "gamma")
        self.assertTrue((landed / "sub" / "empty").is_dir())
        self.assertEqual((landed / "zero.txt").read_bytes(), b"")
        back = self.fs.copy("dell", str(landed), "main", str(self.local))
        self.assertEqual(back["as"], "docs (2)")  # docs already exists on the main
        again = self.local / "docs (2)"
        self.assertEqual((again / "sub" / "c.txt").read_text(), "gamma")
        self.assertTrue((again / "sub" / "empty").is_dir())
        self.assertEqual((again / "b.txt").read_text(), "beta")

    def test_folder_copy_skips_links_instead_of_following_them(self):
        (self.local / "docs" / "leak").symlink_to(self.base / "secret.txt")
        self.fs.copy("main", str(self.local / "docs"), "dell", str(self.node))
        self.assertTrue((self.node / "docs" / "b.txt").exists())
        self.assertFalse((self.node / "docs" / "leak").exists())

    def test_destination_outside_the_nodes_roots_is_refused(self):
        for dest in (str(self.base), str(self.node / ".." / "dell")):
            with self.assertRaisesRegex(ValueError, "outside the allowed"):
                self.fs.copy("main", str(self.local / "a.txt"), "dell", dest)
        self.assertEqual(list(self.base.glob("a*.txt")), [])
        # and the node itself refuses a path op aimed outside its roots, whatever main checked
        for payload in ({"action": "mkdir", "dir": str(self.base), "name": "x"},
                        {"action": "put", "upload": "a" * 32, "offset": 0, "last": True,
                         "data": "", "dest": "path", "dir": str(self.base), "name": "x"},
                        {"action": "tree", "path": str(self.base)},
                        {"action": "get", "path": str(self.base / "secret.txt"), "offset": 0}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "outside"):
                transfer.run_here(payload, self.state, self.node_config)
        self.assertFalse((self.base / "x").exists())

    def test_node_refuses_symlink_escape_and_unconfigured_path_ops(self):
        (self.node / "link").symlink_to(self.base)
        with self.assertRaisesRegex(ValueError, "outside"):
            transfer.run_here({"action": "mkdir", "dir": str(self.node / "link"), "name": "x"},
                              self.state, self.node_config)
        with self.assertRaisesRegex(ValueError, "can't check its storage"):
            transfer.run_here({"action": "tree", "path": str(self.node)}, self.state)

    def test_source_outside_the_roots_is_refused_before_any_transfer(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            self.fs.copy("main", str(self.base / "secret.txt"), "dell", str(self.node))
        with self.assertRaisesRegex(ValueError, "outside"):
            self.fs.copy("dell", str(self.base / "secret.txt"), "main", str(self.local))
        self.assertEqual(self.calls, [])

    def test_traversal_and_separator_names_are_refused_by_validation(self):
        base = {"action": "put", "upload": "a" * 32, "offset": 0, "last": True, "data": "",
                "dest": "path", "dir": str(self.node)}
        bad = ("..", ".", "../x", "a/b", "a\\b", "..\\x", "", "x\x00y", "a" * 256)
        for name in bad:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_action("transfer", {**base, "name": name})
                with self.assertRaises(ValueError):
                    validate_action("transfer", {"action": "mkdir", "dir": str(self.node),
                                                 "name": name})
        for rel in (["..", "x"], ["a/b"], ["ok", ".."], "x", [1]):
            with self.subTest(rel=rel), self.assertRaises(ValueError):
                validate_action("transfer", {"action": "get", "path": str(self.node),
                                             "rel": rel, "offset": 0})
        with self.assertRaises(ValueError):  # a drop name and a path together
            validate_action("transfer", {"action": "get", "path": str(self.node),
                                         "name": "a.txt", "offset": 0})
        with self.assertRaises(ValueError):
            validate_action("transfer", {"action": "tree", "path": ""})

    def test_node_get_by_rel_cannot_leave_the_folder_it_was_given(self):
        (self.node / "dir").mkdir()
        (self.node / "other.txt").write_text("x")
        (self.node / "dir" / "esc").symlink_to(self.node / "other.txt")
        with self.assertRaisesRegex(ValueError, "outside the folder"):
            transfer.run_here({"action": "get", "path": str(self.node / "dir"),
                               "rel": ["esc"], "offset": 0}, self.state, self.node_config)

    def test_a_hostile_node_folder_listing_is_refused(self):
        (self.node / "d").mkdir()
        evil = {"dirs": [["..", "x"]], "files": [], "skipped": 0, "bytes": 0}
        with patch("jarvis.transfer.call", lambda *a, **k: evil), \
                self.assertRaisesRegex(ValueError, "unreadable folder listing"):
            self.fs.copy("dell", str(self.node / "d"), "main", str(self.local))
        self.assertFalse((self.local / "d").exists())

    def test_size_and_count_caps_stay(self):
        (self.local / "big.bin").write_bytes(b"x")
        with patch.object(transfer, "MAX_BYTES", 0), \
                self.assertRaisesRegex(ValueError, "aren't copied this way"):
            self.fs.copy("main", str(self.local / "big.bin"), "dell", str(self.node))
        (self.local / "docs" / "more.txt").write_text("m")
        with patch.object(transfer, "MAX_TREE", 1), \
                self.assertRaisesRegex(ValueError, "smaller pieces"):
            self.fs.copy("main", str(self.local / "docs"), "dell", str(self.node))
        self.assertFalse((self.node / "docs").exists())

    def test_a_failed_folder_copy_says_where_the_rest_landed(self):
        real = transfer.push

        def flaky(*a, **k):
            raise ConnectionError("Node dell is offline.")
        with patch("jarvis.transfer.push", flaky), \
                self.assertRaisesRegex(RuntimeError, "stopped part-way .*offline.*docs"):
            self.fs.copy("main", str(self.local / "docs"), "dell", str(self.node))
        self.assertIs(transfer.push, real)

    def test_ssh_devices_keep_using_scp_even_with_the_cluster_on(self):
        self.config["devices"]["dell"] = {"transport": "ssh", "hostname": "D",
                                         "target": "jdoe@10.0.0.9"}
        fs = Storage(self.config, self.state)
        with patch("jarvis.storage.subprocess.run") as run, \
                patch("jarvis.storage.shutil.which", lambda n: n), \
                patch.object(Storage, "_copy_cluster") as cluster:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            fs.copy("main", str(self.local / "a.txt"), "dell", str(self.node))
        cluster.assert_not_called()
        self.assertEqual(run.call_args.args[0][0], "scp")

    def test_without_the_cluster_or_ssh_the_old_plain_error_stays(self):
        config = {**self.config, "cluster": {"enabled": False}}
        config["devices"] = {**config["devices"], "dell": {"transport": "cluster", "label": "Dell"}}
        fs = Storage(config, self.state)
        fs.is_remote = lambda d: d == "dell"
        with self.assertRaisesRegex(ValueError, "needs SSH"):
            fs.copy("dell", "x", "main", str(self.local))


class StorageHttpTests(Temp):
    def setUp(self):
        super().setUp()
        self.console = Console({**json.loads((ROOT / "jarvis.config.json").read_text("utf-8")),
                                **self.config, "cluster": {"enabled": False},
                                "memory": {"semantic": False}, "decisions": {"shadow": False},
                                "routing": {"interpreter": False}},
                               ROOT, self.base / "state")
        handler = type("H", (Handler,), {"console": self.console,
                                          "hosts": ("127.0.0.1", "localhost")})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def call(self, method, path, body=None, header=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"X-JARVIS": "1"} if header else {}
        conn.request(method, path, data, headers)
        res = conn.getresponse()
        return res.status, json.loads(res.read() or b"{}")

    def test_listing_and_devices_over_http(self):
        status, devices = self.call("GET", "/api/fs/devices")
        self.assertEqual((status, [d["id"] for d in devices]), (200, ["dell", "zen"]))
        status, listing = self.call("GET", "/api/fs?device=dell")
        self.assertEqual((status, listing["entries"][0]["name"]), (200, "docs"))

    def test_http_refuses_escapes_unknown_devices_and_missing_csrf_header(self):
        status, body = self.call("GET", "/api/fs?device=dell&path=" + str(self.base))
        self.assertEqual(status, 400)
        self.assertIn("outside", body["error"])
        self.assertEqual(self.call("GET", "/api/fs?device=nope")[0], 400)
        self.assertEqual(self.call("POST", "/api/fs/copy", {}, header=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/fs/copy", {"src_device": "dell"})[0], 400)


if __name__ == "__main__":
    unittest.main()
