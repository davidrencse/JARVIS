/* The always-on listener's ear: a live wave of what its microphone hears, beside the
   dock's mic button. The listener (daemon.py, its own process) posts its mic level about
   ten times a second; this polls /api/voice/level and only draws. Bars are grey while the
   room is quiet, ink while something speech-loud reaches the mic, flat when it's off. */
(() => {
  const box = document.getElementById("ear"), canvas = document.getElementById("ear-wave");
  if (!box || !canvas || !window.Jarvis) return;
  const BARS = 24, FAST = 110, SLOW = 3000;
  const bars = new Array(BARS).fill(0);
  let floor = 0.004, state = "off", timer = null, busy = false;

  // Same curve as the voice setup orb (voicesetup.js levelOf): quiet speech still shows.
  const height = (rms) => Math.min(1, Math.pow(Math.max(0, rms) * 14, 0.6));

  function setState(next) {
    if (next === state) return;
    state = next;
    box.setAttribute("data-state", next);
    const label = { off: "Always-on listener: off", quiet: "Always-on listener: listening",
                    voice: "Always-on listener: hearing a voice" }[next];
    box.setAttribute("aria-label", label);
    box.setAttribute("title", label);
  }

  function draw() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
    if (canvas.height !== Math.round(h * dpr)) canvas.height = Math.round(h * dpr);
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const css = getComputedStyle(box);
    const ink = css.getPropertyValue("--ear-ink").trim() || "#888";
    const quiet = css.getPropertyValue("--ear-quiet").trim() || "#555";
    const step = w / BARS, bw = Math.max(1.5, step * 0.55), mid = h / 2;
    const loud = Math.max(0.012, floor * 3.2);
    for (let i = 0; i < BARS; i++) {
      const v = bars[i];
      const bh = state === "off" ? 1.5 : Math.max(1.5, height(v) * (h - 4));
      g.fillStyle = state !== "off" && v > loud ? ink : quiet;
      g.fillRect(i * step + (step - bw) / 2, mid - bh / 2, bw, bh);
    }
  }

  async function poll() {
    timer = null;
    if (busy) return schedule(FAST);
    if (document.hidden) return schedule(SLOW);
    busy = true;
    let r = null;
    try { r = await window.Jarvis.api("/api/voice/level"); } catch { r = null; }
    busy = false;
    if (!r || !r.live) {
      bars.fill(0); setState("off"); draw();
      return schedule(SLOW);
    }
    const peak = Number(r.peak) || 0;
    // The room's floor: drops at once to anything quieter, creeps up slowly through noise.
    floor = peak < floor ? Math.max(0.002, peak) : floor + (peak - floor) * 0.01;
    bars.shift(); bars.push(peak);
    const recent = bars.slice(-4).some((v) => v > Math.max(0.012, floor * 3.2));
    setState(recent ? "voice" : "quiet");
    draw();
    schedule(FAST);
  }

  function schedule(ms) {
    if (timer == null) timer = setTimeout(poll, ms);
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && timer != null) { clearTimeout(timer); timer = null; poll(); }
  });
  window.addEventListener("resize", draw);
  draw();
  poll();
})();
