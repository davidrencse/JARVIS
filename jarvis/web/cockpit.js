// Console cockpit: the rail's tabs and the session funnel line in the header.
(function () {
  "use strict";
  const view = document.getElementById("view-console");
  if (!view || !view.classList.contains("ck")) return;
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  };

  // ---------- rail tabs (WAI-ARIA tabs: arrows move, Home/End jump) ----------
  const tabs = [...view.querySelectorAll(".ck-tabs [role=tab]")];
  function select(tab, focus) {
    for (const t of tabs) {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
    }
    if (focus) tab.focus();
    store.set("jarvis.console.rail", tab.id);
    // Canvases in a panel that was hidden measured 0×0: let them re-measure.
    requestAnimationFrame(() => dispatchEvent(new Event("resize")));
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => select(t));
    t.addEventListener("keydown", (e) => {
      const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (j === undefined) return;
      e.preventDefault();
      select(tabs[(j + tabs.length) % tabs.length], true);
    });
  });
  const saved = document.getElementById(store.get("jarvis.console.rail", ""));
  if (saved && tabs.includes(saved)) select(saved);

  // ---------- funnel: this session's requests, how they were handled, what needed David ----------
  const funnel = document.getElementById("ck-funnel");
  const n = { requests: 0, simple: 0, medium: 0, complex: 0, failed: 0, asked: 0, declined: 0 };
  const seen = new Set();
  function draw() {
    if (!n.requests && !n.asked) { funnel.textContent = "No requests yet this session"; return; }
    const tier = [["simple", "simple"], ["medium", "web or app"], ["complex", "software"]]
      .filter(([k]) => n[k]).map(([k, label]) => `<b>${n[k]}</b> ${label}`).join(", ");
    const parts = [`This session: <b>${n.requests}</b> request${n.requests === 1 ? "" : "s"}`];
    if (tier) parts.push(tier);
    parts.push(`<span class="${n.asked ? "needs" : ""}"><b>${n.asked}</b> needed your OK</span>`);
    if (n.declined) parts.push(`<b>${n.declined}</b> declined`);
    if (n.failed) parts.push(`<b>${n.failed}</b> failed`);
    funnel.innerHTML = parts.join('<span class="sep">→</span>');
  }
  addEventListener("jarvis:live", (ev) => {
    const e = ev.detail || {}, d = e.data || {};
    if (e.kind === "turn.start" && d.message && !seen.has(d.message)) {
      seen.add(d.message); n.requests += 1;
      if (n[d.tier] !== undefined && ["simple", "medium", "complex"].includes(d.tier)) n[d.tier] += 1;
      draw();
    } else if (e.kind === "turn.error") { n.failed += 1; draw(); }
  });
  addEventListener("jarvis-gate:asked", (ev) => { n.asked += ev.detail || 1; draw(); });
  addEventListener("jarvis-gate:answered", (ev) => { if (ev.detail === "reject") { n.declined += 1; draw(); } });
  draw();
})();
