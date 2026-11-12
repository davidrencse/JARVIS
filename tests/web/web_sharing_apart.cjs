// Apart / together / unknown for the shared mouse and keyboard panels.
// Fake DOM only: no browser, no network, no Lan Mouse process.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.join(__dirname, "../../jarvis/web");
const mode = process.argv[2];

function device(id, here, running) {
  return {
    id, label: id, here, role: here ? "main" : "node", online: true,
    lan_mouse: { running, build: "jarvis", peers: [] },
    clipboard: { running: false, linked: false, autostart: false },
  };
}

function sharingFor(which) {
  if (which === "apart") return { state: "apart", enforced: true, remote: true, override: false, why: "Dell is on another network." };
  if (which === "apart-hand") return { state: "apart", enforced: true, remote: true, override: true, why: "Dell is on another network." };
  if (which === "unknown") return { state: "unknown", enforced: true, remote: true, why: "The guard hasn't read the network lately." };
  if (which === "switch-off") return { state: "apart", enforced: false, remote: true, why: "memo still says apart" };
  return { state: "together", enforced: true, remote: true, available: true, why: "Dell is on this network." };
}

async function page(which) {
  const nodes = {};
  function element(id) {
    return {
      id, hidden: false, innerHTML: "", textContent: "", disabled: false, value: "", handlers: {},
      addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
      querySelector() { return null; },
      closest() { return null; },
    };
  }
  const doc = {
    visibilityState: "visible",
    addEventListener() {},
    querySelector(sel) {
      const id = String(sel).replace(/^#/, "");
      return nodes[id] || (nodes[id] = element(id));
    },
  };
  const sharing = sharingFor(which);
  const peri = {
    ok: true, role: "main", main: "zenbook", here: "zenbook", checks: [],
    sharing, devices: [device("zenbook", true, which === "apart-hand"), device("dell", false, false)],
  };
  const shared = {
    here: "zenbook", checks: [], layout: {}, sharing,
    devices: [
      { id: "zenbook", label: "Zenbook", name: "Zenbook", here: true, info: null },
      { id: "dell", label: "Dell", name: "Dell", here: false, info: null, error: "offline" },
    ],
  };
  const context = {
    document: doc, console, setTimeout() { return 1; }, clearTimeout() {},
    addEventListener() {},
    JSON, Math, Object, Array, Promise, Error, Date,
    Jarvis: {
      api: async (p) => (String(p).includes("peripherals") ? peri : shared),
      esc: (s) => String(s ?? ""),
      icon: () => "",
      toast() {},
      ago: () => "just now",
    },
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync(path.join(root, "peripherals.js"), "utf8"), context);
  vm.runInNewContext(fs.readFileSync(path.join(root, "sharedinput.js"), "utf8"), context);
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  return {
    peri: nodes.peripherals.innerHTML,
    layout: (nodes["si-layout"] || {}).innerHTML || "",
    checks: (nodes["si-checks"] || {}).innerHTML || "",
  };
}

(async () => {
  const view = await page(mode);
  const html = view.peri + view.layout + view.checks;
  if (mode === "apart") {
    assert.match(view.peri, /off while apart/);
    assert.match(view.peri, /data-action="start"[^>]*disabled/);
    assert.match(view.peri, /Remote control still works/);
    assert.doesNotMatch(view.peri, /data-pair/);
    assert.match(view.peri, /data-do="clipboard"/);
    assert.match(view.layout, /shared screens are hidden/);
    assert.match(view.layout, /Remote control still works/);
  } else if (mode === "apart-hand") {
    assert.match(view.peri, /on by hand/);
    assert.match(view.peri, /will not be turned back on/);
    assert.match(view.peri, /data-action="stop"/);
    assert.doesNotMatch(view.peri, /data-action="stop"[^>]*disabled/);
  } else if (mode === "unknown") {
    assert.match(html, /Can't tell/);
    assert.doesNotMatch(view.peri, /off while apart/);
    assert.match(view.peri, /data-action="start"/);
    assert.doesNotMatch(view.peri, /data-action="start"[^>]*disabled/);
    assert.match(view.peri, /data-pair/);
    assert.doesNotMatch(view.layout, /shared screens are hidden/);
  } else if (mode === "together") {
    assert.doesNotMatch(html, /off while apart/);
    assert.doesNotMatch(html, /Can't tell/);
    assert.match(view.peri, /data-pair/);
    assert.doesNotMatch(view.layout, /shared screens are hidden/);
  } else if (mode === "switch-off") {
    assert.doesNotMatch(view.peri, /off while apart/);
    assert.match(view.peri, /data-action="start"/);
    assert.doesNotMatch(view.peri, /data-action="start"[^>]*disabled/);
    assert.match(view.peri, /data-pair/);
  } else {
    throw new Error("unknown mode " + mode);
  }
  const remoteJs = fs.readFileSync(path.join(root, "remote.js"), "utf8");
  assert.match(remoteJs, /\/api\/remote\/start/);
  console.log("PASS " + mode);
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
