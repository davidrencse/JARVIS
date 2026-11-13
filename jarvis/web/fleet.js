/* Fleet management inside Connections (#fleet-root): computers and servers, connecting a new one,
   permissions, the memory archive vs Git status, and the MAIN transfer readiness report.

   Contract: docs/design/mobile-continuity/FLEET-API.md. Every control here calls one of
   GET /api/fleet, GET /api/fleet/vault, POST /api/fleet/{preview,invite,revoke,capabilities,
   main-transfer/preview} through Jarvis.api. POST /api/fleet/main-transfer is never called.

   The invite code is a secret: it lives only in the `secret` closure variable and one private DOM node
   while shown. It never goes to state, storage, URL, events, logs or the console. All server text is
   set with textContent. Nothing here runs a shell command; join commands are shown and copied, not run. */
(() => {
  "use strict";
  const root = typeof document !== "undefined" ? document.getElementById("fleet-root") : null;
  if (!root) return; // old fixtures / other pages: nothing to mount

  const POLL_MS = 20000;
  const CODE_RE = /^jarvis-invite-1:[A-Za-z0-9_\-.=:+/]+$/;
  const NODE_RE = /^[A-Za-z0-9_-]{1,64}$/;
  const HOST_RE = /^[A-Za-z0-9]([A-Za-z0-9.\-:]{0,251}[A-Za-z0-9])?$/;
  const PLACEHOLDER = "<INVITE CODE>";
  const OSES = [["linux", "Linux"], ["windows", "Windows"], ["macos", "macOS"]];
  const OS_LABEL = { linux: "Linux", windows: "Windows", macos: "macOS" };
  const STATUS = {
    main: ["MAIN", "ok"], online: ["Online", "ok"], busy: ["Busy, awaiting contact", "warn"], offline: ["Offline", "warn"],
    waiting_for_first_contact: ["Waiting for first contact", "warn"], invite_pending: ["Invite pending", "warn"],
    invite_expired: ["Invite expired", "warn"], not_enrolled: ["Not connected", ""], unconfigured: ["Not set up", ""],
  };
  const FRIENDLY = {
    capability_ack_required: "Tick the acknowledgement for the extra permissions first.",
    already_issued: "That request already made a code and it can't be shown again. Replace the invite to get a new one.",
    invite_pending: "An invite for this computer is still pending. Replace it deliberately to get a new code.",
    node_enrolled: "That computer already joined. Revoke it first if you want to invite it again.",
    main_not_allowed: "The MAIN can't be enrolled, changed or revoked here.",
    cluster_off: "The cluster is off. Turn it on before adding computers.",
    not_main: "This computer isn't the MAIN. Do this from the MAIN's console.",
    tunnel_forward: "Invites require forward tunnels. Set cluster.tunnel to forward on the MAIN, then check readiness again.",
    sshd: "The main's SSH server didn't answer. Start it, then try again.",
    profile_mismatch: "That computer is already set up with a different kind. Check the kind and try again.",
    invite_failed: "The invite couldn't be created. No code was shown.",
    busy: "JARVIS is finishing another change. Try again in a moment.",
    unknown_node: "That computer isn't in the config.", not_enrolled: "That computer hasn't joined yet, so it has no permissions to change.",
    config_unreadable: "The config couldn't be read, so nothing was changed.", state_unwritable: "JARVIS couldn't save its state, so nothing was changed.",
  };

  // ---------- small helpers ----------
  const api = (path, body) => window.Jarvis.api(path, body);
  const h = (tag, attrs, ...kids) => {
    const e = document.createElement(tag);
    for (const k in attrs || {}) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "text") e.textContent = v;
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of kids.flat()) if (c != null && c !== false) e.append(typeof c === "object" ? c : document.createTextNode(String(c)));
    return e;
  };
  const clear = (el) => { if (el.replaceChildren) el.replaceChildren(); else el.textContent = ""; };
  const str = (v, max = 200) => (typeof v === "string" ? v.slice(0, max) : "");
  const arr = (v) => (Array.isArray(v) ? v : []);
  const ago = (t) => {
    if (typeof t !== "number" || !isFinite(t)) return "";
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  };
  const left = (t) => {
    const s = Math.round(t - Date.now() / 1000);
    if (s <= 0) return "expired";
    if (s < 90) return "under a minute";
    if (s < 5400) return Math.round(s / 60) + " min";
    return Math.round(s / 3600) + " h";
  };
  let uid = 0;
  const nid = (p) => "fl-" + p + "-" + ++uid;
  const rid = () => {
    try {
      const b = new Uint8Array(12); crypto.getRandomValues(b);
      return "r" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
    } catch (e) { return "r" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12).padEnd(10, "0"); }
  };
  const errText = (e) => {
    // The server's own public reason for a failed invite beats a generic line (an uncertain request must say so).
    if (e && e.code === "invite_failed" && e.status != null && e.message && !/^HTTP \d+/.test(e.message)) return str(e.message, 300);
    if (e && e.code && FRIENDLY[e.code]) return FRIENDLY[e.code];
    if (e && e.status == null) return "Couldn't reach JARVIS. Check that it's running, then retry.";
    return str((e && e.message) || "Something went wrong.", 300);
  };
  const retryable = (e) => !!(e && (e.retryable === true || e.status == null));
  const viewEl = root.parentElement || null;
  const visible = () => (!viewEl || !viewEl.hasAttribute || !viewEl.hasAttribute("hidden")) && !(typeof document.hidden === "boolean" && document.hidden);

  // ---------- state (never holds the invite code) ----------
  const S = { inv: null, vault: null, invErr: null, vaultErr: null, loadingInv: false, loadingVault: false, lastOk: 0 };
  let secret = null;       // { code, commands, expiresAt, node, timer } — the only place the code lives
  let busy = false;        // one write at a time
  let uncertain = null;    // { node, req }: an invite whose cleanup could not be verified; in memory only, never the code
  const rows = new Map();  // device id -> row controller
  let rowOrder = "";

  // ---------- skeleton ----------
  const live = h("p", { class: "fl-live", role: "status", "aria-live": "polite" });
  const refreshBtn = h("button", { type: "button", class: "cx-btn", onclick: () => refreshAll(true) }, "Refresh");
  const summary = h("p", { class: "fl-summary" });
  const stateBox = h("div", { class: "fl-state" });
  const compList = h("ul", { class: "fl-rows", "aria-label": "Computers and servers" });
  const compEmpty = h("p", { class: "fl-empty", hidden: true });
  const connectBody = h("div", { class: "fl-connect-body" });
  const vaultBody = h("div", { class: "fl-vault" });
  const transferBody = h("div", { class: "fl-transfer" });
  const section = (id, title, ...kids) => {
    const hid = nid("h");
    const s = h("section", { class: "fl-sec", id: "fl-" + id, "aria-labelledby": hid }, h("h3", { id: hid, tabindex: "-1", text: title }), ...kids);
    return s;
  };
  const headId = nid("h");
  const head = h("div", { class: "fl-head" },
    h("div", { class: "fl-head-text" }, h("h2", { id: headId, text: "Computers and servers" }), summary),
    h("div", { class: "fl-head-actions" }, refreshBtn));
  root.setAttribute("aria-labelledby", headId);
  const secComputers = section("computers", "Your computers", stateBox, compList, compEmpty);
  const secConnect = section("connect", "Connect a computer or server", connectBody);
  const secVault = section("vault", "Memory archive and Git", vaultBody);
  const secTransfer = section("transfer", "Moving the MAIN", transferBody);
  clear(root);
  root.append(head, live, secComputers, secConnect, secVault, secTransfer);

  const say = (msg) => { live.textContent = ""; setTimeout(() => { live.textContent = msg; }, 30); };
  const note = (kind, text, extra) => h("p", { class: "fl-note" + (kind ? " " + kind : ""), role: kind === "bad" ? "alert" : "status" }, text, extra || null);
  const btn = (label, onclick, cls) => h("button", { type: "button", class: "cx-btn" + (cls ? " " + cls : ""), onclick }, label);
  const pill = (status) => { const s = STATUS[status] || [str(status, 30) || "Unknown", ""]; return h("span", { class: "cx-pill" + (s[1] ? " " + s[1] : ""), text: s[0] }); };
  const field = (label, control, hint, id) => h("div", { class: "fl-field" }, h("label", { for: id, text: label }), control, hint ? h("p", { class: "fl-hint", id: id + "-hint", text: hint }) : null);

  // ---------- data ----------
  async function loadInv() {
    if (S.loadingInv) return;
    S.loadingInv = true; renderState();
    try {
      S.inv = await api("/api/fleet"); S.invErr = null; S.lastOk = Date.now() / 1000;
    } catch (e) { S.invErr = e; }
    S.loadingInv = false; renderInv();
  }
  async function loadVault() {
    if (S.loadingVault) return;
    S.loadingVault = true; renderVault();
    try { S.vault = await api("/api/fleet/vault"); S.vaultErr = null; } catch (e) { S.vaultErr = e; }
    S.loadingVault = false; renderVault();
  }
  function refreshAll(manual) {
    loadInv(); loadVault();
    if (manual) say("Refreshing computers and archive status.");
  }

  // ---------- computers ----------
  function renderState() {
    clear(stateBox);
    if (S.loadingInv && !S.inv) stateBox.append(note("", "Loading computers…"));
    else if (S.invErr) {
      stateBox.append(note("bad", (S.inv ? "Couldn't refresh. Showing the last reading" + (S.lastOk ? " from " + ago(S.lastOk) : "") + ". " : "Couldn't load computers. ") + errText(S.invErr),
        btn("Retry", () => loadInv(), "fl-inline")));
    }
  }
  function renderInv() {
    renderState();
    refreshBtn.disabled = false;
    const inv = S.inv;
    if (!inv) { summary.textContent = S.invErr ? "Not loaded." : "Loading…"; return; }
    const devs = arr(inv.devices);
    const main = devs.find((d) => d.role === "main" || d.status === "main");
    const nodes = devs.filter((d) => d !== main);
    const on = nodes.filter((d) => d.status === "online").length;
    const busyN = nodes.filter((d) => d.status === "busy").length;
    const waiting = nodes.filter((d) => d.status === "invite_pending" || d.status === "waiting_for_first_contact").length;
    const cl = inv.cluster || {};
    summary.textContent = (cl.enabled === false ? "Cluster is off. " : "") + "MAIN: " + str(main && (main.label || main.id) || cl.main || "unknown", 60) +
      " · " + nodes.length + (nodes.length === 1 ? " node" : " nodes") + (nodes.length ? ": " + on + " online" + (busyN ? ", " + busyN + " busy" : "") + (waiting ? ", " + waiting + " waiting" : "") : "") +
      (S.lastOk ? " · checked " + ago(S.lastOk) : "");
    // rows: patch in place so focus, open panels and typed text survive polling
    const seen = new Set();
    devs.forEach((d) => {
      const id = str(d.id, 64); if (!id) return;
      seen.add(id);
      let r = rows.get(id);
      if (!r) { r = makeRow(id); rows.set(id, r); }
      r.update(d);
    });
    rows.forEach((r, id) => { if (!seen.has(id)) { r.li.remove(); rows.delete(id); } });
    const order = devs.map((d) => d.id).sort((a, b) => { const A = rows.get(a), B = rows.get(b); return (A && A.main ? 0 : 1) - (B && B.main ? 0 : 1) || String(a).localeCompare(String(b)); });
    const sig = order.join("|");
    if (sig !== rowOrder) { rowOrder = sig; order.forEach((id) => { const r = rows.get(id); if (r) compList.append(r.li); }); }
    compEmpty.hidden = devs.length > 0;
    if (!devs.length) compEmpty.textContent = "No computers are set up yet. Use “Connect a computer or server” below to add the first one.";
    buildConnect();
    renderTransfer();
    syncForm();
  }

  const catalog = () => arr(S.inv && S.inv.capabilities);
  const profileOf = (id) => arr(S.inv && S.inv.profiles).find((p) => p.id === id) || null;
  const capIds = (d) => arr(d.capabilities).map((c) => (typeof c === "string" ? c : c && c.id)).filter(Boolean);
  const capTitle = (id) => { const c = catalog().find((x) => x && x.id === id); return str((c && c.title) || id, 80); };
  const actOf = (id) => arr(S.inv && S.inv.actions).find((a) => a && a.id === id) || null;
  function actBlocked(id) {
    if (!S.inv) return "";
    const a = actOf(id);
    if (S.inv.can_write === false) return str((a && a.why) || "This computer isn't the MAIN. Do this on the MAIN's console.", 280);
    if (a && a.support && a.support !== "supported") return str(a.why || "Not available from this console.", 280);
    return "";
  }
  function codeLine(d) {
    if (!d || d.role === "main" || d.status === "main") return "";
    if (d.code_ok === true) return "An unused invite code is on record. It is not shown again.";
    if (d.code_ok === false) return "No usable invite code is on record.";
    if (d.status === "invite_pending" || d.status === "invite_expired") return "The invite code is not stored here.";
    return "";
  }

  function makeRow(id) {
    const titleEl = h("span", { class: "fl-title" });
    const idEl = h("span", { class: "fl-id" });
    const pillHost = h("span", { class: "fl-pill-host" });
    const meta = h("p", { class: "fl-meta" });
    const capsLine = h("p", { class: "fl-caps" });
    const nextEl = h("p", { class: "fl-next" });
    const codeEl = h("p", { class: "fl-code-state" });
    const panelId = nid("panel");
    const toggle = h("button", { type: "button", class: "fl-toggle", "aria-expanded": "false", "aria-controls": panelId }, titleEl, idEl);
    const panel = h("div", { class: "fl-panel", id: panelId, hidden: true });
    const li = h("li", { class: "fl-row" }, h("div", { class: "fl-row-main" }, toggle, pillHost), meta, capsLine, nextEl, codeEl, panel);
    const R = { li, main: false, dev: null, sig: "", open: false, dirty: false, panelSig: "" };
    const locked = () => R.dirty || (li.contains && document.activeElement && li.contains(document.activeElement) && document.activeElement !== toggle);
    const build = () => { clear(panel); R.dirty = false; R.panelSig = R.sig; panel.append(...managePanel(R.dev, () => { R.dirty = true; }, () => { R.dirty = false; })); };
    toggle.addEventListener("click", () => {
      R.open = !R.open; toggle.setAttribute("aria-expanded", String(R.open)); panel.hidden = !R.open;
      if (R.open) build(); else { clear(panel); R.dirty = false; }
    });
    R.update = (d) => {
      const sig = JSON.stringify(d);
      const same = sig === R.sig;
      R.dev = d; R.main = d.role === "main" || d.status === "main";
      if (same) {
        if (R.stale && R.open && !locked()) { R.stale = false; build(); }
        return;
      }
      R.sig = sig;
      titleEl.textContent = str(d.label || d.id, 80); idEl.textContent = str(d.id, 64);
      clear(pillHost); pillHost.append(pill(d.status));
      const bits = [str(d.profile_label || d.profile, 40), d.platform ? OS_LABEL[String(d.platform).toLowerCase()] || str(d.platform, 20) : "", d.seen ? "seen " + ago(d.seen) : "",
        d.active_jobs ? d.active_jobs + (d.active_jobs === 1 ? " job running" : " jobs running") : "", d.address ? str(d.address, 80) : ""].filter(Boolean);
      meta.textContent = bits.join(" · ");
      const caps = capIds(d);
      capsLine.textContent = R.main ? "Brain, memory, vault and hub run here." : caps.length ? "May: " + caps.map(capTitle).join(", ") : "No permissions granted.";
      nextEl.textContent = !R.main && d.status === "busy" ? "Running a job, so it isn't checking in. Not proven online until it does." : !R.main && d.status !== "online" && d.step ? "Next: " + str(d.step, 200) : "";
      codeEl.textContent = codeLine(d);
      if (R.open) { if (locked()) R.stale = true; else build(); }
    };
    return R;
  }

  function managePanel(d, markDirty, markClean) {
    const out = [];
    const st = d.status;
    if (d.role === "main" || st === "main") {
      out.push(h("p", { class: "fl-text", text: "This is the MAIN. Nodes join it. Nothing here enrolls it, changes its permissions or revokes it." }));
      out.push(btn("Why moving the MAIN is unavailable", () => { sel.value = ""; focusSection(secTransfer); }));
      return out;
    }
    if (st === "not_enrolled" || st === "unconfigured") {
      out.push(h("p", { class: "fl-text", text: "Not connected yet. Start an invite to join it." }));
      out.push(btn("Connect this computer…", () => prefill(d, false), "strong"));
      return out;
    }
    if (st === "invite_pending" || st === "invite_expired") {
      out.push(h("p", { class: "fl-text", text: (st === "invite_expired" ? "The invite expired unused." : "An invite is waiting for the node to run the join command" + (d.invite_expires ? " (until " + new Date(d.invite_expires * 1000).toLocaleString() + ")" : "") + ".") + " The code can't be shown again." }));
      out.push(btn(st === "invite_expired" ? "Issue a new invite…" : "Replace this invite…", () => prefill(d, st === "invite_pending"), "strong"));
      out.push(revokeBlock(d, markDirty, markClean));
      return out;
    }
    out.push(capsEditor(d, markDirty, markClean));
    out.push(revokeBlock(d, markDirty, markClean));
    return out;
  }

  function capChecks(group, selected, defaults, idPrefix, onchange) {
    const boxes = {};
    const wrap = h("div", { class: "fl-caps-list" });
    catalog().forEach((c) => {
      const id = nid(idPrefix);
      const cb = h("input", { type: "checkbox", id, "aria-describedby": id + "-d" });
      cb.checked = selected.indexOf(c.id) >= 0;
      cb.addEventListener("change", onchange);
      boxes[c.id] = cb;
      wrap.append(h("div", { class: "fl-cap" }, cb,
        h("label", { for: id }, h("span", { class: "fl-cap-title", text: str(c.title, 80) }), " ",
          h("span", { class: "fl-risk " + str(c.risk, 10), text: str(c.risk, 10) + " risk" + (defaults.indexOf(c.id) >= 0 ? " · default" : "") })),
        h("p", { class: "fl-hint", id: id + "-d" }, str(c.explain, 200), c.id === "camera" ? " The node must also run “jarvis camera allow on”." : "")));
    });
    return { wrap, boxes, picked: () => Object.keys(boxes).filter((k) => boxes[k].checked).sort(), set: (ids) => { Object.keys(boxes).forEach((k) => { boxes[k].checked = ids.indexOf(k) >= 0; }); } };
  }
  function ackBox(ids, onchange) {
    const id = nid("ack");
    const cb = h("input", { type: "checkbox", id }); cb.addEventListener("change", onchange);
    const lab = h("label", { for: id });
    const wrap = h("div", { class: "fl-ack", hidden: true }, cb, lab);
    // Titles, not internal ids ("camera" -> "Camera"); hostile titles are only ever set as text.
    const ackText = (extras) => {
      const names = extras.filter((x, i) => extras.indexOf(x) === i).map(capTitle);
      if (!names.length) return "";
      const list = names.length > 1 ? names.slice(0, -1).join(", ") + " and " + names[names.length - 1] : names[0];
      return "I understand that " + list + (names.length > 1 ? " go" : " goes") + " beyond the default permissions and " + (names.length > 1 ? "give" : "gives") + " this computer more control over my data and machines.";
    };
    return { wrap, cb, sync(extras) { wrap.hidden = !extras.length; if (!extras.length) cb.checked = false; lab.textContent = ackText(extras); } };
  }

  function capsEditor(d, markDirty, markClean) {
    const prof = profileOf(d.profile) || { default_capabilities: [] };
    const defaults = arr(prof.default_capabilities);
    const blocked = actBlocked("capabilities");
    const form = h("form", { class: "fl-form", "aria-label": "Permissions for " + str(d.label || d.id, 80) });
    const result = h("div", { class: "fl-result" });
    const save = h("button", { type: "submit", class: "cx-btn strong" }, "Save permissions");
    let ack;
    const onchange = () => { markDirty(); const ex = checks.picked().filter((c) => defaults.indexOf(c) < 0); ack.sync(ex); sync(); };
    const checks = capChecks(form, capIds(d), defaults, "rc", onchange);
    ack = ackBox(defaults, onchange);
    const sync = () => { const ex = checks.picked().filter((c) => defaults.indexOf(c) < 0); save.disabled = !!blocked || busy || !checks.picked().length || (ex.length > 0 && !ack.cb.checked); };
    ack.sync(checks.picked().filter((c) => defaults.indexOf(c) < 0));
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault(); if (blocked || busy || save.disabled) return;
      const picked = checks.picked(), ex = picked.filter((c) => defaults.indexOf(c) < 0);
      const body = { node: d.id, capabilities: picked }; if (ex.length) body.acknowledge = ex;
      busy = true; save.disabled = true; clear(result); result.append(note("", "Saving…"));
      try {
        const r = await api("/api/fleet/capabilities", body);
        clear(result);
        const bits = [arr(r.added).length ? "added " + arr(r.added).join(", ") : "", arr(r.removed).length ? "removed " + arr(r.removed).join(", ") : ""].filter(Boolean);
        result.append(note("ok", r.changed === false ? "No change: those were already the permissions." : "Saved: " + (bits.join("; ") || "updated") + ". The node's credential did not change." + (r.note ? " " + str(r.note, 200) : "")));
        say("Permissions saved for " + str(d.label || d.id, 80) + ".");
        if (markClean) markClean();
        busy = false; syncForm(); loadInv();
      } catch (e) {
        clear(result); result.append(note("bad", errText(e)));
        busy = false; syncForm(); sync();
      }
    });
    sync();
    const parts = [h("h4", { text: "What this computer may do" }), checks.wrap, ack.wrap];
    if (blocked) parts.push(note("", blocked));
    parts.push(h("div", { class: "fl-actions" }, save), result);
    form.append(...parts);
    return form;
  }

  function revokeBlock(d, markDirty, markClean) {
    const det = h("details", { class: "fl-revoke" }, h("summary", { text: "Revoke access…" }));
    const inputId = nid("rv");
    const input = h("input", { type: "text", id: inputId, autocomplete: "off", spellcheck: "false", "aria-describedby": inputId + "-h" });
    const go = h("button", { type: "submit", class: "cx-btn danger" }, "Revoke " + str(d.id, 64));
    const blocked = actBlocked("revoke");
    go.disabled = true;
    const result = h("div", { class: "fl-result" });
    const f = h("form", { class: "fl-form" },
      h("p", { class: "fl-text", text: "Cuts " + str(d.label || d.id, 80) + " off: its hub credential and SSH key are removed" + (d.active_jobs ? " and " + d.active_jobs + " running job(s) are cancelled" : "") + ". It stays off until you invite it again. The MAIN cannot be revoked here." }),
      blocked ? note("", blocked) : null,
      field("Type the computer's id to confirm", input, "Type " + str(d.id, 64) + " exactly.", inputId), h("div", { class: "fl-actions" }, go), result);
    input.addEventListener("input", () => { markDirty(); go.disabled = !!blocked || busy || input.value !== d.id; });
    f.addEventListener("submit", async (ev) => {
      ev.preventDefault(); if (blocked || busy || input.value !== d.id) return;
      busy = true; go.disabled = true; clear(result); result.append(note("", "Revoking…"));
      try {
        const r = await api("/api/fleet/revoke", { node: d.id, confirm: input.value });
        clear(result);
        result.append(note("ok", "Revoked. " + (r.was_enrolled ? "Credential cut" : "It had no live credential") + (r.ssh_key_removed ? ", SSH key removed" : "") + (r.active_jobs_cancelled ? ", " + r.active_jobs_cancelled + " job(s) cancelled" : "") + "." + (r.warning ? " " + str(r.warning, 200) : "")));
        say(str(d.label || d.id, 80) + " revoked.");
        if (markClean) markClean();
        busy = false; input.value = ""; syncForm(); loadInv();
      } catch (e) { clear(result); result.append(note("bad", errText(e))); busy = false; syncForm(); go.disabled = !!blocked || input.value !== d.id; }
    });
    det.append(f);
    return det;
  }

  function focusSection(sec) { const t = sec.querySelector ? sec.querySelector("h3") : null; if (t && t.focus) { t.focus(); } if (sec.scrollIntoView) sec.scrollIntoView({ block: "start" }); }

  // ---------- connect a computer ----------
  const F = {}; // form controls, built once
  let built = false, previewed = null, previewSig = "", replaceNeeded = false, issueReq = null, issueSig = "", lastFailure = null;
  const sel = h("select", { class: "fl-select", "aria-label": "Computer to check" }); // used by transfer; declared early for the main-row shortcut

  function buildConnect() {
    const cat = catalog();
    if (!cat.length || !arr(S.inv.profiles).length) { if (!built) { clear(connectBody); connectBody.append(note("", "This console didn't report what can be connected. Refresh to try again.")); } return; }
    if (built) { syncForm(); return; }
    built = true; clear(connectBody);
    const cloudWhy = arr(S.inv.actions).find((a) => a.id === "cloud_provisioning");
    const guide = arr(S.inv.guide);
    connectBody.append(h("p", { class: "fl-text", text: "Pick what you're connecting, check what would change, then create a one-time invite. Nothing happens on the other computer until you run the join command there." }));
    if (cloudWhy && cloudWhy.why) connectBody.append(h("p", { class: "fl-fine", text: str(cloudWhy.why, 300) }));

    const form = h("form", { class: "fl-form fl-connect", novalidate: "" });
    F.form = form;
    // kind
    const kindId = nid("kind");
    const kinds = h("fieldset", { class: "fl-kinds" }, h("legend", { text: "What are you connecting?" }));
    F.kind = {};
    arr(S.inv.profiles).forEach((p, i) => {
      const id = kindId + "-" + p.id;
      const r = h("input", { type: "radio", name: kindId, value: p.id, id });
      if (i === 0) r.checked = true;
      r.addEventListener("change", () => { onKind(); });
      F.kind[p.id] = r;
      kinds.append(h("div", { class: "fl-kind" }, r, h("label", { for: id }, h("span", { class: "fl-cap-title", text: str(p.label, 40) }), h("span", { class: "fl-hint", text: str(p.about, 240) }))));
    });
    // identity
    F.node = h("input", { type: "text", id: nid("node"), autocomplete: "off", spellcheck: "false", maxlength: "64", "aria-describedby": "" });
    F.node.setAttribute("aria-describedby", F.node.id + "-hint");
    F.label = h("input", { type: "text", id: nid("label"), autocomplete: "off", maxlength: "60" });
    F.os = h("select", { id: nid("os") }, h("option", { value: "", text: "Choose…" }), OSES.map((o) => h("option", { value: o[0], text: o[1] })));
    F.addr = h("input", { type: "text", id: nid("addr"), autocomplete: "off", spellcheck: "false", placeholder: "main.example.net, 203.0.113.7" });
    F.err = h("p", { class: "fl-note bad", role: "alert", hidden: true });
    // capabilities
    F.caps = capChecks(form, [], [], "nc", () => onForm());
    F.ack = ackBox([], () => onForm());
    const capsSet = h("fieldset", { class: "fl-capset" }, h("legend", { text: "Permissions" }), h("p", { class: "fl-hint", text: "Start with the default. Anything beyond it needs your acknowledgement and can be changed later without a new invite." }), F.caps.wrap, F.ack.wrap);
    // replace
    const repId = nid("rep");
    F.rep = h("input", { type: "checkbox", id: repId }); F.rep.addEventListener("change", () => syncForm());
    F.repLab = h("label", { for: repId });
    F.repText = h("p", { class: "fl-text", id: repId + "-t" });
    F.repBox = h("div", { class: "fl-replace", hidden: true }, F.repText, h("div", { class: "fl-ack" }, F.rep, F.repLab));
    // actions
    F.check = h("button", { type: "button", class: "cx-btn", onclick: () => doPreview() }, "Check readiness");
    F.issue = h("button", { type: "submit", class: "cx-btn strong" }, "Create invite");
    F.reset = h("button", { type: "button", class: "cx-btn fl-quiet", onclick: () => resetForm(true) }, "Start over");
    F.hint = h("p", { class: "fl-hint fl-gate", id: "fl-gate" });
    F.preview = h("div", { class: "fl-preview", "aria-live": "polite" });
    F.secret = h("div", { class: "fl-secret", hidden: true });
    ["node", "label", "addr"].forEach((k) => F[k].addEventListener("input", () => onForm()));
    F.os.addEventListener("change", () => onForm());
    form.addEventListener("submit", (ev) => { ev.preventDefault(); doIssue(); });
    form.append(kinds,
      h("div", { class: "fl-grid" },
        field("Computer id", F.node, "Short name, letters, digits, - and _. This is how JARVIS refers to it.", F.node.id),
        field("Display name (optional)", F.label, "Shown in the console and on your phone. Up to 60 characters.", F.label.id),
        field("Operating system", F.os, "Decides which join command you get.", F.os.id),
        field("Where it reaches the MAIN (optional)", F.addr, "Hostnames or IPs the node can reach, up to 4, comma separated. Leave blank to let the MAIN fill in its own.", F.addr.id)),
      capsSet, F.repBox, F.err,
      h("div", { class: "fl-actions" }, F.check, F.issue, F.reset), F.hint, F.preview);
    connectBody.append(form, F.secret);
    if (guide.length) {
      const g = h("details", { class: "fl-guide" }, h("summary", { text: "How joining works, step by step" }));
      const ol = h("ol", {});
      guide.forEach((s) => ol.append(h("li", {}, h("strong", { text: str(s.title, 80) }), s.who ? h("span", { class: "fl-who", text: " (" + str(s.who, 40) + ")" }) : null, h("p", { class: "fl-hint", text: str(s.body, 400) }))));
      g.append(ol); connectBody.append(g);
    }
    onKind(true);
  }

  const kindNow = () => { for (const k in F.kind) if (F.kind[k].checked) return k; return "pc"; };
  const defaultsNow = () => arr((profileOf(kindNow()) || {}).default_capabilities);
  function onKind(first) {
    const p = kindNow();
    F.caps.set(defaultsNow());
    if (!F.os.value && p !== "pc") F.os.value = "linux";
    if (p === "pc" && F.os.value === "linux" && first) F.os.value = "";
    onForm();
  }
  function formBody() {
    const addrs = F.addr.value.split(",").map((s) => s.trim()).filter(Boolean);
    const body = { node: F.node.value.trim(), profile: kindNow(), os: F.os.value || null, capabilities: F.caps.picked() };
    if (F.label.value.trim()) body.label = F.label.value.trim();
    if (addrs.length) body.addresses = addrs;
    return body;
  }
  function validate() {
    const b = formBody();
    if (!NODE_RE.test(b.node)) return "Enter a computer id: letters, digits, - and _ only.";
    if (S.inv && b.node === (S.inv.cluster || {}).main) return "That is the MAIN. Pick another id for the computer you're adding.";
    if (!b.os) return "Choose the operating system.";
    if (b.label && b.label.length > 60) return "The display name is up to 60 characters.";
    if (b.addresses && (b.addresses.length > 4 || b.addresses.some((a) => !HOST_RE.test(a) || a === "localhost" || a.indexOf("127.") === 0))) return "Use up to 4 hostnames or IPs, none of them loopback.";
    if (!b.capabilities.length) return "Pick at least one permission.";
    return "";
  }
  const sigOf = () => JSON.stringify(formBody());
  function pendingFor(node) { const d = arr(S.inv && S.inv.devices).find((x) => x.id === node); return d && d.status === "invite_pending" ? d : null; }
  function onForm() {
    if (F.err) { F.err.hidden = true; F.err.textContent = ""; }
    const ex = F.caps.picked().filter((c) => defaultsNow().indexOf(c) < 0);
    F.ack.sync(ex);
    if (previewed && previewSig !== sigOf()) { previewed = null; clear(F.preview); F.preview.append(note("", "The form changed. Check readiness again before creating the invite.")); }
    if (issueReq && issueSig !== sigOf() && !secret && !uncertain) { issueReq = null; issueSig = ""; lastFailure = null; }
    syncForm();
  }
  function syncForm() {
    if (!built) return;
    const v = validate(), b = formBody(), ex = F.caps.picked().filter((c) => defaultsNow().indexOf(c) < 0);
    const pend = pendingFor(b.node);
    const block = !secret ? actBlocked("invite") : "";
    const unc = !!(uncertain && uncertain.node === b.node);
    const needRep = replaceNeeded || !!pend || unc;
    F.repBox.hidden = !needRep;
    if (needRep) {
      F.repText.textContent = unc ? "The earlier request for " + b.node + " (…" + uncertain.req.slice(-6) + ") stopped after a credential change and could not be verified. Check that computer before replacing; nothing is retried or replaced automatically." : replaceNeeded && !pend ? "The earlier invite or request for " + b.node + " is still on record and its code can't be shown again." : "An invite for " + b.node + " is already pending" + (pend.invite_expires ? " until " + new Date(pend.invite_expires * 1000).toLocaleString() : "") + ". Its code can't be shown again.";
      F.repLab.textContent = unc ? "I checked. Make a new request for it." : "Replace it. The old code stops working and a new one is made.";
    } else F.rep.checked = false;
    let why = "";
    if (busy) why = "Working…";
    else if (secret) why = "Finish with the code above first (Done), then you can make another invite.";
    else if (block) why = block;
    else if (v) why = v;
    else if (!previewed || previewSig !== sigOf()) why = "Check readiness first so you can see what changes.";
    else if (!previewed.ready) why = "Not ready: fix the problems listed above, then check again.";
    else if (ex.length && !F.ack.cb.checked) why = "Acknowledge the extra permissions to continue.";
    else if (needRep && !F.rep.checked) why = "Tick “Replace it” to confirm you want a new code.";
    F.issue.disabled = !!why;
    F.check.disabled = busy || !!v || !!block;
    if (F.reset) F.reset.disabled = !!busy;
    if (F.form) F.form.setAttribute("aria-busy", busy ? "true" : "false");
    F.issue.textContent = needRep ? "Replace invite" : lastFailure && retryable(lastFailure) && issueReq ? "Retry same request" : "Create invite";
    F.hint.textContent = why && why !== "Working…" ? why : "";
  }
  function resetForm(announce) {
    if (secret) clearSecret("");
    F.node.value = ""; F.label.value = ""; F.addr.value = ""; F.os.value = ""; F.rep.checked = false;
    Object.keys(F.kind).forEach((k, i) => { F.kind[k].checked = i === 0; });
    replaceNeeded = false; previewed = null; previewSig = ""; issueReq = null; issueSig = ""; lastFailure = null;
    clear(F.preview); onKind(false);
    if (announce) say("Form cleared.");
  }
  function prefill(d, replace) {
    if (!built) return;
    if (secret) clearSecret("A new invite was started.");
    resetForm(false);
    if (F.kind[d.profile]) { F.kind[d.profile].checked = true; onKind(false); }
    F.node.value = str(d.id, 64);
    if (d.label && d.label !== d.id) F.label.value = str(d.label, 60);
    if (d.platform && OS_LABEL[String(d.platform).toLowerCase()]) F.os.value = String(d.platform).toLowerCase();
    replaceNeeded = !!replace; onForm();
    focusSection(secConnect); if (F.node.focus) F.node.focus();
    say("Connect form filled in for " + str(d.label || d.id, 60) + ".");
  }

  async function doPreview() {
    if (busy) return;
    const v = validate();
    if (v) { F.err.textContent = v; F.err.hidden = false; return; }
    F.err.hidden = true;
    busy = true; syncForm(); clear(F.preview); F.preview.append(note("", "Checking…"));
    const body = formBody(); delete body.acknowledge;
    try {
      const p = await api("/api/fleet/preview", body);
      previewed = p; previewSig = JSON.stringify(formBody());
      renderPreview(p);
      say(p.ready && !arr(p.problems).length ? "Ready to create an invite." : "Review the conditions listed before creating an invite.");
    } catch (e) { previewed = null; clear(F.preview); F.preview.append(note("bad", errText(e), retryable(e) ? btn("Check again", () => doPreview(), "fl-inline") : null)); }
    busy = false; syncForm();
  }
  function renderPreview(p) {
    const box = F.preview; clear(box);
    const probs = arr(p.problems);
    box.append(h("p", { class: "fl-verdict " + (p.ready && !probs.length ? "ok" : "bad"),
      text: !p.ready ? "Not ready. Nothing has changed." : probs.length ? "Review required. Replace the pending invite only if you intend to cancel its old code." : "Ready. Nothing has changed yet." }));
    if (probs.length) { const ul = h("ul", { class: "fl-problems" }); probs.forEach((x) => ul.append(h("li", { text: str(x.why || x.code, 300) }))); box.append(ul); }
    const checks = arr(p.checks);
    if (checks.length) {
      const ul = h("ul", { class: "fl-checks", "aria-label": "Readiness checks" });
      checks.forEach((c) => ul.append(h("li", {}, h("span", { class: "fl-chk " + str(c.state, 10), text: c.state === "ok" ? "OK" : c.state === "problem" ? "Fix" : "Later" }), " ", str(c.why, 300))));
      box.append(ul);
    }
    const steps = arr(p.steps);
    if (steps.length) {
      const ol = h("ol", { class: "fl-steps", "aria-label": "Who does each step" });
      const word = { done: "Done", todo: "To do", pending: "Pending", waiting: "Waiting", blocked: "Blocked" };
      steps.forEach((s) => {
        const name = str(p.node, 64) || "the new computer";
        const where = s.kind === "guide" ? "On " + name + ", the computer you are adding. Not on this MAIN."
          : s.kind === "action" ? "You, in this console, on the MAIN." : "The MAIN does this.";
        ol.append(h("li", {}, h("span", { class: "fl-chk " + str(s.state, 12), text: word[s.state] || "To do" }), " ",
          h("strong", { text: str(s.title, 80) }), h("span", { class: "fl-who", text: " " + where })));
      });
      box.append(h("h4", { text: "Who does each step" }), ol);
    }
    const cmds = arr(p.node_commands).filter((c) => c && typeof c.label === "string");
    if (cmds.length) {
      box.append(h("h4", { text: "Commands for " + (str(p.node, 64) || "the new computer") }),
        h("p", { class: "fl-fine", text: "Run these on that computer, in its JARVIS folder, after you have a code. The placeholder stays on screen. This page never runs it." }));
      cmds.forEach((c) => box.append(h("div", { class: "fl-cmd" }, h("p", { class: "fl-hint", text: str(c.label, 80) }),
        typeof c.command === "string" ? h("pre", { class: "fl-pre", text: c.command.slice(0, 400) }) : h("p", { class: "fl-fine", text: "No command for this OS." }))));
    }
    const wc = p.would_change || {};
    const dl = h("dl", { class: "fl-changes" }); let wcCount = 0;
    [["config", "Config"], ["authorized_keys", "SSH keys"], ["credential", "Credential"], ["network", "Network"]].forEach(([k, l]) => { if (wc[k]) { wcCount++; dl.append(h("dt", { text: l }), h("dd", { text: str(wc[k], 300) })); } });
    if (wcCount) box.append(h("h4", { text: "What creating the invite changes" }), dl);
    const lim = arr(p.limits);
    if (lim.length) { const ul = h("ul", { class: "fl-limits" }); lim.forEach((x) => ul.append(h("li", { text: str(x, 300) }))); box.append(ul); }
  }

  async function doIssue() {
    if (busy || F.issue.disabled || secret) return;
    const b = formBody(), ex = F.caps.picked().filter((c) => defaultsNow().indexOf(c) < 0);
    const unc = !!(uncertain && uncertain.node === b.node);
    const replace = (replaceNeeded || !!pendingFor(b.node) || unc) && F.rep.checked;
    // same request id for a failed/lost attempt of the same form; a deliberate replacement always gets a new one
    if (replace || !issueReq || issueSig !== sigOf()) { issueReq = rid(); issueSig = sigOf(); }
    const body = Object.assign({ request_id: issueReq }, b);
    if (ex.length) body.acknowledge = ex;
    if (replace) body.replace_pending = true;
    busy = true; syncForm(); F.err.hidden = true;
    say("Creating the invite…");
    let r = null;
    try { r = await api("/api/fleet/invite", body); lastFailure = null; }
    catch (e) {
      lastFailure = e; busy = false;
      if (e && (e.code === "already_issued" || e.code === "invite_pending")) { replaceNeeded = true; issueReq = null; issueSig = ""; lastFailure = null; }
      syncForm();
      clear(F.preview);
      F.preview.append(note("bad", "Invite not confirmed. " + errText(e)));
      if (e && e.code === "invite_failed" && e.status != null && !retryable(e)) uncertain = { node: b.node, req: String(issueReq || "") };
      if (e && e.status != null && !retryable(e) && !replaceNeeded) previewed = null;
      syncForm();
      F.err.textContent = errText(e) + (retryable(e) && !replaceNeeded ? " Use “Retry same request”; it can't create a second code." : "");
      F.err.hidden = false; say(F.err.textContent);
      loadInv();
      return;
    }
    busy = false;
    if (!r || typeof r.code !== "string" || !CODE_RE.test(r.code)) {
      replaceNeeded = true; issueReq = null; issueSig = "";
      F.err.textContent = "The invite was created but the reply had no usable code, and it can't be recovered. Replace the invite to get a new one.";
      r = null; syncForm(); F.err.hidden = false; loadInv(); return;
    }
    uncertain = null;
    showSecret(r); r = null;
    loadInv();
  }

  // ---------- the invite code (private) ----------
  function showSecret(r) {
    const cmds = arr(r.node_commands).filter((c) => c && typeof c.label === "string").map((c) => ({ label: str(c.label, 80), command: typeof c.command === "string" ? c.command.slice(0, 400) : null }));
    secret = { code: r.code, commands: cmds, expiresAt: typeof r.expires_at === "number" ? r.expires_at : 0, node: str(r.node, 64), timer: null };
    const codeEl = h("code", { class: "fl-code", id: nid("code"), tabindex: "0", "aria-label": "Invite code for " + secret.node });
    codeEl.textContent = secret.code;
    const out = h("div", { class: "fl-copy-out", role: "status" });
    const expEl = h("p", { class: "fl-expiry" });
    const title = h("h4", { id: nid("sh"), tabindex: "-1", text: "Invite code for " + (secret.node || "the new computer") });
    const body = h("div", { class: "fl-secret-in" }, title,
      h("p", { class: "fl-text", text: "Shown once. It works one time and can't be shown again. It is not saved anywhere. Copy it now." }), expEl, codeEl,
      h("div", { class: "fl-actions" },
        btn("Copy code", () => copy(secret && secret.code, "Code copied. Your clipboard keeps it until you copy something else.", out, codeEl), "strong"),
        btn("Copy join command", () => { const c = secret && secret.commands.find((x) => x.command && x.command.indexOf(PLACEHOLDER) >= 0); copy(c ? c.command.split(PLACEHOLDER).join(secret.code) : null, "Join command copied with the code in it.", out, codeEl); }),
        btn("Done: clear the code", () => { clearSecret("Code cleared."); resetForm(false); if (F.node.focus) F.node.focus(); }, "fl-quiet")), out);
    const steps = h("ol", { class: "fl-join" });
    steps.append(h("li", { text: "On " + (secret.node || "the computer you're connecting") + ", open a terminal in that computer's JARVIS folder. Not on this MAIN." }),
      h("li", { text: "Run the join command there (use “Copy join command”, which has the code filled in). This console never runs it." }),
      h("li", { text: "On that same computer, run the check command, then come back here. It shows Online once it has connected." }));
    body.append(steps);
    secret.commands.forEach((c) => body.append(h("div", { class: "fl-cmd" }, h("p", { class: "fl-hint", text: c.label }), c.command ? h("pre", { class: "fl-pre", text: c.command }) : h("p", { class: "fl-fine", text: "No command for this OS." }))));
    clear(F.secret); F.secret.append(body); F.secret.hidden = false;
    const tick = () => { if (!secret) return; if (secret.expiresAt && secret.expiresAt * 1000 <= Date.now()) { clearSecret("The invite expired and the code was cleared."); return; } expEl.textContent = secret.expiresAt ? "Expires in " + left(secret.expiresAt) + " (" + new Date(secret.expiresAt * 1000).toLocaleString() + "). One use." : "One use."; };
    tick(); secret.timer = setInterval(tick, 15000);
    if (title.focus) title.focus();
    say("Invite code ready for " + (secret.node || "the new computer") + ". Copy it now; it can't be shown again.");
    syncForm();
  }
  async function copy(text, okMsg, out, codeEl) {
    if (!text) { out.textContent = "Nothing to copy for this OS."; return; }
    try { await navigator.clipboard.writeText(text); out.textContent = okMsg; }
    catch (e) {
      out.textContent = "Couldn't copy automatically. The code is selected; copy it by hand.";
      try { const rg = document.createRange(); rg.selectNodeContents(codeEl); const s = window.getSelection(); s.removeAllRanges(); s.addRange(rg); } catch (e2) { /* selection unsupported */ }
    }
  }
  function clearSecret(msg) {
    if (secret && secret.timer) clearInterval(secret.timer);
    secret = null; issueReq = null; issueSig = "";
    if (F.secret) { clear(F.secret); F.secret.hidden = true; }
    if (msg) say(msg);
    syncForm();
  }

  // ---------- memory archive vs Git ----------
  // Polls reuse the panel when this string matches. checked_at is painted on its own.
  let vaultSeen = "", vaultBump = 0;
  const vaultWhen = (t) => !t ? "" : ago(typeof t === "number" ? t : Date.parse(t) / 1000);
  function vaultCheckedText(v) { return "Checked " + vaultWhen(v && v.checked_at) + "."; }
  function vaultSignature() {
    if (!S.vault) return S.loadingVault ? "loading" : S.vaultErr ? "err:" + errText(S.vaultErr) : "empty";
    let raw;
    try { raw = JSON.parse(JSON.stringify(S.vault)); }
    catch (e) { return "bust-" + (++vaultBump); }
    delete raw.checked_at;
    const arch = raw.archive;
    if (arch && typeof arch.oldest_pending_age_s === "number") arch.oldest_pending_age_s = Math.round(arch.oldest_pending_age_s / 60);
    const git = raw.git;
    if (git && typeof git.last_fetch_at === "number") git.fetch_stale = (Date.now() / 1000 - git.last_fetch_at) > 3600;
    return JSON.stringify(raw) + "\n" + (S.vaultErr ? errText(S.vaultErr) : "");
  }
  function walkEls(root, fn) {
    const stack = [root];
    while (stack.length) {
      const n = stack.pop();
      if (!n || !n.tagName) continue;
      fn(n);
      const kids = n.children || [];
      for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
    }
  }
  function findAttr(root, name, value) {
    let found = null;
    walkEls(root, (n) => { if (!found && n.getAttribute && n.getAttribute(name) === value) found = n; });
    return found;
  }
  function vaultFocus(root) {
    const el = document.activeElement;
    if (!el || el === root || !el.tagName || !root.contains || !root.contains(el)) return null;
    const saved = { tag: el.tagName, id: el.id || "", summary: el.tagName === "SUMMARY" ? (el.textContent || "") : "", button: el.tagName === "BUTTON" ? (el.textContent || "") : "" };
    const path = [];
    let node = el;
    while (node && node !== root) {
      const parent = node.parentElement;
      if (!parent || !parent.children) return saved;
      const idx = Array.prototype.indexOf.call(parent.children, node);
      if (idx < 0) return saved;
      path.push(idx);
      node = parent;
    }
    if (node === root) saved.path = path.reverse();
    return saved;
  }
  function putVaultFocus(root, saved) {
    if (!saved) return;
    let el = null;
    if (saved.id) walkEls(root, (n) => { if (!el && n.id === saved.id) el = n; });
    if (!el && saved.summary) walkEls(root, (n) => { if (!el && n.tagName === "SUMMARY" && n.textContent === saved.summary) el = n; });
    if (!el && saved.button) walkEls(root, (n) => { if (!el && n.tagName === "BUTTON" && n.textContent === saved.button) el = n; });
    if (!el && saved.path) {
      let node = root;
      for (let i = 0; i < saved.path.length && node; i++) node = node.children && node.children[saved.path[i]];
      if (node && node.tagName === saved.tag) el = node;
    }
    if (!el || typeof el.focus !== "function") return;
    try { el.focus({ preventScroll: true }); }
    catch (err) { try { el.focus(); } catch (err2) { /* nothing to focus */ } }
  }
  function pageScroll(el) {
    const saved = [];
    let n = el;
    while (n) {
      const top = n.scrollTop || 0, left = n.scrollLeft || 0;
      if (top || left) saved.push({ el: n, top: top, left: left });
      n = n.parentElement || null;
    }
    return saved;
  }
  function putScroll(saved) {
    for (let i = 0; i < saved.length; i++) {
      if (saved[i].top) saved[i].el.scrollTop = saved[i].top;
      if (saved[i].left) saved[i].el.scrollLeft = saved[i].left;
    }
  }
  function innerScroll(root) {
    const out = [];
    const walk = (el, path) => {
      if (!el || !el.tagName) return;
      const top = el.scrollTop || 0, left = el.scrollLeft || 0;
      if ((top || left) && path.length) out.push({ path: path.slice(), top: top, left: left, tag: el.tagName });
      const kids = el.children || [];
      for (let i = 0; i < kids.length; i++) walk(kids[i], path.concat(i));
    };
    walk(root, []);
    return out;
  }
  function putInnerScroll(root, saved) {
    for (let i = 0; i < saved.length; i++) {
      let node = root;
      const path = saved[i].path;
      for (let j = 0; j < path.length && node; j++) node = node.children && node.children[path[j]];
      if (!node || node.tagName !== saved[i].tag) continue;
      if (saved[i].top) node.scrollTop = saved[i].top;
      if (saved[i].left) node.scrollLeft = saved[i].left;
    }
  }
  function openDetailLabels(root) {
    const labels = [];
    walkEls(root, (n) => {
      if (n.tagName !== "DETAILS" || !(n.open || (n.hasAttribute && n.hasAttribute("open")))) return;
      const kids = n.children || [];
      for (let i = 0; i < kids.length; i++) if (kids[i].tagName === "SUMMARY" && kids[i].textContent) labels.push(kids[i].textContent);
    });
    return labels;
  }
  function reopenDetails(root, labels) {
    if (!labels.length) return;
    walkEls(root, (n) => {
      if (n.tagName !== "DETAILS") return;
      const kids = n.children || [];
      let label = "";
      for (let i = 0; i < kids.length; i++) if (kids[i].tagName === "SUMMARY") label = kids[i].textContent || "";
      if (labels.indexOf(label) < 0) return;
      n.open = true;
      if (n.setAttribute) n.setAttribute("open", "");
    });
  }
  function renderVault() {
    const sig = vaultSignature();
    if (sig === vaultSeen && vaultBody.children.length) {
      const label = findAttr(vaultBody, "data-fl-checked", "1");
      if (label && S.vault) {
        const next = vaultCheckedText(S.vault);
        if (label.textContent !== next) label.textContent = next;
      }
      return;
    }
    const focus = vaultFocus(vaultBody);
    const pages = pageScroll(vaultBody);
    const scrolled = innerScroll(vaultBody);
    const open = openDetailLabels(vaultBody);
    clear(vaultBody);
    const finish = () => { reopenDetails(vaultBody, open); putInnerScroll(vaultBody, scrolled); putVaultFocus(vaultBody, focus); putScroll(pages); vaultSeen = sig; };
    if (S.loadingVault && !S.vault) { vaultBody.append(note("", "Loading archive status…")); finish(); return; }
    if (S.vaultErr && !S.vault) { vaultBody.append(note("bad", "Couldn't read archive status. " + errText(S.vaultErr), btn("Retry", () => loadVault(), "fl-inline"))); finish(); return; }
    const v = S.vault; if (!v) { finish(); return; }
    if (S.vaultErr) vaultBody.append(note("bad", "Couldn't refresh. Showing the last reading. " + errText(S.vaultErr)));
    const a = v.archive || {}, g = v.git, vv = v.vault || {};
    const n = (x) => (typeof x === "number" ? x : null);
    const ARCH = { current: "Current", pending: "Items waiting to be archived", held: "Items held back", blocked: "Blocked", unverified: "Not verified yet", partial: "Partly verified", disabled: "Off", error: "Has errors" };
    const archiveState = a.archive_held > 0 ? "held" : a.archive_errors > 0 ? "error" : a.archive_pending > 0 ? "pending" : n(a.archive_baseline_pending) > 0 ? "pending" : a.state;
    const filesUnverified = a.files_stale === true || a.state === "partial";
    const archBlock = h("div", { class: "fl-block" }, h("h4", { text: "Memory archive" }),
      h("p", { class: "fl-line" }, h("span", { class: "cx-pill" + (archiveState === "current" ? " ok" : archiveState === "error" || archiveState === "blocked" || archiveState === "partial" ? " warn" : ""), text: ARCH[archiveState] || str(archiveState, 30) || "Unknown" }), " ",
        a.capture_enabled ? (a.capture_complete ? "Database capture is on for every store." : "Capture is not enabled for every database store.") + (filesUnverified ? " Plans, runs and knowledge files are not verified." : "") : "Capture is off."));
    const facts = h("dl", { class: "fl-facts" });
    const add = (k, val) => { if (val !== null && val !== undefined && val !== "") facts.append(h("dt", { text: k }), h("dd", { text: String(val) })); };
    add("Waiting to archive", n(a.archive_pending)); add("Held back", n(a.archive_held)); add("Errors", n(a.archive_errors));
    add("Oldest waiting", n(a.oldest_pending_age_s) != null ? Math.round(a.oldest_pending_age_s / 60) + " min" : null);
    add("Baseline still to read", n(a.archive_baseline_pending));
    if (a.files_stale === true) add("Files", "Not verified since the last sync");
    add("Last archived", a.last_success ? vaultWhen(a.last_success) : "never");
    add("Gate", a.gate ? str(a.gate, 40) : null); add("Stage", a.stage ? str(a.stage, 40) : null);
    if (a.has_error) add("Needs attention", "The archive reported an error.");
    archBlock.append(h("p", { class: "fl-fine", text: "The archive is JARVIS's own capture of memory, history and tasks into the vault folder. It is separate from Git." }));
    archBlock.append(h("details", { class: "fl-diag" }, h("summary", { text: "Archive diagnostic details" }), facts));

    const gitBlock = h("div", { class: "fl-block" }, h("h4", { text: "Git copy of the vault" }));
    const gFacts = h("dl", { class: "fl-facts" });
    const gadd = (k, val) => { if (val !== null && val !== undefined && val !== "") gFacts.append(h("dt", { text: k }), h("dd", { text: String(val) })); };
    if (!vv.configured || !vv.exists) gitBlock.append(h("p", { class: "fl-line", text: "No vault folder is configured here." }));
    else if (!g) gitBlock.append(h("p", { class: "fl-line", text: vv.is_git ? "Git status isn't available." : "The vault folder is not a Git repository." }));
    else {
      const rem = g.remote;
      gitBlock.append(h("p", { class: "fl-line", text: (g.branch ? "Branch " + str(g.branch, 60) : "Detached") + (rem ? " · remote " + str(rem.name, 30) + " (" + str(rem.provider || rem.host, 30) + "/" + str(rem.repo, 80) + ") is set up" : " · no remote") }));
      const d = g.dirty || {};
      const un = [n(d.staged) ? d.staged + " staged" : "", n(d.modified) ? d.modified + " changed" : "", n(d.untracked) ? d.untracked + " new" : "", n(d.conflicted) ? d.conflicted + " conflicted" : ""].filter(Boolean);
      const unpublished = g.clean ? "None. The working folder matches its last commit." : un.join(", ") + " in the folder, not committed or pushed.";
      gitBlock.append(h("p", { class: "fl-line", text: "Unpublished changes: " + unpublished }));
      let fetchLine = "Ahead / behind is unknown: no upstream branch is set.";
      if (g.upstream_set) {
        const fetched = g.last_fetch_at ? vaultWhen(g.last_fetch_at) : "";
        const stale = !g.last_fetch_at || (typeof g.last_fetch_at === "number" && (Date.now() / 1000 - g.last_fetch_at) > 3600);
        fetchLine = (n(g.ahead) == null || n(g.behind) == null ? "Ahead / behind is unknown" : n(g.ahead) + " ahead, " + n(g.behind) + " behind") + ", as of the last fetch" + (fetched ? " (" + fetched + ")" : ", which never ran, so these numbers can't be trusted") + (stale ? ". Treat that fetch as stale." : ".");
      }
      gitBlock.append(h("p", { class: "fl-line", text: fetchLine }));
      gitBlock.append(h("p", { class: "fl-line", text: "Remote reachable: Not checked. This view never uses the network." }));
      gadd("Staged", n(d.staged)); gadd("Changed", n(d.modified)); gadd("New files", n(d.untracked)); gadd("Conflicted", n(d.conflicted));
      if (rem && rem.credentials_embedded === false) gadd("Saved remote", "No password is stored in the remote setting.");
      gadd("Vault editing", vv.write_enabled ? "Writing into the vault is on." : "Writing into the vault is off.");
    }
    const vis = v.visibility || {};
    if (vis.note) gadd("Visibility", str(vis.note, 300));
    gitBlock.append(h("p", { class: "fl-fine", text: "A configured remote doesn't mean your changes are published or backed up. JARVIS doesn't commit or push for you." }));
    gitBlock.append(h("details", { class: "fl-diag" }, h("summary", { text: "Git diagnostic details" }), gFacts));
    vaultBody.append(h("div", { class: "fl-two" }, archBlock, gitBlock));
    arr(v.problems).forEach((p) => vaultBody.append(note("", str(p, 200))));
    vaultBody.append(h("p", { class: "fl-fine", "data-fl-checked": "1", text: vaultCheckedText(v) }));
    finish();
  }

  // ---------- MAIN transfer (preview only) ----------
  let transferBuilt = false, transferSig = "";
  const tOut = h("div", { class: "fl-preview", "aria-live": "polite" });
  const tGo = h("button", { type: "button", class: "cx-btn", onclick: () => doTransferPreview() }, "Check readiness");
  function renderTransfer() {
    const devs = arr(S.inv && S.inv.devices).filter((d) => d.role !== "main" && d.status !== "main");
    const sig = devs.map((d) => [d.id, d.label, d.profile].join("\t")).join("|");
    if (!transferBuilt) {
      transferBuilt = true;
      const selId = nid("tsel"); sel.id = selId;
      clear(transferBody);
      transferBody.append(
        h("p", { class: "fl-text", text: "Moving the MAIN to another computer is not available yet. Checking readiness only lists what a safe handoff still lacks. It does not move the MAIN, and the button beside it never will." }),
        h("div", { class: "fl-grid" }, field("Move the MAIN to", sel, "", selId)),
        h("div", { class: "fl-actions" }, tGo, h("button", { type: "button", class: "cx-btn", disabled: true, "aria-describedby": "fl-tx-why" }, "Move MAIN (not available)")),
        h("p", { class: "fl-hint", id: "fl-tx-why", text: "Not built. A lease, fencing and a fresh owner proof are missing, so two computers could both act as MAIN. Editing the config by hand moves no data." }), tOut);
    }
    if (sig !== transferSig) {
      transferSig = sig;
      const keep = sel.value;
      clear(sel); sel.append(h("option", { value: "", text: devs.length ? "Choose a computer…" : "No other computers" }));
      devs.forEach((d) => sel.append(h("option", { value: d.id, text: str(d.label || d.id, 80) + (d.profile === "cloud" ? " (cloud, can't be MAIN)" : "") })));
      sel.value = devs.some((d) => d.id === keep) ? keep : "";
    }
    tGo.disabled = !devs.length || busy;
  }
  async function doTransferPreview() {
    if (busy || !sel.value) { if (!sel.value) { clear(tOut); tOut.append(note("", "Choose a computer first.")); } return; }
    busy = true; tGo.disabled = true; clear(tOut); tOut.append(note("", "Checking…"));
    try {
      const p = await api("/api/fleet/main-transfer/preview", { target: sel.value });
      clear(tOut);
      tOut.append(h("p", { class: "fl-verdict bad", text: "Blocked. Nothing was changed." }), h("p", { class: "fl-text", text: str(p.why, 300) }));
      const rd = arr(p.readiness);
      if (rd.length) { const ul = h("ul", { class: "fl-checks", "aria-label": "Target checks" }); rd.forEach((c) => ul.append(h("li", {}, h("span", { class: "fl-chk " + str(c.state, 10), text: c.state === "ok" ? "OK" : c.state === "problem" ? "Fix" : "Later" }), " ", str(c.why, 300)))); tOut.append(ul); }
      const ms = arr(p.missing);
      if (ms.length) { tOut.append(h("h4", { text: "Still missing (" + ms.length + ")" })); const ul = h("ul", { class: "fl-missing" }); ms.forEach((m) => ul.append(h("li", {}, h("strong", { text: str(m.id, 40).replace(/_/g, " ") }), " " + str(m.why, 300)))); tOut.append(ul); }
      const ph = arr(p.phases);
      if (ph.length) { tOut.append(h("h4", { text: "What a safe handoff would do" })); const ol = h("ol", { class: "fl-limits" }); ph.forEach((x) => ol.append(h("li", { text: str(x, 120) }))); tOut.append(ol); }
      const rk = arr(p.risks);
      if (rk.length) { const ul = h("ul", { class: "fl-limits" }); rk.forEach((x) => ul.append(h("li", { text: str(x, 300) }))); tOut.append(h("h4", { text: "Risks of doing it by hand" }), ul); }
      say("Move MAIN check: blocked. " + ms.length + " things are still missing.");
    } catch (e) { clear(tOut); tOut.append(note("bad", errText(e), retryable(e) ? btn("Check again", () => doTransferPreview(), "fl-inline") : null)); }
    busy = false; tGo.disabled = !sel.value;
  }

  // ---------- lifecycle ----------
  let started = false;
  const start = () => { if (!started) { started = true; refreshAll(false); } };
  const stop = () => { if (secret) clearSecret("The invite code was cleared because you left this page."); };
  if (viewEl && typeof MutationObserver === "function") {
    const mo = new MutationObserver(() => { if (viewEl.hasAttribute("hidden")) stop(); else start(); });
    mo.observe(viewEl, { attributes: true, attributeFilter: ["hidden"] });
  }
  if (typeof window.addEventListener === "function") window.addEventListener("pagehide", stop);
  // Polls pause while the tab is hidden and catch up once when it is shown again.
  let invTimer = 0, vaultTimer = 0;
  function armPolls() {
    if (invTimer) clearInterval(invTimer);
    if (vaultTimer) clearInterval(vaultTimer);
    invTimer = vaultTimer = 0;
    if (typeof document.hidden === "boolean" && document.hidden) return;
    invTimer = setInterval(() => { if (started && visible() && !busy && !S.loadingInv) loadInv(); }, POLL_MS);
    vaultTimer = setInterval(() => { if (started && visible() && !S.loadingVault) loadVault(); }, POLL_MS * 3);
  }
  if (typeof document.addEventListener === "function") {
    document.addEventListener("visibilitychange", () => {
      armPolls();
      if (document.hidden || !started || !visible()) return;
      if (!busy && !S.loadingInv) loadInv();
      if (!S.loadingVault) loadVault();
    });
  }
  armPolls();
  if (visible()) start();
  window.JarvisFleet = { refresh: () => refreshAll(false), clearSecret: () => clearSecret("Code cleared.") };
})();
