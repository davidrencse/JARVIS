// Offline shell security: no API traffic or credentials enter Cache Storage.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const handlers = {}, calls = [], writes = [], deleted = [];
const shellCache = new Map();
let offline = false;
const origin = "https://phone.test";
const response = (url, headers = {}) => ({ ok: true, type: "basic", url: origin + url,
  headers: new Headers(headers), clone() { return this; } });
const context = {
  URL, Headers, Request: class { constructor(url, options) { this.url = origin + url; Object.assign(this, options); } },
  Response, Date, setTimeout, clearTimeout,
  self: { location: { origin }, addEventListener: (key, fn) => { handlers[key] = fn; },
    skipWaiting: async () => {}, clients: { claim: async () => {} } },
  caches: { open: async () => ({ put: async (key, value) => { writes.push(key); shellCache.set(key, value); }, match: async (key) => shellCache.get(key) }),
    keys: async () => ["jarvis-phone-old", "other-app"], delete: async (key) => { deleted.push(key); } },
  fetch: async (req, options) => {
    calls.push({ req, options });
    if (offline) throw new TypeError("Network unavailable");
    const url = typeof req === "string" ? new URL(req).pathname : new URL(req.url).pathname;
    return response(url);
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../../jarvis/web/phone/sw.js"), "utf8"), context);
async function lifecycle(name) { let work; handlers[name]({ waitUntil(promise) { work = promise; } }); await work; }
function intercept(url, headers = {}) {
  let answer, work;
  handlers.fetch({ request: { method: "GET", url: origin + url, mode: "navigate", headers: new Headers(headers) },
    respondWith(p) { answer = p; }, waitUntil(p) { work = p; } });
  return { answer, work };
}
(async () => {
  await lifecycle("install");
  assert.ok(writes.includes("/"));
  assert.ok(calls.every((call) => call.req.credentials === "omit"));
  await lifecycle("activate");
  assert.deepEqual(deleted, ["jarvis-phone-old"]);
  for (const url of ["/phone/api/me", "/phone/api/session/frame", "/phone/pair", "/?token=secret"]) {
    assert.equal(intercept(url).answer, undefined);
  }
  for (const name of ["Authorization", "X-CSRF-Token", "X-JARVIS-Session"]) {
    assert.equal(intercept("/", { [name]: "secret" }).answer, undefined);
  }
  const live = intercept("/"); await live.answer; await live.work;
  assert.equal(calls.at(-1).options.credentials, "omit");
  offline = true;
  const fallback = intercept("/");
  assert.equal(await fallback.answer, shellCache.get("/")); await fallback.work;
  context.testResponse = response("/", { "Cache-Control": "private" });
  assert.equal(vm.runInContext('cacheable(testResponse, "/")', context), false);
  context.testResponse = response("/", { "Set-Cookie": "secret" });
  assert.equal(vm.runInContext('cacheable(testResponse, "/")', context), false);
  context.testResponse = response("/phone/api/me");
  assert.equal(vm.runInContext('cacheable(testResponse, "/")', context), false);
  console.log("PASS service worker offline shell, credentials, API bypass, cache guards and version cleanup");
})().catch((error) => { console.error(error); process.exitCode = 1; });
