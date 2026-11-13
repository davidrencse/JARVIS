"""Files tab behavior that has to stay visible: copy failures, double-submit, stale loads.

Runs ``files.js`` under Node with the DOM calls the test reaches stubbed out.
No browser, no hub, no SSH.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES_JS = ROOT / "jarvis" / "web" / "files.js"

# Draws are stubbed. ``api`` and ``toast`` are the closures files.js captured at
# load, so the script swaps the boxes those closures call.
_BEHAVIOR_JS = r"""
const fs = require("fs");
const vm = require("vm");
const srcPath = process.argv[1];
let src = fs.readFileSync(srcPath, "utf8");
const drawAt = src.indexOf("function draw(device) {");
const exportAt = src.indexOf("window.JarvisFiles = { open };");
if (drawAt < 0 || exportAt < 0) {
  console.error("files.js shape changed");
  process.exit(2);
}
src = src.slice(0, drawAt + "function draw(device) {".length)
  + "\n    window.__draws.push(device); return;"
  + src.slice(drawAt + "function draw(device) {".length);
src = src.replace("window.JarvisFiles = { open };",
  "window.JarvisFiles = { open, copy, load, panes };");

const status = { textContent: "", className: "" };
const toastBox = [];
const apiBox = { fn: async () => { throw new Error("api unset"); } };
const windowObj = {
  __draws: [],
  Jarvis: {
    esc: (s) => String(s == null ? "" : s),
    icon: () => "",
    api: (path, body) => apiBox.fn(path, body),
    toast: (message) => { toastBox.push(message); },
  },
};
const documentObj = {
  querySelector(sel) { return sel === "#files-status" ? status : null; },
  querySelectorAll() { return []; },
};
windowObj.window = windowObj;
windowObj.document = documentObj;
vm.runInNewContext(src, windowObj, { filename: srcPath });
const { copy, load, panes } = windowObj.JarvisFiles;
panes.dell = { listing: { path: "/d", entries: [] }, busy: false, error: null, query: "" };
panes.zenbook = { listing: { path: "/z", entries: [] }, busy: false, error: null, query: "" };

function fail(code, detail) {
  console.error(code, detail);
  process.exit(code);
}

(async () => {
  // A failed copy must say why, and the folder on screen must be read again.
  let lists = 0;
  apiBox.fn = async (path) => {
    if (path === "/api/fs/copy") throw new Error("disk full");
    lists += 1;
    return { path: "/d", entries: [], sep: "/" };
  };
  await copy(
    { device: "zenbook", path: "/a.txt", name: "a.txt" },
    { device: "dell", dir: "/d" });
  if (!status.textContent.includes("disk full") || !status.className.includes("bad")) {
    fail(5, status.textContent + " / " + status.className);
  }
  if (!toastBox.includes("disk full")) fail(6, JSON.stringify(toastBox));
  if (lists < 1) fail(7, "listing was not refreshed after the failed copy");

  // An error with no message must still show a failure, not a blank pane.
  apiBox.fn = async () => { throw new Error(""); };
  await load("dell", "/x");
  if (!panes.dell.error || panes.dell.error === "undefined") fail(4, String(panes.dell.error));

  // An older listing must not replace the folder the user opened after it.
  let pending = 0;
  let releaseOld;
  let releaseNew;
  apiBox.fn = () => {
    pending += 1;
    if (pending === 1) return new Promise((resolve) => { releaseOld = resolve; });
    return new Promise((resolve) => { releaseNew = resolve; });
  };
  const older = load("dell", "/old");
  const newer = load("dell", "/new");
  releaseNew({ path: "/new", entries: [], sep: "/" });
  await newer;
  releaseOld({ path: "/old", entries: [], sep: "/" });
  await older;
  if (!panes.dell.listing || panes.dell.listing.path !== "/new") {
    fail(3, panes.dell.listing && panes.dell.listing.path);
  }

  // A second click while the first copy is still running must not send again.
  let copies = 0;
  apiBox.fn = (path) => {
    if (path === "/api/fs/copy") {
      copies += 1;
      return new Promise(() => {});
    }
    return Promise.resolve({ path: "/d", entries: [], sep: "/" });
  };
  panes.dell.listing = { path: "/d", entries: [] };
  const first = copy(
    { device: "zenbook", path: "/a.txt", name: "a.txt" },
    { device: "dell", dir: "/d" });
  await copy(
    { device: "zenbook", path: "/a.txt", name: "a.txt" },
    { device: "dell", dir: "/d" });
  if (copies !== 1) fail(9, String(copies));
  if (!/already/i.test(status.textContent) || !status.className.includes("bad")) {
    fail(10, status.textContent);
  }
  void first;
})().catch((err) => fail(1, err && err.stack || err));
"""


class FilesJsBehaviorTests(unittest.TestCase):
    def test_copy_failure_double_submit_and_stale_load(self):
        node = shutil.which("node")
        if not node:
            self.fail("node is required to run files.js")
        syntax = subprocess.run([node, "--check", str(FILES_JS)],
                                check=False, capture_output=True, text=True, timeout=20)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        done = subprocess.run([node, "-e", _BEHAVIOR_JS, str(FILES_JS)],
                              check=False, capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0, done.stderr or done.stdout)

    def test_a_busy_pane_and_a_bad_drop_say_why(self):
        source = FILES_JS.read_text(encoding="utf-8")
        self.assertNotIn("if (panes[device].busy) return;", source)
        self.assertIn("still loading", source)
        self.assertIn("That drop couldn't be read", source)


if __name__ == "__main__":
    unittest.main()
