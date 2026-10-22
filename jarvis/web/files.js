// Files view: both laptops' storage side by side. Drag a row from one pane to the other
// (or press its arrow button) to copy it across; JARVIS does the transfer over SSH and
// never overwrites or deletes anything. Draws only; the server enforces what's reachable.
(function () {
  "use strict";
  const J = window.Jarvis;
  if (!J) return;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const { esc, icon, api, toast } = J;
  const MIME = "application/x-jarvis-file";
  const panes = {};            // device id -> { listing, busy, error }
  let devices = [], ready = false;

  const size = (n) => {
    if (n == null) return "";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(n >= 10 ? 0 : 1) : n) + " " + u[i];
  };
  const when = (t) => (t ? new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "");
  const join = (dir, sep, name) => dir.replace(/[\\/]+$/, "") + sep + name;

  function setStatus(text, cls) {
    const el = $("#files-status"); if (!el) return;
    el.textContent = text; el.className = "files-status " + (cls || "");
  }

  async function load(device, path) {
    const pane = panes[device]; pane.busy = true; pane.error = null; draw(device);
    try {
      pane.listing = await api(`/api/fs?device=${encodeURIComponent(device)}&path=${encodeURIComponent(path || "")}`);
    } catch (e) { pane.error = e.message; }
    pane.busy = false; draw(device);
  }

  async function copy(src, dst) {
    const dstPane = panes[dst.device];
    if (!dstPane?.listing) return;
    setStatus(`Copying ${src.name} …`, "pending");
    try {
      const r = await api("/api/fs/copy", { src_device: src.device, src_path: src.path, dst_device: dst.device, dst_dir: dst.dir });
      setStatus(`Copied ${r.copied}${r.as !== r.copied ? " as " + r.as : ""} to ${dst.device}.`, "ok");
      load(dst.device, dstPane.listing.path);
    } catch (e) { setStatus(`${src.name}: ${e.message}`, "bad"); toast(e.message); }
  }

  function row(listing, e) {
    const path = join(listing.path, listing.sep, e.name);
    return `<li class="frow" draggable="true" data-name="${esc(e.name)}" data-dir="${e.dir ? 1 : 0}" data-path="${esc(path)}">
      <span class="fname">${icon(e.dir ? "folder-search" : "paperclip", "i i-sm")}<button type="button" class="flink" title="${esc(e.name)}">${esc(e.name)}</button></span>
      <span class="fmeta mono">${e.dir ? "" : size(e.size)}</span><span class="fmeta mono">${when(e.mtime)}</span>
      <button type="button" class="fsend" title="Copy to the other laptop" aria-label="Copy ${esc(e.name)} to the other laptop">${icon("chevron-right")}</button></li>`;
  }

  function draw(device) {
    const pane = panes[device], el = $(`[data-pane="${device}"]`); if (!el) return;
    const d = devices.find((x) => x.id === device), l = pane.listing;
    el.innerHTML = `<header class="ph">${icon("laptop-minimal")}<h2>${esc(d.hostname)}</h2><span class="ph-meta mono">${esc(device)}</span>
        <button type="button" class="round-btn sm" data-act="refresh" aria-label="Refresh ${esc(device)}">${icon("refresh-cw")}</button></header>
      <div class="fpath mono">${l ? `<button type="button" data-act="up" ${l.parent ? "" : "disabled"} aria-label="Up one folder">${icon("arrow-up")}</button><span title="${esc(l.path)}">${esc(l.path)}</span>` : ""}</div>
      ${pane.error ? `<p class="files-status bad" style="padding:8px 14px">${esc(pane.error)}</p>` : ""}
      <ul class="flist" aria-busy="${pane.busy}">${l ? l.entries.map((e) => row(l, e)).join("") || `<li class="muted fempty">Empty folder.</li>` : `<li class="muted fempty">${pane.busy ? "Reading …" : ""}</li>`}</ul>
      ${l?.truncated ? `<p class="muted" style="font-size:12px;padding:6px 14px">Showing the first ${l.entries.length} items.</p>` : ""}`;
  }

  function clearDrop(root) { $$(".fdrop", root).forEach((n) => n.classList.remove("fdrop")); }

  function wire(root) {
    root.addEventListener("click", (e) => {
      const paneEl = e.target.closest("[data-pane]"); if (!paneEl) return;
      const device = paneEl.dataset.pane, l = panes[device].listing;
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "refresh") return load(device, l?.path);
      if (act === "up" && l?.parent) return load(device, l.parent);
      const rowEl = e.target.closest(".frow"); if (!rowEl) return;
      if (e.target.closest(".fsend")) {
        const other = devices.find((x) => x.id !== device); if (!other) return;
        return copy({ device, path: rowEl.dataset.path, name: rowEl.dataset.name }, { device: other.id, dir: panes[other.id].listing?.path });
      }
      if (rowEl.dataset.dir === "1") load(device, rowEl.dataset.path);
    });
    root.addEventListener("dragstart", (e) => {
      const rowEl = e.target.closest?.(".frow"); if (!rowEl) return;
      const device = rowEl.closest("[data-pane]").dataset.pane;
      e.dataTransfer.setData(MIME, JSON.stringify({ device, path: rowEl.dataset.path, name: rowEl.dataset.name }));
      e.dataTransfer.effectAllowed = "copy";
    });
    root.addEventListener("dragover", (e) => {
      if (!e.dataTransfer.types.includes(MIME)) return;
      const paneEl = e.target.closest("[data-pane]"); if (!paneEl) return;
      e.preventDefault(); e.dataTransfer.dropEffect = "copy";
      clearDrop(root);
      (e.target.closest('.frow[data-dir="1"]') || paneEl).classList.add("fdrop");
    });
    root.addEventListener("dragleave", (e) => { if (!root.contains(e.relatedTarget)) clearDrop(root); });
    root.addEventListener("drop", (e) => {
      clearDrop(root);
      const raw = e.dataTransfer.getData(MIME); if (!raw) return;
      e.preventDefault();
      const paneEl = e.target.closest("[data-pane]"); if (!paneEl) return;
      const src = JSON.parse(raw), device = paneEl.dataset.pane;
      if (src.device === device) return setStatus("Drop it on the other laptop's pane: copies go between the two machines.", "bad");
      const folder = e.target.closest('.frow[data-dir="1"]');
      copy(src, { device, dir: folder ? folder.dataset.path : panes[device].listing?.path });
    });
  }

  async function open() {
    const root = $("#files-wrap");
    if (!ready) {
      try { devices = await api("/api/fs/devices"); } catch (e) { root.innerHTML = `<p class="files-status bad">${esc(e.message)}</p>`; return false; }
      if (devices.length < 2) { root.innerHTML = `<p class="muted">Add a second device to jarvis.config.json to browse both.</p>`; return false; }
      devices.sort((a, b) => Number(a.remote) - Number(b.remote));   // this laptop left, the SSH one right
      root.innerHTML = `<p class="files-help muted">Drag a file or folder from one laptop to the other, or press its arrow. Copies never overwrite or delete anything; drop on a folder to copy into it.</p>
        <div class="files-grid">${devices.map((d) => `<section class="panel ticks fpane" data-pane="${esc(d.id)}" aria-label="${esc(d.hostname)} files"></section>`).join("")}</div>
        <p class="files-status" id="files-status" aria-live="polite"></p>`;
      wire(root);
      for (const d of devices) panes[d.id] = { listing: null, busy: false, error: null };
      ready = true;
    }
    await Promise.all(devices.map((d) => load(d.id, panes[d.id].listing?.path)));
    return true;
  }

  window.JarvisFiles = { open };
})();
