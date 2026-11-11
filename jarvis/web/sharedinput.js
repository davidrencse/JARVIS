// Shared mouse and keyboard (Settings): both computers' screens drawn to physical scale
// from /api/input/shared, with Lan Mouse's status, addresses and motion scale cross-checked.
// Read-only: nothing here changes Lan Mouse.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const { api, esc, ago } = J;
  const $ = (s) => document.querySelector(s);
  const card = $("#shared-input");
  if (!card) return;

  const KEY = { KeyLeftCtrl: "Left Ctrl", KeyRightCtrl: "Right Ctrl", KeyLeftShift: "Left Shift",
    KeyRightShift: "Right Shift", KeyLeftMeta: "Win", KeyRightMeta: "Win", KeyLeftAlt: "Left Alt",
    KeyRightAlt: "Right Alt" };
  const ICON = {
    ok: '<path d="M20 6 9 17l-5-5"/>',
    warn: '<path d="m21.7 18-8-14a2 2 0 0 0-3.4 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.7-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    down: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  };
  let data = null, loading = null, loadedAt = 0;

  async function load(force) {
    if (loading) return loading;
    if (!force && data && Date.now() - loadedAt < 60000) return draw();
    $("#si-checked").textContent = "Checking both computers…";
    $("#si-refresh").disabled = true;
    loading = api("/api/input/shared").then((d) => { data = d; loadedAt = Date.now(); draw(); })
      .catch((err) => {
        $("#si-checked").textContent = "Check failed";
        $("#si-checks").innerHTML = row("down", `Couldn't read shared input: ${err.message}`);
      })
      .finally(() => { loading = null; $("#si-refresh").disabled = false; });
    return loading;
  }

  const row = (level, text) =>
    `<li data-level="${esc(level)}"><svg viewBox="0 0 24 24" aria-hidden="true">${ICON[level] || ICON.warn}</svg><span>${esc(text)}</span></li>`;

  function mainMonitor(d) {
    const list = d.info?.monitors || [];
    return list.find((m) => m.primary) || list[0] || null;
  }

  // Physical size in inches from resolution and diagonal; a 100 px/in guess if unknown.
  function size(d, fallback) {
    const m = mainMonitor(d);
    if (!m) return fallback ? { ...fallback, guess: true } : { w: 13.6, h: 7.65, guess: true };
    const diag = Math.hypot(m.width, m.height), inches = d.screen_inches;
    return inches ? { w: inches * m.width / diag, h: inches * m.height / diag }
                  : { w: m.width / 100, h: m.height / 100, guess: true };
  }

  function draw() {
    const { devices = [], layout = {}, checks = [], here, sharing = {} } = data || {};
    $("#si-checked").textContent = "Checked " + ago(loadedAt / 1000);
    // Availability is the guard's projection. "apart" hides the shared screens, "unknown"
    // says so instead of guessing, "together" draws as before. enforced false is the off switch.
    const apart = sharing.state === "apart" && sharing.enforced !== false;
    const lead = apart
      ? row("warn", `Shared mouse and keyboard is unavailable: ${sharing.why || "the laptops are apart"}. Remote control still works.${sharing.override ? " It was started by hand and will not be turned back on if it stops." : ""}`)
      : sharing.state === "unknown" ? row("warn", `Can't tell whether the laptops are together: ${sharing.why || "no reading yet"}`) : "";
    $("#si-checks").innerHTML = lead + (checks.length ? checks.map((c) => row(c.level, c.text)).join("")
      : row("warn", "No computers are configured for shared input."));
    if (apart) $("#si-layout").innerHTML = `<p class="si-empty">The laptops are on different networks, so the shared screens are hidden. They return when you're together again. Remote control still works.</p>`;
    else drawLayout(devices, layout, here, checks);
    drawMachines(devices, sharing);
  }

  function drawLayout(devices, layout, here, checks) {
    const center = devices.find((d) => d.id === here) || devices[0];
    const fig = $("#si-layout");
    if (!center) { fig.innerHTML = `<p class="si-empty">No computers configured.</p>`; return; }
    // Scale to fit about 640 units across, so labels keep their real size; proportions stay true.
    const c = size(center), others = devices.filter((d) => d !== center);
    const across = c.w + others.reduce((sum, d) => sum + (["left", "right"].includes(layout[d.id]) || !layout[d.id] ? size(d, c).w + 0.55 : 0), 0);
    const S = Math.max(12, Math.min(46, 640 / across)), GAP = 0.55 * S;   // px per inch, gap
    const boxes = [{ d: center, x: 0, y: 0, w: c.w * S, h: c.h * S }];
    for (const d of devices) {
      if (d === center) continue;
      const side = layout[d.id];
      const s = size(d, c), w = s.w * S, h = s.h * S, cw = c.w * S, ch = c.h * S;
      const at = { left: [-GAP - w, (ch - h) / 2], right: [cw + GAP, (ch - h) / 2],
                   top: [(cw - w) / 2, -GAP - h], bottom: [(cw - w) / 2, ch + GAP] }[side]
                 || [cw + GAP, (ch - h) / 2];
      boxes.push({ d, x: at[0], y: at[1], w, h, side, guess: s.guess });
    }
    const pad = 26, minX = Math.min(...boxes.map((b) => b.x)) - pad, minY = Math.min(...boxes.map((b) => b.y)) - pad;
    const maxX = Math.max(...boxes.map((b) => b.x + b.w)) + pad, maxY = Math.max(...boxes.map((b) => b.y + b.h)) + pad + 18;
    const disagree = checks.some((k) => k.level === "down" && /puts .* on its/.test(k.text));
    let svg = "";
    for (const b of boxes) {
      const m = mainMonitor(b.d), off = !b.d.info;
      const meta = m ? `${m.width}×${m.height}${b.d.ppi ? ` · ${Math.round(b.d.ppi)} ppi` : ""}${b.d.screen_inches ? ` · ${b.d.screen_inches}″` : ""}`
                     : (off ? "not reachable" : "no screen reported");
      svg += `<g><rect class="si-screen" data-here="${b.d.id === here}" data-state="${off ? "off" : "on"}" x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" rx="7"/>
        <rect class="si-bezel" x="${b.x + 6}" y="${b.y + 6}" width="${b.w - 12}" height="${b.h - 12}" rx="3"/>
        <text class="si-tag" x="${b.x + 14}" y="${b.y + 24}">${b.d.id === here ? "THIS COMPUTER" : esc((b.side || "").toUpperCase())}</text>
        <text class="si-name" x="${b.x + 14}" y="${b.y + b.h / 2}">${esc(b.d.name || b.d.label)}</text>
        <text class="si-meta" x="${b.x + 14}" y="${b.y + b.h / 2 + 18}">${esc(meta)}</text></g>`;
      if (b.d === center || !b.side) continue;
      // The shared edge, in the gap, spanning the overlap of the two screens.
      const horiz = b.side === "left" || b.side === "right";
      if (horiz) {
        const ex = b.side === "left" ? -GAP / 2 : c.w * S + GAP / 2;
        const y1 = Math.max(0, b.y) + 8, y2 = Math.min(c.h * S, b.y + b.h) - 8;
        svg += `<line class="si-edge" data-ok="${!disagree}" x1="${ex}" y1="${y1}" x2="${ex}" y2="${y2}"/>`;
        // The cursor leaves the center screen and arrives at the same relative height.
        const from = b.side === "left" ? GAP * 0.9 : c.w * S - GAP * 0.9, to = b.side === "left" ? -GAP * 1.4 : c.w * S + GAP * 1.4;
        svg += `<circle class="si-cursor" cx="0" cy="${(y1 + y2) / 2}" r="4" style="--si-from:${from};--si-to:${to}"/>`;
      } else {
        const ey = b.side === "top" ? -GAP / 2 : c.h * S + GAP / 2;
        svg += `<line class="si-edge" data-ok="${!disagree}" x1="${Math.max(0, b.x) + 8}" y1="${ey}" x2="${Math.min(c.w * S, b.x + b.w) - 8}" y2="${ey}"/>`;
      }
    }
    svg += `<text class="si-meta" x="${minX + pad}" y="${maxY - 8}">Drawn to scale from each screen’s resolution and size. The dark line is the shared edge.</text>`;
    fig.innerHTML = `<svg viewBox="${minX} ${minY} ${maxX - minX} ${maxY - minY}" role="img" aria-label="${esc(boxes.map((b) => `${b.d.label}${b.side ? ` on the ${b.side}` : ""}`).join(", "))}">${svg}</svg>`;
  }

  function drawMachines(devices, sharing = {}) {
    const host = (h) => String(h || "").trim().toLowerCase().split(".")[0];
    const byHost = new Map(devices.filter((d) => d.info).map((d) => [host(d.info.hostname), d]));
    $("#si-machines").innerHTML = devices.map((d) => {
      const info = d.info, lm = info?.lan_mouse || {}, cfg = lm.config || {};
      const m = mainMonitor(d);
      const facts = [];
      if (!info) facts.push(["Status", d.error || "Not reachable"]);
      else {
        const apart = sharing.state === "apart" && sharing.enforced !== false;
        facts.push(["Lan Mouse", lm.running ? `Running · ${lm.build === "patched" ? "JARVIS build" : "official build"}`
          : apart ? "Off while apart" : "Stopped"]);
        if (m) facts.push(["Screen", `${m.width}×${m.height}${d.screen_inches ? ` · ${d.screen_inches}″` : ""}${d.ppi ? ` · ${Math.round(d.ppi)} ppi` : ""}`]);
        for (const client of cfg.clients || []) {
          const peer = byHost.get(host(client.hostname));
          const live = new Set(peer?.info?.addresses || []);
          const want = d.ppi && peer?.ppi ? d.ppi / peer.ppi : null;
          const ips = (client.ips || []).map((ip) => {
            const ok = !peer?.info || live.has(ip);
            return `<span class="si-addr" data-live="${ok}">${esc(ip)}${ip.startsWith("100.") ? "<small>Tailscale</small>" : ""}${ok ? "" : "<small>gone</small>"}</span>`;
          }).join("");
          facts.push([`${peer ? peer.name || peer.label : client.hostname} is`, `on the ${esc(client.position)}`, true]);
          facts.push([`Looks for it at`, ips || "no addresses", true]);
          if (cfg.motion_scale != null) facts.push(["Motion scale", `${cfg.motion_scale}${want ? ` (density match ${want.toFixed(3)})` : ""}`]);
        }
        if (cfg.release_bind?.length) facts.push(["Release mouse", cfg.release_bind.map((k) => `<kbd class="si-key">${esc(KEY[k] || k.replace(/^Key/, ""))}</kbd>`).join(""), true]);
        if (info.addresses?.length) facts.push(["Own addresses", info.addresses.map((ip) => `<span class="si-addr">${esc(ip)}</span>`).join(""), true]);
      }
      return `<section aria-label="${esc(d.label)}"><header><b>${esc(d.name || d.label)}</b><small class="si-sub">${esc(d.label.split(",").slice(1).join(",").trim())}</small><span>${d.here ? "this computer" : esc(info?.os || "remote")}</span></header>
        <dl>${facts.map(([k, v, html]) => `<div><dt>${esc(k)}</dt><dd>${html ? v : esc(v)}</dd></div>`).join("")}</dl></section>`;
    }).join("");
  }

  $("#si-refresh").addEventListener("click", () => load(true));
  // app.js announces every view change; read both computers when Settings opens.
  addEventListener("jarvis:view", (e) => { if (e.detail === "settings") load(false); });
  if (!$("#view-settings")?.hidden) load(false);
})();
