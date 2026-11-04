"""Execute the shipped quota renderer with isolated DOM stubs."""
import shutil
import subprocess
import unittest
from pathlib import Path


NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(NODE, "Node.js required for dashboard regression")
class CreditDashboardTests(unittest.TestCase):
    def test_missing_expired_and_unknown_windows_are_not_relabelled(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync('jarvis/web/dashboard.js', 'utf8');
const renderer = source.slice(source.indexOf('  function limits() {'), source.indexOf('  on("credits",'));
function render(meters) {
  const dom = {};
  const context = {credits: [{id: 'codex', meters}], probe: null,
    $: (id) => dom[id] ||= {}, esc: (s) => String(s), icon: () => '', until: () => 'later'};
  vm.runInNewContext(renderer + '\nlimits();', context);
  const html = dom['#limit-stats'].innerHTML;
  return {five: html.split('Codex, 5 h')[1].split('</b>')[0],
          week: html.split('Codex, week')[1].split('</b>')[0]};
}
const weekly = {label: 'Weekly window', pct: 14};
for (const primary of [null, {label: '5-hour window', pct: 6, resets_at: '2000-01-01T00:00:00Z'},
                       {label: '5-hour window', pct: null}, {label: 'Primary window', pct: 6}]) {
  const result = render(primary ? [primary, weekly] : [weekly]);
  assert(result.five.endsWith('<b>—'));
  assert(result.week.endsWith('<b>86%'));
}
const valid = render([weekly, {label: '5-hour window', pct: 6}]);
assert(valid.five.endsWith('<b>94%'));
assert(valid.week.endsWith('<b>86%'));
console.log('quota windows correctly identified');
'''
        result = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()


class UsageLockTests(unittest.TestCase):
    def test_each_provider_has_its_own_usage_lock(self):
        import tempfile
        from jarvis.credits import Credits
        with tempfile.TemporaryDirectory() as state:
            locks = Credits(state)._usage_locks
        self.assertIsNot(locks["claude"], locks["codex"])
