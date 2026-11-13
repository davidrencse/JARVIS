"""Files tab over the real console handler.

Temp config shaped like David's cluster: this PC is zenbook (transport ssh, but
it is the local machine), the Dell is a cluster node, Oracle is a gateway with
no files capability. A stub hub enrolls only the Dell, then runs the real
``cluster.execute_local``. No SSH, no live hub, no ``.jarvis`` writes.
"""

import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
import zlib
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

os.environ.setdefault("JARVIS_NO_MODELS", "1")

from jarvis.cluster import _HUBS, execute_local
from jarvis.storage import Storage
from jarvis.ui import Console, Handler

ROOT = Path(__file__).resolve().parents[2]
FILES_JS = ROOT / "jarvis" / "web" / "files.js"

# The click handler is a browser IIFE (window.Jarvis, document). This only
# evaluates the two ``devices.find`` lines that pick the copy-arrow target.
_ARROW_JS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const start = src.indexOf("const here = devices.find");
const end = src.indexOf("if (!other)", start);
if (start < 0 || end < 0) process.exit(2);
let snippet = src.slice(start, end);
if (!snippet.includes("panes[x.id]?.listing")) process.exit(1);
snippet = snippet.replace("const other", "other");
function pick(device, devices, panes) {
  let other;
  eval(snippet);
  return other ? other.id : "";
}
const devices = [
  {id: "oracle", remote: false},
  {id: "zenbook", remote: false},
  {id: "dell", remote: true},
];
const loaded = {
  zenbook: {listing: {path: "/z"}},
  dell: {listing: {path: "/d"}},
  oracle: {},
};
if (pick("dell", devices, loaded) !== "zenbook") process.exit(3);
if (pick("zenbook", devices, loaded) !== "dell") process.exit(4);
const bare = {zenbook: {listing: {path: "/z"}}, dell: {}, oracle: {}};
if (pick("zenbook", devices, bare) !== "") process.exit(5);
"""


def _png_bytes():
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def browser_headers(port, *, post=False, image=False):
    if image:
        # Chrome <img>: no X-JARVIS, no Origin, mode no-cors, dest image.
        return {
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "image",
            "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
        }
    headers = {
        "Origin": f"http://127.0.0.1:{port}",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if post:
        headers["X-JARVIS"] = "1"
        headers["Content-Type"] = "application/json"
    return headers


class StubHub:
    """Enrollment check the live hub does, then the node's real executor."""

    def __init__(self, node_config, state):
        self.node_config = node_config
        self.state = state
        self.caps = {"dell": frozenset({"files", "desktop", "inventory"}),
                     "oracle": frozenset({"inventory"})}
        self.calls = []

    def dispatch(self, node, kind, payload, timeout=45):
        action = payload.get("action") if isinstance(payload, dict) else None
        self.calls.append((node, kind, action))
        capability = "files" if kind in ("storage", "transfer") else kind
        if capability not in self.caps.get(node, frozenset()):
            raise ValueError(f"Node {node} is not enrolled for {kind} actions.")
        return execute_local(kind, payload, self.node_config, self.state)


class FilesTabHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "state"
        self.state.mkdir()
        self.local = self.base / "zenbook"
        self.remote = self.base / "dell"
        self.local.mkdir()
        self.remote.mkdir()
        (self.local / "hello.txt").write_bytes(b"from-zenbook")
        (self.local / "pic.png").write_bytes(_png_bytes())
        (self.remote / "from-dell.txt").write_bytes(b"from-dell")
        (self.remote / "pic.png").write_bytes(_png_bytes())
        config = {
            "devices": {
                "dell": {"transport": "cluster", "hostname": "WinLaptop", "label": "Dell"},
                "zenbook": {"transport": "ssh", "hostname": "archmain", "label": "Zenbook",
                            "target": "not-a-target"},
                "oracle": {"hostname": "oracle-vm", "label": "Oracle", "os": "linux",
                           "profile": "cloud"},
            },
            "cluster": {"enabled": True, "main_device": "zenbook", "local_device": "zenbook"},
            "storage": {"zenbook": {"roots": [str(self.local)]},
                        "dell": {"roots": [str(self.remote)]}},
            "memory": {"semantic": False},
            "decisions": {"shadow": False},
            "routing": {"interpreter": False},
        }
        node_config = {
            "devices": {
                "dell": {"transport": "local", "hostname": "WinLaptop"},
                "zenbook": {"transport": "ssh", "hostname": "archmain"},
            },
            "cluster": {"enabled": True, "main_device": "zenbook", "local_device": "dell"},
            "storage": {"dell": {"roots": [str(self.remote)]}},
            "memory": {"semantic": False},
            "decisions": {"shadow": False},
            "routing": {"interpreter": False},
        }
        self.hub = StubHub(node_config, self.state)
        _HUBS[str(self.state)] = self.hub
        self.addCleanup(_HUBS.pop, str(self.state), None)
        console = Console(config, self.base, self.state)
        handler = type("H", (Handler,), {
            "console": console,
            "hosts": ("127.0.0.1", "localhost", "::1"),
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, method, path, *, post=False, image=False, headers=None, body=None):
        hdrs = browser_headers(self.port, post=post, image=image) if headers is None else headers
        conn = HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            data = None
            if body is not None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
            conn.request(method, path, body=data, headers=hdrs)
            res = conn.getresponse()
            raw = res.read()
            info = {key.lower(): value for key, value in res.getheaders()}
            return res.status, info, raw
        finally:
            conn.close()

    def fs_url(self, device, path=""):
        return "/api/fs?" + urlencode({"device": device, "path": path})

    def listing(self, device):
        status, _, raw = self.call("GET", self.fs_url(device))
        self.assertEqual(status, 200, raw)
        body = json.loads(raw)
        self.assertIsInstance(body.get("entries"), list)
        return body

    @staticmethod
    def entry_path(listing, name):
        return listing["path"].rstrip("/\\") + listing["sep"] + name

    def test_devices_mark_this_pc_local_and_omit_the_gateway(self):
        status, _, raw = self.call("GET", "/api/fs/devices")
        self.assertEqual(status, 200, raw)
        devices = json.loads(raw)
        by_id = {item["id"]: item for item in devices}
        self.assertEqual(set(by_id), {"zenbook", "dell"})
        self.assertIs(by_id["zenbook"]["remote"], False)
        self.assertIs(by_id["dell"]["remote"], True)
        self.assertNotIn("oracle", by_id)

    def test_both_laptops_list_and_thumbnail(self):
        pictures = {}
        for device, name in (("zenbook", "hello.txt"), ("dell", "from-dell.txt")):
            with self.subTest(device=device, listing=True):
                body = self.listing(device)
                names = [entry["name"] for entry in body["entries"]]
                self.assertIn(name, names)
                self.assertIn("pic.png", names)
                pictures[device] = self.entry_path(body, "pic.png")
        for device, path in pictures.items():
            with self.subTest(device=device, thumbnail=True):
                url = "/api/fs/thumbnail?" + urlencode({"device": device, "path": path, "v": "1"})
                status, info, raw = self.call("GET", url, image=True)
                self.assertEqual(status, 200, raw[:80])
                self.assertTrue(info.get("content-type", "").startswith("image/png"), info)
                self.assertTrue(raw.startswith(b"\x89PNG"), raw[:16])

    def _copy(self, src_device, src_path, dst_device, dst_dir):
        status, _, raw = self.call("POST", "/api/fs/copy", post=True, body={
            "src_device": src_device, "src_path": src_path,
            "dst_device": dst_device, "dst_dir": dst_dir,
        })
        self.assertEqual(status, 200, raw)
        return json.loads(raw)

    def _assert_second_copy(self, src_device, src_path, dst_device, dst_dir, folder, source):
        first = self._copy(src_device, src_path, dst_device, dst_dir)
        landed = folder / first["as"]
        self.assertEqual(landed.read_bytes(), source)
        self.assertEqual(Path(src_path).read_bytes(), source)
        second = self._copy(src_device, src_path, dst_device, dst_dir)
        self.assertNotEqual(second["as"], first["as"])
        self.assertEqual((folder / second["as"]).read_bytes(), source)
        self.assertEqual(landed.read_bytes(), source)
        self.assertEqual(Path(src_path).read_bytes(), source)
        self.assertTrue(Path(src_path).is_file())
        self.assertEqual(list(folder.glob(".jarvis-upload-*")), [])

    def test_copy_both_ways_never_overwrites(self):
        zen = self.listing("zenbook")
        dell = self.listing("dell")
        hello = self.entry_path(zen, "hello.txt")
        outbound = (self.local / "hello.txt").read_bytes()
        self._assert_second_copy("zenbook", hello, "dell", dell["path"], self.remote, outbound)
        inbound_path = self.entry_path(dell, "from-dell.txt")
        inbound = (self.remote / "from-dell.txt").read_bytes()
        self._assert_second_copy("dell", inbound_path, "zenbook", zen["path"], self.local, inbound)
        self.assertEqual(list(self.local.glob(".jarvis-upload-*")), [])

    def test_browser_guards_and_unenrolled_target(self):
        evil = browser_headers(self.port)
        evil["Origin"] = "http://evil.example"
        before = len(self.hub.calls)
        status, _, raw = self.call("GET", self.fs_url("dell"), headers=evil)
        self.assertEqual(status, 403, raw)
        self.assertEqual(len(self.hub.calls), before)

        bare = browser_headers(self.port)
        bare["Content-Type"] = "application/json"
        status, _, raw = self.call("POST", "/api/fs/copy", headers=bare, body={
            "src_device": "zenbook", "src_path": str(self.local / "hello.txt"),
            "dst_device": "dell", "dst_dir": str(self.remote),
        })
        self.assertEqual(status, 403, raw)
        self.assertFalse((self.remote / "hello.txt").exists())

        plain = browser_headers(self.port, post=True)
        plain["Content-Type"] = "text/plain"
        status, _, raw = self.call("POST", "/api/fs/copy", headers=plain, body={
            "src_device": "zenbook", "src_path": str(self.local / "hello.txt"),
            "dst_device": "dell", "dst_dir": str(self.remote),
        })
        self.assertEqual(status, 403, raw)
        self.assertFalse((self.remote / "hello.txt").exists())

        dell = self.listing("dell")
        source = self.entry_path(dell, "from-dell.txt")
        original = Path(source).read_bytes()
        status, _, raw = self.call("POST", "/api/fs/copy", post=True, body={
            "src_device": "dell", "src_path": source,
            "dst_device": "oracle", "dst_dir": str(self.remote),
        })
        self.assertEqual(status, 400, raw)
        body = json.loads(raw)
        self.assertTrue(body.get("error"))
        self.assertEqual(Path(source).read_bytes(), original)


class PlainPairTests(unittest.TestCase):
    def test_non_cluster_pair_is_one_local_and_one_remote(self):
        config = {
            "devices": {
                "pc": {"transport": "local", "hostname": "this-pc"},
                "laptop": {"transport": "ssh", "hostname": "other-pc", "target": "user@10.0.0.2"},
            },
            "cluster": {"enabled": False},
        }
        rows = {item["id"]: item["remote"] for item in Storage(config).devices()}
        self.assertEqual(rows, {"pc": False, "laptop": True})


class CopyArrowTests(unittest.TestCase):
    def test_copy_arrow_requires_a_loaded_listing(self):
        source = FILES_JS.read_text(encoding="utf-8")
        self.assertIn("panes[x.id]?.listing", source)
        self.assertNotIn("devices.find((x) => x.id !== device);", source)
        node = shutil.which("node")
        if not node:
            return
        syntax = subprocess.run([node, "--check", str(FILES_JS)],
                                check=False, capture_output=True, text=True, timeout=20)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        # The two find lines are plain JS. The rest of the file needs a DOM.
        done = subprocess.run([node, "-e", _ARROW_JS, str(FILES_JS)],
                              check=False, capture_output=True, text=True, timeout=20)
        if done.returncode == 2:
            return
        self.assertEqual(done.returncode, 0, done.stderr or done.stdout)


if __name__ == "__main__":
    unittest.main()
