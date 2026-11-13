"""Run microphone lifecycle tests with mocked browser APIs; no microphone is opened."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path


NODE = shutil.which("node")
SCRIPT = Path(__file__).with_name("web_voice_test.cjs")


@unittest.skipUnless(NODE, "Node.js is required for the browser microphone unit tests")
class BrowserMicrophoneTests(unittest.TestCase):
    def check_scenario(self, name):
        result = subprocess.run([NODE, str(SCRIPT), name], capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS " + name, result.stdout)

    def test_elevenlabs_handshake_that_never_answers_gives_up(self):
        result = subprocess.run([NODE, str(Path(__file__).with_name("web_agent_test.cjs")),
                                 "handshake_that_never_answers_gives_up"], capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_startup_failures_release_resources_and_allow_retry(self):
        self.check_scenario("failures_and_retry")

    def test_concurrent_starts_share_one_microphone_request(self):
        self.check_scenario("concurrent_start")

    def test_stop_during_permission_request_releases_late_stream(self):
        self.check_scenario("cancel_permission")

    def test_stop_during_worklet_load_releases_resources(self):
        self.check_scenario("cancel_worklet")

    def test_old_startup_cannot_stop_or_replace_a_new_recording(self):
        self.check_scenario("stale_start_cannot_affect_retry")

    def test_successful_recording_keeps_audio_and_releases_resources(self):
        self.check_scenario("audio_and_cleanup")

    def test_noisy_room_speech_reaches_transcription_and_jarvis(self):
        self.check_scenario("noisy_room_mic_to_transcription")

    def test_always_on_mic_rejects_noise_and_recovers_for_speech(self):
        self.check_scenario("noisy_room_wait_does_not_submit_ambience")

    def test_silence_and_context_close_error_do_not_leak_microphone(self):
        self.check_scenario("no_speech_and_close_error")

    def test_malformed_audio_is_cleaned_before_transcription(self):
        self.check_scenario("malformed_audio_is_sanitized")

    def test_recording_memory_is_bounded(self):
        self.check_scenario("recording_length_is_capped")

    def test_throwing_ui_callback_cannot_skip_microphone_cleanup(self):
        self.check_scenario("callback_error_still_releases")

    def test_old_animation_loop_cannot_continue_after_restart(self):
        self.check_scenario("old_animation_loop_stops")

    def test_ui_can_cancel_startup_and_preserves_quick_space_tap(self):
        self.check_scenario("ui_cancels_startup_and_preserves_quick_tap")

    def test_ignored_voice_updates_count_without_overview_fetch(self):
        # Boots app.js in a stub DOM, delivers voice.ignored on the live stream, and
        # checks the listener log moves without a second /api/overview.
        app = Path(__file__).resolve().parents[2] / "jarvis" / "web" / "app.js"
        env = os.environ.copy()
        env["APP_JS"] = str(app)
        result = subprocess.run([NODE], input=IGNORED_VOICE_JS, capture_output=True,
                                text=True, encoding="utf-8", timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS ignored_voice_does_not_refetch_overview", result.stdout)


# Runs jarvis/web/app.js. voice.ignored must bump overview.voice.ignored and redraw
# #listener-log, and must not schedule /api/overview (soonOverview is 500 ms).
IGNORED_VOICE_JS = r'''
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const calls = [];
const sources = [];
const els = new Map();
const windowHandlers = {};
const liveEvents = [];

function makeEl(sel) {
  const el = {
    sel, attrs: {}, handlers: {}, dataset: {}, style: {}, children: [],
    hidden: false, value: "", checked: false, disabled: false,
    textContent: "", innerHTML: "", className: "", title: "", placeholder: "",
    href: "", id: "", tabIndex: 0, scrollHeight: 0, scrollTop: 0, clientHeight: 0, open: false,
  };
  el.classList = { add() {}, remove() {}, contains() { return false; }, toggle() {} };
  el.setAttribute = (k, v) => { el.attrs[k] = String(v); };
  el.getAttribute = (k) => (k in el.attrs ? el.attrs[k] : null);
  el.removeAttribute = (k) => { delete el.attrs[k]; };
  el.hasAttribute = (k) => k in el.attrs;
  el.addEventListener = (type, fn) => { (el.handlers[type] ||= []).push(fn); };
  el.removeEventListener = () => {};
  el.querySelector = (s) => getEl(s);
  el.querySelectorAll = () => [];
  el.focus = () => {};
  el.blur = () => {};
  el.click = () => { for (const fn of el.handlers.click || []) fn({ currentTarget: el, target: el, preventDefault() {} }); };
  el.remove = () => {};
  el.prepend = () => {};
  el.append = () => {};
  el.closest = () => null;
  el.contains = () => false;
  el.matches = () => false;
  el.reset = () => {};
  el.requestSubmit = () => {};
  el.getContext = () => ({});
  return el;
}
function getEl(sel) {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
}

const overview = {
  memory: { items: 3, links: 1, kinds: { note: 1 } },
  voice: {
    voiceprint: true, listening: true,
    recent: ["2026-11-13 10:01:02  alpha-heard-marker"],
    ignored: { count: 2, last: "2026-11-13 09:15" },
  },
  routing: { auto_escalate: false, brain: "claude", simple_provider: "claude", simple_power: "low" },
  brain: { name: "Claude", warm: true },
  models: { claude: {} },
  desktop: {},
  devices: [],
};

function bodyFor(url) {
  const pathOnly = String(url).split("?")[0];
  if (pathOnly === "/api/overview") return overview;
  if (pathOnly === "/api/cases") return [];
  if (pathOnly === "/api/credits") return [];
  if (pathOnly === "/api/history") return [];
  if (pathOnly === "/api/graph/heap") return { nodes: [], edges: [] };
  if (pathOnly === "/api/agent") return { configured: false };
  if (pathOnly === "/api/probe") return { checked_at: 0, input_link: { level: "up", lines: ["ok"] }, devices: {} };
  return null;
}

const document = {
  hidden: false, activeElement: null, title: "",
  documentElement: makeEl("html"), body: makeEl("body"),
  querySelector: getEl,
  querySelectorAll: () => [],
  getElementById: (id) => getEl("#" + id),
  createElement: () => makeEl("element"),
  createTextNode: (t) => ({ textContent: String(t) }),
  addEventListener(type, fn) { (windowHandlers[type] ||= []).push(fn); },
  removeEventListener() {},
};

class EventSource {
  constructor(url) { this.url = url; this.handlers = {}; sources.push(this); }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  close() {}
}
class Mic {
  constructor() { this.active = false; this.wait = false; }
  get starting() { return false; }
  async start() { this.active = true; this.wait = true; }
  async stop() { this.active = false; return null; }
}
class ElevenAgent {
  constructor() { this.state = "idle"; }
  get live() { return false; }
  setAudioSettings() {}
  async stop() {}
}
class ForceGraph {
  constructor() {}
  setData() {}
  draw() {}
  select() {}
}

const sandbox = {
  console,
  URL: globalThis.URL,
  URLSearchParams: globalThis.URLSearchParams,
  Intl: globalThis.Intl,
  TextEncoder: globalThis.TextEncoder,
  TextDecoder: globalThis.TextDecoder,
  FormData: globalThis.FormData,
  Headers: globalThis.Headers,
  AbortController: globalThis.AbortController,
  queueMicrotask: globalThis.queueMicrotask,
  structuredClone: globalThis.structuredClone,
  performance: globalThis.performance,
  crypto: globalThis.crypto,
  atob: globalThis.atob,
  btoa: globalThis.btoa,
  CSS: globalThis.CSS,
  setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
  document,
  location: { search: "", hash: "", pathname: "/", href: "http://jarvis.local/", replace() {} },
  navigator: { mediaDevices: { enumerateDevices: async () => [], addEventListener() {} } },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  EventSource, Mic, ElevenAgent, ForceGraph,
  CustomEvent: globalThis.CustomEvent,
  JarvisEsc: (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
  JarvisTheme: { pref: "system", apply() {} },
  fetch: async (url) => {
    calls.push(String(url));
    const data = bodyFor(url);
    if (data === null) return { ok: false, status: 404, json: async () => ({ error: "missing" }) };
    return { ok: true, status: 200, json: async () => data };
  },
  addEventListener(type, fn) { (windowHandlers[type] ||= []).push(fn); },
  removeEventListener() {},
  dispatchEvent(ev) {
    if (ev.type === "jarvis:live") liveEvents.push(ev.detail);
    for (const fn of windowHandlers[ev.type] || []) fn(ev);
    return true;
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

const src = fs.readFileSync(process.env.APP_JS, "utf8");
try {
  vm.runInNewContext(src, sandbox, { filename: "app.js" });
} catch (err) {
  console.error("BOOT FAIL", err && err.stack || err);
  process.exit(1);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  let html = "";
  for (let i = 0; i < 100; i++) {
    html = els.get("#listener-log")?.innerHTML || "";
    if (html.includes("2 other voices")) break;
    await sleep(20);
  }
  if (!html.includes("2 other voices")) {
    console.error("overview never painted", html, "calls", calls, "jarvis", !!sandbox.Jarvis);
    process.exit(1);
  }
  await sleep(80);
  const before = calls.filter((u) => u.split("?")[0] === "/api/overview").length;
  const es = sources[0];
  assert.ok(es, "EventSource was opened");
  const fire = () => {
    const ev = { data: JSON.stringify({ kind: "voice.ignored", seq: 4, at: Date.now() / 1000, data: { score: 0.12 } }) };
    for (const fn of es.handlers.message || []) fn(ev);
  };
  fire();
  const ign = sandbox.Jarvis.state.overview.voice.ignored;
  assert.equal(ign.count, 3);
  assert.match(ign.last, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  html = els.get("#listener-log").innerHTML;
  assert.ok(html.includes("3 other voices ignored"), html);
  assert.ok(html.includes("last " + ign.last.slice(11, 16)), html);
  assert.ok(html.includes("alpha-heard-marker"), html);
  assert.equal(liveEvents.filter((e) => e.kind === "voice.ignored").length, 1);

  sandbox.Jarvis.state.overview.voice.ignored = null;
  fire();
  assert.equal(sandbox.Jarvis.state.overview.voice.ignored.count, 1);
  assert.ok(els.get("#listener-log").innerHTML.includes("1 other voice ignored"));

  const saved = sandbox.Jarvis.state.overview;
  sandbox.Jarvis.state.overview = null;
  fire();
  sandbox.Jarvis.state.overview = saved;

  await sleep(700);
  const after = calls.filter((u) => u.split("?")[0] === "/api/overview").length;
  assert.equal(after, before, "voice.ignored must not refetch /api/overview: " + calls.join(","));
  console.log("PASS ignored_voice_does_not_refetch_overview");
  process.exit(0);
})().catch((err) => {
  console.error(err && err.stack || err);
  process.exit(1);
});
'''


if __name__ == "__main__":
    unittest.main()
