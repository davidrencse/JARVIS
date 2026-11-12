"""Read-only diagnostics for a possible MAIN candidate.

Nothing here authorizes promotion, failover, sleep, or cutover. Credential checks
report file presence only: they do not open those files, and they are not
authentication, quota, model, or lane readiness. Brain-sync freshness and a brain
handoff are not inferred from a readable local directory.
"""

import errno
import os
import re
import shutil
import stat
import sys
from pathlib import Path

_LIMITATIONS = (
    "ready is false: this diagnostic never authorizes promotion, failover, sleep, or cutover.",
    "ai_ready is false unless a provider session, quota, and mediated lane are independently "
    "verified. Credential file presence is not that verification, and no provider call was made.",
    "Brain sync freshness and brain handoff are unverified. A readable local vault or state "
    "directory is not a completed handoff.",
    "SSH executable presence is not a live login, tunnel, or remote-host check.",
)

_UNVERIFIED = "sync freshness unverified; brain handoff unverified"
_SSH_TOOLS = ("ssh", "ssh-keygen")
_GROK_DIR = ".grokbot"
_GROK_CREDENTIAL = "local-exec-daemon-credential.json"
_REQUIREMENT = re.compile(r'(?m)^requires-python\s*=\s*"([^"]+)"\s*$')
_CLAUSE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_NOT_AUTH = "file was not read; not authentication or model readiness"


def main_readiness(config, state, *, home=None, environ=None, which=None, expected_code=None):
    """JSON-safe local readiness report. Never creates files or reads secret bodies.

    ``control_ready`` is local runtime, plus brain storage accessibility, plus a match
    against the supplied tested-release fingerprint. ``ai_ready`` stays false: this
    function does not call a provider. ``ready`` stays false so the report cannot be
    used as permission to promote.
    """
    config = config if isinstance(config, dict) else {}
    env = _mapping(environ)
    try:
        base = Path.home() if home is None else Path(home)
    except (TypeError, ValueError):
        base = None
    checks = []
    python_ok = _add(checks, *_python_check())
    sqlite_ok = _add(checks, *_sqlite_check())
    vault_ok = _add(checks, *_vault_check(config, base))
    state_ok = _add(checks, *_state_check(state))
    code_ok = _add(checks, *_code_check(expected_code))
    _add(checks, *_ssh_check(which))
    _add(checks, *_credential_check(
        "codex credential presence", _codex_file(env, base), optional=False))
    _add(checks, *_credential_check(
        "claude credential presence", _claude_file(env, base), optional=True))
    _add(checks, *_credential_check(
        "grok bot credential presence", _grok_file(base), optional=False))
    _add(checks, "machine id", True, _machine_detail(config), "")
    return {
        "ready": False,
        "control_ready": bool(python_ok and sqlite_ok and vault_ok and state_ok and code_ok),
        "ai_ready": False,
        "checks": checks,
        "limitations": list(_LIMITATIONS),
    }


def _add(checks, name, ok, detail, fix):
    item = {"check": name, "ok": bool(ok), "detail": detail}
    if not item["ok"]:
        item["fix"] = fix
    checks.append(item)
    return item["ok"]


def _mapping(environ):
    if environ is None:
        return os.environ
    if hasattr(environ, "get"):
        return environ
    return {}


def _env_value(env, key):
    try:
        value = env.get(key)
    except Exception:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _requires_python():
    path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _REQUIREMENT.search(text)
    return match.group(1) if match else None


def _runtime_version():
    info = sys.version_info
    try:
        return (int(info[0]), int(info[1]), int(info[2]))
    except (TypeError, ValueError, IndexError):
        return None


def _satisfies(spec, version):
    clauses = [part.strip() for part in str(spec).split(",")]
    if not clauses or any(not part for part in clauses):
        return False
    for part in clauses:
        match = _CLAUSE.fullmatch(part)
        if not match:
            return False
        op = match.group(1)
        want = (int(match.group(2)), int(match.group(3) or 0), int(match.group(4) or 0))
        if op == ">=" and version < want:
            return False
        if op == ">" and version <= want:
            return False
        if op == "==" and version != want:
            return False
        if op == "<=" and version > want:
            return False
        if op == "<" and version >= want:
            return False
        if op == "!=" and version == want:
            return False
    return True


def _python_check():
    spec = None
    version = None
    try:
        spec = _requires_python()
        version = _runtime_version()
    except Exception:
        spec = None
    if not spec or version is None:
        return ("python runtime", False, "pyproject requires-python could not be read",
                "Restore a readable pyproject.toml requires-python and use a supported interpreter.")
    shown = ".".join(str(part) for part in version)
    if not _satisfies(spec, version):
        return ("python runtime", False, f"Python {shown} does not satisfy requires-python {spec}",
                "Run JARVIS under a Python that satisfies pyproject.toml requires-python.")
    return ("python runtime", True, f"Python {shown} satisfies requires-python {spec}", "")


def _sqlite_check():
    try:
        import sqlite3
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("select 1")
        finally:
            connection.close()
        version = getattr(sqlite3, "sqlite_version", "")
    except Exception:
        return ("sqlite", False, "unavailable",
                "Use a Python build that includes the sqlite3 standard library. "
                "This check does not create a database.")
    detail = f"available ({version})" if version else "available"
    return ("sqlite", True, detail, "")


def _expand_vault(path, home):
    """Expand a leading '~' the way os.path.expanduser does.

    Bare '~' uses the injected home. '~user' stays on os.path.expanduser.
    This does not read the process home and does not create a directory.
    """
    if path.startswith("~") and (len(path) == 1 or path[1] in "/\\"):
        if home is None:
            return None
        return os.fspath(home) + path[1:]
    if path.startswith("~"):
        try:
            expanded = os.path.expanduser(path)
        except (OSError, RuntimeError, ValueError):
            return None
        if isinstance(expanded, str) and expanded.strip():
            return expanded
        return None
    return path


def _vault_setting(config, home):
    vault = config.get("vault") if isinstance(config, dict) else None
    if not isinstance(vault, dict):
        return "unset", None
    if vault.get("enabled", True) is False:
        return "disabled", None
    try:
        from jarvis.vault import vault_path
        selected = vault_path(vault)
    except Exception:
        return "unset", None
    if not isinstance(selected, str) or not selected.strip():
        return "unset", None
    expanded = _expand_vault(selected, home)
    if not isinstance(expanded, str) or not expanded.strip():
        return "unset", None
    return "set", expanded


def _storage_detail(lead):
    return lead + "; " + _UNVERIFIED


def _vault_check(config, home):
    try:
        status, selected = _vault_setting(config, home)
    except Exception:
        status, selected = "unset", None
    if status == "disabled":
        return ("vault storage", False, _storage_detail("vault is disabled"),
                "Enable the local vault or choose another candidate. This check does not create a vault.")
    if status != "set":
        return ("vault storage", False, _storage_detail("vault.path is not configured"),
                "Set vault.path to an existing local directory on this PC.")
    kind, count = _classify_dir(selected)
    return _dir_result("vault storage", "configured vault directory", kind, count)


def _state_check(state):
    if isinstance(state, str) and not state.strip():
        path = None
    elif isinstance(state, (str, Path)) or isinstance(state, os.PathLike):
        try:
            path = Path(state)
        except (TypeError, ValueError):
            path = None
    else:
        path = None
    if path is None:
        kind, count = "missing", 0
    else:
        kind, count = _classify_dir(path)
    return _dir_result("state directory", "state directory", kind, count,
                       missing_fix="Use the existing local JARVIS state directory. This check does not create one.")


def _dir_result(name, label, kind, count, missing_fix=None):
    if kind == "readable":
        return (name, True, _storage_detail(f"readable directory ({count} entries)"), "")
    leads = {
        "missing": f"{label} is missing",
        "not-a-directory": f"{label} is not a directory",
        "unreadable": f"{label} is not readable",
    }
    fixes = {
        "missing": missing_fix or "The configured vault directory is missing. This check does not create it.",
        "not-a-directory": f"{label[0].upper() + label[1:]} must be a directory. This check does not read its contents.",
        "unreadable": f"Make the {label} readable by this user. This check does not read note or database bodies.",
    }
    lead = leads.get(kind, f"{label} is not readable")
    fix = fixes.get(kind, fixes["unreadable"])
    return (name, False, _storage_detail(lead), fix)


def _classify_dir(path):
    """Return (kind, entry_count). Entry names are discarded and file bodies are not opened."""
    try:
        info = os.stat(path, follow_symlinks=True)
    except FileNotFoundError:
        return "missing", 0
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
            return "unreadable", 0
        return "missing", 0
    if not stat.S_ISDIR(info.st_mode):
        return "not-a-directory", 0
    try:
        if not os.access(path, os.R_OK | os.X_OK):
            return "unreadable", 0
        count = len(os.listdir(path))
    except OSError:
        return "unreadable", 0
    return "readable", count


def _classify_file(path):
    """present, missing, or unreadable. Never opens the file."""
    if path is None:
        return "missing"
    try:
        info = os.stat(path, follow_symlinks=True)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
            return "unreadable"
        return "missing"
    if not stat.S_ISREG(info.st_mode):
        return "missing"
    try:
        if not os.access(path, os.R_OK):
            return "unreadable"
    except OSError:
        return "unreadable"
    return "present"


def _codex_file(env, home):
    if home is None and not _env_value(env, "CODEX_HOME"):
        return None
    override = _env_value(env, "CODEX_HOME")
    base = Path(override) if override else Path(home) / ".codex"
    return base / "auth.json"


def _claude_file(env, home):
    if home is None and not _env_value(env, "CLAUDE_CONFIG_DIR"):
        return None
    override = _env_value(env, "CLAUDE_CONFIG_DIR")
    base = Path(override) if override else Path(home) / ".claude"
    return base / ".credentials.json"


def _grok_file(home):
    if home is None:
        return None
    return Path(home) / _GROK_DIR / _GROK_CREDENTIAL


def _credential_check(name, path, *, optional):
    kind = _classify_file(path)
    if kind == "present":
        return (name, True, "present; " + _NOT_AUTH, "")
    if kind == "unreadable":
        return (name, False, "not readable; " + _NOT_AUTH,
                "Restore this user's read access to the local credential file on this PC. "
                "Do not copy it from another machine or print it.")
    if optional:
        return (name, True, "not present; optional; " + _NOT_AUTH, "")
    return (name, False, "not present; " + _NOT_AUTH,
            "Sign in on this PC so the local credential file exists. Do not copy a session "
            "from another machine. Presence still would not make ai_ready, and no provider is called.")


def _deploy_fingerprint():
    from jarvis.deploy import code_fingerprint
    return code_fingerprint()


def _code_check(expected_code):
    if not isinstance(expected_code, str) or expected_code == "":
        return ("tested code", False, "expected tested release fingerprint was not supplied",
                "Supply the tested release fingerprint as expected_code. A missing value is not a match.")
    try:
        live = _deploy_fingerprint()
    except Exception:
        live = None
    if not isinstance(live, str) or live == "":
        return ("tested code", False, "local code fingerprint is unavailable",
                "The local code fingerprint could not be read. Do not treat that as a match.")
    if live == expected_code:
        return ("tested code", True,
                "matches the supplied tested release; this does not authorize promotion", "")
    return ("tested code", False, "local code fingerprint does not match the supplied tested release",
            "This tree does not match the supplied tested release. This check does not deploy or update a node.")


def _which_found(which, name):
    finder = shutil.which if which is None else which
    if not callable(finder):
        return False
    try:
        found = finder(name)
    except Exception:
        return False
    if isinstance(found, str):
        return bool(found.strip())
    return bool(found)


def _ssh_check(which):
    parts = []
    ok = True
    for name in _SSH_TOOLS:
        found = _which_found(which, name)
        parts.append(f"{name} {'found' if found else 'not found'}")
        ok = ok and found
    detail = ", ".join(parts) + "; login and tunnel were not tested"
    if ok:
        return ("ssh executables", True, detail, "")
    return ("ssh executables", False, detail,
            "Install the OpenSSH client tools (ssh and ssh-keygen). This check does not open a connection.")


def _machine_detail(config):
    cluster = config.get("cluster") if isinstance(config, dict) else None
    if not isinstance(cluster, dict):
        return "no machine id configured"
    parts = []
    for label in ("local_device", "main_device"):
        value = cluster.get(label)
        if isinstance(value, str) and value:
            parts.append(f"{label}={value}")
    if not parts:
        return "no machine id configured"
    return "configured " + ", ".join(parts)
