"""The hardware-reading script parses, and a fake DOM prints the blocks it is given."""

import shutil
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WEB = HERE.parents[1] / "jarvis" / "web"


@unittest.skipUnless(shutil.which("node"), "node is required")
class MetersWebTests(unittest.TestCase):
    def test_syntax(self):
        for name in ("meters.js", "app.js", "phone/phone.js"):
            result = subprocess.run(["node", "--check", str(WEB / name)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, name + "\n" + result.stderr)

    def test_renderer(self):
        result = subprocess.run(["node", str(HERE / "web_meters_test.cjs")], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertGreaterEqual(result.stdout.count("PASS "), 8)

    def test_a_probe_without_power_supply_keeps_the_known_battery(self):
        result = subprocess.run(["node", str(HERE / "web_app_meters_card_test.cjs")], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_script_order(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index('src="meters.js"'), html.index('src="app.js"'))
        phone = (WEB / "phone" / "index.html").read_text(encoding="utf-8")
        self.assertLess(phone.index('src="/meters.js"'), phone.index('src="/phone.js"'))
        self.assertIn('id="d-meters"', phone)


if __name__ == "__main__":
    unittest.main()
