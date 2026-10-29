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


def _stem(word):
    return Path(os.fsdecode(word).strip("\"'")).stem.lower()


def _refuse_models(event, args):
    if event != "subprocess.Popen":
        return
    # (executable, args, cwd, env); executable is None unless the caller set it.
    argv = [os.fsdecode(a) for a in ([args[1]] if isinstance(args[1], (str, bytes)) else args[1])]
    words = [w for a in argv for w in re.split(r"[\s;&|()]+", a) if w]
    program = _stem(args[0] if args[0] is not None else (words or [""])[0])
    # A shell (shell=True, cmd /c ...) runs whatever its command string names.
    named = {program} | ({_stem(w) for w in words} if program in SHELLS else set())
    if named & MODEL_CLIS:
        raise PermissionError(f"Blocked {', '.join(sorted(named & MODEL_CLIS))}: real model calls "
                              "are off while tests run (JARVIS_NO_MODELS). Substitute a fake.")


if _under_tests() and not getattr(sys, "_jarvis_models_blocked", False):
    os.environ["JARVIS_NO_MODELS"] = "1"
    sys.addaudithook(_refuse_models)
    sys._jarvis_models_blocked = True
