"""Static checks over the console's app.js that need no browser."""
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "jarvis" / "web" / "app.js"


class AppJsScopeTests(unittest.TestCase):
    def test_never_calls_bare_prompt(self):
        # audit SU-1: `const prompt = $("#prompt")` shadows window.prompt inside the IIFE,
        # so a bare prompt(...) call throws and "New investigation…" silently did nothing
        src = APP.read_text(encoding="utf-8")
        self.assertRegex(src, r"const prompt\s*=")  # the shadowing const this guards against
        bare = [m.start() for m in re.finditer(r"(?<![\w.$])prompt\s*\(", src)]
        self.assertEqual(bare, [], "use window.prompt(...): `prompt` is the #prompt textarea")


if __name__ == "__main__":
    unittest.main()
