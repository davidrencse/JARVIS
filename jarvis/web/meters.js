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

  function render(host, meters) {
    if (!host) return;
    host.replaceChildren();
    const root = document.createElement("div");
    root.className = "hm-meters";
    root.setAttribute("role", "group");
    root.setAttribute("aria-label", "Hardware readings");
    const blocks = meters && Array.isArray(meters.blocks) ? meters.blocks.slice(0, 24) : [
      { title: "Stats", wide: true, rows: [["Status", "Not reported", "No hardware reading"]] },
    ];
    for (const block of blocks) {
      if (!block || typeof block !== "object") continue;
      const card = document.createElement("section");
      card.className = "hm-meter" + (block.wide ? " wide" : "");
      const title = document.createElement("h4");
      title.textContent = clip(block.title || "Reading", 40);
      card.appendChild(title);
      const rows = Array.isArray(block.rows) ? block.rows.slice(0, 8) : [];
      for (const row of rows) {
        if (Array.isArray(row)) card.appendChild(line(row));
      }
      root.appendChild(card);
    }
    host.appendChild(root);
  }

  function clear(host) {
    if (host) host.replaceChildren();
  }

  window.JarvisMeters = { render: render, clear: clear };
})();
