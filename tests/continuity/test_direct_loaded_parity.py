"""Direct controller calls attest runtime code before probing or moving data."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.continuity.production import ProductionController
from jarvis.continuity.runtime import RuntimeRefusal


class DirectRuntimeParity(unittest.TestCase):
    def test_stale_running_hub_refuses_before_broker_or_transfer(self):
        controller=object.__new__(ProductionController)
        controller.state='unused'
        controller.hub=SimpleNamespace(broker=Mock())
        with patch('jarvis.release_runtime.require_loaded_code',side_effect=ValueError('stale')) as gate, \
             patch('jarvis.release.code_parity') as parity:
            with self.assertRaises(RuntimeRefusal) as caught:
                controller._require_transfer_parity('dell')
        self.assertEqual(caught.exception.code,'code_mismatch')
        gate.assert_called_once_with(controller.hub)
        controller.hub.broker.nodes.assert_not_called()
        parity.assert_not_called()
