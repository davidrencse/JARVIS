// System view, in the same grammar as the Run view: the codebase read as a structure
// instead of a run. Inputs on the left, the layered import diagram as the spine, one
// card per module, then layers / externals / layout across the foot.
//
// The layering algorithm (cycle-breaking DFS, longest-path depth) and the diagram's
// roving focus moved here from app.js unchanged; only the shell around them is new.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const { esc, icon, api, words, toast } = J;

  const EXTERNAL = ["provider", "local-model", "state", "device"];
  const bytes = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "K" : String(n);
  const clip = (s, n) => (String(s ?? "").length > n ? String(s).slice(0, n - 1) + "…" : String(s ?? ""));

  function card(title, tag, rows, extra = "", state = "") {
    const body = rows.map(([k, v, cls]) =>
      `<dt>${esc(k)}</dt><dd class="${cls || ""}">${esc(v == null ? "—" : String(v))}</dd>`).join("");
    return `<section class="rv-card"${state ? ` data-state="${esc(state)}"` : ""}>
      <h3>${esc(title)}${tag ? `<span class="rv-tag">${esc(tag)}</span>` : ""}</h3>
      <dl class="rv-rows">${body}</dl>${extra}</section>`;
  }

  // ---------- structure: who imports whom, and how deep ----------
  // Modules that import each other form cycles; drop the edges that close a cycle
  // (found by depth-first search) so layering terminates with real depths.
  function layers(g) {
    const mods = g.nodes.filter((n) => n.kind === "module");
    const allImports = g.edges.filter((e) => e.rel === "imports");
    const out = new Map(mods.map((m) => [m.id, []]));
    allImports.forEach((e) => out.get(e.src)?.push(e));
    const back = new Set(), seen = new Set(), onPath = new Set();
    const visit = (id) => {
      seen.add(id); onPath.add(id);
      for (const e of out.get(id) || []) {
        if (onPath.has(e.dst)) back.add(e);
        else if (!seen.has(e.dst)) visit(e.dst);
      }
      onPath.delete(id);
    };
    mods.forEach((m) => { if (!seen.has(m.id)) visit(m.id); });
    const imports = allImports.filter((e) => !back.has(e));
    const depth = new Map(mods.map((m) => [m.id, 0]));
    for (let pass = 0; pass < mods.length; pass++) {
      let changed = false;
      for (const e of imports) {
        const d = depth.get(e.src) + 1;
        if (d > depth.get(e.dst)) { depth.set(e.dst, d); changed = true; }
      }
      if (!changed) break;
    }
    const maxD = Math.max(0, ...depth.values());
    const bands = [];
    for (let d = 0; d <= maxD; d++) {
      bands.push({
        label: d === 0 ? "Entry points" : d === maxD ? "Foundations" : `Layer ${d + 1}`,
        nodes: mods.filter((m) => depth.get(m.id) === d).sort((a, b) => b.loc - a.loc),
      });
    }
    bands.push({ label: "External services", nodes: g.nodes.filter((n) => EXTERNAL.includes(n.kind)) });
    bands.push({ label: "Agents", nodes: g.nodes.filter((n) => n.kind === "specialist" || n.kind === "personal-agent") });
    return { mods, bands, depth, cycles: back.size, maxD };
  }

  // ---------- the layered diagram (ported from app.js, unchanged) ----------
  function drawDiagram(g, bands) {
    const BW = 150, BH = 40, GX = 12, GY = 12, BAND = 30, LEFT = 124, PER = 7;
    const pos = new Map(), order = [], labelsAt = [];
    let y = 8;
    bands.forEach((b) => {
      if (!b.nodes.length) return;
      labelsAt.push([b.label, y]);
      b.nodes.forEach((n, i) => {
        pos.set(n.id, { x: LEFT + (i % PER) * (BW + GX), y: y + Math.floor(i / PER) * (BH + GY), n });
        order.push(n.id);
      });
      y += Math.ceil(b.nodes.length / PER) * (BH + GY) - GY + BAND;
    });
    const W = LEFT + PER * (BW + GX), H = y - BAND + 8;
    const svg = $("#system-svg");
    if (!svg) return;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.style.minWidth = W * 0.82 + "px";
    const edges = g.edges.filter((e) => pos.has(e.src) && pos.has(e.dst)).map((e) => {
      const a = pos.get(e.src), b = pos.get(e.dst), down = b.y > a.y;
      const x1 = a.x + BW / 2, x2 = b.x + BW / 2;
      const y1 = down ? a.y + BH : a.y, y2 = down ? b.y : b.y + BH;
      const dy = Math.max(18, Math.abs(y2 - y1) / 2) * (down ? 1 : -1);
      return `<path class="edge" aria-hidden="true" data-s="${esc(e.src)}" data-t="${esc(e.dst)}" d="M${x1},${y1} C${x1},${y1 + dy} ${x2},${y2 - dy} ${x2},${y2}" ${e.rel !== "imports" ? 'stroke-dasharray="3 3"' : ""}/>`;
    }).join("");
    const labels = labelsAt.map(([label, ly]) =>
      `<text class="col-label" aria-hidden="true" x="0" y="${ly + 24}">${esc(label)}</text>`).join("");
    const sub = (n) => n.kind === "module" ? `${n.loc} lines`
      : n.kind === "specialist" ? `${n.model || ""} · ${n.power || ""} effort` : words(n.kind);
    const nodes = order.map((id, i) => {
      const { x, y: ny, n } = pos.get(id);
      return `<g class="node ${n.kind === "module" ? "" : "ext"}" data-id="${esc(n.id)}" transform="translate(${x},${ny})" tabindex="${i ? -1 : 0}" role="button" aria-label="${esc(n.label)}, ${esc(sub(n))}">
      <rect width="${BW}" height="${BH}" rx="3"/><text x="10" y="17">${esc(n.label.length > 22 ? n.label.slice(0, 21) + "…" : n.label)}</text><text class="sub" x="10" y="31">${esc(sub(n))}</text></g>`;
    }).join("");
    svg.innerHTML = labels + `<g>${edges}</g>` + nodes;

    const detail = $("#module-detail"), byId = new Map(g.nodes.map((n) => [n.id, n]));
    const focus = (nid) => {
      svg.classList.toggle("focus", !!nid);
      const near = new Set();
      $$(".edge", svg).forEach((p) => {
        const hot = p.dataset.s === nid || p.dataset.t === nid;
        p.classList.toggle("hot", hot);
        if (hot) { near.add(p.dataset.s); near.add(p.dataset.t); }
      });
      $$(".node", svg).forEach((el) => {
        el.classList.toggle("hot", el.dataset.id === nid);
        el.classList.toggle("near", near.has(el.dataset.id));
      });
      if (!nid || !detail) return;
      const n = byId.get(nid), outs = g.edges.filter((e) => e.src === nid), inn = g.edges.filter((e) => e.dst === nid);
      const names = (list, k) => list.map((e) => esc(byId.get(e[k])?.label || e[k])).join(", ") || "—";
      detail.innerHTML = `<b>${esc(n.label)}</b> — ${esc(n.doc || words(n.kind))}<br>` +
        (n.kind === "module" ? `Uses: ${names(outs, "dst")}<br>Used by: ${names(inn, "src")}`
                             : `Connected from: ${names(inn, "src")}`);
    };
    const els = $$(".node", svg);
    els.forEach((el, i) => {
      el.addEventListener("mouseenter", () => focus(el.dataset.id));
      el.addEventListener("focus", () => focus(el.dataset.id));
      el.addEventListener("mouseleave", () => focus(null));
      el.addEventListener("keydown", (e) => { // roving focus: one tab stop, arrows move
        const step = { ArrowRight: 1, ArrowDown: PER, ArrowLeft: -1, ArrowUp: -PER, Home: -i, End: els.length - 1 - i }[e.key];
        if (step === undefined) return;
        e.preventDefault();
        const next = els[Math.max(0, Math.min(els.length - 1, i + step))];
        el.tabIndex = -1; next.tabIndex = 0; next.focus();
      });
    });
  }

  // ---------- bands ----------
  function render(g, tree) {
    const { mods, bands, depth, cycles, maxD } = layers(g);
    const imports = g.edges.filter((e) => e.rel === "imports");
    const externals = g.nodes.filter((n) => EXTERNAL.includes(n.kind));
    const agents = g.nodes.filter((n) => n.kind === "specialist" || n.kind === "personal-agent");
    const loc = mods.reduce((n, m) => n + (m.loc || 0), 0);
    const outDeg = new Map(), inDeg = new Map(), touches = new Map();
    for (const e of g.edges) {
      if (e.rel === "imports") {
        outDeg.set(e.src, (outDeg.get(e.src) || 0) + 1);
        inDeg.set(e.dst, (inDeg.get(e.dst) || 0) + 1);
      } else if (e.rel === "uses") {
        if (!touches.has(e.src)) touches.set(e.src, []);
        touches.get(e.src).push(e.dst);
      }
    }
    const label = (id) => g.nodes.find((n) => n.id === id)?.label || id;

    const head = `<div class="rv-head">
      <span>graph</span><b>${esc(tree.root || "jarvis")}</b>
      <span>modules ${mods.length}</span><span>imports ${imports.length}</span>
      <span>externals ${externals.length}</span><span>agents ${agents.length}</span>
      <span class="rv-spacer">${bytes(loc)} lines</span>
      <span class="rv-state" data-state="finished">static · from real imports</span>
    </div>`;

    const inputs = [
      card("package", tree.root || "", [
        ["modules", mods.length], ["lines", bytes(loc)],
        ["layers", maxD + 1], ["cycles", cycles || "none", cycles ? "rv-live" : "rv-mute"],
      ]),
      card("entry points", "", (bands[0]?.nodes || []).slice(0, 4).map((n) => [n.label.replace(/\.py$/, ""), `${n.loc} ln`])),
      card("externals", String(externals.length),
        externals.slice(0, 5).map((n) => [clip(n.label, 16), words(n.kind)])),
      card("agents", String(agents.length),
        agents.slice(0, 5).map((n) => [clip(n.label, 16), n.power || words(n.kind)])),
    ].join("");

    // One card per module, deepest-first inside each layer: the same unit the Run view
    // gives a step.
    const stages = mods.slice().sort((a, b) => depth.get(a.id) - depth.get(b.id) || b.loc - a.loc)
      .map((m) => card(m.label.replace(/\.py$/, ""), `L${depth.get(m.id) + 1}`, [
        ["lines", m.loc],
        ["imports", outDeg.get(m.id) || 0, outDeg.get(m.id) ? "" : "rv-mute"],
        ["used by", inDeg.get(m.id) || 0, inDeg.get(m.id) ? "" : "rv-mute"],
        ["touches", (touches.get(m.id) || []).length || "—", (touches.get(m.id) || []).length ? "" : "rv-mute"],
        ["public", (m.defs || []).length || "—"],
      ], m.doc ? `<p class="rv-note">${esc(clip(m.doc, 110))}</p>` : "")).join("");

    const funnel = `<div class="rv-funnel"><span class="rv-phase">structure</span>
      <span class="rv-flow"><b>${mods.length}</b> modules<span>→</span><b>${maxD + 1}</b> layers<span>→</span><b>${imports.length}</b> imports<span>→</span><b>${externals.length}</b> externals<span>→</span><b>${agents.length}</b> agents</span></div>`;

    const layerList = `<div class="rv-lanes">${bands.filter((b) => b.nodes.length).map((b, i) =>
      `<span class="rv-w">L${i + 1}</span><span class="rv-name">${esc(b.label)}</span>
       <span class="rv-st">${b.nodes.length}</span>
       <span class="rv-bar done"><i style="width:${(b.nodes.length / Math.max(1, mods.length)) * 100}%"></i></span>
       <span class="rv-st">${esc(clip(b.nodes.map((n) => n.label.replace(/\.py$/, "")).join(" "), 28))}</span>`).join("")}</div>`;

    const extList = externals.length
      ? `<div class="rv-log">${externals.map((n) => {
          const users = g.edges.filter((e) => e.dst === n.id).map((e) => label(e.src).replace(/\.py$/, ""));
          return `<time>${esc(words(n.kind).slice(0, 8))}</time><span class="rv-actor">${esc(clip(n.label, 18))}</span>
                  <span class="rv-text">${esc(users.join(" ") || "—")}</span>`;
        }).join("")}</div>`
      : `<p class="rv-empty">No external services detected.</p>`;

    let files = 0, lines = 0;
    const walk = (list) => list.map((e) => {
      if (e.dir) return `<details ${["jarvis", "docs"].includes(e.name) ? "open" : ""}><summary>${icon("chevron-right")}<span>${esc(e.name)}/</span><span>${e.children.length}</span></summary><div class="kids">${walk(e.children)}</div></details>`;
      files++; lines += e.lines || 0;
      return `<div class="f"><span>${esc(e.name)}</span><span>${e.lines ? e.lines + " ln" : bytes(e.bytes) + " B"}</span></div>`;
    }).join("");
    const layout = walk(tree.children || []);

    const foot = `<div class="rv-foot">
      <section class="rv-card"><h3>layers<span class="rv-tag">${maxD + 1} deep</span></h3>
        <div class="rv-scroll">${layerList}</div></section>
      <section class="rv-card"><h3>externals<span class="rv-tag">${externals.length}</span></h3>
        <div class="rv-scroll">${extList}</div></section>
      <section class="rv-card"><h3>layout<span class="rv-tag">${files} files · ${bytes(lines)} ln</span></h3>
        <div class="rv-scroll tree mono">${layout}</div></section>
    </div>`;

    $("#sv").innerHTML = head +
      `<div class="rv-body sv-body">
         <div class="rv-inputs">${inputs}</div>
         <div class="rv-spine sv-diagram">
           <div class="diagram-scroll"><svg id="system-svg" aria-label="How the code fits together. Arrow keys move between parts."></svg></div>
           <div class="module-detail mono" id="module-detail" aria-live="polite">Hover or focus a part to see what it does and what it connects to.</div>
         </div>
         <div class="rv-stages">${stages}</div>
       </div>` +
      funnel + foot +
      `<div class="rv-cmd"><span class="rv-prompt">~/david $</span><span>jarvis doctor</span>
         <span class="rv-caret" aria-hidden="true"></span>
         <span class="rv-right">read from real imports in jarvis/*.py · nothing runs from this screen</span></div>`;
    drawDiagram(g, bands);
  }

  async function open(again) {
    // The system design page now lives on its own (design.html): the request path in plain
    // words, live health with fixes, data stores and the module map. Shortcut 3 goes there.
    location.href = "design.html";
    return;
    if (again) return;
    if (!$("#sv")) return false;
    try {
      const [g, tree] = await Promise.all([api("/api/graph/system"), api("/api/tree")]);
      render(g, tree);
    } catch (e) { toast("System: " + (e.message || e)); return false; }
  }

  window.JarvisSystem = { open };
})();
