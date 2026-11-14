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


def validate_bundle_proof(raw, code):
    """A signer-attested tested release, not an inference from a package signature."""
    import math
    try:
        proof = json.loads(raw)
        at = proof.get('at')
        if (type(proof.get('schema')) is not int or proof.get('schema') != 1 or proof.get('code') != code
                or not isinstance(code, str) or len(code) != 16
                or any(c not in '0123456789abcdef' for c in code)
                or isinstance(at, bool) or not isinstance(at, (int, float))
                or not math.isfinite(at) or at <= 0
                or not isinstance(proof.get('tests'), str)
                or not isinstance(proof.get('commit'), str) or not proof['commit']
                or not isinstance(proof.get('warnings', []), list)
                or not all(isinstance(w, str) for w in proof.get('warnings', []))):
            raise ValueError('invalid proof')
    except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
        raise ValueError('Invalid tested-release proof; not installed.') from exc
    return {k: proof[k] for k in ('schema', 'code', 'at', 'tests', 'commit', 'warnings') if k in proof}


def bundle_proof(state, root, code):
    """Only the current tested snapshot can assert test evidence inside a signed ZIP."""
    note = current(state)
    if (not note or note.get('code') != code or is_bad(state, code)
            or Path(root).resolve() != (release_dir(state) / 'jarvis').resolve()):
        return None
    try:
        return validate_bundle_proof(json.dumps({**note, 'schema': 1}), code)
    except ValueError:
        return None


def adopt_bundle_proof(state, repo, proof, version):
    """Publish an authenticated package snapshot only after installer health passed.

    The caller verified the pinned version signature and strictly newer transport
    version. The source release time also prevents rewrapping an old tested
    release in a fresh transport version from replacing a newer local snapshot.
    """
    from .locking import exclusive_lock
    from .deploy import installed_version
    import math
    state, repo = Path(state), Path(repo)
    state.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(state / 'code-release.lock'):
        old = current(state)
        if is_bad(state, proof['code']) or version <= installed_version(state):
            return False
        if old:
            at = old.get('at')
            if (not isinstance(at, (int, float)) or isinstance(at, bool)
                    or not math.isfinite(at) or at >= proof['at']):
                return False
        with tempfile.TemporaryDirectory(prefix='tested-bundle-', dir=state) as temporary:
            fresh = Path(temporary) / 'snapshot'
            fresh.mkdir()
            shutil.copytree(repo / 'jarvis', fresh / 'jarvis',
                            ignore=shutil.ignore_patterns(*SKIP, '*.pyc', *EXCLUDE))
            project = repo / 'pyproject.toml'
            if project.is_file():
                shutil.copy2(project, fresh / 'pyproject.toml')
            if code_fingerprint(fresh / 'jarvis') != proof['code']:
                raise ValueError('Installed package changed after health; no tested proof adopted.')
            _write(fresh / 'release.json', {**proof, 'bundle_version': version})
            final = release_dir(state)
            previous = state / (RELEASE + '.adopt-old')
            shutil.rmtree(previous, ignore_errors=True)
            if final.exists():
                os.replace(final, previous)
            try:
                os.replace(fresh, final)
            except OSError:
                if previous.exists():
                    os.replace(previous, final)
                raise
            shutil.rmtree(previous, ignore_errors=True)
    return True


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
            "Nodes pull it automatically after a green push to origin/main; "
            "`jarvis cluster update <node>` only speeds that up. This check does not update a node.")))
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


def settled(root=None, quiet=QUIET, now=None):
    """True when no package file was saved in the last `quiet` seconds (the checkout's, even
    when this process runs from the release copy)."""
    if root is None:
        from .home import home, in_release
        root = home() / "jarvis" if in_release() else PACKAGE
    newest = 0
    for path in _files(Path(root)):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue  # vanished since the listing
    return (now or time.time()) - newest >= quiet


def source_commit(repo=None):
    """Resolve a local remote-tracking commit; never fetch or read dirty files."""
    from .home import home, in_release
    repo = Path(repo or (home() if in_release() else PACKAGE.parent))  # the checkout, even from the release copy
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
    return (commit not in (current(state).get("commit"), _read(Path(state) / FAILED).get("commit"))
            and _successor_commit_allowed(state, commit))


def _successor_commit_allowed(state, commit, repo=None):
    """A stale successor checkout cannot replace its newer imported tested release."""
    note = current(state)
    if not note.get('bundle_version') or note.get('commit') == commit:
        return True
    from .home import home, in_release
    repo = Path(repo or (home() if in_release() else PACKAGE.parent))
    try:
        result = subprocess.run(['git', 'merge-base', '--is-ancestor', note['commit'], commit],
                                cwd=repo, capture_output=True, timeout=15)
    except (OSError, KeyError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


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


LOCK_NOTE = "code-release.holder.json"   # who holds code-release.lock: {pid, at, command, commit}


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def lock_holder(state, now=None):
    """Who is testing a release now: {pid, age, command, commit}, or None. A note whose
    process is gone is a stale lock: it is cleared here, so the next cut is never blocked by it."""
    path = Path(state) / LOCK_NOTE
    note = _read(path)
    if not note:
        return None
    if not _pid_alive(note.get("pid")):
        path.unlink(missing_ok=True)
        return None
    at = note.get("at")
    age = max(0, int((now or time.time()) - at)) if isinstance(at, (int, float)) else None
    return {"pid": note["pid"], "age": age, "command": note.get("command") or "unknown",
            "commit": note.get("commit")}


def describe_holder(holder):
    if not holder:
        return "Another release is being tested."
    age = "" if holder.get("age") is None else f", running {holder['age'] // 60} min {holder['age'] % 60} s"
    return f"Another release is being tested by pid {holder['pid']}{age}: {holder['command']}"


def status(state):
    failed = _read(Path(state) / FAILED)
    note = current(state)
    holder = lock_holder(state)
    if holder:
        return {**note, "testing": holder, "message": describe_holder(holder)}
    if failed and failed.get("blocking", True):
        names = failed.get("failures") or [failed.get("tests") or "unknown test"]
        return {**failed, "message": "Release blocked: " + str(names[0])}
    return {**note, "message": "Release shipped with warnings" if note.get("warnings") else "Release ready"}


def retry_failures(stage, ids, timeout=300):
    """Re-run only these test ids once; return the ones that fail again. A runner failure
    ("runner:core") has no test id to re-run and stays failed."""
    from .release_gate import _run
    again = []
    for name in ids:
        if not isinstance(name, str) or name.startswith("runner:") or _run(stage, name, timeout).get("failures"):
            again.append(name)
    return again


def cut(state, repo=None, quiet=QUIET, tests=run_tests, retry=retry_failures):
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
        holder = lock_holder(state)
        return {"released": False, "note": describe_holder(holder), "holder": holder}
    try:
        _write(state / LOCK_NOTE, {"pid": os.getpid(), "at": time.time(),
                                   "command": " ".join(sys.argv[:4]) or "jarvis"})
        return _cut(state, repo, quiet, tests, retry)
    finally:
        (state / LOCK_NOTE).unlink(missing_ok=True)
        lock.__exit__(None, None, None)


def _cut(state, repo, quiet, tests, retry=retry_failures):
    try:
        commit = source_commit(repo)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"released": False, "note": str(exc)}
    if commit == current(state).get("commit"):
        return {"released": False, "code": current(state)["code"], "note": "Nodes already get this code."}
    if not _successor_commit_allowed(state, commit, repo):
        return {'released': False, 'code': current(state).get('code'),
                'note': 'The local Git commit does not descend from the imported tested release. '
                        'Synchronize Git before cutting a successor release; kept the authenticated snapshot.'}
    with tempfile.TemporaryDirectory(prefix="jarvis-release-") as stage:
        stage = Path(stage)
        _archive(repo, stage, commit)
        code = code_fingerprint(stage / "jarvis")  # retain old nodes' wire fingerprint
        passed, result = tests(stage)
        detail = result if isinstance(result, dict) else {"summary": result}
        tail = detail.get("summary", "")
        if detail.get("deferred"):
            return {"released": False, "deferred": True, "note": tail}
        attempts = []
        first = detail.get("failures")
        if not passed and isinstance(first, list) and first:
            # One more try for just the failing ids: a flaky test must not hold a release back.
            second = retry(stage, first)
            attempts = [{"failures": list(first)}, {"failures": list(second)}]
            if not second:
                passed = True
                detail = {**detail, "failures": [], "retried": list(first)}
                tail = "Passed after re-running: " + ", ".join(first)
        if not passed:
            note = {"code": code, "commit": commit, "at": time.time(), "tests": tail,
                    "failures": attempts[1]["failures"] if attempts else detail.get("failures", [tail]),
                    "blocking": True, **({"attempts": attempts} if attempts else {})}
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
            "warnings": detail.get("warnings", []),
            **({"retried": detail["retried"]} if detail.get("retried") else {})}
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
    if current(state):
        shutil.copytree(release_dir(state), fresh / RELEASE)
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
# A promoted node must never serve the failed package's tested snapshot. Preserve
# a concurrently newer snapshot; restore only the one this watchdog installed.
release = os.path.join(state, "code-release")
try:
    with open(os.path.join(release, "release.json"), encoding="utf-8") as f:
        installed_release = json.load(f)
except (OSError, ValueError):
    installed_release = {}
if installed_release.get("code") == want:
    shutil.rmtree(release)
    saved = os.path.join(prev, "code-release")
    if os.path.isdir(saved):
        shutil.copytree(saved, release)
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
