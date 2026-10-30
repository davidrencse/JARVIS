// The one HTML escaper every page uses (loaded first). Escapes ' too, so values are safe in
// single-quoted attributes; null and undefined render as "".
window.JarvisEsc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
