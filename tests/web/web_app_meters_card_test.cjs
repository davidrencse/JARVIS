// metersForCard in app.js: an SSH probe that could not read power_supply (Linux script against a
// Windows host, bats=missing) must not replace a valid battery reading the node reported itself.
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "../../jarvis/web/app.js"), "utf8");
const start = src.indexOf("function metersForCard");
assert(start > 0, "metersForCard exists");
const end = src.indexOf("function paintMeters", start);
const metersForCard = new Function(src.slice(start, end) + "; return metersForCard;")();

const cim = { battery: { applicability: "present" }, blocks: [{ title: "Battery" }], source: "cim" };
const missing = { battery: { applicability: "unavailable", reason: "power_supply is not readable." }, blocks: [{ title: "Battery" }, { title: "CPU" }] };
const linux = { battery: { applicability: "present" }, blocks: [{ title: "Battery" }] };
const none = { battery: { applicability: "not-applicable" }, blocks: [{ title: "Battery" }] };

assert.strictEqual(metersForCard({ id: "dell", meters: cim }, { meters: missing }), cim, "Windows CIM battery survives a Linux probe that saw no power_supply");
assert.strictEqual(metersForCard({ id: "dell", meters: cim }, { meters: linux }), linux, "a probe that did read a battery is used");
assert.strictEqual(metersForCard({ id: "dell", meters: none }, { meters: missing }), none);
assert.strictEqual(metersForCard({ id: "dell", meters: null }, { meters: missing }), missing, "nothing known: show the probe, honestly unavailable");
assert.strictEqual(metersForCard({ id: "dell", meters: cim }, null), cim, "no probe: the node's own reading");
assert.strictEqual(metersForCard({ id: "main", here: true, meters: cim }, { meters: linux }), cim, "this computer's own reading wins");
assert.strictEqual(metersForCard(null, null), null);
console.log("PASS metersForCard\nok 1");
