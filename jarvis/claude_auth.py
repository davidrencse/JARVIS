"""Let Claude Code renew its own subscription session without a model turn.

Only an SDK initialize control request is sent, followed by EOF. No user message,
prompt, login command or direct credential write is involved. The caller must
reread credentials and verify the resulting expiry: a zero CLI exit alone is not
proof that a revoked session was renewed.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

TIMEOUT = 20
COOLDOWN = 60
_lock = threading.Lock()
_attempts = {}  # normalized config directory -> (completion time, sanitized error)

# Preserve the user's configuration directory and home. Remove alternate auth
# routes, custom endpoints and inherited lean mode so the native OAuth session is
# the only authentication route. Safe mode disables user hooks and plugins.
_AUTH_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_FOUNDRY_API_KEY", "ANTHROPIC_FOUNDRY_BASE_URL",
    "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "OPENAI_API_KEY",
})


def _command():
    binary = shutil.which("claude")
    if not binary:
        raise RuntimeError("Claude Code is unavailable for session renewal")
    if Path(binary).suffix.lower() in (".cmd", ".bat"):
        raise RuntimeError("Claude session renewal requires the native CLI executable")
    return [binary, "-p", "--safe-mode", "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose", "--no-session-persistence",
            "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-chrome"]


def renew_claude_session():
    """Run one bounded initialize-only CLI startup; coalesce attempts for 60s.

    Returns None after a successful CLI exit (including a coalesced success).
    Raises a sanitized RuntimeError otherwise. Credentials remain owned by Claude.
    """
    env = {key: value for key, value in os.environ.items() if key.upper() not in _AUTH_ENV}
    directory = env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    # Resolve before selecting the temporary cwd; relative config directories
    # otherwise silently switch accounts when the subprocess starts there.
    directory = os.path.normcase(os.path.abspath(os.path.expanduser(directory)))
    env["CLAUDE_CONFIG_DIR"] = directory
    if not _lock.acquire(timeout=TIMEOUT):
        raise RuntimeError("Claude session renewal is already in progress")
    try:
        previous = _attempts.get(directory)
        if previous and time.monotonic() - previous[0] < COOLDOWN:
            if previous[1]:
                raise RuntimeError(previous[1])
            return
        error = None
        try:
            command = _command()
            initialize = {"type": "control_request", "request_id": "jarvis-quota-init",
                          "request": {"subtype": "initialize", "hooks": {}}}
            with tempfile.TemporaryDirectory(prefix="jarvis-auth-check-") as cwd:
                result = subprocess.run(
                    command, input=json.dumps(initialize) + "\n", cwd=cwd, env=env,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT,
                    shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                error = "Claude session renewal did not complete; sign-in may need attention"
        except subprocess.TimeoutExpired:
            # subprocess.run kills and waits for its child before raising.
            error = "Claude session renewal timed out"
        except Exception:
            # No subprocess output, exception text, auth data or paths are exposed.
            error = "Claude session renewal could not start or complete"
        _attempts[directory] = (time.monotonic(), error)
        # Prevent an unbounded account-path cache in a long-running process.
        for key, value in list(_attempts.items()):
            if time.monotonic() - value[0] >= COOLDOWN:
                _attempts.pop(key, None)
        if error:
            raise RuntimeError(error)
    finally:
        _lock.release()
