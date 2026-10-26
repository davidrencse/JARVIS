// Console hero: Claude's output over the last 48 hours drawn as a point-cloud ridge, the
// one picture on the page. Each hour is a peak; depth layers behind it give the cloud its
// volume. The cloud is drawn once per data, size or theme change and then left alone:
// hovering only moves a DOM readout line, so the page stays cheap while it sits open.
// It also stands in for the old backdrop's window.Terrain hook (theme redraws, mic level).
(function () {
  "use strict";
  let draw = () => {};
  window.Terrain = { level: 0, redraw() { draw(); } };

  const $ = (s) => document.querySelector(s);
  const canvas = $("#cx-ridge");
  if (!canvas) return;
  const box = canvas.parentElement;
  const g = canvas.getContext("2d");
  const nf = new Intl.NumberFormat();
  const compact = (n) => n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M" : n >= 1e4 ? (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "K" : nf.format(Math.round(n));
  const hourLabel = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric" });
  const LAYERS = 28;

  let usage = null, drawn = "";

  // Deterministic noise so the cloud keeps its shape between redraws.
  const rand = (i) => { const x = Math.sin(i * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };

  // Smooth the hourly bars into a ridge line sampled at `n` points, scaled 0..1.
  function ridge(values, n) {
    const max = Math.max(...values, 1), out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const at = (i / (n - 1)) * (values.length - 1);
      let sum = 0, wsum = 0;
      for (let k = Math.max(0, Math.floor(at) - 3); k <= Math.min(values.length - 1, Math.ceil(at) + 3); k++) {
        const w = Math.exp(-((k - at) ** 2) / 1.6);
        sum += w * values[k]; wsum += w;
      }
      out[i] = Math.sqrt(sum / wsum / max); // sqrt lifts quiet hours so they still read as hills
    }
    return out;
  }

  draw = function () {
    if (!usage) return;
    const r = box.getBoundingClientRect(), dpr = Math.min(2, devicePixelRatio || 1);
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    const ink = getComputedStyle(document.documentElement).getPropertyValue("--ink").trim() || "#fff";
    const key = `${w}x${h}|${ink}|${usage.start}|${usage.output.join(",")}`;
    if (key === drawn) return; // nothing changed: keep the pixels we have
    drawn = key;
    canvas.width = w; canvas.height = h;
    const step = 3 * dpr, cols = Math.max(2, Math.floor(w / step)), profile = ridge(usage.output, cols);
    g.clearRect(0, 0, w, h);
    g.fillStyle = ink;
    const top = h * 0.1, floor = h * 0.86, depth = h * 0.12;
    // Back layers first: higher on the canvas, flatter and dimmer.
    for (let z = 0; z < LAYERS; z++) {
      const d = z / (LAYERS - 1), base = floor - depth + d * depth;
      const amp = (floor - top) * (0.45 + 0.55 * d), sway = Math.round(Math.sin(z * 0.7) * 0.012 * cols);
      for (let c = 0; c < cols; c++) {
        const v = profile[Math.min(cols - 1, Math.max(0, c + sway))] * (0.55 + 0.45 * d) + (rand(z * 997 + c) - 0.5) * 0.04;
        if (v <= 0.01 && rand(z * 31 + c) > 0.35) continue;
        const x = c * step + (rand(z + c * 7) - 0.5) * step, y = base - Math.max(0, v) * amp;
        g.globalAlpha = Math.min(1, (0.22 + 0.95 * v) * (0.45 + 0.55 * d));
        const s = dpr * (1.1 + 0.7 * v);
        g.fillRect(x, y, s, s);
        // Volume: a few dots falling from the ridge toward its base, denser under peaks.
        const fill = Math.floor(v * 6 * (0.3 + 0.7 * d));
        for (let k = 0; k < fill; k++) {
          const f = rand(z * 53 + c * 17 + k);
          g.globalAlpha = (0.08 + 0.5 * v) * (1 - f * 0.8) * (0.4 + 0.6 * d);
          g.fillRect(x + (rand(k + c) - 0.5) * step, y + f * (base - y), dpr, dpr);
        }
      }
    }
    g.globalAlpha = 0.35; // a dotted floor line at the front
    for (let x = 0; x < w; x += step * 2) g.fillRect(x, floor + 6 * dpr, dpr, dpr);
    g.globalAlpha = 1;
    canvas.classList.add("ready");
  };

  function facts() {
    const n = usage.output.length, cur = usage.output.slice(n - 24), prev = usage.output.slice(0, n - 24);
    const total = cur.reduce((a, b) => a + b, 0), before = prev.reduce((a, b) => a + b, 0);
    const replies = usage.replies.slice(n - 24).reduce((a, b) => a + b, 0);
    const peak = Math.max(...cur), peakAt = usage.start + (n - 24 + cur.indexOf(peak)) * 3600;
    $("#ridge-total").textContent = compact(total);
    const change = before ? Math.round(((total - before) / before) * 100) : null;
    $("#ridge-facts").innerHTML = [
      ["Against the day before", change == null ? "—" : `${change >= 0 ? "+" : "−"}${Math.abs(change)}%`],
      ["Replies", nf.format(replies)],
      ["Busiest hour", peak ? `${hourLabel(peakAt)}, ${compact(peak)}` : "—"],
      ["Last 5 hours", compact(cur.slice(-5).reduce((a, b) => a + b, 0))],
    ].map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
    $("#ridge-axis").innerHTML = [0, 12, 24, 36].map((i) => `<span style="left:${(i / (n - 1)) * 100}%">${hourLabel(usage.start + i * 3600)}</span>`).join("") + `<span style="left:100%">now</span>`;
  }

  async function load() {
    try {
      const res = await fetch("/api/usage?hours=48");
      if (!res.ok) throw new Error("HTTP " + res.status);
      usage = await res.json();
    } catch (e) {
      $("#ridge-total").textContent = "—";
      $("#ridge-facts").innerHTML = `<div><dt>Claude usage</dt><dd>can't be read right now</dd></div>`;
      return;
    }
    facts(); draw();
  }

  const tip = $("#ridge-tip");
  const tipValue = tip.querySelector("span");
  const pointReadout = $("#ridge-point-readout");
  let keyboardIndex = null;
  function showAt(x, i, announce = false) {
    const r = box.getBoundingClientRect(), n = usage.output.length;
    x = Math.min(r.width, Math.max(0, x));
    i = Math.min(n - 1, Math.max(0, i));
    tip.hidden = false;
    tip.style.left = x + "px";
    tip.classList.toggle("flip", x > r.width - 180);
    tipValue.textContent = `${hourLabel(usage.start + i * 3600)}  ${usage.output[i] ? compact(usage.output[i]) + " tokens" : "no output"}`;
    if (announce) pointReadout.textContent = `${hourLabel(usage.start + i * 3600)}, ${nf.format(usage.output[i])} tokens`;
  }
  box.addEventListener("pointermove", (e) => {
    if (!usage) return;
    const r = box.getBoundingClientRect(), n = usage.output.length;
    const x = Math.min(r.width, Math.max(0, e.clientX - r.left)), i = Math.round((x / r.width) * (n - 1));
    keyboardIndex = i;
    showAt(x, i);
  });
  box.addEventListener("pointerleave", () => { tip.hidden = true; });
  box.addEventListener("keydown", (e) => {
    if (!usage || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const last = usage.output.length - 1;
    if (e.key === "Home") keyboardIndex = 0;
    else if (e.key === "End") keyboardIndex = last;
    else keyboardIndex = Math.min(last, Math.max(0, (keyboardIndex ?? last) + (e.key === "ArrowRight" ? 1 : -1)));
    showAt((keyboardIndex / last) * box.getBoundingClientRect().width, keyboardIndex, true);
  });
  let resizeT = 0;
  new ResizeObserver(() => { clearTimeout(resizeT); resizeT = setTimeout(draw, 150); }).observe(box);
  load();
  setInterval(() => { if (!document.hidden) load(); }, 5 * 60 * 1000);
})();
