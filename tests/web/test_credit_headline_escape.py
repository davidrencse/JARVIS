"""The console's credit headline must escape a hostile unit (it is spliced into innerHTML)."""
import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(NODE, "Node.js required")
class CreditHeadlineEscapeTests(unittest.TestCase):
    def test_hostile_unit_is_escaped_in_headline(self):
        script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const src = fs.readFileSync('jarvis/web/app.js', 'utf8');
const start = src.indexOf('  const nf = new Intl.NumberFormat();');
const mid = src.indexOf('  const stamp = (iso)');
const h0 = src.indexOf('  function headline(r) {');
const h1 = src.indexOf('  let creditRowsSig');
assert(start >= 0 && mid > start && h0 > 0 && h1 > h0, 'markers moved');
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const ctx = {esc, Intl, Number, Math, isNaN};
vm.runInNewContext(src.slice(start, mid) + src.slice(h0, h1) + '\nthis.headline = headline;', ctx);
const evil = '</span><style>*{display:none}</style>';
const html = ctx.headline({status: 'ok', headline: {value: 5, unit: evil, label: 'balance'}});
assert(!html.includes('<style>'), html);
assert(!html.includes('</span><style'), html);
assert(html.includes('&lt;style&gt;'), html);
console.log('headline unit escaped');
'''
        result = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
