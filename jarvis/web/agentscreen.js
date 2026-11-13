// Agent screen: what JARVIS's agent browser is showing, live, at the top of the
// Assistant panel, the way hosted personal agents show their browser while they work.
// The box appears only while the agent browser is open. Frames are screenshots polled
// from /api/agent-browser (it never launches the browser); polling slows while the
// browser is closed and stops while this tab is hidden or the Console view is not
// the one on screen. Expand opens the same view in
// a modal <dialog> (the top layer, so the sticky sidebar can't clip it; Esc closes it),
// with the buttons moved in beside it.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const $ = (s) => document.querySelector(s);
  const box = $("#agent-screen");
  if (!box) return;
  const img = $("#as-img"), url = $("#as-url"), note = $("#as-note"), ctl = box.querySelector(".as-ctl");
  const dialog = $("#as-dialog"), bigImg = $("#as-dlg-img"), bigUrl = $("#as-dlg-url");
  const LIVE = 700, BIG = 400, CLOSED = 4000, FAILED = 8000;
  let timer = 0, busy = false, last = "";

  function show(open) {
    if (box.hidden === !open) return;
    box.hidden = !open;
    if (!open) {
      big(false);
      [img, bigImg].forEach((i) => i.removeAttribute("src"));
      last = ""; url.textContent = bigUrl.textContent = "";
    }
  }
  function big(on) {
    if (on && !dialog.open) { dialog.append(ctl); dialog.showModal(); later(50); }
    else if (!on && dialog.open) dialog.close();
  }
  dialog.addEventListener("close", () => box.append(ctl)); // Esc lands here too
  // app.js shows one #view-* at a time (the hidden attribute) and emits jarvis:view.
  // The box's own hidden flag only means the agent browser is closed, so the poll
  // still runs then — just slower. It does not run on another view.
  function consoleShown() {
    const view = box.closest("[id^=view-]");
    return !view || !view.hidden;
  }
  // A hidden tab or another view does not poll. visibilitychange and jarvis:view
  // start the loop again with one tick.
  function later(ms) {
    clearTimeout(timer); timer = 0;
    if (document.hidden || !consoleShown()) return;
    timer = setTimeout(tick, ms);
  }

  async function tick() {
    if (document.hidden || !consoleShown()) return;
    if (busy) return;
    busy = true;
    let wait = CLOSED;
    try {
      const shot = await J.api("/api/agent-browser");
      show(!!shot.open);
      if (shot.open) {
        wait = dialog.open ? BIG : LIVE;
        if (shot.image && shot.image !== last) { img.src = bigImg.src = last = shot.image; }
        const label = shot.title || shot.url || "";
        if (url.textContent !== label) url.textContent = bigUrl.textContent = label;
        const tip = shot.url || "";
        if (url.title !== tip) url.title = bigUrl.title = tip;
        box.classList.toggle("stale", !!shot.error);
        if (shot.error && note.textContent !== shot.error) note.textContent = shot.error;
      }
    } catch (e) {
      show(false);
      wait = FAILED;
    } finally {
      busy = false;
      later(wait);
    }
  }

  async function command(action, button) {
    button.disabled = true;
    note.textContent = "";
    try {
      const r = await J.api("/api/agent-browser", { action });
      note.textContent = r.text || "";
    } catch (e) {
      note.textContent = e.message;
    } finally {
      button.disabled = false;
      later(150);
    }
  }

  box.querySelectorAll("[data-as]").forEach((b) => b.addEventListener("click", () => command(b.dataset.as, b)));
  $("#as-expand").addEventListener("click", () => big(true));
  $("#as-frame").addEventListener("click", () => big(true));
  $("#as-shrink").addEventListener("click", () => big(false));
  dialog.addEventListener("click", (e) => { if (e.target === dialog) big(false); }); // the backdrop
  document.addEventListener("visibilitychange", () => { if (!document.hidden) later(0); });
  // Leaving Console drops a pending tick. Coming back polls once, immediately,
  // and does not reset a tick that is already waiting.
  addEventListener("jarvis:view", () => {
    if (document.hidden || !consoleShown()) { clearTimeout(timer); timer = 0; return; }
    if (!timer) later(0);
  });
  // A turn that just ran something may have opened the browser: look now, not in 4 s.
  addEventListener("jarvis:live", (e) => {
    if (document.hidden) return;
    if (!consoleShown()) { clearTimeout(timer); timer = 0; return; }
    if (box.hidden && /^(turn|step)/.test(e.detail?.kind || "")) later(300);
  });
  later(300);
})();
