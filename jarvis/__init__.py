"""JARVIS local workflow kernel."""

import os
import re
import sys
from pathlib import Path

__version__ = "0.1.0"

# Tests must never reach a real model: every codex or claude run spends David's subscription.
# Under a test runner (or with JARVIS_NO_MODELS=1) an audit hook refuses to start either CLI,
# whichever code path tries; fakes run as python scripts, so they still work. The variable
# is exported so `python -m jarvis` children started by tests are covered too.
MODEL_CLIS = {"codex", "claude"}


def _under_tests():
    if os.environ.get("JARVIS_NO_MODELS") == "1":
        return True
    first = sys.argv[0] if sys.argv else ""
    return (first == "python -m unittest" or "pytest" in sys.modules
            or ("unittest" in sys.modules and Path(first).name.startswith("test_")))


SHELLS = {"sh", "bash", "dash", "zsh", "fish", "cmd", "powershell", "pwsh"}
# Programs that run another program named in their arguments (`env codex`, `npx
# @openai/codex`, `node .../codex.js`, `ssh dell claude`): every word they get is checked.
LAUNCHERS = SHELLS | {"env", "npx", "npm", "pnpm", "pnpx", "yarn", "bunx", "bun", "node",
                      "deno", "uv", "uvx", "pipx", "nice", "ionice", "nohup", "timeout",
                      "setsid", "stdbuf", "sudo", "doas", "script", "xargs", "exec", "time",
                      "wsl", "ssh", "start", "flatpak-spawn", "systemd-run"}
# Process starts that name a program; each event's args begin (path, argv, ...), except
# os.system's (command,) and os.startfile's (path, operation, ...).
EVENTS = {"subprocess.Popen", "os.exec", "os.posix_spawn", "os.spawn", "os.system", "os.startfile"}


def _stem(word):
    return Path(os.fsdecode(word).strip("\"'")).stem.lower()


def _model(name):
    # codex's npm wrapper runs a native codex-x86_64-...; Claude's package is claude-code.
    return name in MODEL_CLIS or name.startswith(("codex-", "claude-"))


def _refuse_models(event, args):
    if event not in EVENTS:
        return
    if event == "os.system":
        executable, argv = None, [args[0]]
    elif event == "os.startfile":
        executable, argv = args[0], []
    else:
        executable = args[0] if event != "os.spawn" else args[1]  # os.spawn: (mode, path, argv, env)
        argv = args[1] if event != "os.spawn" else args[2]
        argv = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv or ())
    words = [w for a in argv for w in re.split(r"[\s;&|()]+", os.fsdecode(a)) if w]
    program = _stem(executable if executable is not None else (words or [""])[0])
    # A shell (shell=True, cmd /c ...) or launcher runs whatever its arguments name.
    named = {program} | ({_stem(w) for w in words} if program in LAUNCHERS or event == "os.system"
                         else set())
    blocked = sorted(n for n in named if _model(n))
    if blocked:
        raise PermissionError(f"Blocked {', '.join(blocked)}: real model calls "
                              "are off while tests run (JARVIS_NO_MODELS). Substitute a fake.")
    # A test that hands a child its own environment still passes the guard on to it, so a
    # `python -m jarvis` child refuses models too.
    env = args[3] if event == "subprocess.Popen" else None
    if isinstance(env, dict):
        key = b"JARVIS_NO_MODELS" if any(isinstance(k, bytes) for k in env) else "JARVIS_NO_MODELS"
        env[key] = b"1" if isinstance(key, bytes) else "1"


if _under_tests() and not getattr(sys, "_jarvis_models_blocked", False):
    os.environ["JARVIS_NO_MODELS"] = "1"
    sys.addaudithook(_refuse_models)
    sys._jarvis_models_blocked = True
