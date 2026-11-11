"""Runs the fleet console tests (tests/web/web_fleet_test.cjs) in Node with a fake DOM. No network, no clipboard."""
import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parent.parent / "jarvis" / "web"


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FleetWebTests(unittest.TestCase):
    def test_scenarios(self):
        r = subprocess.run(["node", str(HERE / "web_fleet_test.cjs")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGreaterEqual(r.stdout.count("PASS "), 20)

    def test_syntax(self):
        r = subprocess.run(["node", "--check", str(WEB / "fleet.js")], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_static_rules(self):
        src = (WEB / "fleet.js").read_text()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "localStorage", "sessionStorage", "console.log", "console.error", "console.warn", "console.debug", "eval(", "new Function"):
            self.assertNotIn(bad, src, bad)
        self.assertNotIn("/api/fleet/main-transfer\"", src)   # only /main-transfer/preview may be called
        html = (WEB / "index.html").read_text()
        self.assertIn('id="fleet-root"', html)
        self.assertLess(html.index("settings.js"), html.index("fleet.js"))


if __name__ == "__main__":
    unittest.main()
