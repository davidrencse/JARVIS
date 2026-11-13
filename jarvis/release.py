"""Which code nodes get: a tested snapshot of the main's package, not its live working tree.

Nodes used to pull whatever `jarvis/` held on disk, and every saved file changed the
fingerprint, so a half-finished edit reached them at once: on 2026-10-30 the Dell pulled code
mid-edit at 14:38, its node agent stopped checking in, and nothing put the old code back.

Main side: cut() resolves origin/main (local HEAD fallback), extracts that commit
with git archive, and tests the clean stage. Commit hashes trigger cuts; the existing
package fingerprint remains the node wire identity. Core and safety failures block;
other full-suite failures ship with recorded warnings. State lives under the caller's
--state directory, including code-release-failed.json.

Node side: before installing, `backup()` copies the running package to
`.jarvis/code-prev/`. `guard()` then starts a stdlib-only watchdog (GUARD, run from a file in
the state folder, so it doesn't import the code it judges) that waits for the new code to
check in (`confirm()`, after its first successful poll). If it doesn't within GUARD_WAIT
seconds, or dies first, the watchdog restores the backup, records the fingerprint as bad
(`is_bad`: never pulled again), and starts the old code.

`code_parity` is the read-only transfer check. `served_code` still falls back to the live
tree until a snapshot exists; parity does not. A match there does not cut a release, deploy,
or update, and it does not authorize a transfer.
"""

import json
import tarfile
import os
from .platforms import on_windows
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .deploy import EXCLUDE, PACKAGE, SKIP, _files, code_fingerprint

RELEASE = "code-release"        # in the main's state folder: jarvis/, pyproject.toml, release.json
FAILED = "code-release-failed.json"
QUIET = 600                     # seconds without a saved file before the tree counts as settled (a cut runs the whole suite)
FAST_QUIET = 60                 # ...when an online node is behind on code and waiting for it
TEST_TIMEOUT = 3600
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
    from .fsio import read_json
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


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


def _tested_fingerprint(state):
    """The snapshot fingerprint, or None. Never the live-tree fallback `served_code` uses."""
    try:
        note = current(state)
    except (OSError, ValueError, TypeError):
        return None
    code = note.get("code") if isinstance(note, dict) else None
    return code if isinstance(code, str) and code else None


def _live_fingerprint():
    try:
        live = code_fingerprint()
    except (OSError, ValueError, TypeError):
        return None
    return live if isinstance(live, str) and live else None


def _failed_code(state):
    code = _read(Path(state) / FAILED).get("code")
    return code if isinstance(code, str) and code else None


def _parity(tested, live, reported, blocks):
    """`authorizes` stays false: a fingerprint match is not permission to transfer."""
    return {
        "matched": not blocks,
        "authorizes": False,
        "code": tested,
        "live": live,
        "reported": reported,
        "blocks": [{"code": code, "instruction": text} for code, text in blocks],
        "instruction": blocks[0][1] if blocks else "",
        "limits": "This check does not release, deploy, update, or authorize a transfer.",
    }


def _unreleased_instruction():
    try:
        quiet = settled(quiet=QUIET)
    except (OSError, ValueError, TypeError):
        quiet = None
    if quiet is False:
        return (f"Code was saved in the last {QUIET} s and differs from the tested release. "
                "Wait for it to settle, then: jarvis cluster release. This check does not cut one.")
    if quiet is True:
        return ("The working tree differs from the tested release and has been quiet long enough to cut. "
                "On this main: jarvis cluster release. This check does not cut one.")
    return ("The working tree differs from the tested release. "
            "On this main: jarvis cluster release. This check does not cut one.")


def code_parity(state, reported=None):
    """Whether a transfer's code matches the tested release. Reads state. Changes nothing.

    ``matched`` is true only when a tested release exists, the live tree equals it,
    ``reported`` (the other PC's fingerprint) equals it, and neither fingerprint was
    rolled back here. ``served_code`` matching ``reported`` is not enough: before the
    first snapshot it is the live tree. ``authorizes`` is always false.
    """
    limits = "This check does not release, deploy, update, or authorize a transfer."
    try:
        state = Path(state)
    except (TypeError, ValueError):
        return _parity(None, None, None, [("unreadable",
            "Code parity could not be read. Do not treat that as a match. " + limits)])
    tested = _tested_fingerprint(state)
    live = _live_fingerprint()
    reported_code = reported if isinstance(reported, str) and reported else None
    failed_code = _failed_code(state)
    blocks = []
    if live and failed_code == live and live != tested:
        tail = str(_read(state / FAILED).get("tests") or "").strip()
        extra = f" ({tail[-160:]})" if tail else ""
        failed = (
            "This computer's latest code failed its release tests, so nodes can't get it yet. "
            "Fix the failing test, then run: jarvis cluster release." + extra
            + " This check does not cut one."
        )
        blocks.append(("release_tests_failed", failed))
    if not tested:
        blocks.append(("no_tested_release", (
            "No tested release on this main. On this main: jarvis cluster release. "
            "A live tree is not a tested release. This check does not cut one.")))
    elif live and live != tested and failed_code != live:
        blocks.append(("unreleased_edits", _unreleased_instruction()))
    if live is None:
        blocks.append(("live_unreadable", (
            "The local code fingerprint could not be read. Do not treat that as a match. "
            "This check does not deploy or update.")))
    if reported_code is None:
        blocks.append(("reported_missing", (
            "The other PC's code fingerprint was not supplied. A transfer needs that fingerprint. "
            "This check does not contact the other PC.")))
    elif tested and reported_code != tested:
        blocks.append(("code_mismatch", (
            f"The other PC runs different JARVIS code ({reported_code}, tested release {tested}). "
            "On the main: jarvis cluster update <node>. This check does not update a node.")))
    bad = [code for code in (tested, reported_code) if code and is_bad(state, code)]
    if bad:
        shown = ", ".join(dict.fromkeys(bad))
        blocks.append(("rolled_back", (
            f"Fingerprint {shown} was rolled back on this PC and must not be installed again. "
            "This check does not deploy it.")))
    return _parity(tested, live, reported_code, blocks)


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


def source_commit(repo=None):
    """Resolve a local remote-tracking commit; never fetch or read dirty files."""
    repo = Path(repo or PACKAGE.parent)
    for ref in ("origin/main", "HEAD"):
        result = subprocess.run(["git", "rev-parse", "--verify", ref + "^{commit}"],
                                cwd=repo, capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            return result.stdout.strip()
    raise ValueError("No origin/main or local HEAD commit available")


def due(state, quiet=QUIET):
    try:
        commit = source_commit()
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return commit not in (current(state).get("commit"), _read(Path(state) / FAILED).get("commit"))


def _archive(repo, stage, commit):
    archive = Path(stage) / "source.tar"
    subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), commit],
                   cwd=repo, check=True, capture_output=True, timeout=60)
    with tarfile.open(archive) as tar:
        # Reject links and traversal even on Python versions without extraction filters.
        for member in tar.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise ValueError("Unsafe git archive entry: " + member.name)
        tar.extractall(stage)
    archive.unlink()


def _nested_repo(path):
    """A folder with its own .git is another checkout (audit cluster-10: a stray repo copy
    rode into every release stage), never part of this one."""
    return os.path.isdir(path) and os.path.exists(os.path.join(path, ".git"))


def _stage(repo, stage):
    def skip(folder, names):
        return [n for n in names if n in STAGE_SKIP or n.endswith(".pyc")
                or _nested_repo(os.path.join(folder, n))]
    for item in Path(repo).iterdir():
        if item.name in STAGE_SKIP or _nested_repo(item):
            continue
        target = Path(stage) / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=skip, symlinks=True)
        elif item.is_file():
            shutil.copy2(item, target)


def run_tests(stage, timeout=TEST_TIMEOUT):
    from .release_gate import run_tests as gate
    return gate(stage, timeout=timeout)


def status(state):
    failed = _read(Path(state) / FAILED)
    note = current(state)
    if failed and failed.get("blocking", True):
        names = failed.get("failures") or [failed.get("tests") or "unknown test"]
        return {**failed, "message": "Release blocked: " + str(names[0])}
    return {**note, "message": "Release shipped with warnings" if note.get("warnings") else "Release ready"}


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
    try:
        commit = source_commit(repo)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"released": False, "note": str(exc)}
    if commit == current(state).get("commit"):
        return {"released": False, "code": current(state)["code"], "note": "Nodes already get this code."}
    with tempfile.TemporaryDirectory(prefix="jarvis-release-") as stage:
        stage = Path(stage)
        _archive(repo, stage, commit)
        code = code_fingerprint(stage / "jarvis")  # retain old nodes' wire fingerprint
        passed, result = tests(stage)
        detail = result if isinstance(result, dict) else {"summary": result}
        tail = detail.get("summary", "")
        if detail.get("deferred"):
            return {"released": False, "deferred": True, "note": tail}
        if not passed:
            note = {"code": code, "commit": commit, "at": time.time(), "tests": tail,
                    "failures": detail.get("failures", [tail]), "blocking": True}
            _write(state / FAILED, note)
            notified = _read(state / "code-release-notified.json")
            commits = notified.get("commits", [])
            if commit not in commits:
                _write(state / "code-release-notified.json", {"commits": [*commits, commit]})
                from .confirm import notify
                notify("Release blocked: " + str(note["failures"][0] if note["failures"] else tail))
            return {"released": False, **note, "note": "Tests failed; nodes keep the last release."}
        fresh, final = state / (RELEASE + ".new"), release_dir(state)
        shutil.rmtree(fresh, ignore_errors=True)
        fresh.mkdir()
        shutil.copytree(stage / "jarvis", fresh / "jarvis", ignore=shutil.ignore_patterns(*SKIP, "*.pyc", *EXCLUDE))
        if (stage / "pyproject.toml").is_file():
            shutil.copy2(stage / "pyproject.toml", fresh / "pyproject.toml")
    if code_fingerprint(fresh / "jarvis") != code:
        shutil.rmtree(fresh, ignore_errors=True)
        return {"released": False, "note": "The release copy didn't match what was tested."}
    note = {"code": code, "commit": commit, "at": time.time(), "tests": tail,
            "warnings": detail.get("warnings", [])}
    _write(fresh / "release.json", note)
    old = state / (RELEASE + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if final.exists():
        os.replace(final, old)
    os.replace(fresh, final)
    shutil.rmtree(old, ignore_errors=True)
    if note["warnings"]:
        _write(state / FAILED, {**note, "blocking": False, "failures": note["warnings"]})
    else:
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
                     **({"creationflags": flags} if on_windows() else {"start_new_session": True}))
    return command
