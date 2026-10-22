// Small force-directed graph on a 2D canvas. No dependencies.
// Shapes carry the node kind so the graph stays legible in one ink colour.
(function () {
  const SHAPES = {
    module: "square", provider: "diamond", "local-model": "diamond", state: "diamond",
    device: "frame", "personal-agent": "dashed", specialist: "dot", item: "circle",
    kind: "ring", tag: "ring-sm",
  };
  const LABELS = {
    module: "Module", provider: "External service", "local-model": "Local model",
    state: "Stored state", device: "Machine", "personal-agent": "Personal agent",
    specialist: "Specialist agent", item: "Memory item", kind: "Memory kind", tag: "Tag",
  };

  const REDUCE = matchMedia("(prefers-reduced-motion: reduce)");
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  class ForceGraph {
    constructor(canvas, opts = {}) {
      this.c = canvas; this.ctx = canvas.getContext("2d");
      this.opts = { mini: false, onSelect: () => {}, ...opts };
      this.nodes = []; this.edges = []; this.byId = new Map();
      this.view = { x: 0, y: 0, k: 1 }; this.alpha = 0; this.hover = null; this.selected = null;
      this.filter = ""; this.raf = 0;
      this.resize = this.resize.bind(this);
      new ResizeObserver(this.resize).observe(canvas);
      if (!this.opts.mini) this.bind();
    }
    setData(data) {
      const old = this.byId;
      this.nodes = data.nodes.map((n) => {
        const prev = old.get(n.id);
        return { ...n, shape: SHAPES[n.kind] || "circle",
          x: prev ? prev.x : (Math.random() - 0.5) * 400, y: prev ? prev.y : (Math.random() - 0.5) * 400,
          vx: 0, vy: 0, r: this.radius(n) };
      });
      this.byId = new Map(this.nodes.map((n) => [n.id, n]));
      this.edges = data.edges.map((e) => ({ ...e, s: this.byId.get(e.src), t: this.byId.get(e.dst) }))
        .filter((e) => e.s && e.t);
      this.deg = new Map();
      for (const e of this.edges) {
        this.deg.set(e.s.id, (this.deg.get(e.s.id) || 0) + 1);
        this.deg.set(e.t.id, (this.deg.get(e.t.id) || 0) + 1);
      }
      this.alpha = 1; this.fitted = false;
      if (REDUCE.matches) this.settle(); else this.kick();
    }
    // Reduced motion: lay the graph out off-screen, then draw it once.
    settle() {
      this.raf && cancelAnimationFrame(this.raf); this.raf = 0;
      const draw = this.draw; this.draw = () => {};
      while (this.alpha > 0.004) this.step();
      this.draw = draw; this.fit(); this.fitted = true; this.draw();
    }
    matching() {
      if (!this.filter) return [];
      return this.nodes.filter((n) => (n.label || n.id).toLowerCase().includes(this.filter) || n.id.toLowerCase().includes(this.filter));
    }
    radius(n) {
      const s = this.opts.mini ? 0.7 : 1;
      if (n.kind === "module") return s * (4 + Math.min(9, Math.sqrt(n.loc || 40) / 2.2));
      if (n.kind === "item") return s * (3 + 5 * (n.importance ?? 0.5));
      if (n.kind === "kind") return s * 9;
      if (n.kind === "tag") return s * 5;
      if (n.kind === "specialist") return s * 3.5;
      return s * 6;
    }
    neighbors(id) {
      const out = new Set([id]);
      for (const e of this.edges) { if (e.s.id === id) out.add(e.t.id); if (e.t.id === id) out.add(e.s.id); }
      return out;
    }
    select(id) {
      this.selected = id ? this.byId.get(id) || null : null;
      this.opts.onSelect(this.selected, this.selected ? this.links(this.selected.id) : []);
      this.draw();
    }
    links(id) {
      return this.edges.filter((e) => e.s.id === id || e.t.id === id)
        .map((e) => ({ rel: e.rel, other: e.s.id === id ? e.t : e.s, out: e.s.id === id }));
    }
    setFilter(text) { this.filter = text.trim().toLowerCase(); this.draw(); }
    reheat() { for (const n of this.nodes) { n.x += (Math.random() - .5) * 60; n.y += (Math.random() - .5) * 60; } this.alpha = 1; this.fitted = false; REDUCE.matches ? this.settle() : this.kick(); }

    resize() {
      const dpr = Math.min(devicePixelRatio || 1, 2), r = this.c.getBoundingClientRect();
      if (!r.width) return;
      this.w = r.width; this.h = r.height;
      this.c.width = Math.round(r.width * dpr); this.c.height = Math.round(r.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    }
    kick() { if (!this.raf) this.raf = requestAnimationFrame(() => this.tick()); }
    tick() {
      this.raf = 0;
      if (this.alpha > 0.004) {
        this.step();
        if (!this.fitted && (this.alpha < 0.35 || this.opts.mini)) this.fit();
        this.draw();
        this.kick();
      } else if (!this.fitted) { this.fit(); this.draw(); }
    }
    step() {
      const N = this.nodes, a = this.alpha;
      {
        const rep = this.opts.mini ? 500 : 1500;
        for (let i = 0; i < N.length; i++) {
          const p = N[i];
          for (let j = i + 1; j < N.length; j++) {
            const q = N[j]; let dx = p.x - q.x, dy = p.y - q.y, d2 = dx * dx + dy * dy + 0.01;
            if (d2 > 160000) continue;
            const f = rep / d2 * a; const d = Math.sqrt(d2); dx /= d; dy /= d;
            p.vx += dx * f; p.vy += dy * f; q.vx -= dx * f; q.vy -= dy * f;
          }
        }
        for (const e of this.edges) {
          const len = (this.opts.mini ? 28 : 54) + e.s.r + e.t.r;
          let dx = e.t.x - e.s.x, dy = e.t.y - e.s.y; const d = Math.sqrt(dx * dx + dy * dy) || 1;
          const f = (d - len) / d * 0.06 * a; dx *= f; dy *= f;
          e.s.vx += dx; e.s.vy += dy; e.t.vx -= dx; e.t.vy -= dy;
        }
        for (const n of N) {
          n.vx -= n.x * 0.004 * a; n.vy -= n.y * 0.004 * a;
          if (n === this.dragNode) { n.vx = n.vy = 0; continue; }
          n.vx *= 0.82; n.vy *= 0.82; n.x += n.vx; n.y += n.vy;
        }
        this.alpha *= 0.985;
      }
    }
    fit() {
      if (!this.nodes.length || !this.w) return;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const n of this.nodes) { x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y); x1 = Math.max(x1, n.x); y1 = Math.max(y1, n.y); }
      const pad = this.opts.mini ? 16 : 90;
      const k = Math.min(2.2, (this.w - pad * 2) / (x1 - x0 || 1), (this.h - pad * 2) / (y1 - y0 || 1));
      this.view = { k, x: this.w / 2 - k * (x0 + x1) / 2, y: this.h / 2 - k * (y0 + y1) / 2 };
      this.fitted = this.alpha < 0.35;
    }
    toWorld(px, py) { return { x: (px - this.view.x) / this.view.k, y: (py - this.view.y) / this.view.k }; }
    pick(px, py) {
      const p = this.toWorld(px, py); let best = null, bd = Infinity;
      for (const n of this.nodes) {
        const d = Math.hypot(n.x - p.x, n.y - p.y);
        if (d < Math.max(n.r + 4, 9 / this.view.k) && d < bd) { best = n; bd = d; }
      }
      return best;
    }
    bind() {
      const c = this.c; let down = null, moved = false;
      c.addEventListener("pointerdown", (ev) => {
        c.setPointerCapture(ev.pointerId); moved = false;
        const n = this.pick(ev.offsetX, ev.offsetY);
        down = { x: ev.offsetX, y: ev.offsetY, vx: this.view.x, vy: this.view.y, node: n };
        if (n) { this.dragNode = n; this.alpha = Math.max(this.alpha, 0.25); this.kick(); }
        c.classList.add("dragging");
      });
      c.addEventListener("pointermove", (ev) => {
        if (down) {
          if (Math.hypot(ev.offsetX - down.x, ev.offsetY - down.y) > 3) moved = true;
          if (down.node) { const p = this.toWorld(ev.offsetX, ev.offsetY); down.node.x = p.x; down.node.y = p.y; this.alpha = Math.max(this.alpha, 0.15); this.kick(); }
          else { this.view.x = down.vx + ev.offsetX - down.x; this.view.y = down.vy + ev.offsetY - down.y; this.fitted = true; this.draw(); }
          return;
        }
        const n = this.pick(ev.offsetX, ev.offsetY);
        if (n !== this.hover) { this.hover = n; c.style.cursor = n ? "pointer" : ""; this.draw(); }
      });
      const up = (ev) => {
        if (down && !moved) this.select(down.node ? down.node.id : null);
        down = null; this.dragNode = null; c.classList.remove("dragging");
      };
      c.addEventListener("pointerup", up); c.addEventListener("pointercancel", up);
      c.addEventListener("pointerleave", () => { if (!down && this.hover) { this.hover = null; this.draw(); } });
      c.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        const k = Math.min(6, Math.max(0.15, this.view.k * Math.exp(-ev.deltaY * 0.0015)));
        const p = this.toWorld(ev.offsetX, ev.offsetY);
        this.view = { k, x: ev.offsetX - p.x * k, y: ev.offsetY - p.y * k }; this.fitted = true; this.draw();
      }, { passive: false });
      c.addEventListener("keydown", (ev) => {
        const pan = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] }[ev.key];
        if (pan) { this.view.x += pan[0]; this.view.y += pan[1]; }
        else if (ev.key === "+" || ev.key === "=" || ev.key === "-") {
          const k = Math.min(6, Math.max(0.15, this.view.k * (ev.key === "-" ? 0.83 : 1.2)));
          const cx = this.w / 2, cy = this.h / 2, p = this.toWorld(cx, cy);
          this.view = { k, x: cx - p.x * k, y: cy - p.y * k };
        } else if (ev.key === "Escape") { this.select(null); }
        else return;
        ev.preventDefault(); this.fitted = true; this.draw();
      });
    }
    draw() {
      const ctx = this.ctx; if (!this.w) return;
      const ink = css("--ink"), ink3 = css("--ink-3"), line = css("--line-2"), bg = css("--bg");
      ctx.clearRect(0, 0, this.w, this.h);
      ctx.save(); ctx.translate(this.view.x, this.view.y); ctx.scale(this.view.k, this.view.k);
      const focus = this.selected || this.hover;
      const near = focus ? this.neighbors(focus.id) : null;
      const match = this.filter ? (n) => (n.label || n.id).toLowerCase().includes(this.filter) || n.id.toLowerCase().includes(this.filter) : null;
      const lw = 1 / this.view.k;
      for (const e of this.edges) {
        const hot = near && near.has(e.s.id) && near.has(e.t.id) && (e.s === focus || e.t === focus);
        ctx.globalAlpha = near ? (hot ? 0.95 : 0.08) : (match ? 0.12 : 0.55);
        ctx.strokeStyle = hot ? ink : line; ctx.lineWidth = hot ? 1.3 * lw : lw;
        if (e.rel === "uses" || e.rel === "tagged" || e.rel === "is") ctx.setLineDash([3 * lw, 3 * lw]); else ctx.setLineDash([]);
        ctx.beginPath(); ctx.moveTo(e.s.x, e.s.y); ctx.lineTo(e.t.x, e.t.y); ctx.stroke();
      }
      ctx.setLineDash([]);
      for (const n of this.nodes) {
        const dim = (near && !near.has(n.id)) || (match && !match(n));
        ctx.globalAlpha = dim ? 0.18 : 1;
        this.shape(ctx, n, ink, bg, lw);
      }
      ctx.globalAlpha = 1;
      if (!this.opts.mini) {
        ctx.font = `500 ${11 / this.view.k}px Geist, system-ui, sans-serif`; ctx.textBaseline = "middle";
        for (const n of this.nodes) {
          const show = n === focus || (near && near.has(n.id)) || (match && match(n)) ||
            (!near && !match && (n.kind === "module" || n.kind === "kind" || n.kind === "device" || this.view.k > 1.4 || (this.deg.get(n.id) || 0) > 4));
          if (!show) continue;
          const dim = (near && !near.has(n.id));
          ctx.fillStyle = n === focus ? ink : (dim ? ink3 : ink);
          ctx.globalAlpha = n === focus || (near && near.has(n.id)) ? 1 : 0.78;
          const label = (n.label || n.id).length > 34 ? (n.label || n.id).slice(0, 33) + "…" : (n.label || n.id);
          ctx.fillText(label, n.x + n.r + 5 / this.view.k, n.y);
        }
        ctx.globalAlpha = 1;
      }
      if (this.selected) {
        const n = this.selected, s = n.r + 6 / this.view.k, t = 5 / this.view.k;
        ctx.strokeStyle = ink; ctx.lineWidth = lw;
        ctx.beginPath();
        for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
          ctx.moveTo(n.x + sx * s, n.y + sy * (s - t)); ctx.lineTo(n.x + sx * s, n.y + sy * s); ctx.lineTo(n.x + sx * (s - t), n.y + sy * s);
        }
        ctx.stroke();
      }
      ctx.restore();
    }
    shape(ctx, n, ink, bg, lw) {
      const r = n.r; ctx.lineWidth = 1.2 * lw; ctx.strokeStyle = ink; ctx.fillStyle = ink;
      ctx.beginPath();
      switch (n.shape) {
        case "square": ctx.rect(n.x - r, n.y - r, r * 2, r * 2); ctx.fill(); break;
        case "frame": ctx.rect(n.x - r, n.y - r, r * 2, r * 2); ctx.fillStyle = bg; ctx.fill(); ctx.lineWidth = 2 * lw; ctx.stroke(); break;
        case "diamond": ctx.moveTo(n.x, n.y - r * 1.2); ctx.lineTo(n.x + r * 1.2, n.y); ctx.lineTo(n.x, n.y + r * 1.2); ctx.lineTo(n.x - r * 1.2, n.y); ctx.closePath(); ctx.fillStyle = bg; ctx.fill(); ctx.stroke(); break;
        case "dashed": ctx.setLineDash([2 * lw, 2 * lw]); ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fillStyle = bg; ctx.fill(); ctx.stroke(); ctx.setLineDash([]); break;
        case "ring": ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fillStyle = bg; ctx.fill(); ctx.lineWidth = 1.5 * lw; ctx.stroke(); ctx.beginPath(); ctx.arc(n.x, n.y, r * 0.35, 0, Math.PI * 2); ctx.fillStyle = ink; ctx.fill(); break;
        case "ring-sm": ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fillStyle = bg; ctx.fill(); ctx.stroke(); break;
        default: ctx.arc(n.x, n.y, r, 0, Math.PI * 2); ctx.fill();
      }
    }
  }
  ForceGraph.LABELS = LABELS; ForceGraph.SHAPES = SHAPES;
  window.ForceGraph = ForceGraph;
})();
