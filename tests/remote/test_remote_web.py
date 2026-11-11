"""The console's Remote view (jarvis/web/remote.js) in a Node VM with fake DOM, network and
timers (tests/remote/web_remote_test.cjs). No real pointer, keyboard or network is touched."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SCRIPT = Path(__file__).with_name("web_remote_test.cjs")
SCENARIOS = re.findall(r"^  async (\w+)\(\)", SCRIPT.read_text(encoding="utf-8"), re.M)


@unittest.skipUnless(NODE, "Node.js is required for the Remote view's browser tests")
class RemoteViewBrowserTests(unittest.TestCase):
    def test_every_scenario(self):
        self.assertGreaterEqual(len(SCENARIOS), 15)
        for name in SCENARIOS:
            with self.subTest(name):
                result = subprocess.run([NODE, str(SCRIPT), name], capture_output=True,
                                        text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("PASS " + name, result.stdout)


if __name__ == "__main__":
    unittest.main()
