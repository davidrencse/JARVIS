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
