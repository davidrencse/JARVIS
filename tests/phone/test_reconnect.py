"""Regression checks for a paired phone recovering through a gateway outage."""
import shutil
import subprocess
import unittest
from pathlib import Path


class PhoneReconnectTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_gateway_outage_recovers_without_pairing_or_user_action(self):
        script = Path(__file__).with_suffix('.cjs')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('PASS automatic phone recovery', result.stdout)
