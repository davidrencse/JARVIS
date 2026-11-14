"""Console startup must handle owner-origin refusal and attach only over loopback."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis import cluster_proxy as proxy
from jarvis.cluster_store import OWNER_TOKEN
from tests.cluster.test_cluster import configuration


class ConsoleOwnerStartup(unittest.TestCase):
    def test_extra_window_uses_actual_local_hub_listener(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);(state/OWNER_TOKEN).write_text('fixture-owner-token-123456789')
            cfg=configuration();cfg['cluster']['main_url']='https://public-successor.example:8443'
            before=copy.deepcopy(cfg)
            hub=SimpleNamespace(main='main',server=SimpleNamespace(server_address=('127.0.0.1',7781)))
            client=Mock();client.request.return_value={'main_device':'main'}
            with patch.dict(proxy._HUBS,{str(state.resolve()):hub},clear=True), \
                 patch.object(proxy,'Client',return_value=client) as make, \
                 patch('jarvis.ui.ConsoleServer',return_value='local-window'), \
                 patch.object(proxy,'proxy_handler',return_value=object()):
                server,_runtime=proxy.local_server(cfg,state,state,7777)
            self.assertEqual(server,'local-window')
            selected=make.call_args.args[0]['cluster']
            self.assertEqual(selected['main_url'],'http://127.0.0.1:7781')
            self.assertEqual(selected['main_device'],'main')
            self.assertEqual(cfg,before)

    def test_refused_stale_owner_origin_does_not_crash_new_runtime_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);(state/OWNER_TOKEN).write_text('fixture-owner-token-123456789')
            console=Mock();hub=Mock()
            with patch.dict(proxy._HUBS,{},clear=True), \
                 patch.object(proxy,'Client',side_effect=ValueError('owner origin refused')), \
                 patch.object(proxy,'Hub',return_value=hub), \
                 patch('jarvis.ui.Console',return_value=SimpleNamespace(start_live=lambda:console)), \
                 patch('jarvis.ui.ConsoleServer',return_value='local-window'), \
                 patch('jarvis.phone.start',return_value=None), \
                 patch.object(proxy,'close_on_sigterm',return_value=None):
                server,runtime=proxy.local_server(configuration(),state,state,7777)
            self.assertEqual(server,'local-window')
            self.assertIs(runtime.hub,hub)
            hub.start.assert_called_once()
