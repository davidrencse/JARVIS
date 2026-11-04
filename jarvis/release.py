"""Which code nodes get: a tested snapshot of the main's package, not its live working tree.

Nodes used to pull whatever `jarvis/` held on disk, and every saved file changed the
fingerprint, so a half-finished edit reached them at once: on 2026-10-30 the Dell pulled code
mid-edit at 14:38, its node agent stopped checking in, and nothing put the old code back.

Main side: `cut()` copies the repo into a staging folder (only once the tree has been quiet
for QUIET seconds, and only if it didn't change while copying), runs the full test suite on
that copy, and only when it passes swaps the copy's `jarvis/` and `pyproject.toml` into
`.jarvis/code-release/`. /v1/code, the SSH push (`jarvis cluster update`) and the main's
"same code?" check all use that snapshot (`served_code`). The hub cuts one by itself when
the tree has changed and gone quiet; `jarvis cluster release` cuts one now. A fingerprint that
failed its tests isn't tried again until the code changes.

Node side: before installing, `backup()` copies the running package to
`.jarvis/code-prev/`. `guard()` then starts a stdlib-only watchdog (GUARD, run from a file in
the state folder, so it doesn't import the code it judges) that waits for the new code to
check in (`confirm()`, after its first successful poll). If it doesn't within GUARD_WAIT
seconds, or dies first, the watchdog restores the backup, records the fingerprint as bad
(`is_bad`: never pulled again), and starts the old code.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .deploy import EXCLUDE, PACKAGE, SKIP, _files, code_fingerprint

RELEASE = "code-release"        # in the main's state folder: jarvis/, pyproject.toml, release.json
FAILED = "code-release-failed.json"
QUIET = 60                      # seconds without a saved file before the tree counts as settled
TEST_TIMEOUT = 900
# Copied into the staging folder for the test run: everything but history, state and caches.
STAGE_SKIP = {".git", ".jarvis", ".venv", "node_modules", "__pycache__", "jarvis.egg-info",
              "dist", "build"}

BACKUP = "code-prev"            # in the node's state folder: the code that ran before an update
CONFIRMED = "cluster-code-ok.json"
BAD = "cluster-code-bad.json"
GUARD_FILE = "code-guard.py"
GUARD_WAIT = 120                # seconds new code has to check in before it's rolled back


# -- main: cutting and serving a release -------------------------------------------------------

def release_dir(state):
    return Path(state) / RELEASE


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, value):
    from .fsio import atomic_write
    atomic_write(path, json.dumps(value, indent=2))


def current(state):
    """The release nodes get now ({code, at, tests}), or {} before the first one."""
    note = _read(release_dir(state) / "release.json")
    return note if (release_dir(state) / "jarvis").is_dir() and note.get("code") else {}


def served_code(state):
    """The fingerprint nodes must run. Before any release exists: the live tree, as before."""
    return current(state).get("code") or code_fingerprint()


def source(state):
    """The package folder nodes are sent (the release's jarvis/, else the live one)."""
    return release_dir(state) / "jarvis" if current(state) else PACKAGE


def settled(root=PACKAGE, quiet=QUIET, now=None):
    """True when no package file was saved in the last `quiet` seconds."""
    newest = 0
    for path in _files(Path(root)):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue  # vanished since the listing
    return (now or time.time()) - newest >= quiet


def due(state, quiet=QUIET):
    """Should the hub cut a release now? The live code differs from the release, hasn't
    already failed its tests, and has been left alone for `quiet` seconds."""
    live = code_fingerprint()
    if live == current(state).get("code") or live == _read(Path(state) / FAILED).get("code"):
        return False
    return settled(quiet=quiet)


def _stage(repo, stage):
    def skip(folder, names):
        return [n for n in names if n in STAGE_SKIP or n.endswith(".pyc")]
    for item in Path(repo).iterdir():
        if item.name in STAGE_SKIP:
            continue
        target = Path(stage) / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=skip, symlinks=True)
        elif item.is_file():
            shutil.copy2(item, target)


def run_tests(stage, timeout=TEST_TIMEOUT):
    """(passed, last lines of output) for the suite run on the staged copy."""
    env = {**os.environ, "PYTHONPATH": str(stage)}
    nice = ["nice", "-n", "10"] if os.name != "nt" and shutil.which("nice") else []
    try:
        out = subprocess.run([*nice, sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                             cwd=stage, env=env, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, f"tests took over {timeout} s"
    tail = "\n".join((out.stderr or out.stdout).strip().splitlines()[-3:])
    return out.returncode == 0, tail


def cut(state, repo=None, quiet=QUIET, tests=run_tests):
    """Test a snapshot of the code and, if it passes, make it what nodes get. Returns a
    report: {released, code, ...}. Never touches the release when anything fails."""
    from .locking import exclusive_lock
    repo = Path(repo or PACKAGE.parent)
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    lock = exclusive_lock(state / "code-release.lock")
    try:
        lock.__enter__()
    except ValueError:  # busy: the hub's own cut, or `jarvis cluster release`
        return {"released": False, "note": "Another release is being tested."}
    try:
        return _cut(state, repo, quiet, tests)
    finally:
        lock.__exit__(None, None, None)


def _cut(state, repo, quiet, tests):
    package = repo / "jarvis"
    if quiet and not settled(package, quiet):
        return {"released": False, "note": f"Code was saved in the last {quiet} s; waiting for it to settle."}
    live = code_fingerprint(package)
    if live == current(state).get("code"):
        return {"released": False, "code": live, "note": "Nodes already get this code."}
    with tempfile.TemporaryDirectory(prefix="jarvis-release-") as stage:
        stage = Path(stage)
        _stage(repo, stage)
        code = code_fingerprint(stage / "jarvis")
        if code != live or code_fingerprint(package) != live:
            return {"released": False, "note": "Code changed while it was being copied; try again."}
        passed, tail = tests(stage)
        if not passed:
            _write(state / FAILED, {"code": code, "at": time.time(), "tests": tail})
            return {"released": False, "code": code, "tests": tail,
                    "note": "Tests failed; nodes keep the last release."}
        fresh, final = state / (RELEASE + ".new"), release_dir(state)
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir()
        shutil.copytree(stage / "jarvis", fresh / "jarvis", ignore=shutil.ignore_patterns(*SKIP, "*.pyc", *EXCLUDE))
        if (stage / "pyproject.toml").is_file():
            shutil.copy2(stage / "pyproject.toml", fresh / "pyproject.toml")
    if code_fingerprint(fresh / "jarvis") != code:
        shutil.rmtree(fresh, ignore_errors=True)
        return {"released": False, "note": "The release copy didn't match what was tested."}
    note = {"code": code, "at": time.time(), "tests": tail}
    _write(fresh / "release.json", note)
    old = state / (RELEASE + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        os.replace(final, old)
    os.replace(fresh, final)
    shutil.rmtree(old, ignore_errors=True)
    (state / FAILED).unlink(missing_ok=True)
    return {"released": True, **note}


# -- node: backup, confirmation, rollback ------------------------------------------------------

def backup(state, repo=None):
    """Copy the running package (and pyproject.toml) aside before an update replaces it."""
    repo = Path(repo or PACKAGE.parent)
    target = Path(state) / BACKUP
    fresh = Path(state) / (BACKUP + ".new")
    shutil.rmtree(fresh, ignore_errors=True)
    shutil.copytree(repo / "jarvis", fresh / "jarvis", ignore=shutil.ignore_patterns(*SKIP, "*.pyc", *EXCLUDE))
    if (repo / "pyproject.toml").is_file():
        shutil.copy2(repo / "pyproject.toml", fresh / "pyproject.toml")
    shutil.rmtree(target, ignore_errors=True)
    os.replace(fresh, target)
    return target


def dependencies(text):
    """[project] dependencies from a pyproject.toml's text, or None if it can't be read."""
    try:
        import tomllib
        value = tomllib.loads(text or "").get("project", {}).get("dependencies")
    except Exception:  # Python 3.10 has no tomllib; a broken file has no list
        return None
    return value if isinstance(value, list) and all(isinstance(d, str) for d in value) else None


def install_dependencies(before, after, log=print, run=subprocess.run):
    """New code brought new requirements: install them into the Python running this node,
    before the restart, so the update doesn't die on an import. Returns True when pip ran
    and succeeded, False when it failed, None when nothing changed."""
    old, new = dependencies(before), dependencies(after)
    if new is None or sorted(new) == sorted(old or []):
        return None
    log(f"JARVIS node: dependencies changed; installing {', '.join(new)}")
    try:
        out = run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *new],
                  capture_output=True, text=True, timeout=900,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"JARVIS node: pip failed: {exc}")
        return False
    if out.returncode:
        # Restart anyway: if the new code can't import without them, the watchdog rolls back.
        log(f"JARVIS node: pip failed: {(out.stderr or out.stdout).strip()[-300:]}")
        return False
    return True


def confirm(state, code):
    """The node agent checked in on `code`: the watchdog stands down."""
    _write(Path(state) / CONFIRMED, {"code": code, "at": time.time()})


def is_bad(state, code):
    """Did `code` already fail to start here (and get rolled back)?"""
    return bool(code) and _read(Path(state) / BAD).get("code") == code


# Stdlib only, and written out before the update: it must work when the new code doesn't.
GUARD = r'''
import fnmatch, json, os, shutil, subprocess, sys, time
EXCLUDE = ("*.pre-*", "*~", "*.swp", ".#*", "*.tmp")  # as deploy.EXCLUDE
def excluded(name):
    return any(fnmatch.fnmatchcase(name, x) for x in EXCLUDE)
repo, state, want, wait, mode = sys.argv[1:6]
argv = sys.argv[6:]
wait = float(wait)
def log(text):
    try:
        with open(os.path.join(state, "logs", "code-guard.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + text + "\n")
    except OSError:
        pass
def confirmed():
    try:
        with open(os.path.join(state, "cluster-code-ok.json"), encoding="utf-8") as f:
            return json.load(f).get("code") == want
    except (OSError, ValueError):
        return False
time.sleep(2)  # let the old process exit and free its lock
proc = subprocess.Popen(argv) if mode == "launch" else None
deadline = time.monotonic() + wait  # monotonic: a suspended laptop doesn't eat the wait
while time.monotonic() < deadline and not confirmed():
    if proc is not None and proc.poll() is not None:
        break
    time.sleep(1)
if confirmed():
    log("new code %s checked in" % want)
    sys.exit(proc.wait() if proc is not None else 0)
log("new code %s did not check in within %ds; rolling back" % (want, wait))
if proc is not None and proc.poll() is None:
    proc.kill()
    proc.wait()
prev = os.path.join(state, "code-prev")
if not os.path.isdir(os.path.join(prev, "jarvis")):
    log("no backup to roll back to")
    sys.exit(1)
keep = set()
for base, dirs, files in os.walk(os.path.join(prev, "jarvis")):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for name in files:
        if excluded(name):
            continue
        rel = os.path.relpath(os.path.join(base, name), prev)
        keep.add(os.path.normcase(rel))
        target = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".rollback"
        shutil.copyfile(os.path.join(base, name), tmp)
        os.replace(tmp, target)
for base, dirs, files in os.walk(os.path.join(repo, "jarvis")):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for name in files:
        path = os.path.join(base, name)
        if not name.endswith(".pyc") and not excluded(name) and os.path.normcase(os.path.relpath(path, repo)) not in keep:
            os.remove(path)
if os.path.isfile(os.path.join(prev, "pyproject.toml")):
    shutil.copyfile(os.path.join(prev, "pyproject.toml"), os.path.join(repo, "pyproject.toml"))
with open(os.path.join(state, "cluster-code-bad.json"), "w", encoding="utf-8") as f:
    json.dump({"code": want, "at": time.time()}, f)
log("restored the previous code; starting it")
if mode == "launch":
    sys.exit(subprocess.call(argv))
subprocess.call(["systemctl", "--user", "reset-failed", "jarvis-cluster"])  # crash loop: start limit
subprocess.call(["systemctl", "--user", "restart", "jarvis-cluster"])
'''


def guard(state, want, argv=None, systemd=False, repo=None, wait=GUARD_WAIT):
    """Start the watchdog detached. With `argv` it launches the new node agent itself and
    keeps supervising it (the plain respawn); under systemd it only watches, and restarts
    the unit after a rollback. Returns the watchdog's argv."""
    state = Path(state)
    (state / "logs").mkdir(parents=True, exist_ok=True)
    (state / CONFIRMED).unlink(missing_ok=True)
    script = state / GUARD_FILE
    script.write_text(GUARD, encoding="utf-8")
    command = [sys.executable, str(script), str(Path(repo or PACKAGE.parent)), str(state), want,
               str(wait), "watch" if systemd else "launch", *(argv or ())]
    if systemd:
        # A child of the unit dies with its cgroup when the unit restarts: run as its own unit.
        command = ["systemd-run", "--user", "--collect", "--quiet",
                   "--unit", f"jarvis-code-guard-{int(time.time())}", *command]
        # Waited for: the unit exits right after this, and its cgroup kill would take a
        # still-running systemd-run client (and the watchdog it was queuing) with it.
        subprocess.run(command, close_fds=True, timeout=15, check=False)
        return command
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.Popen(command, close_fds=True,
                     **({"creationflags": flags} if os.name == "nt" else {"start_new_session": True}))
    return command
