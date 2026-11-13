"""Per-file parse cache for the design page's nested module walk."""

import ast
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from jarvis import sysview
from jarvis.sysview import _import_targets, _module_name

INIT = '''"""Package doc."""
from jarvis.sub import worker
import jarvis.memory

def visible():
    return 1
'''
WORKER = '''"""Does work."""
from . import helper

class Public:
    pass

def load():
    import jarvis.locking
'''
GONE = '''"""Gone soon."""
import jarvis.events

def farewell():
    return 0
'''
BROKEN = "def oops(\n"


def _uncached_nested(package):
    """The walk before the mtime cache: read and parse every nested file."""
    nodes, imports = {}, {}
    package = Path(package)
    if not package.is_dir():
        return nodes, imports
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(package)
        if len(rel.parts) == 1:
            continue
        name = _module_name(path, package)
        if not name:
            continue
        text = path.read_text(encoding="utf-8")
        doc, defs, tree = "", [], None
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            lines = (ast.get_docstring(tree) or "").strip().splitlines()
            doc = lines[0] if lines else ""
            defs = [n.name for n in tree.body
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not n.name.startswith("_")][:12]
        qual = tuple(name.split("."))
        nodes[name] = {"loc": text.count("\n") + 1, "doc": doc, "defs": defs}
        imports[name] = _import_targets(tree, qual if rel.name == "__init__.py" else qual[:-1])
    return nodes, imports


class NestedModuleCacheTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(sysview._NESTED)
        sysview._NESTED.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._restore)
        self.pkg = Path(self.tmp.name) / "pkg"
        (self.pkg / "sub").mkdir(parents=True)
        (self.pkg / "top.py").write_text("x = 1\n", encoding="utf-8")
        (self.pkg / "__init__.py").write_text('"""root"""\n', encoding="utf-8")
        (self.pkg / "sub" / "__init__.py").write_text(INIT, encoding="utf-8")
        (self.pkg / "sub" / "worker.py").write_text(WORKER, encoding="utf-8")
        (self.pkg / "sub" / "gone.py").write_text(GONE, encoding="utf-8")
        (self.pkg / "sub" / "broken.py").write_text(BROKEN, encoding="utf-8")

    def _restore(self):
        self.tmp.cleanup()
        sysview._NESTED.clear()
        sysview._NESTED.update(self.saved)

    def test_second_call_reparses_nothing(self):
        first = sysview._nested_modules(self.pkg)
        worker = str(self.pkg / "sub" / "worker.py")
        with mock.patch("jarvis.sysview.ast.parse", wraps=ast.parse) as parse, \
                mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
            second = sysview._nested_modules(self.pkg)
        self.assertEqual(parse.call_count, 0)
        self.assertEqual(read.call_count, 0)
        self.assertEqual(second, first)
        self.assertIsNot(second[0]["sub.worker"]["defs"], sysview._NESTED[worker][1]["defs"])
        self.assertIsNot(second[1]["sub.worker"], sysview._NESTED[worker][2])
        second[0]["sub.worker"]["defs"].append("nope")
        second[0]["sub.worker"]["doc"] = "changed"
        second[1]["sub.worker"].append("nope")
        third = sysview._nested_modules(self.pkg)
        self.assertEqual(third, first)
        self.assertNotIn("nope", sysview._NESTED[worker][1]["defs"])
        self.assertNotIn("nope", sysview._NESTED[worker][2])

    def test_touch_reparses_only_that_file(self):
        before = sysview._nested_modules(self.pkg)
        worker = self.pkg / "sub" / "worker.py"
        worker.write_text(WORKER + "\ndef extra():\n    return 1\n", encoding="utf-8")
        ns = max(time.time_ns(), worker.stat().st_mtime_ns) + 1_000_000_000
        os.utime(worker, ns=(ns, ns))
        with mock.patch("jarvis.sysview.ast.parse", wraps=ast.parse) as parse, \
                mock.patch.object(Path, "read_text", autospec=True, side_effect=Path.read_text) as read:
            after = sysview._nested_modules(self.pkg)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(read.call_count, 1)
        self.assertIn("def extra", parse.call_args.args[0])
        self.assertEqual(read.call_args.args[0], worker)
        self.assertEqual(after[0]["sub.worker"]["defs"], ["Public", "load", "extra"])
        for name in before[0]:
            if name == "sub.worker":
                continue
            self.assertEqual(after[0][name], before[0][name])
            self.assertEqual(after[1][name], before[1][name])
        self.assertEqual(after[1]["sub.worker"], before[1]["sub.worker"])

    def test_deleted_file_drops_out(self):
        gone = self.pkg / "sub" / "gone.py"
        nodes, imports = sysview._nested_modules(self.pkg)
        self.assertIn("sub.gone", nodes)
        self.assertIn("sub.gone", imports)
        self.assertIn(str(gone), sysview._NESTED)
        gone.unlink()
        with mock.patch("jarvis.sysview.ast.parse", wraps=ast.parse) as parse:
            nodes, imports = sysview._nested_modules(self.pkg)
        self.assertEqual(parse.call_count, 0)
        self.assertNotIn("sub.gone", nodes)
        self.assertNotIn("sub.gone", imports)
        self.assertNotIn(str(gone), sysview._NESTED)
        self.assertIn("sub.worker", nodes)

    def test_cached_result_matches_uncached(self):
        nested = [p for p in self.pkg.rglob("*.py") if len(p.relative_to(self.pkg).parts) > 1]
        with mock.patch("jarvis.sysview.ast.parse", wraps=ast.parse) as parse:
            cached = sysview._nested_modules(self.pkg)
        self.assertEqual(parse.call_count, len(nested))
        self.assertNotIn("top", cached[0])
        self.assertEqual(cached[0]["sub"], {
            "loc": INIT.count("\n") + 1, "doc": "Package doc.", "defs": ["visible"]})
        self.assertEqual(cached[1]["sub"], ["sub", "sub.worker", "memory"])
        self.assertEqual(cached[0]["sub.worker"]["defs"], ["Public", "load"])
        self.assertEqual(cached[1]["sub.worker"], ["sub.helper", "locking"])
        self.assertEqual(cached[0]["sub.gone"]["defs"], ["farewell"])
        self.assertEqual(cached[1]["sub.gone"], ["events"])
        self.assertEqual(cached[0]["sub.broken"], {"loc": BROKEN.count("\n") + 1, "doc": "", "defs": []})
        self.assertEqual(cached[1]["sub.broken"], [])
        fresh = _uncached_nested(self.pkg)
        self.assertEqual(list(cached[0]), list(fresh[0]))
        self.assertEqual(cached, fresh)
        with mock.patch("jarvis.sysview.ast.parse", wraps=ast.parse) as parse:
            self.assertEqual(sysview._nested_modules(self.pkg), fresh)
        self.assertEqual(parse.call_count, 0)


if __name__ == "__main__":
    unittest.main()
