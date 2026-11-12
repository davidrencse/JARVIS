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


# Tests must never start a real app or change real playback/volume either: on 2026-11-01 a
# voice-loop test sent "open up <app>" through the real executor and launched it on every
# suite run, including the hub's release test runs. The same hook refuses these programs.
APP_LAUNCHERS = {"gtk-launch", "xdg-open", "kioclient", "kioclient5", "kde-open", "kde-open5",
                 "gnome-open", "exo-open", "playerctl", "wpctl", "explorer"}
APP_SUBCOMMANDS = {"gio": {"launch", "open"}, "flatpak": {"run"}}


def _launches_app(program, words):
    if program in APP_LAUNCHERS:
        return True
    verbs = APP_SUBCOMMANDS.get(program)
    return bool(verbs) and any(w in verbs for w in words[1:3])


# Login managers. A test that reaches one of these edits the real user session:
# `systemctl --user disable --now` stops and removes a unit on this machine.
AUTOSTART_CMDS = {"systemctl", "launchctl", "schtasks", "loginctl", "reg"}
_AUTOSTART_REFUSAL = (
    "real autostart is off while tests run (JARVIS_NO_MODELS). "
    "Patch jarvis.autostart.install or use a temp home."
)
# Filled once, when the hook is installed, from the account home. Not from $HOME:
# a test can patch HOME before this module is imported, and that must not move the roots.
_AUTOSTART_ROOTS = ()
_AUTOSTART_SEP = os.path.normcase(os.sep)


def _account_home():
    try:
        import pwd
        return os.path.abspath(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, AttributeError, KeyError, OSError):
        return os.path.abspath(os.path.expanduser("~"))


def _freeze_autostart_roots():
    """Real autostart directories, captured before any test patches HOME or APPDATA."""
    home = os.path.normpath(_account_home())
    homes = [home]
    try:
        real = os.path.normpath(os.path.realpath(home))
    except OSError:
        real = home
    if real not in homes:
        homes.append(real)
    roots = []
    for item in homes:
        roots.append(os.path.join(item, ".config", "systemd", "user"))
        roots.append(os.path.join(item, "Library", "LaunchAgents"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        bases = [os.path.abspath(appdata)]
        try:
            bases.append(os.path.realpath(appdata))
        except OSError:
            pass
    else:
        bases = [os.path.join(item, "AppData", "Roaming") for item in homes]
    tail = os.path.join("Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    for base in bases:
        roots.append(os.path.join(os.path.normpath(base), tail))
    return tuple(dict.fromkeys(os.path.normcase(os.path.normpath(r)) for r in roots))


def _path_text(value):
    if isinstance(value, int) or value is None:
        return None
    try:
        return os.fsdecode(os.fspath(value))
    except (TypeError, ValueError):
        return None


def _under_autostart(value):
    text = _path_text(value)
    if not text:
        return False
    # expanduser follows the HOME of this call. A temp HOME therefore does not match
    # the roots frozen above; an absolute path into the real directory still does.
    folded = os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(text))))
    for root in _AUTOSTART_ROOTS:
        if folded == root or folded.startswith(root + _AUTOSTART_SEP):
            return True
    return False


def _open_writes(args):
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if isinstance(mode, bytes):
        try:
            mode = os.fsdecode(mode)
        except (TypeError, ValueError):
            mode = ""
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    if not isinstance(flags, int):
        return False
    access = flags & getattr(os, "O_ACCMODE", 3)
    if access in (os.O_WRONLY, os.O_RDWR):
        return True
    extra = 0
    for name in ("O_CREAT", "O_TRUNC", "O_APPEND"):
        extra |= getattr(os, name, 0)
    return bool(flags & extra)


def _refuse_autostart_files(event, args):
    if event == "open":
        if args and _open_writes(args) and _under_autostart(args[0]):
            raise RuntimeError(f"Blocked write {args[0]}: {_AUTOSTART_REFUSAL}")
        return
    if event in ("os.remove", "os.unlink"):
        if args and _under_autostart(args[0]):
            raise RuntimeError(f"Blocked unlink {args[0]}: {_AUTOSTART_REFUSAL}")
        return
    if event == "os.rename" and any(_under_autostart(arg) for arg in args[:2]):
        raise RuntimeError(f"Blocked rename {args[0]} -> {args[1]}: {_AUTOSTART_REFUSAL}")


def _stem(word):
    return Path(os.fsdecode(word).strip("\"'")).stem.lower()


def _model(name):
    # codex's npm wrapper runs a native codex-x86_64-...; Claude's package is claude-code.
    return name in MODEL_CLIS or name.startswith(("codex-", "claude-"))


def _refuse_models(event, args):
    # File events are not process starts. Checked first so a write into the real
    # autostart directory is refused even when no service manager is launched.
    _refuse_autostart_files(event, args)
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
    stems = [_stem(w) for w in words]
    starts = [i for i, w in enumerate(stems) if w in APP_LAUNCHERS or w in APP_SUBCOMMANDS]
    if _launches_app(program, stems) or (
            (program in LAUNCHERS or event == "os.system")
            and any(_launches_app(stems[i], stems[i:]) for i in starts)):
        raise PermissionError(f"Blocked {program}: tests never launch real apps or change "
                              "real playback (JARVIS_NO_MODELS). Mock the executor.")
    # Same launcher rule as the model CLIs: `bash -c "systemctl ..."` is still systemctl.
    blocked_auto = sorted(n for n in named if n in AUTOSTART_CMDS)
    if blocked_auto:
        raise RuntimeError(f"Blocked {', '.join(blocked_auto)}: {_AUTOSTART_REFUSAL}")
    # A test that hands a child its own environment still passes the guard on to it, so a
    # `python -m jarvis` child refuses models too.
    env = args[3] if event == "subprocess.Popen" else None
    if isinstance(env, dict):
        key = b"JARVIS_NO_MODELS" if any(isinstance(k, bytes) for k in env) else "JARVIS_NO_MODELS"
        env[key] = b"1" if isinstance(key, bytes) else "1"


if _under_tests() and not getattr(sys, "_jarvis_models_blocked", False):
    os.environ["JARVIS_NO_MODELS"] = "1"
    _AUTOSTART_ROOTS = _freeze_autostart_roots()
    sys.addaudithook(_refuse_models)
    sys._jarvis_models_blocked = True
