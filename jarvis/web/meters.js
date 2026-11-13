/* Hardware readings for one computer. Prints the blocks the backend already computed.
   Charge, health, used and free are never calculated here. */
(function () {
  "use strict";

  function clip(value, max) {
    if (value == null) return "";
    const text = String(value);
    return text.length > max ? text.slice(0, max) : text;
  }

  function line(row) {
    const wrap = document.createElement("div");
    wrap.className = "hm-row";
    const top = document.createElement("div");
    top.className = "hm-line";
    const label = document.createElement("span");
    label.className = "hm-k";
    label.textContent = clip(row[0], 40);
    const value = document.createElement("span");
    value.className = "hm-v";
    value.textContent = clip(row[1], 80);
    top.appendChild(label);
    top.appendChild(value);
    wrap.appendChild(top);
    if (row[2]) {
      const note = document.createElement("p");
      note.className = "hm-d";
      note.textContent = clip(row[2], 180);
      wrap.appendChild(note);
    }
    return wrap;
  }

  // Same characters render() prints, so a repeat on the same host can skip the rebuild.
  function blockSig(meters) {
    if (!meters || !Array.isArray(meters.blocks)) return "\0";
    let out = "";
    const blocks = meters.blocks;
    for (let i = 0; i < blocks.length && i < 24; i++) {
      const block = blocks[i];
      if (!block || typeof block !== "object") { out += "\n"; continue; }
      out += (block.wide ? "1" : "0") + "\t" + clip(block.title || "Reading", 40) + "\n";
      const rows = Array.isArray(block.rows) ? block.rows : [];
      for (let j = 0; j < rows.length && j < 8; j++) {
        const row = rows[j];
        if (!Array.isArray(row)) continue;
        out += clip(row[0], 40) + "\t" + clip(row[1], 80) + "\t" + (row[2] ? clip(row[2], 180) : "") + "\n";
      }
    }
    return out;
  }

  function render(host, meters) {
    if (!host) return;
    const sig = blockSig(meters);
    if (host._hmSig === sig && host.children.length) return;
    host._hmSig = sig;
    host.replaceChildren();
    const root = document.createElement("div");
    root.className = "hm-meters";
    root.setAttribute("role", "group");
    root.setAttribute("aria-label", "Hardware readings");
    const reported = meters && Array.isArray(meters.blocks);
    const blocks = reported ? meters.blocks : [
      { title: "Stats", wide: true, rows: [["Status", "Not reported", "No hardware reading"]] },
    ];
    const n = reported ? Math.min(blocks.length, 24) : blocks.length;
    for (let i = 0; i < n; i++) {
      const block = blocks[i];
      if (!block || typeof block !== "object") continue;
      const card = document.createElement("section");
      card.className = "hm-meter" + (block.wide ? " wide" : "");
      const title = document.createElement("h4");
      title.textContent = clip(block.title || "Reading", 40);
      card.appendChild(title);
      const rows = Array.isArray(block.rows) ? block.rows : [];
      const rn = Math.min(rows.length, 8);
      for (let j = 0; j < rn; j++) {
        if (Array.isArray(rows[j])) card.appendChild(line(rows[j]));
      }
      root.appendChild(card);
    }
    host.appendChild(root);
  }

  function clear(host) {
    if (!host) return;
    host._hmSig = "";
    host.replaceChildren();
  }

  window.JarvisMeters = { render: render, clear: clear };
})();
