// The gate: everything JARVIS is waiting on David for, as a first-class panel at the bottom
// of the screen. Always shown on the console (empty: one quiet line); on other views it
// appears only while something waits. Reads /api/confirm; answers with approve / reject /
// grant ("Allow on this site for 10 min", low-stakes browser clicks only) and revokes grants.
// Older servers send only {id, summary, hash}: every extra field is optional here.
(function () {
  "use strict";
  const J = window.Jarvis;
  const gate = document.getElementById("gate");
  if (!J || !gate) return;
  const esc = J.esc || window.JarvisEsc;
  const $ = (s) => gate.querySelector(s);
  const list = $("#gate-list"), recent = $("#gate-recent"), grantsEl = $("#gate-grants");
  const title = $("#gate-title"), histBtn = $("#gate-hist-btn"), say = $("#gate-say");
  const consoleView = document.getElementById("view-console");
  // The overlay controller finds its window by its exact title: never badge it there.
  const badgeTitle = !new URLSearchParams(location.search).has("overlay");

  let pending = [], grants = [], history = [], busy = false, seen = new Set(), timer = 0, lastSig = "";
  const VERBS = { click: "Click", type: "Type into", press: "Press a key in", select: "Choose in", dialog: "Accept a dialog on", other: "Act on" };

  // ---------- placement: span the stage, reserve room on the console ----------
  function place() {
    const stage = document.getElementById("stage");
    if (!stage) return;
    const r = stage.getBoundingClientRect(), cs = getComputedStyle(stage);
    const padL = parseFloat(cs.paddingLeft) || 0, padR = parseFloat(cs.paddingRight) || 0;
    gate.style.setProperty("--gate-left", `${Math.max(8, r.left + padL)}px`);
    gate.style.setProperty("--gate-right", `${Math.max(8, innerWidth - r.right + padR)}px`);
    // On narrow screens the command dock sits at the bottom of the window: stay above it.
    const dock = document.getElementById("dock");
    const dockShown = dock && !(consoleView && consoleView.contains(dock)) && dock.offsetParent;
    const dockTop = dockShown ? dock.getBoundingClientRect().top : innerHeight;
    document.documentElement.style.setProperty("--gate-bottom", `${Math.max(10, innerHeight - dockTop + 8)}px`);
    const onConsole = consoleView && !consoleView.hidden;
    gate.hidden = !onConsole && !pending.length;
    const root = document.documentElement.style;
    const h = gate.hidden ? 0 : gate.offsetHeight + 10;
    root.setProperty("--gate-h", `${h}px`);
    root.setProperty("--gate-pad", onConsole ? "0px" : `${h}px`); // the console reserves its own room
    if (onConsole) root.setProperty("--ck-top", `${Math.round(consoleView.getBoundingClientRect().top + scrollY)}px`);
  }
  const ro = new ResizeObserver(place);
  ro.observe(gate);
  if (document.getElementById("dock")) ro.observe(document.getElementById("dock"));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { clearTimeout(timer); refresh(); } });
  addEventListener("resize", place);
  addEventListener("jarvis:view", () => requestAnimationFrame(place));

  // ---------- rendering ----------
  const secsLeft = (item) => item.expires_at ? Math.max(0, Math.round(item.expires_at - Date.now() / 1000)) : null;
  const total = (item) => item.stakes === "high" ? 180 : 60;

  function what(item) {
    if (item.kind === "browser" && (item.target || item.verb)) {
      const verb = VERBS[item.verb] || VERBS.other;
      const target = item.target ? ` <span class="target">${esc(item.target)}</span>` : " an element the page didn't name";
      const site = item.site ? ` on ${esc(item.site)}` : "";
      return `<span class="verb">${esc(verb)}</span>${target}${site}?`;
    }
    const text = String(item.summary || "").replace(/^let the browser\s+/i, "Let the browser ");
    return esc(text.charAt(0).toUpperCase() + text.slice(1)) + (/[?.!]$/.test(text) ? "" : "?");
  }

  function meta(item) {
    const bits = [];
    if (item.stakes === "high") bits.push(`<span class="stakes-high">Posts, sends, buys or deletes</span>`);
    else if (item.kind === "browser") bits.push(`<span>Low stakes</span>`);
    if (item.site) bits.push(`<span class="site">${esc(item.site)}</span>`);
    bits.push(`<span>Or say “Jarvis, yes”</span>`);
    return bits.join("");
  }

  function clock(item) {
    const left = secsLeft(item);
    if (left == null) return "";
    const pct = Math.min(100, (left / total(item)) * 100);
    return `<span class="gate-clock${left <= 15 ? " low" : ""}"><i style="--left:${pct}%"></i>declines in ${left} s</span>`;
  }

  function render() {
    const n = pending.length;
    gate.dataset.empty = n ? "false" : "true";
    title.textContent = n ? (n === 1 ? "JARVIS is waiting for your OK" : `${n} things are waiting for your OK`) : "Nothing needs your OK";
    if (badgeTitle) { const base = document.title.replace(/^\(\d+\)\s*/, ""); document.title = n ? `(${n}) ${base}` : base; }
    list.innerHTML = pending.map((item) => `<li class="gate-item" data-id="${esc(item.id)}" tabindex="-1">
        <p class="gate-what">${what(item)}</p>
        <p class="gate-meta">${meta(item)}</p>
        <div class="gate-acts">
          <button type="button" class="gate-btn go" data-act="approve">Allow<kbd>Y</kbd></button>
          ${item.can_grant ? `<button type="button" class="gate-btn" data-act="grant" title="Allow ${esc(VERBS[item.verb] || "this").toLowerCase()} on ${esc(item.site || "this site")} without asking for 10 minutes">Allow on this site, 10 min<kbd>A</kbd></button>` : ""}
          <button type="button" class="gate-btn" data-act="reject">Decline<kbd>N</kbd></button>
        </div>
        ${clock(item)}
      </li>`).join("");
    grantsEl.innerHTML = grants.map((g) => {
      const mins = Math.max(1, Math.round((g.expires_at - Date.now() / 1000) / 60));
      return `<span class="gate-grant" title="Allowed without asking for ${mins} more min">${esc(g.verb)} on ${esc(g.site)} · ${mins} min
        <button type="button" data-revoke-site="${esc(g.site)}" data-revoke-verb="${esc(g.verb)}" aria-label="Stop allowing ${esc(g.verb)} on ${esc(g.site)}">×</button></span>`;
    }).join("");
    renderRecent();
    place();
  }

  function renderRecent() {
    const OUT = { approved: "Allowed", granted: "Allowed 10 min", rejected: "Declined", expired: "Timed out" };
    recent.innerHTML = history.length ? history.map((h) => {
      const t = h.at ? new Date(h.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
      return `<li><time>${esc(t)}</time><span class="out ${esc(h.outcome)}">${esc(OUT[h.outcome] || h.outcome)}</span><span>${esc(h.summary || "")}</span></li>`;
    }).join("") : `<li class="none">No answers yet. Everything you allow or decline shows up here.</li>`;
  }

  function tick() {
    for (const li of list.querySelectorAll(".gate-item")) {
      const item = pending.find((x) => x.id === li.dataset.id);
      const c = li.querySelector(".gate-clock");
      if (!item || !c) continue;
      const left = secsLeft(item);
      c.classList.toggle("low", left <= 15);
      c.lastChild.textContent = `declines in ${left} s`;
      c.querySelector("i").style.setProperty("--left", `${Math.min(100, (left / total(item)) * 100)}%`);
    }
  }
  setInterval(() => { if (!document.hidden && pending.length) tick(); }, 1000);
  setInterval(() => { lastSig = ""; }, 60000); // grant chips show minutes left

  // ---------- data ----------
  async function refresh() {
    clearTimeout(timer);
    if (!busy) {
      try {
        const data = await J.api("/api/confirm");
        const next = data.pending || [];
        const fresh = next.filter((x) => !seen.has(x.id));
        next.forEach((x) => seen.add(x.id));
        const sig = JSON.stringify([next.map((x) => [x.id, x.hash, x.can_grant, x.target, x.expires_at]),
          data.grants || [], (data.recent || history).map((h) => [h.id, h.outcome])]);
        pending = next; grants = data.grants || []; history = data.recent || history;
        // Redraw only on change: a redraw would drop keyboard focus and re-announce the list.
        if (sig !== lastSig) { lastSig = sig; render(); } else place();
        if (fresh.length) {
          // Announce only what is new: rewriting the list must not re-read every question.
          const tmp = document.createElement("p");
          tmp.innerHTML = fresh.map(what).join(" ");
          say.textContent = `JARVIS needs your OK: ${tmp.textContent}`;
        }
        if (fresh.length) dispatchEvent(new CustomEvent("jarvis-gate:asked", { detail: fresh.length }));
      } catch (e) { /* offline: try again on the next tick */ }
    }
    // A hidden tab keeps the title badge current, slowly.
    timer = setTimeout(refresh, document.hidden ? 15000 : pending.length ? 1500 : 4000);
  }

  async function answer(id, act) {
    const item = pending.find((x) => x.id === id);
    if (!item || busy) return;
    busy = true;
    list.querySelectorAll(`[data-id="${CSS.escape(id)}"] button`).forEach((b) => { b.disabled = true; });
    try {
      const result = await J.api("/api/confirm", { id, action: act, hash: item.hash });
      if (result.reply) J.toast?.(result.reply);
      dispatchEvent(new CustomEvent("jarvis-gate:answered", { detail: act }));
    } catch (e) {
      lastSig = ""; // redraw: the buttons this answer disabled must come back
      J.toast?.(`Couldn't send your answer (${e.message}). Check that JARVIS is running, then answer again or say “Jarvis, yes”.`);
    }
    busy = false;
    refresh();
  }

  list.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (b) answer(b.closest(".gate-item").dataset.id, b.dataset.act);
  });
  list.addEventListener("keydown", (e) => {
    const li = e.target.closest(".gate-item");
    if (!li || e.ctrlKey || e.metaKey || e.altKey) return;
    const act = { y: "approve", n: "reject", a: "grant" }[e.key.toLowerCase()];
    if (!act || (act === "grant" && !li.querySelector('[data-act="grant"]'))) return;
    e.preventDefault();
    answer(li.dataset.id, act);
  });
  grantsEl.addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-revoke-site]");
    if (!b) return;
    try { await J.api("/api/confirm", { action: "revoke", site: b.dataset.revokeSite, verb: b.dataset.revokeVerb }); }
    catch (err) { J.toast?.(`Couldn't stop allowing it (${err.message}). Check that JARVIS is running, then press × again.`); }
    refresh();
  });
  histBtn.addEventListener("click", () => {
    const open = recent.hidden;
    recent.hidden = !open;
    histBtn.setAttribute("aria-expanded", String(open));
    place();
  });

  // Turns can park a confirmation at any moment: look again soon after one moves.
  addEventListener("jarvis:live", (ev) => {
    const k = ev.detail?.kind || "";
    if (k.startsWith("turn.") || k.startsWith("confirm")) { clearTimeout(timer); timer = setTimeout(refresh, 300); }
  });

  renderRecent();
  refresh();
  requestAnimationFrame(place);
})();
