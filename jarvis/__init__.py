"""JARVIS local workflow kernel."""

import ipaddress
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
# The real account's ~/.local/bin and the whole ~/.config/systemd tree (which includes
# the user unit directory above). A separate list so a write there says so, and a write
# into the user unit directory keeps the autostart message.
_HOME_WRITE_ROOTS = ()
_HOME_WRITE_REFUSAL = (
    "real ~/.local/bin and ~/.config/systemd are off while tests run (JARVIS_NO_MODELS). "
    "Patch the writer or use a temp home."
)
# ssh/scp/sftp leave the machine. Loopback stays allowed so a test can talk to a local
# server; any other destination, and any destination we cannot parse, is refused.
# ssh itself stays in LAUNCHERS so `ssh host claude` is still caught as a model.
_REMOTE_CMDS = {"ssh", "scp", "sftp"}
_REMOTE_REFUSAL = (
    "real ssh, scp, and sftp are off while tests run (JARVIS_NO_MODELS) "
    "unless the destination is loopback. Patch the call or fake the transport."
)
# Options that consume the next argument. Clustered and attached forms (-4p 22, -p22)
# are handled in _positionals. scp and sftp take the port as -P; ssh takes it as -p.
_SSH_VALUE_OPTS = {"-p", "-i", "-o", "-l", "-F", "-J", "-L", "-R", "-D", "-b", "-c",
                   "-E", "-e", "-m", "-O", "-Q", "-S", "-W", "-w", "-B", "-I"}
_SCP_VALUE_OPTS = {"-c", "-D", "-F", "-i", "-J", "-l", "-o", "-P", "-S", "-X"}
_SFTP_VALUE_OPTS = {"-B", "-b", "-c", "-D", "-F", "-i", "-J", "-l", "-o", "-P",
                    "-R", "-S", "-s", "-X"}
_UNPARSED = object()
_TAILSCALE_CMDS = {"tailscale", "tailscaled"}
_TAILSCALE_REFUSAL = (
    "real tailscale is off while tests run (JARVIS_NO_MODELS). Patch the call or fake it."
)
_NOTIFY_REFUSAL = "tests must patch jarvis notification to a fake."


def _account_home():
    try:
        import pwd
        return os.path.abspath(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, AttributeError, KeyError, OSError):
        return os.path.abspath(os.path.expanduser("~"))


def _account_homes():
    """The account home and, when it differs, its real path. Not $HOME."""
    home = os.path.normpath(_account_home())
    homes = [home]
    try:
        real = os.path.normpath(os.path.realpath(home))
    except OSError:
        real = home
    if real not in homes:
        homes.append(real)
    return homes


def _freeze_autostart_roots():
    """Real autostart directories, captured before any test patches HOME or APPDATA."""
    homes = _account_homes()
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


def _freeze_home_write_roots():
    """Real ~/.local/bin and ~/.config/systemd, frozen with the autostart roots."""
    roots = []
    for item in _account_homes():
        roots.append(os.path.join(item, ".local", "bin"))
        roots.append(os.path.join(item, ".config", "systemd"))
    return tuple(dict.fromkeys(os.path.normcase(os.path.normpath(r)) for r in roots))


def _path_text(value):
    if isinstance(value, int) or value is None:
        return None
    try:
        return os.fsdecode(os.fspath(value))
    except (TypeError, ValueError):
        return None


def _under_roots(value, roots):
    text = _path_text(value)
    if not text:
        return False
    # expanduser follows the HOME of this call. A temp HOME therefore does not match
    # the roots frozen above; an absolute path into the real directory still does.
    folded = os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(text))))
    for root in roots:
        if folded == root or folded.startswith(root + _AUTOSTART_SEP):
            return True
    return False


def _under_autostart(value):
    return _under_roots(value, _AUTOSTART_ROOTS)


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


def _refuse_rooted_files(event, args, under, refusal):
    if event == "open":
        if args and _open_writes(args) and under(args[0]):
            raise RuntimeError(f"Blocked write {args[0]}: {refusal}")
        return
    if event in ("os.remove", "os.unlink"):
        if args and under(args[0]):
            raise RuntimeError(f"Blocked unlink {args[0]}: {refusal}")
        return
    if event == "os.rename" and any(under(arg) for arg in args[:2]):
        src = args[0] if args else ""
        dst = args[1] if len(args) > 1 else ""
        raise RuntimeError(f"Blocked rename {src} -> {dst}: {refusal}")


def _refuse_autostart_files(event, args):
    _refuse_rooted_files(event, args, _under_autostart, _AUTOSTART_REFUSAL)


def _refuse_home_writes(event, args):
    _refuse_rooted_files(event, args, lambda value: _under_roots(value, _HOME_WRITE_ROOTS),
                         _HOME_WRITE_REFUSAL)


def _stem(word):
    return Path(os.fsdecode(word).strip("\"'")).stem.lower()


def _model(name):
    # codex's npm wrapper runs a native codex-x86_64-...; Claude's package is claude-code.
    return name in MODEL_CLIS or name.startswith(("codex-", "claude-"))


def _as_text(value):
    if isinstance(value, str):
        return value
    try:
        return os.fsdecode(value if isinstance(value, bytes) else os.fspath(value))
    except (TypeError, ValueError):
        return None


def _positionals(args, value_opts):
    """Arguments left after options. None when a value-option's argument is missing."""
    text = []
    for item in args:
        decoded = _as_text(item)
        if decoded is None:
            return None
        text.append(decoded)
    out = []
    index = 0
    count = len(text)
    while index < count:
        arg = text[index]
        if arg == "--":
            out.extend(text[index + 1:])
            return out
        if arg.startswith("--"):
            index += 1
            continue
        if len(arg) > 1 and arg.startswith("-"):
            rest = arg[1:]
            index += 1
            while rest:
                flag = "-" + rest[0]
                rest = rest[1:]
                if flag not in value_opts:
                    continue
                if rest:  # -p22: the rest of this word is the value
                    rest = ""
                    continue
                if index >= count:
                    return None
                index += 1
            continue
        out.append(arg)
        index += 1
    return out


def _is_loopback(host):
    text = host.strip()
    if text.startswith("[") and text.endswith("]") and len(text) > 2:
        text = text[1:-1]
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _bare_ip_host(token):
    """An address with no path (`::1`, `user@[::1]`). None when the token is host:path."""
    text = token.strip()
    host = text
    if text.startswith("["):
        end = text.find("]")
        if end != len(text) - 1 or end <= 1:
            return None
        host = text[1:end]
    elif "@" in text:
        user, host = text.rsplit("@", 1)
        if not user:
            return None
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


def _bare_host(token):
    """Host from `user@host`, `[::1]`, or a bare name. `_UNPARSED` when it is empty."""
    token = token.strip()
    if not token or token.startswith("-"):
        return _UNPARSED
    address = _bare_ip_host(token)
    if address is not None:
        return address
    if token.startswith("["):
        end = token.find("]")
        if end <= 1:
            return _UNPARSED
        if token[end + 1:] and not token[end + 1:].startswith(":"):
            return _UNPARSED
        return token[1:end].strip() or _UNPARSED
    if "@" in token:
        user, host = token.rsplit("@", 1)
        if not user or not host:
            return _UNPARSED
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        return host.strip() or _UNPARSED
    return token


def _unbracketed_ipv6(token):
    """Address of `v6:/path` or `v6:name`. None when the token is not an IPv6 destination."""
    if ":/" in token:
        candidate = token.split(":/", 1)[0]
    else:
        candidate, _path = token.rsplit(":", 1)
    if "@" in candidate:
        candidate = candidate.rsplit("@", 1)[1]
    text = candidate.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return None
    return text


def _remote_spec_host(token):
    """Host of an scp/sftp `[user@]host:path`. None when the token is a local path."""
    if not token or token.startswith("-"):
        return _UNPARSED
    # A bare address is a local file name for scp (a remote needs host:path).
    if _bare_ip_host(token) is not None:
        return None
    if len(token) >= 2 and token[0].isalpha() and token[1] == ":":
        return None  # Windows drive letter, not a remote host
    if token.startswith("/") or token.startswith("./"):
        return None
    bracket = token.find("[")
    if bracket != -1:
        end = token.find("]", bracket + 1)
        if end == -1:
            return _UNPARSED
        prefix = token[:bracket]
        if prefix and not prefix.endswith("@"):
            return _UNPARSED
        if prefix.endswith("@") and not prefix[:-1]:
            return _UNPARSED
        rest = token[end + 1:]
        if rest and not rest.startswith(":"):
            return _UNPARSED
        return token[bracket + 1:end].strip() or _UNPARSED
    if ":" not in token:
        return None
    if token.count(":") >= 2:
        host = _unbracketed_ipv6(token)
        if host is not None:
            return host
    host_part, _path = token.split(":", 1)
    if "@" in host_part:
        user, host_part = host_part.rsplit("@", 1)
        if not user:
            return _UNPARSED
    if not host_part or "/" in host_part or "\\" in host_part:
        return _UNPARSED
    return host_part


def _ssh_targets(args):
    positional = _positionals(args, _SSH_VALUE_OPTS)
    if not positional:
        return _UNPARSED
    host = _bare_host(positional[0])
    if host is _UNPARSED:
        return _UNPARSED
    return [host]


def _scp_targets(args):
    positional = _positionals(args, _SCP_VALUE_OPTS)
    if not positional:
        return _UNPARSED
    hosts = []
    for token in positional:
        host = _remote_spec_host(token)
        if host is _UNPARSED:
            return _UNPARSED
        if host is not None:
            hosts.append(host)
    return hosts


def _sftp_targets(args):
    positional = _positionals(args, _SFTP_VALUE_OPTS)
    if not positional or len(positional) != 1:
        return _UNPARSED
    token = positional[0]
    address = _bare_ip_host(token)
    if address is not None:
        return [address]
    if ":" in token or "[" in token:
        host = _remote_spec_host(token)
        if host is None or host is _UNPARSED:
            return _UNPARSED
        return [host]
    host = _bare_host(token)
    if host is _UNPARSED:
        return _UNPARSED
    return [host]


def _remote_targets(command, args):
    if command == "ssh":
        return _ssh_targets(args)
    if command == "scp":
        return _scp_targets(args)
    if command == "sftp":
        return _sftp_targets(args)
    return _UNPARSED


def _refuse_one_remote(command, args):
    targets = _remote_targets(command, args)
    if targets is _UNPARSED or any(not _is_loopback(host) for host in targets):
        raise PermissionError(f"Blocked {command}: {_REMOTE_REFUSAL}")


def _direct_args(program, argv, words, event):
    if event == "os.system":
        if words and _stem(words[0]) == program:
            return words[1:]
        return list(words)
    decoded = []
    for item in argv:
        text = _as_text(item)
        if text is None:
            return None
        decoded.append(text)
    if decoded and _stem(decoded[0]) == program:
        return decoded[1:]
    return decoded


def _refuse_remote_commands(program, argv, words, event):
    # The program itself. Its own argv is not whitespace-split, so `-o "a b"` stays one value.
    if program in _REMOTE_CMDS:
        args = _direct_args(program, argv, words, event)
        if args is None:
            raise PermissionError(f"Blocked {program}: {_REMOTE_REFUSAL}")
        _refuse_one_remote(program, args)
        return
    # `bash -c "ssh dell …"` and `env scp a dell:b`: same word scan as the model CLIs.
    if program not in LAUNCHERS and event != "os.system":
        return
    stems = [_stem(word) for word in words]
    for index, stem in enumerate(stems):
        if stem in _REMOTE_CMDS:
            _refuse_one_remote(stem, words[index + 1:])


def _refuse_side_effects(named):
    if "notify-send" in named:
        raise PermissionError(f"Blocked notify-send: {_NOTIFY_REFUSAL}")
    blocked = sorted(name for name in named if name in _TAILSCALE_CMDS)
    if blocked:
        raise PermissionError(f"Blocked {', '.join(blocked)}: {_TAILSCALE_REFUSAL}")


def _refuse_models(event, args):
    # File events are not process starts. Checked first so a write into the real
    # autostart directory is refused even when no service manager is launched.
    # The user unit directory is inside the systemd tree; its own message wins.
    _refuse_autostart_files(event, args)
    _refuse_home_writes(event, args)
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
    _refuse_side_effects(named)
    _refuse_remote_commands(program, argv, words, event)
    # A test that hands a child its own environment still passes the guard on to it, so a
    # `python -m jarvis` child refuses models too.
    env = args[3] if event == "subprocess.Popen" else None
    if isinstance(env, dict):
        key = b"JARVIS_NO_MODELS" if any(isinstance(k, bytes) for k in env) else "JARVIS_NO_MODELS"
        env[key] = b"1" if isinstance(key, bytes) else "1"


if _under_tests() and not getattr(sys, "_jarvis_models_blocked", False):
    os.environ["JARVIS_NO_MODELS"] = "1"
    _AUTOSTART_ROOTS = _freeze_autostart_roots()
    _HOME_WRITE_ROOTS = _freeze_home_write_roots()
    sys.addaudithook(_refuse_models)
    sys._jarvis_models_blocked = True
