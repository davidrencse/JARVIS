// Reuse the phone VM harness without changing the separately owned mobile tests.
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const filename = path.join(__dirname, '../mobile/web_phone_test.cjs');
const harness = fs.readFileSync(filename, 'utf8').split('(async () => {\n  const names =')[0];
const checks = `
(async () => {
  const token = 'savedCsrfToken0123456789';
  const p = page({ storage: { 'jarvis.phone.csrf': token } });
  p.state.routes['GET /phone/api/me'] = () => [503, {code:'no_relay', error:'The authority refused that.'}];
  await p.settle(10);
  assert.equal(p.els['down-h'].textContent, 'Reconnecting to JARVIS');
  assert.match(p.els['down-p'].textContent, /automatically/);
  assert.equal(p.storage('jarvis.phone.csrf'), token);
  p.state.routes['GET /phone/api/me'] = () => [200, p.state.me];
  await p.settle(15100);
  assert.equal(p.els.app.hidden, false);
  p.state.routes['GET /phone/api/overview'] = () => [503, {code:'no_main',error:'No ready MAIN.'}];
  await p.settle(5100);
  assert.equal(p.els.net.hidden, false);
  assert.equal(p.els.pair.hidden, true);
  assert.equal(p.storage('jarvis.phone.csrf'), token);
  p.state.overview.devices.forEach(d => {d.role = d.id === 'dell' ? 'main' : 'node';});
  p.state.routes['GET /phone/api/overview'] = () => [200, p.state.overview];
  await p.settle(5100);
  assert.equal(p.els.net.hidden, true);
  assert.equal(p.els['main-label'].textContent, p.state.overview.devices.find(d => d.id === 'dell').label);
  assert.equal(p.callsTo('/phone/api/logout').length, 0);
  console.log('PASS automatic phone recovery');
})().catch(e => {console.error(e); process.exitCode=1;});
`;
const m = new Module(filename, module);
m.filename = filename;
m.paths = module.paths;
m._compile(harness + checks, filename);
