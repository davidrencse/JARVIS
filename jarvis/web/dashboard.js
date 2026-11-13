// Porcelain dashboard layer: sidebar pill, header pills, the prompt's dock and recent memory.
// app.js owns data and behaviour and announces changes as "jarvis:*" events; this file only
// draws. The sliding-pill motion is ported from Watermelon UI's Continuous Tabs (MIT,
// ui.watermelon.sh) to plain DOM.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const { esc, icon } = J;
  const on = (name, fn) => addEventListener("jarvis:" + name, (e) => fn(e.detail));

  // ---------- sliding pill (Continuous Tabs), horizontal or vertical ----------
  function slide(pill, target) {
    if (!pill) return;
    if (!target || !target.offsetParent) { pill.style.opacity = 0; return; }
    const box = target.getBoundingClientRect(), host = pill.parentElement.getBoundingClientRect();
    const vertical = getComputedStyle(pill.parentElement).flexDirection === "column";
    // The first placement jumps; only later moves spring, so nothing slides in on load.
    if (!pill.dataset.placed) { pill.style.transition = "none"; requestAnimationFrame(() => requestAnimationFrame(() => { pill.style.transition = ""; })); pill.dataset.placed = "1"; }
    pill.style.opacity = 1;
    pill.style.width = box.width + "px";
    pill.style.height = vertical ? box.height + "px" : "";
    pill.style.transform = vertical ? `translateY(${box.top - host.top}px)` : `translateX(${box.left - host.left}px)`;
  }
  const navPill = () => slide($(".rail-pill"), $('.rail a[aria-current="page"]'));
  let pillQueued = 0;
  const queuePill = () => {
    if (pillQueued) return;
    pillQueued = requestAnimationFrame(() => { pillQueued = 0; navPill(); });
  };
  addEventListener("resize", queuePill);
  // Fonts and wrapping change the pill's target after first paint; re-measure then too.
  if (window.ResizeObserver) new ResizeObserver(queuePill).observe($(".rail"));
  document.fonts?.ready.then(queuePill);

  // ---------- header title follows the view; the prompt lives in the assistant ----------
  const dock = $("#dock"), shell = $(".shell"), home = $("#dock-home");
  let view = location.hash.slice(1) || "console";
  function placeDock() {
    const inPanel = view === "console" && innerWidth > 860;
    if (inPanel && dock.parentElement !== home) home.appendChild(dock);
    if (!inPanel && dock.parentElement !== shell) shell.appendChild(dock);
    document.documentElement.classList.toggle("dock-in-panel", inPanel);
  }
  function onView(name) {
    view = name;
    $("#page-title").textContent = $("#view-" + name)?.dataset.title || "Console";
    document.documentElement.dataset.view = name;
    placeDock(); queuePill();
  }
  on("view", onView);
  addEventListener("resize", placeDock);

  // The assistant column is sticky: below the header at rest, 22px from the top once the
  // page scrolls. Size it from where it sits now so the prompt never runs off the bottom.
  const side = $(".cx-side");
  let fitting = 0;
  function fitSide() {
    fitting = 0;
    if (!side || !side.offsetParent || getComputedStyle(side).position !== "sticky") return;
    const top = Math.max(side.getBoundingClientRect().top, 22);
    side.style.setProperty("--side-h", Math.max(420, Math.floor(innerHeight - top - 16)) + "px");
  }
  const queueFit = () => { if (!fitting) fitting = requestAnimationFrame(fitSide); };
  addEventListener("resize", queueFit);
  addEventListener("scroll", queueFit, { capture: true, passive: true });
  on("view", queueFit);
  queueFit();

  // ---------- header pills ----------
  $("#hero-search").addEventListener("submit", (e) => {
    e.preventDefault();
    $("#mem-q").value = $("#hero-q").value.trim();
    location.hash = "memory";
    setTimeout(() => $("#mem-q").dispatchEvent(new Event("input")), 50);
  });
  $("#hero-refresh").addEventListener("click", () => { $("#probe-refresh").click(); $("#credits-refresh").click(); });

  // ---------- limits: stats + tiles. No longer on the console (signals.js draws the Codex and
  // Claude windows); kept only because tests/test_credit_dashboard.py runs this renderer. ----------
  let credits = J.state.credits, probe = J.state.probe;
  const until = (iso) => { const s = Math.max(0, (Date.parse(iso) - Date.now()) / 1000);
    return s < 3600 ? Math.max(1, Math.round(s / 60)) + " min" : s < 86400 ? Math.round(s / 3600) + " h" : Math.round(s / 86400) + " d"; };
  function limits() {
    if (!$("#limit-stats")) return;
    const codex = credits?.find((r) => r.id === "codex");
    const current = (codex?.meters || []).filter((m) => Number.isFinite(m.pct) &&
      (!m.resets_at || Date.parse(m.resets_at) > Date.now()));
    const five = current.find((m) => m.label === "5-hour window");
    const week = current.find((m) => m.label === "Weekly window");
    const left = (m) => (m ? Math.round(100 - m.pct) : null);
    const zen = probe?.devices?.zenbook, dell = probe?.devices?.dell;
    const stat = (ic, label, value, sub, tone) => `<div class="stat ${tone || ""}"><span class="stat-ic" aria-hidden="true">${icon(ic)}</span>
      <div><span class="stat-label">${esc(label)}</span><b>${value == null ? "—" : value}</b><span class="stat-sub">${esc(sub || "")}</span></div></div>`;
    const tile = (title, sub, bits, prog, progLabel, tone) => `<div class="silk-tile">
      <h3>${esc(title)}</h3><p>${esc(sub)}</p>
      <div class="tile-bits">${bits.map((b) => `<span>${esc(b)}</span>`).join("")}</div>
      <div class="tile-prog ${tone || ""}"><span>${esc(progLabel)}</span><span>${prog == null ? "—" : Math.round(prog) + "%"}</span>
        <i aria-hidden="true"><b style="width:${prog == null ? 0 : Math.max(2, Math.min(100, prog))}%"></b></i></div></div>`;
    const lm = probe?.input_link;
    const done = credits && probe;
    const count = done ? String([five, week, zen].filter(Boolean).length) : "…";
    const statsHtml =
      stat("gauge", "Codex, 5 h", left(five) == null ? null : left(five) + "%", five ? (five.resets_at ? "resets in " + until(five.resets_at) : "left") : "no data") +
      stat("calendar", "Codex, week", left(week) == null ? null : left(week) + "%", week?.resets_at ? "resets in " + until(week.resets_at) : "left") +
      stat("laptop-minimal", "Zenbook memory", zen?.mem_avail_pct == null ? null : zen.mem_avail_pct + "%", "free", zen?.mem_avail_pct != null && zen.mem_avail_pct < 10 ? "alert" : "");
    const tilesHtml =
      tile("WinLaptop", "Dell G15 · this laptop",
        [dell?.lan_mouse?.running ? "Lan Mouse up" : "Lan Mouse off", lm ? (lm.level === "down" ? "link down" : "link up") : "checking"],
        five ? 100 - (five.pct ?? 0) : null, "Codex window left") +
      tile("archmain", zen?.ssh ? `Zenbook · ${zen.ip || "online"}` : zen ? "Zenbook · unreachable" : "Zenbook · checking",
        [zen?.ping_ms != null ? zen.ping_ms + " ms" : "—", zen?.wifi_power_save === "on" ? "power save on" : "power save off"],
        zen?.mem_avail_pct ?? null, "Memory free", zen?.mem_avail_pct != null && zen.mem_avail_pct < 10 ? "alert" : "");
    const sig = statsHtml + "\0" + tilesHtml + "\0" + count;
    if (limits._sig === sig) return;
    limits._sig = sig;
    $("#limit-stats").innerHTML = statsHtml;
    $("#silk-tiles").innerHTML = tilesHtml;
    $("#limits-count").textContent = count;
  }
  on("credits", (d) => { credits = d; limits(); });
  on("probe", (d) => { probe = d; limits(); });
  limits();

  // ---------- recent memory ----------
  const KIND_ICON = { message: "message-square", reply: "audio-lines", note: "file-text", finding: "flag", plan: "list-checks", result: "activity", case: "folder-search", url: "link", output: "terminal" };
  function recent(o) {
    const items = (o?.memory?.recent || []).slice(0, 5);
    const list = $("#recent-list");
    const sig = items.map((it) => [it.id, it.kind, it.title].join("\t")).join("\n");
    if (list._sig === sig) return;
    list._sig = sig;
    list.innerHTML = items.length ? items.map((it) => `<li><a href="#memory" data-id="${esc(it.id)}">
        <span class="pl-ic" aria-hidden="true">${icon(KIND_ICON[it.kind] || "file-text")}</span><span class="pl-t">${esc(it.title)}</span><span class="pl-k">${esc(it.kind)}</span></a></li>`).join("")
      : `<li class="empty"><b>Nothing remembered yet.</b>Requests, replies and notes land here.</li>`;
    $$("#recent-list a").forEach((a) => a.addEventListener("click", () => {
      $("#mem-q").value = ""; setTimeout(() => $(`#mem-list [data-id="${CSS.escape(a.dataset.id)}"]`)?.click(), 400);
    }));
  }
  on("overview", recent);

  // ---------- boot from whatever app.js already has ----------
  onView(location.hash.slice(1) || "console");
  if (J.state.overview) recent(J.state.overview);
})();
