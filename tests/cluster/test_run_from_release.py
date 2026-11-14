"""cluster.run_from_release: the main's services import the tested release copy."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import autostart, home as home_mod, release_runtime as runtime


def make_release(state, code='rel1'):
    folder = Path(state) / 'code-release'
    (folder / 'jarvis').mkdir(parents=True)
    (folder / 'release.json').write_text(json.dumps({'code': code}))
    return folder


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.state = self.root / '.jarvis'
        self.state.mkdir()

    def config(self, **cluster):
        (self.root / 'jarvis.config.json').write_text(json.dumps({'cluster': cluster}))


class UnitTests(Base):
    argv = ['/v/bin/python', '-m', 'jarvis', '--state', 'STATE', 'cluster', 'run']

    def args(self):
        return [*self.argv[:4], str(self.state), *self.argv[5:]]

    def test_off_by_default_and_without_release(self):
        self.assertEqual(autostart.release_env(self.args(), self.root), [])
        self.config(run_from_release=True)
        self.assertEqual(autostart.release_env(self.args(), self.root), [])  # no release yet
        self.config(run_from_release=False)
        make_release(self.state)
        self.assertEqual(autostart.release_env(self.args(), self.root), [])

    def test_on_points_at_release_and_keeps_checkout_home(self):
        self.config(run_from_release=True)
        folder = make_release(self.state)
        line, = autostart.release_env(self.args(), self.root)
        self.assertIn(f'"PYTHONPATH={folder}"', line)
        self.assertIn('"PYTHONSAFEPATH=1"', line)
        self.assertIn(f'"JARVIS_HOME={self.root}"', line)

    def test_only_jarvis_units(self):
        self.config(run_from_release=True)
        make_release(self.state)
        self.assertEqual(autostart.release_env(['/usr/bin/ssh', '-N'], self.root), [])

    def test_systemd_unit_carries_the_environment(self):
        self.config(run_from_release=True)
        make_release(self.state)
        with patch.object(autostart, 'ROOT', self.root):
            unit = autostart.systemd_unit('t', self.args())
        self.assertIn('Environment="PYTHONPATH=', unit)
        with patch.object(autostart, 'ROOT', self.root):
            (self.root / 'jarvis.config.json').write_text('{}')
            self.assertNotIn('PYTHONPATH', autostart.systemd_unit('t', self.args()))


class HomeTests(unittest.TestCase):
    def test_env_only_honoured_from_a_release_folder(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'JARVIS_HOME': tmp}):
            self.assertEqual(home_mod.home(), home_mod.PACKAGE_PARENT)  # ordinary checkout/test stage
            with patch.object(home_mod, 'PACKAGE_PARENT', Path('/x/.jarvis/code-release')):
                self.assertEqual(home_mod.home(), Path(tmp).resolve())

    def test_relative_or_missing_env_is_ignored(self):
        with patch.object(home_mod, 'PACKAGE_PARENT', Path('/x/code-release')):
            for value in ('relative', '/does/not/exist'):
                with patch.dict('os.environ', {'JARVIS_HOME': value}):
                    self.assertEqual(home_mod.home(), Path('/x/code-release'))


class RestartTests(Base):
    def hub(self, loaded='old', **cluster):
        return SimpleNamespace(state=self.state, loaded_code=loaded,
                               config={'cluster': {'auto_restart': True, **cluster}})

    def run_restart(self, hub, dev='dirty', **patches):
        with patch('jarvis.deploy.code_fingerprint', return_value=dev), \
                patch.object(runtime, 'busy', return_value=patches.get('busy', False)), \
                patch('jarvis.cluster._systemd_service', return_value=True), \
                patch.object(runtime, 'running_from', return_value=patches.get('where', 'dev tree')), \
                patch.object(runtime, 'unit_runs_release', return_value=patches.get('unit', False)), \
                patch('os._exit', side_effect=SystemExit(75)) as leave:
            try:
                runtime.maybe_restart(hub)
            except SystemExit:
                pass
            return leave.called

    def test_dirty_checkout_blocks_restart_when_flag_off(self):
        make_release(self.state, 'rel1')
        self.assertFalse(self.run_restart(self.hub()))

    def test_flag_on_restarts_despite_dirty_checkout_once_unit_is_switched(self):
        make_release(self.state, 'rel1')
        self.assertTrue(self.run_restart(self.hub(run_from_release=True), unit=True))
        self.assertTrue(self.run_restart(self.hub(run_from_release=True), where='release'))

    def test_flag_on_but_unit_not_switched_does_not_loop(self):
        make_release(self.state, 'rel1')  # a restart would reload the same checkout code
        self.assertFalse(self.run_restart(self.hub(run_from_release=True)))

    def test_busy_guard_unchanged(self):
        make_release(self.state, 'rel1')
        self.assertFalse(self.run_restart(self.hub(run_from_release=True), unit=True, busy=True))

    def test_loaded_equals_release_never_restarts(self):
        make_release(self.state, 'rel1')
        self.assertFalse(self.run_restart(self.hub(loaded='rel1', run_from_release=True), unit=True))


class DoctorLineTests(Base):
    def test_release_dev_and_misconfigured(self):
        folder = make_release(self.state, 'rel1')
        with patch('jarvis.deploy.PACKAGE', folder / 'jarvis'):
            ok, detail, _ = runtime.run_source({}, self.state)
        self.assertTrue(ok)
        self.assertTrue(detail.startswith('release'))
        ok, detail, _ = runtime.run_source({}, self.state)
        self.assertEqual((ok, detail.split(' ')[0]), (True, 'dev'))
        ok, detail, fix = runtime.run_source({'cluster': {'run_from_release': True}}, self.state)
        self.assertFalse(ok)
        self.assertIn('cluster autostart on', fix)

    def test_no_release_yet_says_cut_one(self):
        ok, _, fix = runtime.run_source({'cluster': {'run_from_release': True}}, self.state)
        self.assertEqual((ok, fix), (False, 'jarvis cluster release'))

    def test_unit_detection(self):
        folder = make_release(self.state)
        unit = self.root / 'u.service'
        unit.write_text(f'Environment="PYTHONPATH={folder.resolve()}"\n')
        self.assertTrue(runtime.unit_runs_release(self.state, unit))
        unit.write_text('ExecStart=x\n')
        self.assertFalse(runtime.unit_runs_release(self.state, unit))
        self.assertFalse(runtime.unit_runs_release(self.state, self.root / 'none'))


if __name__ == '__main__':
    unittest.main()
