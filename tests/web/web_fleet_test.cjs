// Behavioural tests for the fleet section of the Connections view (jarvis/web/fleet.js) and the structured
// errors from app.js's api(). Everything is fake: no network, clipboard, timers or real node is touched.
// Run: node tests/web/web_fleet_test.cjs [scenario]
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const WEB = path.join(__dirname, "../../jarvis/web");
const SOURCE = fs.readFileSync(path.join(WEB, "fleet.js"), "utf8");
const APP = fs.readFileSync(path.join(WEB, "app.js"), "utf8");
const FIX = JSON.parse(fs.readFileSync(path.join(__dirname, "fleet_fixtures.json"), "utf8"));
const flush = async () => { for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r)); };
const clone = (o) => JSON.parse(JSON.stringify(o));
const CODE = "jarvis-invite-1:SECRETSECRET0123456789abcdef.ghij=";
const HOSTILE = '<img src=x onerror="alert(1)">';

function mount(opt = {}) {
  const state = { calls: [], clip: [], store: {}, innerHTML: [], logs: [], timers: [], intervals: [], events: [], routes: {} };
  function element(tag) {
    const e = {
      tagName: String(tag).toUpperCase(), attrs: {}, handlers: {}, children: [], parent: null, _text: "", value: "", checked: false, disabled: false,
      get parentElement() { return this.parent && this.parent.tagName ? this.parent : null; },
      get id() { return this.attrs.id || ""; }, set id(v) { this.attrs.id = String(v); },
      get hidden() { return "hidden" in this.attrs; }, set hidden(v) { if (v) this.attrs.hidden = ""; else delete this.attrs.hidden; },
      get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); },
      set textContent(v) { for (const c of this.children) c.parent = null; this.children = []; this._text = String(v); },
      get innerHTML() { return ""; }, set innerHTML(v) { state.innerHTML.push(String(v)); },
      setAttribute(k, v) { this.attrs[k] = String(v); if (k === "disabled") this.disabled = true; }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
      hasAttribute(k) { return k in this.attrs; }, removeAttribute(k) { delete this.attrs[k]; },
      addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
      append(...kids) { for (const k of kids) { const n = typeof k === "string" ? { nodeType: 3, textContent: k, parent: null } : k; n.parent = this; this.children.push(n); } },
      replaceChildren(...kids) { for (const c of this.children) c.parent = null; this.children = []; this._text = ""; this.append(...kids); },
      remove() { if (this.parent) { this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; } },
      contains(n) { for (let x = n; x; x = x.parent) if (x === this) return true; return false; },
      focus() { doc.activeElement = this; }, scrollIntoView() {},
      querySelector(sel) { return walk(this, (n) => n.tagName === sel.toUpperCase())[0] || null; },
    };
    return e;
  }
  function walk(n, pred, out = []) { if (n.tagName && pred(n)) out.push(n); for (const c of n.children || []) walk(c, pred, out); return out; }
  const doc = {
    hidden: false, activeElement: null, els: {}, handlers: {},
    getElementById(id) { return id in this.els ? this.els[id] : null; },
    createElement: (t) => element(t), createTextNode: (t) => ({ nodeType: 3, textContent: String(t), parent: null }),
    addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
    createRange: () => ({ selectNodeContents() {} }),
  };
  const view = element("section"); view.attrs.id = "view-devices";
  if (opt.hidden) view.hidden = true;
  const root = element("section"); root.attrs.id = "fleet-root";
  if (!opt.noRoot) { view.append(root); doc.els["fleet-root"] = root; }
  let observer = null;
  const win = {
    handlers: {}, addEventListener(k, f) { (this.handlers[k] ||= []).push(f); },
    getSelection: () => ({ removeAllRanges() {}, addRange() {} }),
  };
  const routes = state.routes;
  const api = async (p, body) => {
    state.calls.push({ method: body === undefined ? "GET" : "POST", path: p, body: body === undefined ? undefined : clone(body) });
    await null;
    const r = routes[(body === undefined ? "GET " : "POST ") + p];
    if (!r) { const e = new Error("no route " + p); e.status = 404; throw e; }
    return r(body);
  };
  routes["GET /api/fleet"] = () => clone(FIX.owner);
  routes["GET /api/fleet/vault"] = () => clone(opt.vault || FIX.vault_active);
  routes["POST /api/fleet/preview"] = () => clone(FIX.preview);
  routes["POST /api/fleet/main-transfer/preview"] = () => clone(FIX.main_preview);
  const context = {
    document: doc, window: win, console: { log: (...a) => state.logs.push(a.join(" ")), error: (...a) => state.logs.push(a.join(" ")), warn: (...a) => state.logs.push(a.join(" ")) },
    navigator: { clipboard: { writeText: async (t) => { if (opt.clipFail) throw new Error("denied"); state.clip.push(t); } } },
    localStorage: { setItem: (k, v) => { state.store[k] = v; }, getItem: () => null }, sessionStorage: { setItem: (k, v) => { state.store["s:" + k] = v; }, getItem: () => null },
    crypto: { getRandomValues: (a) => { for (let i = 0; i < a.length; i++) a[i] = Math.floor(Math.random() * 256); return a; } },
    MutationObserver: class { constructor(f) { observer = f; } observe() {} },
    setTimeout: (f, ms) => { state.timers.push([f, ms]); return state.timers.length; }, clearTimeout() {},
    setInterval: (f, ms) => { state.intervals.push([f, ms]); return state.intervals.length; }, clearInterval(id) { state.intervals[id - 1] = null; },
    Date, Math, JSON, Promise, Uint8Array, Intl, parseInt, isFinite, String, Number, Array, Object, RegExp, Error,
  };
  win.Jarvis = { api };
  win.JarvisEsc = (s) => s;
  vm.runInNewContext(SOURCE, context, { filename: "fleet.js" });
  const t = {
    state, root, doc, view, win,
    all: (pred, from = root) => walk(from, pred),
    one: (pred, from = root) => walk(from, pred)[0] || null,
    cls: (c, from = root) => walk(from, (n) => (n.attrs.class || "").split(" ").includes(c)),
    text: (from = root) => from.textContent,
    byText: (tag, re, from = root) => walk(from, (n) => n.tagName === tag.toUpperCase() && re.test(n.textContent))[0] || null,
    control(labelRe) { const l = walk(root, (n) => n.tagName === "LABEL" && labelRe.test(n.textContent))[0]; assert(l, "label " + labelRe); return walk(root, (n) => n.attrs.id === l.attrs.for)[0]; },
    fire(el, kind, init = {}) { assert(el, "no element for " + kind); const ev = Object.assign({ defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }, init); for (const f of el.handlers[kind] || []) f(ev); return ev; },
    click(el) {
      assert(el && !el.disabled, "click on missing/disabled " + (el && el.textContent));
      const ev = t.fire(el, "click");
      if (el.attrs.type === "submit") { let f = el.parent; while (f && f.tagName !== "FORM") f = f.parent; if (f) t.fire(f, "submit"); }
      return ev;
    },
    type(el, v) { el.value = v; t.fire(el, "input"); },
    tick: flush,
    calls: (m, p) => state.calls.filter((c) => c.method === m && c.path === p),
    posts: () => state.calls.filter((c) => c.method === "POST"),
    setHidden(on) { view.hidden = on; observer && observer(); },
    kind(id) { for (const r of t.all((n) => n.tagName === "INPUT" && n.attrs.type === "radio")) r.checked = r.attrs.value === id; t.fire(t.one((n) => n.tagName === "INPUT" && n.attrs.type === "radio" && n.attrs.value === id), "change"); },
    capBox(title) { const l = walk(t.one((n) => n.attrs.id === "fl-connect"), (n) => n.tagName === "LABEL" && n.textContent.startsWith(title))[0]; assert(l, "cap " + title); return walk(root, (n) => n.attrs.id === l.attrs.for)[0]; },
    gate: () => t.one((n) => n.attrs.id === "fl-gate").textContent,
    issueBtn: () => t.all((n) => n.tagName === "BUTTON" && /^(Create invite|Replace invite|Retry same request)$/.test(n.textContent))[0],
    checkBtn: () => t.byText("button", /^Check readiness$/, t.one((n) => n.attrs.id === "fl-connect")),
    secretText: () => t.text(t.cls("fl-secret")[0]),
  };
  return t;
}
async function booted(opt) { const t = mount(opt); await t.tick(); return t; }
async function fillNew(t, { node = "oracle2", os = "linux", kind = "cloud" } = {}) {
  t.kind(kind);
  t.type(t.control(/^Computer id/), node);
  const osel = t.control(/^Operating system/); osel.value = os; t.fire(osel, "change");
}
async function ready(t, o) { await fillNew(t, o); t.click(t.checkBtn()); await t.tick(); }

const scenarios = {
  async loads_with_reads_only_and_never_issues_anything_on_mount() {
    const t = await booted();
    assert.deepEqual(t.state.calls.map((c) => c.method + " " + c.path).sort(), ["GET /api/fleet", "GET /api/fleet/vault"]);
    assert.equal(t.posts().length, 0);
    const txt = t.text();
    for (const w of ["Main PC", "dell", "oracle1", "spare"]) assert(txt.includes(w) || txt.includes(w.toLowerCase()), w);
    assert.match(txt, /Invite pending/);
    assert.match(txt, /1 online|online/);
    assert.equal(t.secretText(), "");
    assert(t.cls("fl-secret")[0].hidden);
    assert.deepEqual(t.state.innerHTML, []);
  },

  async does_nothing_when_the_fleet_root_is_missing() {
    const t = mount({ noRoot: true });
    await t.tick();
    assert.equal(t.state.calls.length, 0);
  },

  async waits_to_load_until_the_connections_view_is_shown() {
    const t = mount({ hidden: true }); await t.tick();
    assert.equal(t.state.calls.length, 0);
    t.setHidden(false); await t.tick();
    assert.equal(t.calls("GET", "/api/fleet").length, 1);
  },

  async a_failed_load_shows_retry_and_a_later_success_clears_it() {
    let fail = true;
    const t = mount(); t.state.routes["GET /api/fleet"] = async () => { if (fail) { const e = new Error("HTTP 500"); e.status = 500; throw e; } return clone(FIX.owner); };
    await t.tick();
    assert.match(t.text(), /Couldn't load computers/);
    fail = false; t.click(t.byText("button", /^Retry$/)); await t.tick();
    assert.doesNotMatch(t.text(), /Couldn't load computers/);
    assert.match(t.text(), /oracle1/);
  },

  async offline_load_says_unreachable() {
    const t = mount(); t.state.routes["GET /api/fleet"] = async () => { throw new TypeError("Failed to fetch"); };
    await t.tick();
    assert.match(t.text(), /Couldn't reach JARVIS/);
  },

  async create_invite_is_gated_by_a_readiness_preview() {
    const t = await booted();
    await fillNew(t);
    assert(t.issueBtn().disabled);
    assert.match(t.gate(), /Check readiness first/);
    t.click(t.checkBtn()); await t.tick();
    const call = t.calls("POST", "/api/fleet/preview")[0];
    assert.equal(call.body.node, "oracle2"); assert.equal(call.body.profile, "cloud"); assert.equal(call.body.os, "linux");
    assert.deepEqual(call.body.capabilities, ["inventory"]);
    assert.equal("request_id" in call.body, false);
    assert.match(t.text(), /Ready\. Nothing has changed yet/);
    assert.match(t.text(), /What creating the invite changes/);
    assert.equal(t.issueBtn().disabled, false);
    // changing the form invalidates the preview
    t.type(t.control(/^Computer id/), "oracle3");
    assert(t.issueBtn().disabled);
    assert.match(t.text(), /form changed/);
  },

  async failed_invite_replaces_ready_verdict_and_explains_forward_requirement() {
    const t = await booted();
    t.state.routes["POST /api/fleet/invite"] = () => {
      const e = new Error('Invites need cluster.tunnel forward.');
      e.status = 409; e.code = "tunnel_forward"; e.retryable = false; throw e;
    };
    await ready(t);
    t.click(t.issueBtn()); await t.tick();
    assert.match(t.text(), /Invite not confirmed/);
    assert.match(t.text(), /Invites require forward tunnels/);
    assert.doesNotMatch(t.text(), /Ready\. Nothing has changed yet|This cluster uses forward tunnels/);
    assert(t.issueBtn().disabled);
  },

  async pending_invite_preview_never_calls_it_ready() {
    const t = await booted();
    t.state.routes["POST /api/fleet/preview"] = () => ({ ...clone(FIX.preview), ready: true,
      problems: [{ code: "invite_pending", why: "An invite is still pending." }] });
    await ready(t);
    assert.match(t.text(), /Review required/);
    assert.doesNotMatch(t.text(), /Ready\. Nothing has changed yet/);
  },

  async backlog_overrides_a_stale_current_archive_label() {
    const t = mount();
    t.state.routes["GET /api/fleet/vault"] = () => {
      const v = clone(FIX.vault); v.archive.state = "current";
      v.archive.capture_enabled = true; v.archive.capture_complete = true; v.archive.archive_pending = 2;
      return v;
    };
    await t.tick();
    const txt = t.text(t.one((n) => n.attrs.id === "fl-vault"));
    assert.match(txt, /Items waiting to be archived/);
    assert.doesNotMatch(txt, /Capture on and complete/);
  },

  async a_blocked_preview_keeps_create_disabled_and_lists_why() {
    const t = await booted();
    t.state.routes["POST /api/fleet/preview"] = () => clone(FIX.preview_exists);
    await ready(t, { node: "dell", kind: "pc" });
    assert.match(t.text(), /Not ready/);
    assert(t.issueBtn().disabled);
    assert.match(t.gate(), /Not ready/);
  },

  async issues_the_invite_shows_the_secret_privately_and_clears_it() {
    const t = await booted();
    const secretResp = clone(FIX.invite_ok); secretResp.code = CODE;
    t.state.routes["POST /api/fleet/invite"] = () => clone(secretResp);
    await ready(t);
    t.click(t.issueBtn()); await t.tick();
    const inv = t.calls("POST", "/api/fleet/invite")[0];
    assert.match(inv.body.request_id, /^[A-Za-z0-9_-]{8,64}$/);
    assert.equal(inv.body.node, "oracle2"); assert.equal("replace_pending" in inv.body, false); assert.equal("acknowledge" in inv.body, false);
    // shown exactly once, in the private block, with one-use / expiry text
    assert.equal(t.text().split(CODE).length - 1, 1);
    assert(t.text(t.cls("fl-secret")[0]).includes(CODE));
    assert.match(t.secretText(), /One use|can't be shown again/);
    assert.match(t.secretText(), /Expires in/);
    assert.match(t.secretText(), /never runs it|this console never runs it/);
    // nowhere else
    assert.equal(JSON.stringify(t.state.store).includes("SECRETSECRET"), false);
    assert.equal(t.state.logs.join("\n").includes("SECRETSECRET"), false);
    assert.equal(JSON.stringify(t.state.calls.filter((c) => c.method === "GET")).includes("SECRETSECRET"), false);
    // creating a second one while it's shown is blocked
    assert(t.issueBtn().disabled);
    // copy code / copy join command
    t.click(t.byText("button", /^Copy code$/)); await t.tick();
    t.click(t.byText("button", /^Copy join command$/)); await t.tick();
    assert.equal(t.state.clip[0], CODE);
    assert.match(t.state.clip[1], /^\.venv\/bin\/python -m jarvis cluster join "jarvis-invite-1:/);
    assert.equal(t.state.clip[1].includes("<INVITE CODE>"), false);
    // the displayed command still has the placeholder, never the code
    assert.match(t.text(t.cls("fl-pre")[0]), /<INVITE CODE>/);
    // done clears it
    t.click(t.byText("button", /^Done: clear the code$/)); await t.tick();
    assert.equal(t.text().includes("SECRETSECRET"), false);
    assert(t.cls("fl-secret")[0].hidden);
    assert.equal(t.cls("fl-secret")[0].textContent, "");
  },

  async leaving_the_view_clears_the_secret() {
    const t = await booted();
    const r = clone(FIX.invite_ok); r.code = CODE;
    t.state.routes["POST /api/fleet/invite"] = () => clone(r);
    await ready(t); t.click(t.issueBtn()); await t.tick();
    assert(t.text().includes(CODE));
    t.setHidden(true);
    assert.equal(t.text().includes("SECRETSECRET"), false);
  },

  async a_code_that_fails_the_format_check_is_never_shown_or_copied() {
    const t = await booted();
    const r = clone(FIX.invite_ok); r.code = 'jarvis-invite-1:x" ; rm -rf ~ #';
    t.state.routes["POST /api/fleet/invite"] = () => clone(r);
    await ready(t); t.click(t.issueBtn()); await t.tick();
    assert.equal(t.text().includes("rm -rf"), false);
    assert.match(t.text(), /no usable code|can't be recovered/);
    assert.equal(t.state.clip.length, 0);
  },

  async a_lost_reply_retries_with_the_same_request_id_then_replace_uses_a_new_one() {
    const t = await booted();
    let n = 0;
    t.state.routes["POST /api/fleet/invite"] = (b) => {
      n++;
      if (n === 1) { const e = new Error("HTTP 502"); e.status = 502; e.code = "invite_failed"; e.retryable = true; throw e; }
      if (n === 2) { const e = new Error("already"); e.status = 409; e.code = "already_issued"; e.retryable = false; throw e; }
      const r = clone(FIX.invite_ok); r.code = CODE; return r;
    };
    await ready(t);
    t.click(t.issueBtn()); await t.tick();
    assert.match(t.text(), /Retry same request|same request/);
    assert.equal(t.issueBtn().textContent, "Retry same request");
    t.click(t.issueBtn()); await t.tick();
    const calls = t.calls("POST", "/api/fleet/invite");
    assert.equal(calls[0].body.request_id, calls[1].body.request_id);
    // 409 already_issued: no implicit new code. A deliberate replace needs a tick and a new id.
    assert.match(t.text(), /can't be shown again/);
    assert.equal(t.cls("fl-replace")[0].hidden, false);
    assert(t.issueBtn().disabled);
    assert.equal(t.issueBtn().textContent, "Replace invite");
    const box = t.cls("fl-replace")[0];
    t.fire(t.one((x) => x.tagName === "INPUT", box), "change"); // fake checkbox: set below
    t.one((x) => x.tagName === "INPUT", box).checked = true; t.fire(t.one((x) => x.tagName === "INPUT", box), "change");
    assert.equal(t.issueBtn().disabled, false);
    t.click(t.issueBtn()); await t.tick();
    const third = t.calls("POST", "/api/fleet/invite")[2];
    assert.equal(third.body.replace_pending, true);
    assert.notEqual(third.body.request_id, calls[0].body.request_id);
    assert(t.text().includes(CODE));
  },

  async a_non_retryable_invite_failure_shows_the_server_reason_and_survives_a_label_edit() {
    const t = await booted();
    const REASON = "This request stopped after a credential change and the cleanup could not be verified. It was not atomic and it was not retried.";
    let n = 0;
    t.state.routes["POST /api/fleet/invite"] = () => {
      n++;
      const e = new Error(REASON); e.status = 409; e.code = "invite_failed"; e.retryable = false; throw e;
    };
    await ready(t);
    t.click(t.issueBtn()); await t.tick();
    const txt = t.text();
    assert(txt.includes("cleanup could not be verified"), "server reason shown");
    assert.doesNotMatch(txt, /Trying again reuses the same request/);
    assert.doesNotMatch(txt, /Retry same request/);
    const first = t.calls("POST", "/api/fleet/invite")[0].body.request_id;
    // editing the label must not forget the uncertain request, and must not allow a silent new one
    t.type(t.control(/^Display name/), "Renamed");
    assert(t.issueBtn().disabled, "create stays blocked after the edit");
    assert.equal(t.cls("fl-replace")[0].hidden, false);
    assert.throws(() => t.click(t.issueBtn()), /disabled/, "the harness refuses a click on a disabled button");
    assert.equal(t.issueBtn().textContent, "Replace invite");
    assert.equal(t.calls("POST", "/api/fleet/invite").length, 1, "no automatic second attempt");
    assert.equal(n, 1);
    assert(first && first.length > 8);
  },

  async a_node_busy_past_the_online_window_reads_busy_not_online_or_offline() {
    const t = mount();
    t.state.routes["GET /api/fleet"] = async () => {
      const inv = clone(FIX.owner);
      const d = inv.devices.find((x) => x.id === "dell");
      d.status = "busy"; d.online = false; d.presence = "busy"; d.seen = 1;
      return inv;
    };
    await t.tick();
    const txt = t.text();
    assert.match(txt, /Busy, awaiting contact/);
    assert.doesNotMatch(txt, /1 online/);
  },

  async a_partly_verified_archive_never_claims_complete_capture() {
    const t = mount();
    t.state.routes["GET /api/fleet/vault"] = () => {
      const v = clone(FIX.vault); v.archive.state = "partial"; v.archive.files_stale = true;
      v.archive.capture_enabled = true; v.archive.capture_complete = true; v.archive.archive_pending = 0;
      v.archive.archive_baseline_pending = 3;
      return v;
    };
    await t.tick();
    const txt = t.text(t.one((n) => n.attrs.id === "fl-vault"));
    assert.match(txt, /Items waiting to be archived|Partly verified/);
    assert.match(txt, /files are not verified/);
    assert.doesNotMatch(txt, /Capture on and complete|Capture is enabled for all/);
    assert.doesNotMatch(txt, /null/);
  },

  async invite_pending_on_a_listed_node_requires_a_deliberate_replace() {
    const t = await booted();
    t.click(t.byText("button", /oracle1/));
    t.click(t.byText("button", /^Replace this invite…$/));
    assert.equal(t.control(/^Computer id/).value, "oracle1");
    assert.equal(t.cls("fl-replace")[0].hidden, false);
    assert(t.checkBtn().disabled); // OS unknown for a pending node: must be chosen
    const osel = t.control(/^Operating system/); osel.value = "linux"; t.fire(osel, "change");
    t.click(t.checkBtn()); await t.tick();
    assert(t.issueBtn().disabled);
    assert.match(t.gate(), /Replace it/);
    const cb = t.one((x) => x.tagName === "INPUT", t.cls("fl-replace")[0]); cb.checked = true; t.fire(cb, "change");
    assert.equal(t.issueBtn().disabled, false);
  },

  async duplicate_clicks_send_one_request() {
    const t = await booted();
    let resolve; t.state.routes["POST /api/fleet/invite"] = () => new Promise((ok) => { resolve = () => ok(Object.assign(clone(FIX.invite_ok), { code: CODE })); });
    await ready(t);
    const b = t.issueBtn();
    t.click(b); await t.tick();
    assert.equal(b.disabled, true);
    t.fire(t.one((x) => x.tagName === "FORM" && /fl-connect/.test(x.attrs.class)), "submit");
    await t.tick();
    assert.equal(t.calls("POST", "/api/fleet/invite").length, 1);
    resolve(); await t.tick();
  },

  async extra_permissions_need_an_acknowledgement_and_are_sent() {
    const t = await booted();
    await fillNew(t, { node: "box1", kind: "headless" });
    const cam = t.capBox("Webcam view"); cam.checked = true; t.fire(cam, "change");
    t.click(t.checkBtn()); await t.tick();
    assert.equal(t.calls("POST", "/api/fleet/preview")[0].body.capabilities.includes("camera"), true);
    assert(t.issueBtn().disabled);
    assert.match(t.gate(), /Acknowledge/);
    const ack = t.one((x) => x.tagName === "INPUT", t.cls("fl-ack").find((a) => !a.hidden)); ack.checked = true; t.fire(ack, "change");
    // the preview is still valid (same form), so create now works
    const r = clone(FIX.invite_ok); r.code = CODE; t.state.routes["POST /api/fleet/invite"] = () => r;
    assert.equal(t.issueBtn().disabled, false);
    t.click(t.issueBtn()); await t.tick();
    assert.deepEqual(t.calls("POST", "/api/fleet/invite")[0].body.acknowledge, ["camera"]);
  },

  async acknowledgement_names_permissions_by_title_with_correct_number() {
    const t = await booted();
    await fillNew(t, { node: "box1", kind: "headless" });
    const ackText = () => t.text(t.cls("fl-ack").find((a) => !a.hidden));
    const TAIL = " beyond the default permissions and ";
    const cam = t.capBox("Webcam view"); cam.checked = true; t.fire(cam, "change");
    assert.equal(ackText(), "I understand that Webcam view goes" + TAIL + "gives this computer more control over my data and machines.");
    assert.doesNotMatch(ackText(), /\bcamera\b/, "internal id leaked");
    const loc = t.capBox("Find where it is"); loc.checked = true; t.fire(loc, "change");
    assert.equal(ackText(), "I understand that Webcam view and Find where it is go" + TAIL + "give this computer more control over my data and machines.");
    const ask = t.capBox("Ask JARVIS"); ask.checked = true; t.fire(ask, "change");
    const three = ackText();
    assert.match(three, /^I understand that .+, .+ and .+ go beyond the default permissions and give /);
    assert.doesNotMatch(three, /\b(camera|locate|ask)\b/);
    // the gate still needs the box, and the request still carries the real ids
    assert(t.issueBtn().disabled);
    const box = t.one((x) => x.tagName === "INPUT", t.cls("fl-ack").find((a) => !a.hidden)); box.checked = true; t.fire(box, "change");
    t.click(t.checkBtn()); await t.tick();
    assert.deepEqual(t.calls("POST", "/api/fleet/preview")[0].body.capabilities.filter((c) => ["camera", "locate", "ask"].includes(c)).sort(), ["ask", "camera", "locate"]);
    // unticking every extra hides the sentence and clears the acknowledgement
    for (const b of [cam, loc, ask]) { b.checked = false; t.fire(b, "change"); }
    assert.equal(t.all((n) => n.tagName === "LABEL" && /^I understand/.test(n.textContent) && n.textContent !== "").length, 0);
  },

  async acknowledgement_shows_hostile_and_long_titles_as_plain_text() {
    const t = mount();
    const inv = clone(FIX.owner);
    const cam = inv.capabilities.find((c) => c.id === "camera"); cam.title = HOSTILE;
    const loc = inv.capabilities.find((c) => c.id === "locate"); loc.title = "x".repeat(500);
    t.state.routes["GET /api/fleet"] = () => clone(inv);
    await t.tick();
    await fillNew(t, { node: "box1", kind: "headless" });
    const boxes = t.all((n) => n.tagName === "INPUT" && n.attrs.type === "checkbox", t.one((n) => n.attrs.id === "fl-connect"));
    const byTitle = (title) => t.capBox(title);
    byTitle(HOSTILE).checked = true; t.fire(byTitle(HOSTILE), "change");
    const label = () => t.text(t.cls("fl-ack").find((a) => !a.hidden));
    assert.equal(label(), "I understand that " + HOSTILE + " goes beyond the default permissions and gives this computer more control over my data and machines.");
    const longBox = byTitle("x"); longBox.checked = true; t.fire(longBox, "change");
    assert(label().includes("x".repeat(80)) && !label().includes("x".repeat(81)), "title is capped at 80");
    assert.deepEqual(t.state.innerHTML, []);
    assert.equal(t.all((n) => n.tagName === "IMG").length, 0);
    assert(boxes.length >= 5);
  },

  async validation_blocks_bad_ids_the_main_and_missing_os() {
    const t = await booted();
    t.kind("cloud");
    t.type(t.control(/^Computer id/), "bad id!");
    assert(t.checkBtn().disabled);
    t.type(t.control(/^Computer id/), "main");
    assert(t.checkBtn().disabled);
    t.type(t.control(/^Computer id/), "ok1");
    const osel = t.control(/^Operating system/); osel.value = ""; t.fire(osel, "change");
    assert(t.checkBtn().disabled);
    assert.equal(t.posts().length, 0);
  },

  async capabilities_save_posts_real_changes_and_asks_ack_for_extras() {
    const t = await booted();
    t.click(t.byText("button", /^Dell/).parent ? t.all((n) => n.tagName === "BUTTON" && n.attrs["aria-expanded"] === "false")[1] : null);
    const form = t.all((n) => n.tagName === "FORM" && /Permissions for/.test(n.attrs["aria-label"] || ""))[0];
    assert(form, "caps form");
    const save = t.byText("button", /^Save permissions$/, form);
    const files = t.all((n) => n.tagName === "INPUT" && n.attrs.type === "checkbox", form);
    // untick one, tick console (extra)
    const labels = t.all((n) => n.tagName === "LABEL", form);
    const boxOf = (title) => t.one((n) => n.attrs.id === labels.find((l) => l.textContent.startsWith(title)).attrs.for, form);
    t.state.routes["POST /api/fleet/capabilities"] = () => ({ ok: true, changed: true, added: ["console"], removed: ["grokbot"] });
    boxOf("Grok Bot").checked = false; t.fire(boxOf("Grok Bot"), "change");
    boxOf("Use the console").checked = true; t.fire(boxOf("Use the console"), "change");
    assert(save.disabled, "extra needs ack");
    const ack = t.one((n) => n.tagName === "INPUT", t.cls("fl-ack", form).find((a) => !a.hidden)); ack.checked = true; t.fire(ack, "change");
    assert.equal(save.disabled, false);
    t.fire(form, "submit"); await t.tick();
    const c = t.calls("POST", "/api/fleet/capabilities")[0];
    assert.equal(c.body.node, "dell"); assert(c.body.capabilities.includes("console")); assert(!c.body.capabilities.includes("grokbot"));
    assert.deepEqual(c.body.acknowledge, ["console"]);
    assert.match(t.text(), /Saved: added console; removed grokbot/);
    assert(files.length > 3);
  },

  async revoke_needs_the_exact_node_id_and_never_applies_to_the_main() {
    const t = await booted();
    const toggles = t.all((n) => n.tagName === "BUTTON" && n.attrs["aria-expanded"] === "false");
    // main row: no revoke form
    t.click(toggles.find((b) => /Main PC/.test(b.textContent)));
    assert.equal(t.all((n) => n.tagName === "BUTTON" && /^Revoke /.test(n.textContent)).length, 0);
    t.click(toggles.find((b) => /^Dell|dell/.test(b.textContent)));
    const go = t.all((n) => n.tagName === "BUTTON" && /^Revoke dell$/.test(n.textContent))[0];
    assert(go.disabled);
    const input = t.control(/^Type the computer's id/);
    t.type(input, "Dell"); assert(go.disabled);
    t.type(input, "dell"); assert.equal(go.disabled, false);
    t.state.routes["POST /api/fleet/revoke"] = () => ({ ok: true, was_enrolled: true, ssh_key_removed: true, active_jobs_cancelled: 0 });
    t.fire(t.one((n) => n.tagName === "FORM" && n.contains(go)), "submit"); await t.tick();
    assert.deepEqual(t.calls("POST", "/api/fleet/revoke")[0].body, { node: "dell", confirm: "dell" });
    assert.match(t.text(), /Revoked\./);
  },

  async main_transfer_is_preview_only_and_shows_what_is_missing() {
    const t = await booted();
    const move = t.byText("button", /^Move MAIN \(not available\)$/);
    assert(move.disabled);
    t.click(t.byText("button", /^Check readiness$/, t.one((n) => n.attrs.id === "fl-transfer")));
    assert.match(t.text(t.one((n) => n.attrs.id === "fl-transfer")), /Choose a computer first/);
    const sel = t.one((n) => n.tagName === "SELECT" && n.attrs["aria-label"] === "Computer to check"); sel.value = "dell";
    t.click(t.byText("button", /^Check readiness$/, t.one((n) => n.attrs.id === "fl-transfer"))); await t.tick();
    assert.deepEqual(t.calls("POST", "/api/fleet/main-transfer/preview")[0].body, { target: "dell" });
    const tx = t.text(t.one((n) => n.attrs.id === "fl-transfer"));
    assert.match(tx, /Blocked\. Nothing was changed/);
    assert.match(tx, /Still missing \(7\)/);
    assert.equal(t.state.calls.some((c) => c.path === "/api/fleet/main-transfer"), false);
    assert(move.disabled);
  },

  async archive_and_git_are_shown_separately_without_claiming_a_backup() {
    const t = await booted();
    const txt = t.text(t.one((n) => n.attrs.id === "fl-vault"));
    assert.match(txt, /Memory archive/); assert.match(txt, /Git copy of the vault/);
    assert.match(txt, /Unpublished changes/); assert.match(txt, /as of the last fetch/);
    assert.match(txt, /Not checked\. This view never uses the network/);
    assert.match(txt, /doesn't mean your changes are published or backed up/);
    assert.doesNotMatch(txt.replace(/doesn't mean your changes are published or backed up/, ""), /backed up|synced|in sync|is private\b(?! needs)|push now/i);
    assert.equal(t.all((n) => n.tagName === "BUTTON" && /push|publish|sync/i.test(n.textContent)).length, 0);
  },

  async hostile_server_text_is_only_ever_text() {
    const t = mount();
    const inv = clone(FIX.owner); inv.devices[1].label = HOSTILE; inv.devices[1].hostname = HOSTILE;
    t.state.routes["GET /api/fleet"] = () => clone(inv);
    await t.tick();
    assert(t.text().includes(HOSTILE));
    assert.deepEqual(t.state.innerHTML, []);
    assert.equal(t.all((n) => n.tagName === "IMG").length, 0);
  },

  async polling_keeps_typed_text_and_open_panels() {
    const t = await booted();
    t.type(t.control(/^Computer id/), "typing-here");
    const tg = t.all((n) => n.tagName === "BUTTON" && n.attrs["aria-expanded"] === "false")[1]; t.click(tg);
    const before = t.all((n) => n.tagName === "FORM" && /Permissions for/.test(n.attrs["aria-label"] || ""))[0];
    for (const iv of t.state.intervals) if (iv && iv[1] === 20000) iv[0]();
    await t.tick();
    assert.equal(t.control(/^Computer id/).value, "typing-here");
    assert.equal(tg.attrs["aria-expanded"], "true");
    assert.equal(t.all((n) => n.tagName === "FORM" && /Permissions for/.test(n.attrs["aria-label"] || ""))[0], before);
  },

  async next_step_and_code_status_show_without_a_code() {
    const t = await booted();
    const txt = t.text();
    assert.match(txt, /Next: Run the join command on the node/);
    assert.match(txt, /Next: Create an invite for this node/);
    assert.match(txt, /The invite code is not stored here/);
    assert.doesNotMatch(txt, /Next: Connected/);
    assert.doesNotMatch(txt, /jarvis-invite-1:/);
    assert.match(txt, /May: Screens, apps and mouse/);
  },

  async preview_names_which_computer_runs_each_step() {
    const t = await booted();
    await ready(t);
    const txt = t.text(t.one((n) => n.attrs.id === "fl-connect"));
    assert.match(txt, /Who does each step/);
    assert.match(txt, /On oracle2, the computer you are adding/);
    assert.match(txt, /You, in this console, on the MAIN/);
    assert.match(txt, /This page never runs it/);
    assert.match(txt, /<INVITE CODE>/);
    assert.equal(t.posts().some((c) => c.path === "/api/fleet/invite"), false);
  },

  async start_over_clears_a_shown_code() {
    const t = await booted();
    const r = clone(FIX.invite_ok); r.code = CODE;
    t.state.routes["POST /api/fleet/invite"] = () => clone(r);
    await ready(t); t.click(t.issueBtn()); await t.tick();
    assert(t.text().includes(CODE));
    t.click(t.byText("button", /^Start over$/));
    assert.equal(t.text().includes("SECRETSECRET"), false);
    assert(t.cls("fl-secret")[0].hidden);
    assert.equal(JSON.stringify(t.win.JarvisFleet || {}).includes("SECRETSECRET"), false);
  },

  async writes_stay_disabled_when_this_console_cannot_change_the_fleet() {
    const t = mount();
    const inv = clone(FIX.owner);
    inv.can_write = false;
    inv.cluster.local_is_main = false;
    for (const a of inv.actions) if (a.id === "invite" || a.id === "revoke" || a.id === "capabilities") { a.support = "blocked"; a.why = "Do this on the main PC's console."; }
    t.state.routes["GET /api/fleet"] = () => clone(inv);
    await t.tick();
    await fillNew(t);
    assert(t.checkBtn().disabled);
    assert(t.issueBtn().disabled);
    assert.match(t.gate(), /main PC/);
    assert.equal(t.posts().length, 0);
    t.click(t.all((n) => n.tagName === "BUTTON" && n.attrs["aria-expanded"] === "false").find((b) => /Dell/.test(b.textContent)));
    const save = t.byText("button", /^Save permissions$/);
    assert(save.disabled);
    const go = t.byText("button", /^Revoke dell$/);
    assert(go.disabled);
  },

  async an_open_panel_catches_up_after_focus_leaves() {
    const t = await booted();
    const tg = t.all((n) => n.tagName === "BUTTON" && n.attrs["aria-expanded"] === "false")[1];
    t.click(tg);
    const form = t.all((n) => n.tagName === "FORM" && /Permissions for/.test(n.attrs["aria-label"] || ""))[0];
    t.one((n) => n.tagName === "INPUT", form).focus();
    const inv = clone(FIX.owner);
    inv.devices.find((d) => d.id === "dell").label = "Dell renamed";
    t.state.routes["GET /api/fleet"] = () => clone(inv);
    const poll = () => { for (const iv of t.state.intervals) if (iv && iv[1] === 20000) iv[0](); };
    poll(); await t.tick();
    assert.equal(form.parent != null, true, "focused panel is not rebuilt");
    assert.match(t.text(), /Dell renamed/);
    t.doc.activeElement = null;
    poll(); await t.tick();
    assert.equal(form.parent, null);
    const next = t.all((n) => n.tagName === "FORM" && /Permissions for/.test(n.attrs["aria-label"] || ""))[0];
    assert(next && next !== form);
    assert.match(next.attrs["aria-label"], /Permissions for Dell renamed/);
  },

  async archive_diagnostics_stay_collapsed_and_do_not_claim_a_backup() {
    const t = await booted();
    const vault = t.one((n) => n.attrs.id === "fl-vault");
    const sums = t.all((n) => n.tagName === "SUMMARY", vault).map((n) => n.textContent);
    assert(sums.some((s) => /Archive diagnostic details/.test(s)));
    assert(sums.some((s) => /Git diagnostic details/.test(s)));
    assert.equal(t.all((n) => n.tagName === "DETAILS", vault).every((d) => !d.hasAttribute("open")), true);
  },
};

// app.js api(): structured errors survive, plain Error(message) behaviour unchanged.
scenarios.app_api_keeps_error_code_and_flags = async function () {
  const m = /  async function api\(path, body, raw\) \{[\s\S]*?\n  const nf = /.exec(APP);
  assert(m, "api block not found");
  const code = m[0].replace(/\n  const nf = $/, "\n");
  let reply;
  const ctx = { fetch: async () => reply(), Error };
  const fn = vm.runInNewContext("(function(){" + code + "; return api; })()", ctx);
  reply = () => ({ ok: false, status: 409, json: async () => ({ error: "That already happened.", code: "already_issued", retryable: false, step: "invite", secret_field: "x", needs_ack: ["camera"] }) });
  let err; try { await fn("/api/fleet/invite", {}); } catch (e) { err = e; }
  assert(err instanceof Error); assert.equal(err.message, "That already happened.");
  assert.equal(err.status, 409); assert.equal(err.code, "already_issued"); assert.equal(err.retryable, false); assert.equal(err.step, "invite");
  assert.deepEqual(Object.keys(err.data).sort(), ["code", "needs_ack", "retryable", "step"]);
  reply = () => ({ ok: false, status: 500, json: async () => { throw new Error("bad json"); } });
  try { await fn("/x", {}); assert.fail("no throw"); } catch (e) { assert.equal(e.message, "HTTP 500"); assert.equal(e.status, 500); assert.equal(e.code, undefined); }
  reply = () => ({ ok: true, status: 200, json: async () => ({ ok: 1 }) });
  assert.deepEqual(await fn("/x"), { ok: 1 });
  reply = () => ({ ok: false, status: 409, json: async () => ({ error: "no", code: CODE, retryable: false, step: "join" }) });
  try { await fn("/api/fleet/invite", {}); assert.fail("no throw"); } catch (e) {
    assert.equal(e.code, undefined);
    assert.equal(JSON.stringify(e.data).includes("SECRETSECRET"), false);
    assert.equal(e.message, "no");
  }
};

(async () => {
  const names = process.argv[2] ? [process.argv[2]] : Object.keys(scenarios);
  for (const name of names) {
    if (!scenarios[name]) throw new Error("unknown scenario " + name);
    await scenarios[name]();
    console.log("PASS " + name);
  }
  console.log("ok " + names.length);
})().catch((e) => { console.error(e); process.exit(1); });
