"""Console copy for shared mouse and keyboard: apart, together, unknown. No browser."""

import shutil
import subprocess
import unittest
from pathlib import Path

NODE = shutil.which("node")
SCRIPT = Path(__file__).with_name("web_sharing_apart.cjs")


@unittest.skipUnless(NODE, "Node.js is required for the shared-input panel tests")
class SharingPanelTests(unittest.TestCase):
    def check(self, mode):
        result = subprocess.run([NODE, str(SCRIPT), mode], capture_output=True,
                                text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS " + mode, result.stdout)

    def test_apart_hides_local_sharing_and_keeps_remote(self):
        self.check("apart")

    def test_hand_start_is_not_offered_again(self):
        self.check("apart-hand")

    def test_unknown_says_so_and_leaves_the_controls(self):
        self.check("unknown")

    def test_together_draws_the_controls(self):
        self.check("together")

    def test_switch_off_does_not_hide_sharing(self):
        self.check("switch-off")


if __name__ == "__main__":
    unittest.main()
