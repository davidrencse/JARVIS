"""Low-priority, isolated, two-tier release tests. No model or live-node calls."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Run inside the staged commit, using stdlib only. Persist results separately from
# test output: tracebacks and chatty tests must not obscure a failing test's ID.
RUNNER = r'''
import json, sys, unittest
from pathlib import Path
mode, output = sys.argv[1:3]
loader = unittest.TestLoader()
if mode == 'full':
    suite = loader.discover('tests', top_level_dir='.')
elif mode == 'core':
    suite = unittest.TestSuite()
    for path in sorted(Path('tests').rglob('test*.py')):
        text = path.as_posix().lower()
        if any(x in text for x in ('tests/cluster/', 'tests/security/', 'gates', 'hard_deny', 'transfer', 'autostart')):
            suite.addTests(loader.loadTestsFromName('.'.join(path.with_suffix('').parts)))
else:
    suite = loader.loadTestsFromName(mode)
def flat(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            yield from flat(t)
        else:
            yield t
ids = [t.id() for t in flat(suite)]
class Result(unittest.TextTestResult):
    safety = set()
    def startTest(self, test):
        super().startTest(test)
        self.tag(test)
        failures = sorted({getattr(t, 'test_case', t).id() for t, _ in self.failures + self.errors})
        Path(output).write_text(json.dumps({'failures': failures, 'safety': sorted(self.safety),
                                           'active': test.id(), 'complete': False, 'count': self.testsRun}))
    def tag(self, test):
        parent = getattr(test, 'test_case', test)
        method = getattr(parent, getattr(parent, '_testMethodName', ''), None)
        tags = getattr(parent, 'tags', ()) or ()
        if getattr(parent, 'safety', False) or getattr(method, 'safety', False) or 'safety' in tags:
            self.safety.add(parent.id())
    def addFailure(self, test, err):
        self.tag(test)
        super().addFailure(test, err)
    def addError(self, test, err):
        self.tag(test)
        super().addError(test, err)
    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.tag(test)
        super().addSubTest(test, subtest, err)
result = unittest.TextTestRunner(verbosity=1, resultclass=Result).run(suite)
def identity(test):
    return getattr(test, 'test_case', test).id()
failures = sorted({identity(t) for t, _ in result.failures + result.errors})
Path(output).write_text(json.dumps({'failures': failures, 'safety': sorted(result.safety), 'ids': ids, 'count': result.testsRun, 'complete': True}))
sys.exit(0 if result.wasSuccessful() else 1)
'''


def wait_for_capacity(wait=60, interval=2):
    """Bounded deferral. Do not start tests when the host remains overloaded."""
    deadline = time.monotonic() + wait
    while True:
        try:
            overloaded = os.getloadavg()[0] > (os.cpu_count() or 1)
        except (AttributeError, OSError):
            return True
        if not overloaded:
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(interval, left))


def _run(stage, selector, timeout):
    env = {k: v for k, v in os.environ.items() if not k.startswith('CODEX_')}
    env.update(PYTHONPATH=str(stage), JARVIS_NO_MODELS='1', JARVIS_NO_NOTIFY='1', PYTHONDONTWRITEBYTECODE='1')
    prefix = []
    if os.name != 'nt':
        if shutil.which('nice'):
            prefix += ['nice', '-n', '19']
        if shutil.which('ionice'):
            prefix += ['ionice', '-c', '3']
    with tempfile.TemporaryDirectory(prefix='jarvis-gate-result-') as tmp:
        result = Path(tmp) / 'result.json'
        try:
            done = subprocess.run([*prefix, sys.executable, '-c', RUNNER, selector, str(result)],
                                  cwd=stage, env=env, capture_output=True, text=True,
                                  timeout=max(1, timeout), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            report = json.loads(result.read_text())
            if not report.get('complete') or not report.get('count'):
                raise ValueError('runner did not finish a nonempty suite')
            if done.returncode and not report.get('failures'):
                raise ValueError('runner exited without test failures')
            return report
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            try:
                partial = json.loads(result.read_text())
            except (OSError, ValueError):
                partial = {}
            names = partial.get('failures', [])
            active = partial.get('active')
            if active:
                names = sorted(set([*names, active]))
            return {**partial, 'failures': [*names, 'runner:' + selector],
                    'safety': partial.get('safety', []), 'ids': [], 'error': str(exc)}


def _tier(stage, selector, timeout):
    report = _run(stage, selector, timeout)
    failures = []
    retried = []
    safety = set(report.get('safety', []))
    for name in report.get('failures', []):
        if name.startswith('runner:'):
            failures.append(name)
            continue
        retried.append(name)
        retry = _run(stage, name, min(timeout, 300))
        safety.update(retry.get('safety', []))
        if retry.get('failures'):
            failures.append(name)
    return {**report, 'failures': failures, 'retried': retried, 'safety': sorted(safety)}


def safety(name):
    text = name.lower()
    return any(token in text for token in ('security.', '.gates', 'test_gates', 'hard_deny', 'autostart'))


def run_tests(stage, timeout=3600):
    if not wait_for_capacity():
        return False, {'deferred': True, 'failures': [], 'warnings': [],
                       'summary': 'Release deferred: host load exceeds CPU count.'}
    core = _tier(stage, 'core', timeout)
    if core['failures']:
        return False, {'failures': core['failures'], 'warnings': [], 'core': core,
                       'summary': 'Core tests failed: ' + ', '.join(core['failures'])}
    full = _tier(stage, 'full', timeout)
    core_ids = set(core.get('ids', []))
    tagged = set(full.get('safety', []))
    blocked = [name for name in full['failures'] if safety(name) or name in tagged or name in core_ids]
    warnings = [name for name in full['failures'] if name not in blocked]
    return not blocked, {'failures': blocked, 'warnings': warnings, 'core': core, 'full': full,
                         'summary': ('Safety tests failed: ' + ', '.join(blocked) if blocked else
                                     'Release shipped with warnings: ' + ', '.join(warnings) if warnings else 'Core and full suite passed.')}
