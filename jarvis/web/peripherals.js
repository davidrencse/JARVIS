// Connect & share (Devices): the cluster link with its fixes, and per computer the shared
// mouse/keyboard (Lan Mouse) and clipboard halves, each switchable from here. Data from
// /api/peripherals; every button posts to the same path. Works from the main or a node.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const { api, esc, icon, toast } = J;
  const $ = (s, r = document) => r.querySelector(s);
  const root = $("#peripherals");
  if (!root) return;

  const SIDES = ["left", "right", "top", "bottom"];
  // Doctor says "last seen 67499 s ago"; read it as a time.
  const span = (s) => s < 90 ? `${s} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} days`;
  const human = (text) => String(text || "").replace(/(\d+) s ago/, (_, n) => span(Number(n)) + " ago");
  const view = { data: null, at: 0, loading: null, busy: new Set(), invite: null, error: "", timer: 0 };

  async function load() {
    if (view.loading) return view.loading;
    view.loading = api("/api/peripherals")
      .then((d) => { view.data = d; view.at = Date.now(); view.error = ""; })
      .catch((e) => { view.error = e.message; })
      .finally(() => { view.loading = null; draw(); });
    draw();
    return view.loading;
  }

  async function act(key, body, done) {
    if (view.busy.has(key)) return;
    view.busy.add(key); draw();
    try {
      const r = await api("/api/peripherals", body);
      if (done) done(r);
    } catch (e) { toast(e.message); }
    finally { view.busy.delete(key); }
    await load();
  }

  const word = (text, ok) => `<span class="state-word ${ok === true ? "ok" : ok === false ? "warn" : ""}">${esc(text)}</span>`;
  const btn = (key, label, attrs, primary, locked = false) => {
    const off = view.busy.has(key) || locked;
    return `<button type="button" class="pe-btn${primary ? " primary" : ""}" data-key="${esc(key)}" ${attrs}${off ? " disabled" : ""}${view.busy.has(key) ? " aria-busy=\"true\"" : ""}>${esc(view.busy.has(key) ? "Working…" : label)}</button>`;
  };
  // The guard's projection. A phone's network is not in it. enforced false is the off switch.
  const heldApart = (sh) => sh.state === "apart" && sh.enforced !== false;

  function lanRow(d) {
    const lan = d.lan_mouse;
    if (!lan) return row("keyboard", "Mouse & keyboard", word(d.lan_mouse_error === "offline" ? "offline" : "no answer", false),
                         d.lan_mouse_error && d.lan_mouse_error !== "offline" ? `<p class="pe-err">${esc(d.lan_mouse_error)}</p>` : "");
    const peers = (lan.peers || []).map((p) => `${esc(p.hostname || "?")} on the ${esc(p.position || "?")}`).join(", ");
    const key = `lan:${d.id}`;
    // The guard's reading only. Apart hides the start; unknown says so and leaves the button.
    const sh = view.data?.sharing || {};
    const held = heldApart(sh);
    const label = held && !lan.running ? "off while apart" : held && lan.running ? "on by hand" : lan.running ? "running" : "stopped";
    const note = held ? `<p class="pe-note">Unavailable while the laptops are apart${sh.why ? ` (${esc(sh.why)})` : ""}. Remote control still works.${sh.override && lan.running ? " It was started by hand and will not be turned back on if it stops." : ""}</p>`
      : sh.state === "unknown" ? `<p class="pe-note">Can't tell if the laptops are together: ${esc(sh.why || "no reading yet")}</p>` : "";
    return row("keyboard", "Mouse & keyboard", word(label, held && !lan.running ? null : lan.running),
      `<p class="pe-note">${peers ? "Paired with " + peers : "Not paired yet"}${lan.build ? ` · <span class="mono">${esc(lan.build)}</span>` : ""}</p>${note}`,
      btn(key, lan.running ? "Stop" : "Start", `data-do="lan_mouse" data-dev="${esc(d.id)}" data-action="${lan.running ? "stop" : "start"}"`, !lan.running && !held, held && !lan.running));
  }

  function clipRow(d) {
    const c = d.clipboard;
    if (!c) return row("paperclip", "Clipboard", word(d.clipboard_error === "offline" ? "offline" : "no answer", false),
                       d.clipboard_error && d.clipboard_error !== "offline" ? `<p class="pe-err">${esc(d.clipboard_error)}</p>` : "");
    const state = c.running ? (c.linked ? "sharing" : "running") : "stopped";
    return row("paperclip", "Clipboard", word(state, c.running ? (c.linked ? true : null) : false),
      `<p class="pe-note">${c.autostart ? "Starts at login" : "Doesn't start at login"}</p>`,
      btn(`clip:${d.id}`, c.running ? "Stop" : "Start", `data-do="clipboard" data-dev="${esc(d.id)}" data-action="${c.running ? "stop" : "start"}"`, !c.running) +
      btn(`clipauto:${d.id}`, c.autostart ? "Don't start at login" : "Start at login", `data-do="clipboard" data-dev="${esc(d.id)}" data-action="${c.autostart ? "autostart-off" : "autostart-on"}"`));
  }

  const row = (ic, title, status, body = "", actions = "") =>
    `<div class="pe-row"><div class="pe-row-head">${icon(ic)}<b>${esc(title)}</b>${status}</div>${body}${actions ? `<div class="pe-actions">${actions}</div>` : ""}</div>`;

  function machine(d) {
    const link = d.here ? word("this computer", true) : d.online === true ? word("online", true)
      : d.online === false ? word("offline", false) : word("not checked");
    return `<section class="pe-machine" data-online="${d.here || d.online !== false}">
      <header>${icon("laptop-minimal")}<h3>${esc(d.id)}</h3>${d.role ? `<span class="pe-role mono">${esc(d.role)}</span>` : ""}${link}</header>
      <p class="pe-label">${esc(d.label)}</p>
      ${lanRow(d)}${clipRow(d)}</section>`;
  }

  function pairBox(o) {
    const others = o.devices.filter((d) => !d.here);
    if (!others.length) return "";
    if (heldApart(o.sharing || {})) return `<div class="pe-block"><h4>Arrange mouse &amp; keyboard</h4>
      <p class="pe-note">Hidden while the computers are apart. Remote control still works. It comes back when you're on the same network.</p></div>`;
    // Start from the current arrangement when this computer has exactly one peer.
    const peers = o.devices.find((d) => d.here)?.lan_mouse?.peers || [];
    const side = peers.length === 1 && SIDES.includes(peers[0].position) ? peers[0].position : "right";
    return `<div class="pe-block"><h4>Arrange mouse &amp; keyboard</h4>
      <p class="pe-note">Both computers write their own Lan Mouse config, and the old one is kept as a backup. Lan Mouse must have run once on each.</p>
      <div class="pe-pair">
        <select class="field" id="pe-pair-dev" aria-label="Which computer">${others.map((d) => `<option value="${esc(d.id)}">${esc(d.id)}</option>`).join("")}</select>
        <span>sits on the</span>
        <select class="field" id="pe-pair-side" aria-label="Which side">${SIDES.map((s) => `<option value="${s}" ${s === side ? "selected" : ""}>${s}</option>`).join("")}</select>
        <span>of this computer</span>
        ${btn("pair", "Pair", `data-pair`, true)}</div></div>`;
  }

  function linkBox(o) {
    const checks = o.checks.map((c) => `<li data-ok="${c.ok}">${icon(c.ok ? "check" : "x")}<div><span>${esc(c.check)}</span><small class="mono">${esc(human(c.detail))}</small>${c.fix ? `<code>${esc(c.fix)}</code>` : ""}</div></li>`).join("");
    let repair = "";
    if (o.role === "main") {
      const nodes = o.devices.filter((d) => d.id !== o.main);
      repair = nodes.length ? `<div class="pe-repair"><p class="pe-note">If a computer stays offline after you've started JARVIS on it, pair it again. This replaces its current pairing.</p>
        <div class="pe-actions">${nodes.map((d) => view.invite?.confirm === d.id
          ? btn(`invite:${d.id}`, `Yes, re-pair ${d.id}`, `data-invite="${esc(d.id)}" data-sure="1"`, true) + `<button type="button" class="pe-btn" data-cancel>Cancel</button>`
          : btn(`invite:${d.id}`, `Re-pair ${d.id}`, `data-invite="${esc(d.id)}"`)).join("")}</div>
        ${view.invite?.code ? `<div class="pe-code"><p>On <b>${esc(view.invite.invited)}</b>, paste this into Devices → Connect &amp; share, or run <span class="mono">jarvis cluster join "&lt;code&gt;"</span>. It works once, for 24 hours, and it's a password: don't share it.</p>
          <textarea class="field mono" readonly rows="3" id="pe-code">${esc(view.invite.code)}</textarea>
          <div class="pe-actions"><button type="button" class="pe-btn primary" data-copy>Copy code</button><button type="button" class="pe-btn" data-hide>Hide</button></div></div>` : ""}</div>` : "";
    } else {
      repair = `<div class="pe-repair"><p class="pe-note">Got an invite code from the main computer? Paste it here to pair this one.</p>
        <div class="pe-pair"><input class="field mono" id="pe-join" placeholder="jarvis-invite-1:…" autocomplete="off" spellcheck="false" aria-label="Invite code">
        ${btn("join", "Join", "data-join", true)}</div></div>`;
    }
    return `<div class="pe-block"><h4>Link between the computers</h4><ul class="pe-checks">${checks}</ul>${repair}</div>`;
  }

  function draw() {
    const o = view.data;
    const head = `<header class="ph">${icon("link")}<h2>Connect &amp; share</h2>
      ${o ? word(o.ok ? "linked" : "needs attention", o.ok) : ""}
      <span class="ph-meta mono">${view.loading ? "checking…" : view.at ? "checked " + new Date(view.at).toLocaleTimeString() : ""}</span>
      <button type="button" class="pe-btn ph-action" data-refresh ${view.loading ? "disabled" : ""} aria-label="Check again">${icon("refresh-cw")}</button></header>`;
    if (!o) {
      root.innerHTML = `<article class="panel ticks pe">${head}<p class="pe-note">${view.error ? "Couldn't check: " + esc(view.error) : "Checking every computer…"}</p></article>`;
      return;
    }
    // Keep a half-typed invite code across redraws.
    const typed = $("#pe-join", root)?.value || "";
    root.innerHTML = `<article class="panel ticks pe">${head}
      ${view.error ? `<p class="pe-err">Last check failed: ${esc(view.error)}</p>` : ""}
      <div class="pe-machines">${o.devices.map(machine).join("")}</div>
      <div class="pe-blocks">${pairBox(o)}${linkBox(o)}</div></article>`;
    if (typed && $("#pe-join", root)) $("#pe-join", root).value = typed;
  }

  root.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b || b.disabled) return;
    const d = b.dataset;
    if ("refresh" in d) return load();
    if (d.do) return act(d.key, { do: d.do, device: d.dev, action: d.action });
    if ("pair" in d) return act("pair", { do: "pair", device: $("#pe-pair-dev").value, position: $("#pe-pair-side").value },
                                () => toast("Paired. Move the mouse across the shared edge to try it."));
    if (d.invite && !d.sure) { view.invite = { confirm: d.invite }; return draw(); }
    if (d.invite) return act(d.key, { do: "invite", device: d.invite }, (r) => { view.invite = r; });
    if ("cancel" in d) { view.invite = null; return draw(); }
    if ("hide" in d) { view.invite = null; return draw(); }
    if ("copy" in d) {
      const code = view.invite?.code || "";
      return navigator.clipboard?.writeText(code).then(() => toast("Invite code copied."),
        () => { $("#pe-code")?.select(); toast("Select the code and copy it."); });
    }
    if ("join" in d) {
      const code = $("#pe-join").value.trim();
      if (!code) return toast("Paste the invite code first.");
      return act("join", { do: "join", code }, () => toast("Paired with the main computer."));
    }
  });

  // Check when Devices opens, then every 20 s while it's open and the tab is visible.
  function tick() {
    clearTimeout(view.timer);
    if ($("#view-devices").hidden) return;
    if (document.visibilityState === "visible" && !view.busy.size && !view.invite) load();
    view.timer = setTimeout(tick, 20000);
  }
  addEventListener("hashchange", tick);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") tick(); });
  tick();
})();
