"""Exercise the real module entry point without any live operator credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ContinuityEntryTests(unittest.TestCase):
    def invoke(self, root, *args):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('CODEX_')}
        environment.update(JARVIS_NO_MODELS='1',
                           PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        return subprocess.run([sys.executable, '-m', 'jarvis', *args], cwd=root,
                              env=environment, capture_output=True, text=True, timeout=15)

    def test_bootstrap_without_operator_config_reaches_structured_preflight_refusal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.invoke(root, 'continuity', 'bootstrap', 'preflight',
                '--hub-config', str(root / 'hub.json'), '--state', str(root / 'state'),
                '--operator-config', str(root / 'missing-operator.json'))
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('error', json.loads(result.stdout))
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse((root / 'state').exists())

    def test_status_uses_operator_errors_without_loading_application_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.invoke(root, '--config', str(root / 'missing-app.json'),
                'continuity', '--config', str(root / 'missing-operator.json'), 'status')
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('error', json.loads(result.stdout))
            self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
