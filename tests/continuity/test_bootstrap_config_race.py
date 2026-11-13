"""Bootstrap preparation must not overwrite a concurrent MAIN/config edit."""
import json
import unittest
from unittest.mock import patch

from jarvis.continuity import bootstrap
from tests.continuity import test_bootstrap as fixtures


class BootstrapConfigurationRaceTests(unittest.TestCase):
    def test_configure_refuses_role_drift_after_validation_without_publishing(self):
        fixture = fixtures.BootstrapTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        staged = bootstrap.stage(fixture.operator_path, fixture.root / 'staged',
            hub_config=fixture.hub_path, settings_manifest=fixture.settings_path,
            client_factory=fixture.factory)
        real_validation = bootstrap._validate_binding
        changed = dict(fixture.original_hub)
        changed['cluster'] = {**changed['cluster'], 'main_device': 'dell'}

        def concurrent_change(*args, **kwargs):
            real_validation(*args, **kwargs)
            fixture.hub_path.write_text(json.dumps(changed), encoding='utf-8')

        with patch.object(bootstrap, '_validate_binding', side_effect=concurrent_change):
            with self.assertRaisesRegex(bootstrap.BootstrapError, 'local_config_changed'):
                bootstrap.configure(fixture.hub_path, fixture.state, staged['operator_config'],
                    settings_manifest=staged['settings_manifest'], confirmed=True,
                    client_factory=fixture.factory)
        self.assertEqual(json.loads(fixture.hub_path.read_text()), changed)
        self.assertFalse(bootstrap._operator_target().exists())
        self.assertFalse(fixture.hub_path.with_name(
            fixture.hub_path.name + '.pre-continuity-bootstrap').exists())
        self.assertFalse((fixture.state / 'continuity-managed.json').exists())


if __name__ == '__main__':
    unittest.main()
