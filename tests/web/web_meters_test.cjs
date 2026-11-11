/* Renderer checks. The numbers are already computed; this only checks they are printed. */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.join(__dirname, "../../jarvis/web");
const source = fs.readFileSync(path.join(root, "meters.js"), "utf8");
let failed = 0;
function check(name, ok) {
  if (!ok) { failed += 1; console.error("FAIL " + name); }
  else console.log("PASS " + name);
}

for (const sink of ["innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function"]) {
  check("meters.js has no " + sink, !source.includes(sink));
}

function element(tag) {
  const node = { tag, className: "", attrs: {}, title: "", children: [], _text: "" };
  node.setAttribute = (k, v) => { node.attrs[k] = String(v); };
  node.appendChild = (child) => { node.children.push(child); return child; };
  node.replaceChildren = () => { node.children = []; };
  Object.defineProperty(node, "textContent", {
    get() { return node._text; },
    set(v) { node._text = String(v); },
  });
  return node;
}
function texts(node) {
  const out = [];
  if (node._text) out.push(node._text);
  for (const child of node.children || []) out.push(...texts(child));
  return out;
}

const context = { window: {}, document: { createElement: element }, console };
context.window = context;
vm.runInNewContext(source, context, { filename: "meters.js" });
const api = context.window.JarvisMeters;

function paint(meters) {
  const host = element("div");
  api.render(host, meters);
  return { host, text: texts(host).join(" | "), labels: host.children[0].getAttribute ? null : null };
}

const charging = paint({
  blocks: [
    { title: "RAM", rows: [["Used", "12.9 GB"], ["Free", "2.4 GB"], ["Total", "15.3 GB"]] },
    { title: "Swap", rows: [["Used", "10 GB"], ["Free", "13.7 GB"], ["Total", "23.7 GB"]] },
    { title: "Disk", rows: [["Used", "218.3 GB"], ["Free", "86 GB"], ["Total", "304.3 GB"]] },
    { title: "CPU", rows: [["Load", "16%", "1-minute load average"]] },
    { title: "Battery charge", wide: true, rows: [["Now", "77% · charging"]] },
    { title: "Battery health", wide: true, rows: [["Health", "85%"], ["When", "just now · fresh"]] },
  ],
});
check("prints used free total and charge", charging.text.includes("12.9 GB") && charging.text.includes("77% · charging") && charging.text.includes("85%"));
check("does not invent a second percent", !charging.text.includes("100%"));
const wide = charging.host.children[0].children.filter((c) => c.className.includes("wide"));
check("battery blocks span the row", wide.length === 2);

const down = paint({ blocks: [
  { title: "RAM", rows: [["Status", "Unavailable", "free is larger than total"]] },
  { title: "Battery", wide: true, rows: [["Status", "Unavailable", "the reading timed out"]] },
  { title: "Reading", wide: true, rows: [["When", "3 d ago · stale"], ["Link", "Offline"]] },
]});
check("unavailable stale and offline are the words given", down.text.includes("Unavailable") && down.text.includes("3 d ago · stale") && down.text.includes("Offline") && down.text.includes("the reading timed out"));

const desk = paint({ blocks: [
  { title: "RAM", rows: [["Used", "4 GB"], ["Free", "12 GB"], ["Total", "16 GB"]] },
  { title: "Battery", wide: true, rows: [["Status", "Not applicable", "No battery reported."]] },
]});
check("desktop with no battery is not a failure", desk.text.includes("Not applicable") && desk.text.includes("4 GB") && !desk.text.includes("0%"));

const empty = paint(null);
check("missing reading is not reported", empty.text.includes("Not reported") && empty.text.includes("Stats"));
api.clear(empty.host);
check("clear removes a reading", texts(empty.host).length === 0);

const css = fs.readFileSync(path.join(root, "connections.css"), "utf8") + "\n" + fs.readFileSync(path.join(root, "phone/phone.css"), "utf8");
const chunk = css.split(".hm-host")[1] || "";
check("meter css uses ink tokens", chunk.includes("var(--ink)") && chunk.includes("var(--ink-3)") && chunk.includes("var(--line)"));
check("meter css has no painted icon colour", !/#([0-9a-f]{3,8})/i.test(chunk.split("@media (max-width: 900px)")[0].split(".cx-tiles")[0]));
check("wide cards get four columns", css.includes(".cx-node .hm-meters"));
check("narrow width wraps to one column", css.includes("max-width: 360px"));

if (failed) process.exit(1);
