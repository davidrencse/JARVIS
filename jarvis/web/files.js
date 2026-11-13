// Files view: both laptops' storage side by side. Drag a tile from one pane to the other
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

  function failText(e) {
    const msg = e && typeof e.message === "string" ? e.message.trim() : "";
    return msg || "Something went wrong.";
  }

  const loadGen = {};
  async function load(device, path) {
    const pane = panes[device];
    const gen = (loadGen[device] || 0) + 1;
    loadGen[device] = gen;
    pane.busy = true; pane.error = null; draw(device);
    try {
      const listing = await api(`/api/fs?device=${encodeURIComponent(device)}&path=${encodeURIComponent(path || "")}`);
      if (loadGen[device] !== gen) return;
      if (listing.path !== pane.listing?.path) pane.query = "";
      pane.listing = listing;
    } catch (e) {
      if (loadGen[device] !== gen) return;
      pane.error = failText(e);
    }
    if (loadGen[device] !== gen) return;
    pane.busy = false; draw(device);
  }

  let copying = false;
  async function copy(src, dst) {
    if (copying) return setStatus("A copy is already running. Wait for it to finish.", "bad");
    const dstPane = panes[dst.device];
    if (!dstPane?.listing) return setStatus("Open a folder on the other laptop first.", "bad");
    copying = true;
    setStatus(`Copying ${src.name} …`, "pending");
    try {
      const r = await api("/api/fs/copy", { src_device: src.device, src_path: src.path, dst_device: dst.device, dst_dir: dst.dir });
      setStatus(`Copied ${r.copied}${r.as !== r.copied ? " as " + r.as : ""} to ${dst.device}.`, "ok");
      if (dstPane.listing) load(dst.device, dstPane.listing.path);
    } catch (e) {
      const message = failText(e);
      setStatus(`${src.name}: ${message}`, "bad");
      toast(message);
      // A folder copy can stop after some of it has landed. Refresh the folder
      // still on screen so that partial result is not a stale listing.
      if (panes[dst.device]?.listing) load(dst.device, panes[dst.device].listing.path);
    } finally {
      copying = false;
    }
  }

  function row(listing, e) {
    const path = join(listing.path, listing.sep, e.name);
    const artwork = e.dir
      ? `<svg viewBox="0 0 80 64" aria-hidden="true"><path class="folder-back" d="M5 14a5 5 0 0 1 5-5h20l8 8h32a5 5 0 0 1 5 5v31H5Z"/><path class="folder-front" d="M5 25a5 5 0 0 1 5-5h60a5 5 0 0 1 5 5v28a5 5 0 0 1-5 5H10a5 5 0 0 1-5-5Z"/></svg>`
      : `<svg viewBox="0 0 80 64" aria-hidden="true"><path class="file-sheet" d="M22 5h25l12 12v42H22Z"/><path class="file-lines" d="M47 5v13h12M30 32h21M30 40h21M30 48h14"/></svg>`;
    const preview = !e.dir && /\.(jpe?g|png|webp|gif|bmp)$/i.test(e.name)
      ? `<img class="fthumbnail" src="/api/fs/thumbnail?device=${encodeURIComponent(listing.device)}&amp;path=${encodeURIComponent(path)}&amp;v=${encodeURIComponent(e.mtime)}" alt="" loading="lazy" decoding="async" width="240" height="180">` : "";
    const label = `<span class="fart${preview ? " has-preview" : ""}">${artwork}${preview}</span><span class="fname">${esc(e.name)}</span>`;
    return `<li class="frow" draggable="true" data-name="${esc(e.name)}" data-dir="${e.dir ? 1 : 0}" data-path="${esc(path)}">
      ${e.dir ? `<button type="button" class="flink" title="Open ${esc(e.name)}">${label}</button>` : `<div class="flink" title="${esc(e.name)}">${label}</div>`}
      <span class="fmeta">${e.dir ? "Folder" : size(e.size)}${e.mtime ? `<span class="fdate">${when(e.mtime)}</span>` : ""}</span>
      <button type="button" class="fsend" title="Copy to the other laptop" aria-label="Copy ${esc(e.name)} to the other laptop">${icon("chevron-right")}</button></li>`;
  }

  function draw(device) {
    const pane = panes[device], el = $(`[data-pane="${device}"]`); if (!el) return;
    const d = devices.find((x) => x.id === device), l = pane.listing;
    el.innerHTML = `<header class="ph">${icon("laptop-minimal")}<h2>${esc(d.hostname)}</h2><span class="ph-meta mono">${esc(device)}</span>
        <button type="button" class="round-btn sm" data-act="refresh" aria-label="Refresh ${esc(device)}">${icon("refresh-cw")}</button></header>
      <div class="fpath mono">${l ? `<button type="button" data-act="up" ${l.parent ? "" : "disabled"} aria-label="Up one folder">${icon("arrow-up")}</button><span title="${esc(l.path)}">${esc(l.path)}</span>` : ""}</div>
      <label class="fsearch"><span>Search this folder</span><input type="search" class="fsearch-input" placeholder="File or folder name" aria-label="Search current folder on ${esc(d.hostname)}" value="${esc(pane.query)}" autocomplete="off" spellcheck="false" ${!l || pane.busy ? "disabled" : ""}></label>
      ${pane.error ? `<p class="files-status bad" style="padding:8px 14px">${esc(pane.error)}</p>` : ""}
      <div class="fsummary" aria-live="polite">${pane.busy ? "Reading folder…" : l ? `${l.entries.filter((e) => e.dir).length} folders · ${l.entries.filter((e) => !e.dir).length} files` : "Folder unavailable"}</div>
      <ul class="flist" aria-label="${esc(d.hostname)} folder contents" aria-busy="${pane.busy}">${l ? [...l.entries].sort((a, b) => Number(b.dir) - Number(a.dir) || a.name.localeCompare(b.name, undefined, { numeric: true })).map((e) => row(l, e)).join("") || `<li class="muted fempty">This folder is empty.<span>Drop an item here from the other laptop.</span></li>` : `<li class="muted fempty">${pane.busy ? "Reading …" : "Use refresh to try again."}</li>`}</ul>
      ${l?.truncated ? `<p class="muted" style="font-size:12px;padding:6px 14px">Showing the first ${l.entries.length} items. Search only includes these items.</p>` : ""}`;
    filter(device);
  }

  function filter(device) {
    const pane = panes[device], el = $(`[data-pane="${device}"]`);
    if (!el || !pane.listing || pane.busy) return;
    const query = pane.query.trim().toLocaleLowerCase();
    let folders = 0, files = 0;
    $$(".frow", el).forEach((tile) => {
      tile.hidden = !tile.dataset.name.toLocaleLowerCase().includes(query);
      if (!tile.hidden) tile.dataset.dir === "1" ? folders++ : files++;
    });
    $(".fsummary", el).textContent = query
      ? `${folders + files} of ${pane.listing.entries.length} items match`
      : `${folders} folders · ${files} files`;
    let empty = $(".fno-results", el);
    if (!empty) {
      empty = document.createElement("li");
      empty.className = "muted fempty fno-results";
      empty.textContent = "No matching files or folders. Try another name or clear your search.";
      $(".flist", el).append(empty);
    }
    empty.hidden = !query || folders + files > 0;
    const folderEmpty = $(".fempty:not(.fno-results)", el);
    if (folderEmpty) folderEmpty.hidden = !!query;
  }

  function clearDrop(root) { $$(".fdrop", root).forEach((n) => n.classList.remove("fdrop")); }

  function wire(root) {
    root.addEventListener("error", (e) => {
      if (e.target.matches?.(".fthumbnail")) e.target.remove();
    }, true);
    root.addEventListener("input", (e) => {
      if (!e.target.matches(".fsearch-input")) return;
      const device = e.target.closest("[data-pane]").dataset.pane;
      panes[device].query = e.target.value;
      filter(device);
    });
    root.addEventListener("click", (e) => {
      const paneEl = e.target.closest("[data-pane]"); if (!paneEl) return;
      const device = paneEl.dataset.pane, l = panes[device].listing;
      if (panes[device].busy) return setStatus("This folder is still loading.", "bad");
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "refresh") return load(device, l?.path);
      if (act === "up" && l?.parent) return load(device, l.parent);
      const rowEl = e.target.closest(".frow"); if (!rowEl) return;
      if (e.target.closest(".fsend")) {
        const here = devices.find((x) => x.id === device);
        const other = devices.find((x) => x.id !== device && x.remote !== here?.remote && panes[x.id]?.listing)
          || devices.find((x) => x.id !== device && panes[x.id]?.listing);
        if (!other) return setStatus("The other laptop's folder hasn't loaded, so there is nowhere to copy to. Refresh it first.", "bad");
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
      let src;
      try { src = JSON.parse(raw); }
      catch (err) { return setStatus("That drop couldn't be read. Try the copy arrow.", "bad"); }
      const device = paneEl.dataset.pane;
      if (!src || src.device === device) return setStatus("Drop it on the other laptop's pane: copies go between the two machines.", "bad");
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
      root.innerHTML = `<p class="files-help muted">Open a folder to browse. Drag items between laptops or use the copy arrow. Copies never overwrite or delete anything.</p>
        <div class="files-grid">${devices.map((d) => `<section class="panel ticks fpane" data-pane="${esc(d.id)}" aria-label="${esc(d.hostname)} files"></section>`).join("")}</div>
        <p class="files-status" id="files-status" aria-live="polite"></p>`;
      wire(root);
      for (const d of devices) panes[d.id] = { listing: null, busy: false, error: null, query: "" };
      ready = true;
    }
    await Promise.all(devices.map((d) => load(d.id, panes[d.id].listing?.path)));
    return true;
  }

  window.JarvisFiles = { open };
})();
