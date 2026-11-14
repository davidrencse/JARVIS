"""L3 gates: the checks every command passes before it is sent (docs/jarvis-os-architecture.md).

Cheapest first, fail fast. Each check returns {"name", "pass", "why"} for the trace.
A command is dispatched only when every applicable check passes and its risk is
"allow". "confirm" commands wait for David; until the confirmation queue exists they
are held back with a reply saying so, never sent.

The checks that need the outside world take it as arguments (reachable, ready,
app_exists), so the gates themselves stay pure and testable.
"""

from .home import home as _jarvis_home
import json
import os
from .platforms import on_windows
import posixpath
import time
import threading
import re
from pathlib import Path

from . import registry

AGENT_ORIGINS = frozenset({"agent", "system"})
DUPLICATE_WINDOW = 2.0   # seconds after a run finishes: a double trigger is merged, not resent
RUNNING_CAP = 300.0      # seconds: an unreleased run (a lost thread) stops blocking repeats


class InFlight:
    """Commands being executed now (U7, and the in-flight half of H3)."""

    def __init__(self):
        # key -> (time, request, running): running since `time`, or finished at `time`.
        self._lock, self._items = threading.Lock(), {}

    @staticmethod
    def key(command):
        return (command["device"], command["intent"],
                tuple(sorted((k, str(v)) for k, v in command["args"].items())))

    def claim(self, command, request=None):
        """True if this caller now owns the command; False if an identical one is still
        running (however long it takes: "open spotify on arch" can take 45 s over the
        cluster) or finished within DUPLICATE_WINDOW. Commands of the same `request` are
        never merged ("next and next" means twice); only another request's duplicate is.
        A run never released stops blocking after RUNNING_CAP, so nothing sticks forever."""
        key, now = self.key(command), time.monotonic()
        with self._lock:
            for old in [k for k, (at, _, run) in self._items.items()  # forget expired entries
                        if now - at >= (RUNNING_CAP if run else DUPLICATE_WINDOW)]:
                del self._items[old]
            since, owner, running = self._items.get(key, (None, None, False))
            busy = since is not None and now - since < (RUNNING_CAP if running else DUPLICATE_WINDOW)
            if busy and (request is None or owner is None or owner != request):
                return False
            self._items[key] = (now, request, True)
            return True

    def release(self, command, ok=True, request=None):
        """Done. A success keeps the merge window (a double trigger stays merged); a
        failure clears it, so retrying right away runs instead of "already doing that"."""
        with self._lock:
            if ok:
                self._items[self.key(command)] = (time.monotonic(), request, False)
            else:
                self._items.pop(self.key(command), None)


# rules.trusted_local in jarvis.config.json: David's own low-risk actions on the PC he is
# using skip the checks that cost time (the installed-app lookup, history, the on-screen
# wait). Schema validation and the risk policy still run: they cost microseconds and are
# what keep a bad argument or a risky command (close_app, agent tasks) from being sent.
TRUSTED_LOCAL = {"enabled": True,
                 "intents": ["open_app", "open_url", "focus_window", "media", "volume",
                             "scroll", "list_apps", "spotify_play"],
                 "skip": ["app_exists", "history", "observe"]}
SKIPPABLE = frozenset({"app_exists", "history", "observe", "dedupe"})


CONFIRM_DEFAULT = "ask"  # rules.confirm when unset; settings.DEFAULTS and sysview read this


def asks(config):
    """rules.confirm in jarvis.config.json: "ask" (default) parks risky actions for David's
    OK; "never" runs them at once (his choice, 2026-11-01). Refusals (passwords, 2FA codes,
    card numbers; agent-origin actions) stay either way: they are not questions (hard_deny).
    Only exactly "never" (whitespace aside) turns asking off; any other value asks."""
    return str(((config or {}).get("rules") or {}).get("confirm", CONFIRM_DEFAULT)).strip() != "never"


_LIVE = {}  # config path -> (mtime, rules.confirm)


def asks_now(config, state=None):
    """asks(), but from jarvis.config.json as it is on disk now when it sits next to the
    state folder (the usual layout): the brain and browser gates are built once, so a
    switch back to "ask" would otherwise wait for a restart. Falls back to `config`."""
    if state is not None and Path(state).is_dir():
        path = Path(state).resolve().parent / "jarvis.config.json"
        try:
            mtime = path.stat().st_mtime
            cached = _LIVE.get(str(path))
            if not cached or cached[0] != mtime:
                rules = (json.loads(path.read_text(encoding="utf-8")) or {}).get("rules") or {}
                _LIVE[str(path)] = cached = (mtime, rules.get("confirm", CONFIRM_DEFAULT))
            return asks({"rules": {"confirm": cached[1]}})
        except (OSError, ValueError, AttributeError):
            pass  # no file there, or it's mid-edit: the config the gate was built with
    return asks(config)


_LOCAL = {}  # config path -> (mtime, config)


def local_config(base=None):
    """This computer's jarvis.config.json (next to the jarvis package), for checks that run
    where no config was handed down (a node's terminal, desktop typing). {} if unreadable:
    hard_deny still refuses everything it can judge without the vault paths."""
    path = Path(base or _jarvis_home()) / "jarvis.config.json"
    try:
        mtime = path.stat().st_mtime
        cached = _LOCAL.get(str(path))
        if not cached or cached[0] != mtime:
            data = json.loads(path.read_text(encoding="utf-8"))
            _LOCAL[str(path)] = cached = (mtime, data if isinstance(data, dict) else {})
        return cached[1]
    except (OSError, ValueError):
        return {}


# What a brain terminal command may never do, asked or not (hard_deny): enter credentials.
CREDENTIAL_CMD = re.compile(
    r"\bsudo\s+(?:-\w*S\w*|--stdin)\b|--password\b|--passwd\b|--passphrase\b|--with-token\b|"
    r"\bpasswd\b|\bchpasswd\b|\bsshpass\b|\bgh\s+auth\s+(?:login|token)\b|\bssh-keygen\b|"
    r"\bssh-add\b|\bsecret-tool\s+store\b|\bgit\s+credential\b|\bpass\s+(?:insert|edit)\b|"
    r"\bcmdkey\b.*\s/pass|\bnet\s+use\b.*\s/user|\bconvertto-securestring\b|"
    r"\bsudo(?:\s+(?:-[ugpCghrtTU]\s+\S+|--\w[\w-]*(?:=\S+)?|-\w+))*?\s+(?:-\w*[SA]\w*|--stdin|--askpass)\b|"
    r"\bSUDO_ASKPASS=|\bgh\s+auth\b|"
    r"\b(?:docker|podman|helm|oras)\s+login\b|\bnpm\s+(?:login|adduser)\b|\bgcloud\s+auth\b|\baz\s+login\b|"
    r"\baws\s+configure\b|\bhtpasswd\b|\bnet\s+user\s+\S+\s+\S+|\bkeyring\s+set\b", re.I)
RM_NAMES = frozenset({"rm", "rmdir", "remove-item", "ri", "rd", "del", "erase", "unlink"})
# Every way bash starts another command: lists, pipes, background &, subshells, groups and
# substitutions (audit 2026-11-03: `(rm -rf ~)`, `true & rm -rf ~` and `$(rm -rf ~)` got past).
SEPARATORS = re.compile(r"&&|\|\||\$\(|[;|\n&()`]|(?<![^\s;&|)])\{(?=\s)|(?<=[\s;])\}")  # { } only as groups
# Words that run the next word as the command: `/bin/rm`, `env rm`, `timeout 5 rm`, `then rm`.
WRAPPERS = frozenset({"sudo", "doas", "command", "builtin", "env", "nice", "ionice", "nohup", "timeout",
                      "time", "stdbuf", "setsid", "chrt", "taskset", "xargs", "busybox", "exec",
                      "then", "do", "else", "elif", "if", "while", "until", "!", "nocorrect", "noglob"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "busybox", "pwsh", "powershell", "cmd"})


# Options of a wrapper that take the next word as their value (`env -u X rm`, `timeout -s KILL 5 rm`):
# skipping only the dash word would read the value as the command and let the real one through.
WRAPPER_VALUE_OPTS = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-a", "--argv0"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "exec": frozenset({"-a"}),
    "ionice": frozenset({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"}),
    "chrt": frozenset({"-p"}),
    "taskset": frozenset({"-p", "-c"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "sudo": frozenset({"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-T", "-U", "-D", "-R", "--user", "--group",
                       "--close-from", "--host", "--prompt", "--role", "--type", "--command-timeout",
                       "--other-user", "--chdir", "--chroot"}),
    "doas": frozenset({"-u", "-C"}),
    "xargs": frozenset({"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "--max-args", "--max-procs",
                        "--max-lines", "--max-chars", "--delimiter", "--arg-file"}),
    "setsid": frozenset(), "nohup": frozenset(), "time": frozenset({"-f", "-o", "--format", "--output"}),
}
SHELL_KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!"})


def _base(word):
    return re.sub(r"\.exe$", "", word.replace("\\", "/").rsplit("/", 1)[-1].lower())


def _command_name(values):
    """(index, name) of the word that really runs: wrappers, their options (and those options'
    values) and VAR=x skipped, paths cut to the program's name (/usr/bin/rm -> rm, rm.exe -> rm).
    Name is "" when nothing is left to run."""
    i, wrapper = 0, None
    while i < len(values):
        word = values[i]
        base = _base(word)
        if base in WRAPPERS:
            wrapper = base
            i += 1
            continue
        if word.startswith("-"):
            i += 2 if word in WRAPPER_VALUE_OPTS.get(wrapper, ()) else 1
            continue
        if re.fullmatch(r"\w+=.*|\d+(?:\.\d+)?[smhd]?|0x[0-9a-f]+|[0-9,-]+", word, re.I):
            i += 1
            continue
        return i, base
    return len(values), ""


def _split_string(values):
    """The command text `env -S 'rm -rf ~'` / `env --split-string=...` runs, else None."""
    for k, word in enumerate(values):
        if _base(word) != "env":
            continue
        for j in range(k + 1, len(values)):
            v = values[j]
            if not v.startswith("-"):
                if re.fullmatch(r"\w+=.*", v):
                    continue
                break
            if v in ("-S", "--split-string"):
                return values[j + 1] if j + 1 < len(values) else ""
            if v.startswith("--split-string="):
                return v.split("=", 1)[1]
            if re.fullmatch(r"-[a-zA-Z]*S.+", v):
                return v[v.index("S") + 1:]
    return None


def _git_discards(args):
    """Why this git command (the words after `git`) throws away uncommitted work, or None.
    Global options (-C dir, -c k=v, --git-dir x) are skipped to find the subcommand."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") else 1
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    why = "JARVIS doesn't run git commands that discard uncommitted work"
    flags = [a for a in rest if a.startswith("-") and a != "--"]
    plain = [a for a in rest if not a.startswith("-")]
    if sub in ("checkout", "reset") and any(re.fullmatch(r"-[a-zA-Z]*p[a-zA-Z]*|--patch", f) for f in flags):
        return why
    if sub == "checkout":
        if "--" in rest or any(re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*|--force|--ours|--theirs|--overwrite-ignore|"
                                            r"--discard-changes", f) for f in flags):
            return why
        makes = any(re.fullmatch(r"-[a-zA-Z]*[bB][a-zA-Z]*|--orphan", f) for f in flags)
        # `checkout <branch>` has one name; a second (or ".", a glob, an existing path) is a pathspec
        if len(plain) > (2 if makes else 1) or any(a == "." or re.search(r"[*?\[]", a) or
                                                    (not makes and os.path.exists(a)) for a in plain):
            return why
    if sub == "restore":
        staged_only = any(f in ("--staged", "-S") for f in flags) and \
            not any(f in ("--worktree", "-W") for f in flags)
        if not staged_only:
            return why
    if sub == "switch" and any(re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*|--force|--discard-changes", f) for f in flags):
        return why
    return None


_HOME_NAMES = r"\$\{?env:(?:USERPROFILE|HOME)\}?|%(?:USERPROFILE|HOMEPATH|HOME)%|\$\{?HOME\}?"


def _unresolved(word, command):
    """True when a write target still holds a variable, % or ( after the ones this check fills in
    (home, $PWD, and bash VAR= assigned in the same command) are taken out."""
    assigned = set(re.findall(r"(?:^|[\s;&])([A-Za-z_]\w*)=", command))
    text = re.sub(_HOME_NAMES + r"|\$\{?PWD\}?|\$env:PWD", "", word, flags=re.I)
    text = re.sub(r"\$\{?(\w+)\}?", lambda m: "" if m.group(1) in assigned else m.group(0), text)
    return bool(re.search(r"[$%`(]", text))


def _vault_paths(config):
    path = ((config or {}).get("vault") or {}).get("path")
    paths = [v for v in path.values()] if isinstance(path, dict) else [path]
    return [str(p) for p in paths if p]


def _abs(path, here, real=True):
    """`path` made absolute against `here`, with symlinks resolved; real=False keeps the
    path as written (normalised only), for checks on where a write lands by name."""
    # expanduser gives C:\Users\x on Windows: slashes after it too, or no guard ever matches.
    text = os.path.expanduser(os.path.expandvars(str(path).replace("\\", "/"))).replace("\\", "/")
    if not (text.startswith("/") or re.match(r"[A-Za-z]:", text)):
        text = posixpath.join(str(here).replace("\\", "/"), text)
    text = re.sub(r"^/{2,}", "/", text)  # bash reads //home as /home; normpath keeps the //
    text = posixpath.normpath(text).rstrip("/") or "/"
    return _real(text) if real else text


def _real(path):
    """`path` with symlinks resolved (audit 2026-11-04: `ln -s ~ /tmp/h`, then `rm -rf
    /tmp/h/projects` was judged as /tmp). A C:/ path on a non-Windows PC stays as written."""
    if re.match(r"[A-Za-z]:", path) and not on_windows():
        return path
    try:
        real = os.path.realpath(path).replace("\\", "/")
    except (OSError, ValueError):
        return path
    return real.rstrip("/") or "/" if not re.fullmatch(r"[A-Za-z]:/", real) else real


# Windows paths keep their backslashes as path separators: bash-style parsing (_words) would
# read C:\Users\jdoe as C:Usersdavid (audit 2026-11-04: `cd C:\Users && rd /s /q david`).
_WINPATH = re.compile(r"[A-Za-z]:\\[^\s'\"]*|(?<![\w\\$`])(?:~|\.\.?)\\[^\s'\"]*")


def _winslash(text):
    return _WINPATH.sub(lambda m: m.group(0).replace("\\", "/"), text)


# Any home folder or drive root, on any OS: `rm -rf C:\Users\jdoe` on the Dell is checked on
# the main, whose own home is /home/jdoe.
TOP_FOLDERS = re.compile(r"^(?:/|/home/[^/]+|/root|/Users/[^/]+|[a-z]:/?|[a-z]:/users(?:/[^/]+)?)$", re.I)
EXPANDS = re.compile(r"[$`*?\[{]|^~[^/]|%\w+%")

# Shapes a text check can't see through, refused outright when they could delete or run other
# code (red team 2026-11-03). Fail-closed: the brain can always write the command out plainly.
_DELETE = r"(?:rm|rmdir|rd|del|erase|remove-item|ri|unlink|shred|truncate)(?:\.exe)?"
_SHELL = r"(?:bash|sh|zsh|dash|ksh|fish|pwsh|powershell|cmd)(?:\.exe)?"
# The console (7777), the hub, the relay and browser debug ports trust any local caller (a
# fixed header is all the console asks), so a brain command that talks to them could approve
# its own confirmation. On Linux the sandbox has no route to the host's loopback; Windows and
# macOS have no sandbox, so refuse the shape here (audit 2026-11-04).
# Every spelling of 127.0.0.0/8, 0.0.0.0 and ::1: dotted 1-4 parts (decimal, octal, hex first
# part), one big integer (decimal, octal, hex), expanded IPv6. A bare `0` host is URL-only.
_LOOPBACK = (r"(?:localhost|0\.0\.0\.0|(?:0*127|0+177|0x0*7f)(?:\.(?:0x[0-9a-f]+|\d+)){1,3}|"
             r"21(?:3\d|4[0-7])\d{6}|0+17[0-7]{9}|0x0*7f[0-9a-f]*|"
             r"\[?(?:(?:0{1,4}(?::0{1,4}){0,5})?::0*1|(?:0{1,4}:){7}0{0,3}1)\]?)")
# A trailing dot or a wildcard-DNS suffix still resolves to loopback: localhost., 127.0.0.1.nip.io.
LOCAL_CONSOLE = re.compile(r"(?<![\w.])" + _LOOPBACK + r"(?!\w|\.\d)|(?:(?<=//)|(?<=@))0+(?=[:/\s]|$)|"
                           r"/api/(?:confirm|ask|settings|send|remote)\b|\bX-JARVIS\b", re.I)

# console, hub, grokbot RELAY_PORT/TUNNEL_PORT, the phone app (phone.py); cluster.listen_port
# and phone.port too
CONSOLE_PORTS = (7777, 7780, 7779, 7778, 7787)
_CONSOLE_WHY = "JARVIS doesn't let commands talk to its own console or hub on this computer"


def _console_port(text, config):
    """A command naming the console's or hub's port with any host: a hostname that resolves to
    loopback (localtest.me, *.nip.io) reaches them as well as 127.0.0.1 does."""
    ports = set(CONSOLE_PORTS)
    for section, key in (("cluster", "listen_port"), ("phone", "port")):
        try:
            ports.add(int(((config or {}).get(section) or {}).get(key)))
        except (TypeError, ValueError):
            pass
    alts = "0*(?:" + "|".join(str(p) for p in sorted(ports)) + r")(?!\d)"
    if re.search(r"[\w\]\-.%]:" + alts, text):
        return True
    # bash's /dev/tcp/HOST/PORT (and udp): the port, or one the text can't read ($P, {a,b})
    if re.search(r"/dev/(?:tcp|udp)/[^/\s]+/(?:" + alts + r"|(?!\d+(?![^\s;|&)<>'\"])))", text, re.I):
        return True
    # A bare port argument to a network tool: nc localtest.me 7777, telnet h 7780 < body.txt
    port = r"(?<![\w.:/=%-])" + alts + r"(?![\w.:])"
    if NET_TOOL.search(text) and re.search(port, text):
        return True
    # Inline code with the port as its own literal: ('localtest.me',7777), connect((h, 7780))
    return bool(INLINE_CODE.search(text) and re.search(r"(?:[,(]\s*|\bport\s*[=:]\s*)" + alts + r"\s*[,)}]", text, re.I))


# The phone app (phone.py), the webcam and a PC's location (2026-11-08): a brain command must
# not mint a pairing code (an owner-level key), switch camera consent on, open a webcam or
# read where a computer is. These are text checks, a speed bump in front of code that runs
# unsandboxed (Windows, macOS), not a wall: string-built Python still gets past them.
# The `jarvis` CLI by any spelling (jarvis, jarvis.exe/.cmd/.ps1, python/py -m jarvis), with
# global options (--state X) before a `phone`/`camera` subcommand, or a subcommand the shell
# fills in later (a glob, a variable, a brace list).
JARVIS_CLI = re.compile(
    r"(?:^|(?<=[\s;&|(`'\"=/\\!{]))(?:jarvis(?:\.exe|\.cmd|\.bat|\.ps1|\.py)?|-[a-z]*m\s*['\"]?jarvis"
    r"(?:\.__main__)?)['\"]?(?:\s+--?[\w-]+(?:=\S*|\s+(?!--?\w)\S+)?)*"
    r"(?:\s+['\"]?(?:phone|camera)\b|\s+[^\s;&|]*[*?\[$`{])", re.I)
JARVIS_FED = re.compile(r"\b(?:xargs|parallel)\b[^|;&\n]*(?:^|[\s/\\'\"])(?:jarvis(?:\.\w+)?|-[a-z]*m\s*jarvis)"
                        r"(?![\w-])", re.I)
JARVIS_MODULES = re.compile(r"\bjarvis\s*\.\s*(?:phone|camera|locate)\b|"
                            r"\bfrom\s+jarvis\s+import\b[^\n;]*\b(?:phone|camera|locate)\b|"
                            r"\b(?:run_module|import_module|__import__)\s*\(\s*['\"]jarvis", re.I)
INLINE_RUN = re.compile(r"\b(?:python[\d.]*|py|perl|ruby|node|deno|bun|php|lua)(?:\.exe)?\b[^;&|\n]*"
                        r"\s-[a-zA-Z]*[ce]\b|<<|\b(?:pwsh|powershell)\b", re.I)
WEBCAM = re.compile(r"\bff(?:mpeg|play|probe)(?:\.exe)?\b[^;&|\n]*(?:\bdshow\b|\bv4l2?\b|video4linux|"
                    r"\bavfoundation\b|\bvfwcap\b)|/dev/video\d*|/dev/v4l\b|\bv4l2src\b|"
                    r"\bv4l2-ctl\b[^;&|\n]*--stream|\bfswebcam\b|\bVideoCapture\s*\(|\b(?:av://)?v4l2://|"
                    r"\bdshow://", re.I)
LOCATION = re.compile(r"\bwhere-am-i\b|geoclue-2\.0[/\\]+demos|org\.freedesktop\.GeoClue2|"
                      r"GeoCoordinateWatcher|System\.Device\.Location|Windows\.Devices\.Geolocation|"
                      r"\bGeolocator\b", re.I)
PHONE_PATHS = re.compile(r"/phone/(?:api|pair)\b|/phone/[^\s'\"]*[{\[*?]|__Host-jarvis_phone|"
                         r"\bjarvis_phone\b", re.I)
# Serve and Funnel decide who can reach the console and the phone app: never changed by a command.
# Only `serve status` / `funnel status` (flags around it, nothing else) only looks: a "status"
# later in the line (--set-path /status, 2>status.log, a comment) publishes all the same.
_TS_FLAGS = r"(?:\s+--?[\w-]+(?:=\S+)?)*"
TAILSCALE_PUBLISH = re.compile(r"\btailscale(?:\.exe)?\b[^;&|\n]*?\s(?:serve|funnel)\b"
                               r"(?!" + _TS_FLAGS + r"\s+status" + _TS_FLAGS + r"\s*(?:$|[;&|\n]))", re.I)
# PowerShell's backtick and cmd's caret escape the next character: d`show and d^show are dshow,
# jar^vis is jarvis. Bash reads both literally, so taking them out only ever refuses more.
_ESCAPE = re.compile(r"[`^](.)", re.S)
_CONTINUATION = re.compile(r"[\\`^]\r?\n")  # a line continued in bash, PowerShell or cmd
_CLI_PROGRAM = re.compile(r"(?:.*[/\\])?jarvis(?:\.(?:exe|cmd|bat|ps1|py))?", re.I)
_MODULE_FLAG = re.compile(r"-[a-z]*m(.*)", re.I)  # python -m X, -Im X, -mX
_CLI_MODULE = re.compile(r"jarvis(?:\.__main__)?", re.I)
_UNREADABLE = re.compile(r"[$*?\[{@%`]")  # a variable, glob, brace list or splat the shell fills in


def _argv_words(text):
    """Roughly the words bash, PowerShell or cmd hand a program. Quoted text is one word and
    joins what touches it ("x y", ph''one). PowerShell's ( ) , and @( split words, with "(" kept
    as a word of its own (a group's value can't be read here). ; & | and newlines end a command
    and become ";"."""
    words, word, quote, started = [], [], None, False

    def end_word():
        nonlocal word, started
        if started:
            words.append("".join(word))
        word, started = [], False

    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            else:
                word.append(ch)
        elif ch in "'\"":
            quote, started = ch, True
        elif ch in ";&|\n" or ch == "(":
            end_word()
            words.append("(" if ch == "(" else ";")
        elif ch.isspace() or ch in ")," or (ch == "@" and text[i + 1:i + 2] == "("):
            end_word()
        else:
            word.append(ch)
            started = True
    end_word()
    return words


def _cli_subcommand(rest):
    """rest: the words after `jarvis` (or `python -m jarvis`). True when its subcommand is phone
    or camera, or could be. Global options are skipped with their values (--state X,
    --config 'x y'); a word after an option might be its value or the subcommand, so it is
    judged as both."""
    def suspect(word):
        first = (word.split() or [""])[0].lower()
        return first in ("phone", "camera") or word == "(" or bool(_UNREADABLE.search(word))

    j = 0
    while j < len(rest) and rest[j] != ";":
        word = rest[j]
        if suspect(word):
            return True
        if not word.startswith("-") or word == "-":
            return False  # the subcommand, and not one of those
        if "=" in word:
            j += 1
            continue
        value = rest[j + 1] if j + 1 < len(rest) else ";"
        if value == ";":
            return False
        if suspect(value):
            return True
        j += 1 if value.startswith("-") else 2
    return False


def _jarvis_cli(text, depth=0):
    """The jarvis CLI with a phone or camera subcommand anywhere in text, read word by word
    (bash, PowerShell or cmd). A quoted word holding spaces may be a command for another shell
    (bash -c '...', -ArgumentList '...'): it is read the same way, a few levels deep."""
    words = _argv_words(text)
    for i, word in enumerate(words):
        rest = None
        if _CLI_PROGRAM.fullmatch(word):
            rest = words[i + 1:]
        else:
            module = _MODULE_FLAG.fullmatch(word)
            if module and module.group(1):
                rest = words[i + 1:] if _CLI_MODULE.fullmatch(module.group(1)) else None
            elif module and i + 1 < len(words) and _CLI_MODULE.fullmatch(words[i + 1]):
                rest = words[i + 2:]
        if rest is not None and _cli_subcommand(rest):
            return True
    return depth < 3 and any(_jarvis_cli(w, depth + 1) for w in words if re.search(r"\s", w))


def _phone_denial(command):
    """Why a command reaches for the phone app, the webcam or a computer's location, or None."""
    plain = re.sub(r"[\\'\"]", "", command)
    slashed = re.sub(r"['\"]", "", command.replace("\\", "/"))
    escaped = _ESCAPE.sub(r"\1", command)
    bare = re.sub(r"[\\'\"]", "", escaped)  # escapes, quotes and backslashes all taken out
    texts = (command, plain, slashed, bare)
    if any(JARVIS_CLI.search(t) or JARVIS_FED.search(t) for t in texts) or JARVIS_MODULES.search(command) \
            or JARVIS_MODULES.search(plain) or _jarvis_cli(escaped) or _jarvis_cli(escaped.replace("\\", "")) \
            or (INLINE_RUN.search(command) and re.search(r"\bjarvis\b", plain, re.I)
                and re.search(r"\b(?:phone|camera)\b", plain, re.I)):
        return "JARVIS doesn't run its own phone or camera commands; David runs them at the PC"
    if any(WEBCAM.search(t) for t in texts):
        return "JARVIS doesn't open a webcam from a command"
    if any(LOCATION.search(t) for t in texts):
        return "JARVIS doesn't look up where a computer is from a command"
    unglobbed = re.sub(r"[{}\[\]]", "", plain)
    if any(PHONE_PATHS.search(t) for t in (command, plain, bare, unglobbed, _url_path_norm(plain), _url_path_norm(bare))):
        return "JARVIS doesn't let commands talk to its phone app"
    if any(TAILSCALE_PUBLISH.search(t) for t in texts):
        return "JARVIS doesn't change what Tailscale serves or publishes"
    return None


NET_TOOL = re.compile(r"(?<![\w.-])(?:nc|ncat|netcat|telnet|socat|openssl|curl|wget|nmap|httpie|http(?!s?://)|xh|"
                      r"test-netconnection|tnc|invoke-webrequest|iwr|invoke-restmethod|irm)(?:\.exe)?(?![\w.-])", re.I)
INLINE_CODE = re.compile(r"\b(?:python[\d.]*|(?<!\.)py|perl|ruby|node|php|deno|bun|lua|pwsh|powershell|awk|gawk|"
                         r"tclsh|osascript)\b|new-object|\[[\w.]*sockets?[\w.]*\]|::new\(", re.I)


def _url_path_norm(text):
    """Percent-decoded, with dot segments and doubled slashes taken out, as curl and browsers
    send the path: /api/./remote/start and /x/../api/%72emote are /api/remote."""
    from urllib.parse import unquote
    for _ in range(3):
        text = unquote(text)
    text = text.replace("\\", "/")
    while True:
        new = re.sub(r"/\.(?=/|\s|$)", "", text)
        new = re.sub(r"/[^/\s]+/\.\.(?=/|\s|$)", "", new)
        new = re.sub(r"(?<!:)//+", "/", new)
        if new == text:
            return text
        text = new


STRUCTURAL = [
    (LOCAL_CONSOLE, _CONSOLE_WHY),
    (re.compile(r"(?:^|[\s;&|({`])(?:\S*/)?" + _DELETE + r"\b.*(?:\$\(|`|<\()|(?:\$\(|`|<\().*\b" + _DELETE + r"\b", re.I),
     "deletes can't use command substitution; name the target plainly"),
    (re.compile(r"\b(?:xargs|parallel)\b[^|;&]*\b(?:" + _DELETE + r"|" + _SHELL + r"|find)\b", re.I),
     "xargs/parallel can't feed deletes or shells"),
    (re.compile(r"\b" + _SHELL + r"\b[^|;&]*(?:<<<|<\(|<<|\s<\s)", re.I),
     "JARVIS doesn't run code fed to a shell from text"),
    (re.compile(r">\s*['\"]?([^\s'\";&|]+).*\b(?:" + _SHELL + r"|source|python3?|perl|ruby|node)\s+['\"]?\1\b", re.I),
     "JARVIS doesn't write a script and run it in one go"),
    (re.compile(r"\b(?:python3?|perl|ruby|node|php|awk|gawk|mawk|lua|deno|bun)\b.*(?:rmtree|remove|unlink|rmdir|"
                r"system\s*\(|\bexec|spawn|popen|subprocess|child_process|\brm\s+-)", re.I),
     "inline code that deletes or runs commands isn't allowed; use plain commands"),
    (re.compile(r"\bi`?e`?x\b|invoke-?expression|\bCDPATH=", re.I), "JARVIS doesn't evaluate built-up commands"),
    (re.compile(r"\b(?:start-process|saps|start)\b.*\b" + _SHELL + r"\b", re.I),
     "JARVIS doesn't start a shell through Start-Process"),
    (re.compile(r"\btar\b.*--remove-files", re.I), "tar --remove-files deletes what it archives"),
    (re.compile(r"\[(?:system\.)?io\.(?:directory|file|fileinfo|directoryinfo)\]::(?:delete|move|replace)\b", re.I),
     "JARVIS doesn't delete or move files through .NET calls; use plain commands"),
    (re.compile(r"(?:^|[\s(,'\"])(?:env|cert|hk(?:cu|lm|cr|u|cc)|wsman|variable):|\\\\[\w.-]+\\|-computername\b|"
                r"\benter-pssession\b|\binvoke-command\b.*-computer", re.I),
     "JARVIS doesn't read PowerShell's env, certificate or registry drives, or other machines"),
    (re.compile(r"scriptblock\]::create|invoke-command\b.*-scriptblock|\bnew-module\b", re.I),
     "JARVIS doesn't run PowerShell built from text"),
    (re.compile(r"\b\w[\w-]*\s*\(\s*\)\s*\{", re.I), "JARVIS doesn't define shell functions"),
    (re.compile(r"\b(?:node|deno|bun|python3?|perl|ruby|php)\b.*(?:rmSync|rmdirSync|unlinkSync|rimraf|rmtree|"
                r"homedir|expanduser|Path\.home|os\.remove|os\.unlink|shutil)", re.I),
     "inline code that deletes files isn't allowed; use plain commands"),
    (re.compile(r"\bgit\b.*\b(?:stash\b.*(?:-u|--include-untracked|--all)|reset\b.*--hard|checkout\b.*\s--\s|"
                r"clean\b.*-\w*[fdx])", re.I), "JARVIS doesn't run git commands that throw work away"),
    (re.compile(r"\|\s*(?:remove-item|ri|rm|del|erase)\b[^|;&]*-r", re.I),
     "piping a listing into a recursive delete isn't allowed"),
    # audit 2026-11-04: Windows shapes a path check can't read.
    (re.compile(r"\|\s*(?:(?:foreach-object|foreach|%)\s*\{\s*)?(?:remove-item|ri|rm|del|erase|rd|rmdir|"
                r"unlink)(?:\.exe)?\b", re.I), "piping into a delete isn't allowed; name the files plainly"),
    (re.compile(r"\.(?:delete|moveto)\s*\(", re.I), "JARVIS doesn't delete or move through .Delete()/.MoveTo()"),
    (re.compile(r"\brobocopy\b.*\s/(?:mir|purge|mov|move)\b", re.I),
     "robocopy /MIR, /PURGE and /MOV delete files; use plain commands"),
    (re.compile(r"(?:^|[\s;&|({])" + _DELETE + r"\b[^;&|\n]*(?:\(|\[(?:system\.)?environment\])", re.I),
     "deletes can't take their target from a subexpression; name it plainly"),
    # audit 2026-11-07: autostart a path check can't see (registry Run keys, scheduled tasks,
    # cron, systemd links) and the PowerShell $PROFILE. `crontab -l` and `reg query` stay allowed.
    (re.compile(r"\breg(?:\.exe)?\s+(?:import|restore|load)\b|"
                r"(?:\breg(?:\.exe)?\s+(?:add|copy)\b|\b(?:new|set|copy|rename)-itemproperty\b)[^;&|\n]*"
                r"currentversion[\\/]*(?:policies[\\/]*explorer[\\/]*)?run(?:once)?\b", re.I),
     "JARVIS doesn't add programs to the registry's autostart keys"),
    (re.compile(r"\bschtasks(?:\.exe)?\b[^;&|\n]*\s[/-](?:create|change)\b|"
                r"\b(?:register|set)-scheduledtask\b", re.I),
     "JARVIS doesn't create or change scheduled tasks"),
    # crontab as the command word (segment start, after sudo/env/..., or inside `sh -c`), unless its
    # only options are -l and -u USER in any order: `man crontab`, `cat /etc/crontab` aren't writes.
    (re.compile(r"(?:^|[;&|(`{\n]|\b" + _SHELL + r"\b[^;&|\n]*?\s-\w*c)\s*"
                r"(?:(?:sudo|doas|env|exec|nohup|command|nice|time|timeout|setsid|stdbuf|ionice|xargs|watch|"
                r"then|do|else|!)(?![\w.-])[^;&|\n`)]*?\s)?(?:\S*/)?crontab(?:\.exe)?(?![\w.-])"
                r"(?!(?:\s+-u\s*[^\s;&|]+)*\s+-l(?:\s+-u\s*[^\s;&|]+)*\s*(?:$|[;&|\n)<>`]|\d>))", re.I),
     "JARVIS doesn't change crontabs (crontab -l is fine)"),
    (re.compile(r"\bsystemctl\b[^;&|\n]*(?<![\w.-])(?:enable|link|edit|add-wants|add-requires|preset|"
                r"preset-all|reenable)(?![\w.-])", re.I),
     "JARVIS doesn't enable or link systemd units"),
    # $PROFILE as a destination only: a redirect, a content/tee writer, -Destination, or the last
    # plain argument of a copy/move (`cp $PROFILE /tmp/x` only reads it).
    (re.compile(r"(?:>>?\|?\s*|-destination\s*:?\s*|\b(?:add-content|ac|set-content|sc|out-file|tee-object|tee|"
                r"new-item|ni)\b[^;&|\n]*)\$\{?profile\b|"
                r"\b(?:copy-item|cpi|copy|cp|move-item|mi|mv|move)\b[^;&|\n]*?\s(?!-)[^\s;&|]+\s+"
                r"\$\{?profile\b(?:\.\w+)*\}?(?:\s+-\S+)*\s*(?:$|[;&|\n)])", re.I),
     "JARVIS doesn't write the PowerShell profile"),
    # a write whose path comes from a subexpression: Set-Content -Path (Join-Path ~\Documents 'x') -Value hi
    (re.compile(r"\b(?:set-content|sc|add-content|ac|out-file|tee-object|new-item|ni|copy-item|cpi|move-item|mi|"
                r"rename-item|ren)\b(?:[^;&|\n()]*?\s-(?:destination|path|literalpath|filepath)\s*:?\s*|\s+)\(", re.I),
     "JARVIS can't tell where a write lands when its path comes from a subexpression; name it plainly"),
    (re.compile(r"\bgit\b[^;&|\n]*(?:\bconfig\b(?![^;&|\n]*--get)[^;&|\n]*|\s-c\s*)(?:core\.(?:fsmonitor|hookspath|"
                r"sshcommand|pager|editor|askpass|gitproxy)|sequence\.editor|include(?:if)?\.|credential\.helper|"
                r"diff\.external|filter\.\S+\.(?:clean|smudge|process)|alias\.\S*(?:\s+|=)['\"]?!)", re.I),
     "JARVIS doesn't set git options that run programs (hooks, fsmonitor, ! aliases)"),
]  # what bash fills in at run time: unknowable here


# A link made and a destructive command in one line: the link doesn't exist yet when this
# check resolves paths (audit 2026-11-04: `ln -s ~ /tmp/h && rm -rf /tmp/h/projects`).
LINK_MADE = re.compile(r"\bln\b[^;&|\n]*\s-\w*s|\bln\b[^;&|\n]*--symbolic|\bcp\b[^;&|\n]*\s-\w*s\b|"
                       r"\bmklink\b|-itemtype\s*:?\s*['\"]?(?:symboliclink|junction|hardlink)", re.I)
DESTROYS = re.compile(r"(?:^|[\s;&|({])(?:" + _DELETE + r")\b[^;&|\n]*\s(?:-\w*[rR]\w*|--r\w*|-recurse\w*|/s)\b|"
                      r"(?:^|[\s;&|({])(?:rmdir|rd)\b|\bfind\b.*\s-(?:delete|exec|execdir|ok)\b|"
                      r"\brsync\b.*--del|(?:^|[\s;&|({])(?:mv|move|move-item|mi|rename-item|ren)\b|"
                      r"\b(?:chmod|chown|chgrp|setfacl)\b[^;&|\n]*\s-\w*R|\b(?:icacls|takeown|robocopy)\b", re.I)
# Folders directly in a home that nobody moves or locks away (Move-Item ~\Documents x).
HOME_DIRS = re.compile(r"^(?:/home/[^/]+|/Users/[^/]+|/root|[a-z]:/users/[^/]+)/(?:documents|desktop|pictures|"
                       r"music|videos|downloads|onedrive[^/]*|appdata|projects|src)$", re.I)
# Where a written file runs later on its own (autostart, PATH, git hooks): never written by the
# brain (defence in depth next to the sandbox; the only barrier on Windows/macOS).
PERSIST = re.compile(r"/(?:\.local/bin|\.local/share/systemd|\.config/systemd|\.config/autostart|\.gitconfig|"
                     r"\.config/git|\.git/hooks|\.git/config|\.local/share/applications|library/launchagents|"
                     r"appdata/roaming/microsoft/windows/start menu/programs/startup)(?:/|$)", re.I)
# Shell/desktop startup files and the PowerShell profile run in every new shell (audit 2026-11-07).
# Judged on written paths only: reading or git-inspecting dotfiles (`awk 1 ~/.bashrc`, `git status`
# in ~/.config/hypr) stays allowed. shortcut: inline code that writes them (awk print >, python -c)
# isn't seen here; on Linux the bubblewrap sandbox makes them read-only.
PERSIST_STARTUP = re.compile(r"/(?:\.bashrc|\.bashrc\.d|\.bash_profile|\.bash_login|\.bash_logout|\.profile|"
                             r"\.zshrc|\.zprofile|\.zshenv|\.zlogin|\.xprofile|\.xinitrc|\.xsession|\.xsessionrc|"
                             r"\.config/(?:fish|hypr|uwsm|environment\.d)|"
                             r"documents/(?:windows)?powershell/[^/]*profile\.ps1)(?:/|$)", re.I)
# Commands that write their destination (the last plain argument) or every plain argument.
WRITE_DEST = frozenset({"cp", "mv", "install", "ln", "rsync", "scp", "copy-item", "cpi", "copy", "move-item",
                        "mi", "move", "xcopy", "robocopy", "rename-item", "ren"})
WRITE_ALL = frozenset({"tee", "touch", "truncate", "mkdir", "set-content", "sc", "add-content", "ac", "out-file",
                       "new-item", "ni", "md"})

# audit 2026-11-13: the program word itself must be readable, or every name-based check (rm, git,
# chmod...) is blind. `{rm,-rf,~}`, `r{m,m}`, `$'\162\155'`, `$(printf rm)`, `$x` all run rm.
_DYN_BRACE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_PS_VALUE = re.compile(r"\$\{?[\w:]+\}?(?:[.\[][^\s=]*)?(?:[-+*/%]?=.*)?")  # $x, $x.Foo, $x=5 (PowerShell)
_PS_ASSIGN = re.compile(r"[-+*/%]?=|\+\+|--")
_CMD_LEAD = r"(?:^|&&|\|\||[;|&\n({`]|\b(?:then|do|else|elif|if|while|until)\s)\s*"
_CMD_PRE = r"(?:[A-Za-z_]\w*=\S*\s+)*(?:(?:" + "|".join(sorted(WRAPPERS - SHELL_KEYWORDS)) + r")\s+(?:-\S+\s+)*)*"
# A substitution where the program word should be: `$(printf '\162\155') -rf ~`, `` `which rm` -rf ~ ``.
DYNAMIC_COMMAND = re.compile(_CMD_LEAD + _CMD_PRE + r"[\"']?(?:\$\(|`)")
_DYNAMIC_WHY = "JARVIS can't tell which program that command runs (variable, substitution or brace list); name it plainly"


def _dynamic_word(value, bare, alone, following):
    """True when the program word (value: quotes removed; bare: unquoted characters only) is
    filled in by the shell. A lone PowerShell `$x` / `$x.Foo` / `$x = 1` is an expression, not a run."""
    if _DYN_BRACE.search(bare):
        return True
    if not re.search(r"[$`]", value):
        return False
    if _PS_VALUE.fullmatch(value) and (alone or _PS_ASSIGN.fullmatch(following or "")):
        return False
    return True


# Programs that take -r/-R and only read or copy (or are interpreters whose script decides): the
# generic "recursive flag against home/repo/vault" refusal skips them. Everything else is judged
# like rm: a copied or renamed binary (`cp /bin/rm /tmp/r; /tmp/r -rf ~`) must not slip through.
RECURSIVE_OK = frozenset({
    "ls", "dir", "gci", "get-childitem", "cp", "copy", "copy-item", "cpi", "scp", "rsync", "grep", "egrep", "fgrep",
    "rg", "ag", "ack", "du", "tar", "zip", "unzip", "7z", "diff", "tree", "stat", "file", "wc", "sed", "sort", "cat",
    "head", "tail", "git", "chmod", "chown", "chgrp", "setfacl", "find", "xargs", "pip", "pip3", "npm", "pnpm", "yarn",
    "pytest", "python", "python3", "py", "node", "deno", "bun", "perl", "ruby", "php", "make", "cargo", "go", "docker",
    "mkdir", "md", "touch", "ln", "install", "du", "ncdu", "bat", "less", "more", "open", "xdg-open", "code", "ssh"})
_RECURSIVE_FLAG = re.compile(r"-[rRf]*[rR][rRf]*|--recursive|-recurse|-recurse:\S*", re.I)
_GENERIC_WIPE_WHY = "that recursive command would wipe JARVIS, its vault or the home folder"

# Everything run as a program or read as a script: a file this command wrote (or made executable)
# earlier must not be handed to one of these in the same command line.
RUNNERS = SHELLS | frozenset({"python", "python3", "py", "perl", "ruby", "node", "php", "lua", "deno", "bun",
                              "source", ".", "cscript", "wscript", "start-process", "saps", "invoke-item", "ii"})
_NOT_FILES = frozenset({"null", "stdout", "stderr", "stdin", "tty", "con", "nul"})
_PS_WRITERS = frozenset({"set-content", "sc", "add-content", "ac", "out-file", "new-item", "ni", "tee-object"})
_WRITE_RUN_WHY = ("JARVIS doesn't write, copy or chmod +x a file and run it in one command; "
                  "create it first, then ask to run it")


def _leaf(path):
    path = str(path).strip("'\"").replace("\\", "/")
    if path.startswith("/dev/"):
        return ""
    leaf = posixpath.basename(posixpath.normpath(path)).casefold() if path else ""
    return "" if leaf in (".", "..") or leaf in _NOT_FILES else leaf


def _runs_what_it_wrote(pieces):
    """True when a later step of one command line runs (command word) or hands to a shell or
    interpreter a file an earlier step wrote, copied or made executable (audit 2026-11-13:
    `printf 'rm -rf ~' > /tmp/x; chmod +x /tmp/x; /tmp/x`, `cp /bin/rm /tmp/r && /tmp/r -rf ~`,
    `Set-Content x '...'; pwsh -File x`). Judged on file names, so `cd` in between changes nothing."""
    from . import terminal
    made = set()
    for piece in pieces:
        text = _winslash(piece)
        values = [v for v, _ in (terminal._words(text) or [(w, w) for w in text.split()])]
        at, name = _command_name(values) if values else (0, "")
        rest = values[at + 1:] if name else []
        if made and name:
            plain = [v for v in rest if not v.startswith("-")]
            if _leaf(values[at]) in made or (name in RUNNERS and any(_leaf(v) in made for v in plain)):
                return True
        for m in re.finditer(r"(?<![0-9&])>>?\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s;&|<>()]+))", piece):
            made.add(_leaf(m.group(1) or m.group(2) or m.group(3)))
        if not name:
            continue
        plain = [v for v in rest if not v.startswith("-")]
        if name in WRITE_DEST and plain:
            made.add(_leaf(plain[-1]))
        elif name == "tee" or name in _PS_WRITERS and not any(v.casefold() in ("directory", "dir") for v in rest):
            made.update(_leaf(v) for v in plain)
        elif name == "chmod" and plain:
            mode = plain[0]
            if re.search(r"[+=][rwXst]*x", mode) or re.fullmatch(r"[0-7]{3,4}", mode) and any(c in "1357" for c in mode):
                made.update(_leaf(v) for v in plain[1:])
        for k, v in enumerate(rest[:-1]):
            if v.casefold() in ("-o", "--output", "-outfile", "--output-document", "-out-file"):
                made.add(_leaf(rest[k + 1]))
        made.update(_leaf(v.split("=", 1)[1]) for v in rest if v.startswith("of="))
        made.discard("")
    return False


# Places a written file runs from later on its own, as path segments (lower case): a wildcard or
# brace list in a write target is judged against these (`touch .git/hoo{ks}/pre-commit`).
_PERSIST_SEGMENTS = [
    (".local", "bin"), (".local", "share", "systemd"), (".config", "systemd"), (".config", "autostart"),
    (".gitconfig",), (".config", "git"), (".git", "hooks"), (".git", "config"), (".local", "share", "applications"),
    ("library", "launchagents"), ("start menu", "programs", "startup"), ("usr", "local", "bin"), ("usr", "bin"),
    ("usr", "sbin"), ("usr", "local", "sbin"), (".bashrc",), (".bashrc.d",), (".bash_profile",), (".bash_login",),
    (".bash_logout",), (".profile",), (".zshrc",), (".zprofile",), (".zshenv",), (".zlogin",), (".xprofile",),
    (".xinitrc",), (".xsession",), (".xsessionrc",), (".config", "fish"), (".config", "hypr"), (".config", "uwsm"),
    (".config", "environment.d"), (".ssh",), (".gnupg",), (".jarvis",), (".codex",), (".claude",)]
_BRACE_CAP = 256


def _brace_expand(text, cap=_BRACE_CAP):
    """Every spelling bash gives `a{b,c}d` (nested too); a {group} without a comma or .. is kept
    as a wildcard. None when there would be more than `cap` of them."""
    out, stack, start = [text], [], None
    for i, c in enumerate(text):
        if c == "{":
            stack.append(i)
        elif c == "}" and stack:
            start = stack.pop()
            if not stack:
                break
    else:
        return [text]
    body = text[start + 1:i]
    parts, depth, cur = [], 0, ""
    for c in body:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    parts.append(cur)
    if len(parts) == 1:  # {x} or {a..b}: not a list bash expands to known names
        parts = ["*"]
    result = []
    for part in parts:
        sub = _brace_expand(text[:start] + part + text[i + 1:], cap)
        if sub is None or len(result) + len(sub) > cap:
            return None
        result += sub
    return result


def _could_persist(path):
    """True when a write target holding {}, *, ? or [ could land in autostart, a PATH folder, a git
    hook/config, a shell startup file or the key folders. Segments are matched as globs; a window
    of segments needs at least one literal piece, so `~/Documents/*` or `/tmp/{a,b}` stay allowed."""
    import fnmatch
    variants = _brace_expand(path.replace("\\", "/"))
    if variants is None:
        return True
    for variant in variants:
        segs = [s.casefold() for s in variant.split("/") if s]
        for sample in _PERSIST_SEGMENTS:
            for i in range(len(segs) - len(sample) + 1):
                window = segs[i:i + len(sample)]
                if all(fnmatch.fnmatchcase(s, w) for s, w in zip(sample, window)) and \
                        any(re.search(r"[^*?\[\]]", w) for w in window):
                    return True
    return False


# Folder moves the cwd tracking below can't follow (audit 2026-11-04: `cd /tmp; cd -; rm -rf
# projects` deleted ~/projects): back-jumps, the directory stack, and $PWD/$OLDPWD, which _abs
# would fill in from JARVIS's own environment, not the shell's.
_CDWORD = r"(?:cd|chdir|pushd|set-location|sl)"
_CDOPTS = r"(?:\s+(?:--|-[LPe@]+))*"  # `cd -P -`, `cd -- -`: options before the dash
CWD_JUMP = re.compile(
    r"(?:^|[\s;&|(){])" + _CDWORD + _CDOPTS + r"\s+-(?:\s|$|[;&|)}])|"
    r"(?:^|[\s;&|(){])pushd(?:\s*(?:$|[;&|)}])|\s+[+-]\d)|"
    r"(?:^|[\s;&|(){])(?:popd|pop-location)(?=\s|$|[;&|)}])|"
    r"(?:^|[\s=:'\"(])~[-+](?:[/\\'\"\s;&|)}]|$)|\$\{?OLDPWD\b|\$env:OLDPWD\b", re.I)
# $PWD is JARVIS's own folder until a cd/pushd/popd runs; after one it is unknowable here.
_PWD_USE = re.compile(r"\$\{?PWD\b|\$env:PWD\b", re.I)
_ANY_CD = re.compile(r"(?:^|[\s;&|(){])(?:" + _CDWORD[3:-1] + r"|popd|pop-location)(?=\s|$|[;&|)}])", re.I)


def _pwd_after_cd(text):
    m = _PWD_USE.search(text)
    return bool(m and _ANY_CD.search(text[:m.start()]))


REPO_BUILD = frozenset({"node_modules", "dist", "build", "__pycache__", ".pytest_cache", ".ruff_cache",
                        ".mypy_cache", "coverage"})  # rebuilt by the tools: fine to delete


def _repo_keeps(path, repo_s):
    """True when deleting `path` (inside the repo) would hit .git, .venv or a top-level folder
    of the repo (or something in one); build and cache folders may go, and so may names
    that don't exist yet."""
    parts = [p for p in path[len(repo_s):].split("/") if p]
    if not parts:
        return True
    if any(p.casefold() in REPO_BUILD for p in parts):
        return False
    return parts[0].casefold() in (".git", ".venv") or os.path.isdir(os.path.join(repo_s, parts[0]))


def _under(path, root):
    path, root = path.casefold(), root.casefold()
    return path == root or path.startswith(root.rstrip("/") + "/")


# Login and key files no command may touch, read-only or not: their contents would go to
# the model, and from there anywhere (audit 2026-11-01: `cat ~/.codex/auth.json`,
# `curl -d @$HOME/.codex/auth.json ...`). Matched on the raw text, quotes and all.
SECRET_TEXT = re.compile(
    r"\.codex[/\\]+auth|\.claude[/\\]+\.?credentials|\.ssh(?:[/\\]|\b)|[/\\]id_(?:rsa|ed25519|ecdsa|dsa)\b|"
    r"\.gnupg\b|\.password-store\b|\.aws[/\\]+credentials|\.config[/\\]+gh[/\\]+hosts|"
    r"\.netrc\b|\.git-credentials\b|\bcode-signing\.key\b|cluster-owner\.token|"
    # Other assistants' and tools' logins (audit 2026-11-04).
    r"\.grok(?:[/\\'\"\s]|$)|\.hermes(?:[/\\'\"\s]|$)|\.config[/\\]+(?:openclaw|grok\\? ?bot|codex|cursor|gogcli)\b",
    re.I)
# JARVIS's own config, state and gate code, named anywhere in a command that changes things
# ("python -c \"open('.../jarvis.config.json','w')\"" slipped past the path check).
OWN_TEXT = re.compile(r"jarvis\.config\.json|(?:^|[\s'\"=/\\(])\.jarvis(?:[/\\'\"\s)]|$)|JARVIS\.md", re.I)
# Running whatever a pipe or a decode produces: never from the brain.
PIPE_TO_SHELL = re.compile(r"(?:^|[\s;&|])(?:source|\.)\s+(?:<\(|/dev/stdin|/dev/fd|/proc/self/fd)|"
                           r"(?:\|&?|>\()\s*(?:(?:sudo|env|doas|command|exec|nice|nohup|stdbuf|time|busybox)\b(?:\s+-\S+(?:\s+\w+)?)*\s+)*"
                           r"[\"']?(?:\S*/)?(?:sh|bash|zsh|dash|fish|ksh|pwsh|powershell|cmd|python3?|perl|ruby|node)(?:\.exe)?[\"']?(?:\s|$|\b)|"
                           r"\b(?:iex|invoke-expression)\b|\beval\s|base64\s+(?:-d|--decode)\b[^|]*\|", re.I)
PROTECTED_NAMES = (".ssh", ".codex", ".claude", ".jarvis", ".gnupg", "jarvis.config.json",
                   "JARVIS.md", "auth.json", "id_rsa", "id_ed25519", ".credentials.json")


def _globbed_secret(command):
    """A glob that could expand to a protected name (`~/.s?h/id_ed25519`, `jarvis.conf*.json`)."""
    import fnmatch
    for word in re.split(r"[\s'\"=;|&()<>]+", command):
        if not any(c in word for c in "*?["):
            continue
        for part in re.split(r"[/\\]+", word):
            if any(c in part for c in "*?[") and any(fnmatch.fnmatchcase(name.lower(), part.lower())
                                                       for name in PROTECTED_NAMES):
                return True
    return False


_VAR_USE = re.compile(r"\$\{?([A-Za-z_]\w*)\}?(?!\w)")
NESTING = 8  # bash -c / variable-filled re-checks deep enough to stop judging: refused


def _home_variants(command, config):
    r"""The command with home variables and same-command assignments filled in, once per home
    it could mean (this PC's and each Windows home the vault paths name): `$env:USERPROFILE\
    Documents\Vault` and `V=~/vault; echo x >> $V/n` are judged as the paths they become."""
    homes = {os.path.expanduser("~")}
    for path in _vault_paths(config):
        found = re.match(r"([A-Za-z]:[\\/]Users[\\/][^\\/]+)", str(path))
        if found:
            homes.add(found.group(1))
    names = _HOME_NAMES
    # Left to right, each use filled in from the assignments before it, never into an
    # assignment's own value (audit 2026-11-04: `export PATH=$PATH:/x` doubled on every pass
    # until MemoryError). The filled text is capped: None means too big to judge, refuse.
    limit, known, parts, at, size = max(4000, 4 * len(command)), {}, [], 0, 0

    def fill(piece):
        def use(m):
            nonlocal size
            value = known.get(m.group(1), m.group(0))
            size += len(value)
            if size > limit:
                raise OverflowError
            return value
        return _VAR_USE.sub(use, piece)
    try:
        for m in re.finditer(r"(?:^|[\s;&])([A-Za-z_]\w*)=([^\s;&|]+)", command):
            size += m.start(2) - at
            parts.append(fill(command[at:m.start(2)]))
            known[m.group(1)] = value = fill(m.group(2))
            parts.append(value)
            at = m.end(2)
        parts.append(fill(command[at:]))
    except OverflowError:
        return None
    text = "".join(parts)
    if len(text) > limit:
        return None
    out = []
    if text != command or re.search(names, text, re.I):
        for home in homes:
            out.append(re.sub(names, lambda _: home, text, flags=re.I))
    return out


def _terminal_denial(args, config, base, depth=0, expand=True):
    from . import terminal
    command = str((args or {}).get("command") or "")
    if depth > NESTING:
        return "that command nests too deeply for JARVIS to check"
    # A continued line is one command to the shell (bash \, PowerShell `, cmd ^ before the
    # newline), but every check here stops at a newline. Judge the joined text both ways:
    # bash drops the pair, PowerShell and cmd leave a word break.
    for joined in {_CONTINUATION.sub("", command), _CONTINUATION.sub(" ", command)} - {command}:
        found = _terminal_denial({**(args or {}), "command": joined}, config, base, depth + 1, expand)
        if found:
            return found
    variants = _home_variants(command, config) if expand else []
    if variants is None:
        return "JARVIS can't check that command: its variables grow too large"
    for variant in variants:
        if variant != command:  # already filled in: judged as is, never expanded again
            found = _terminal_denial({**(args or {}), "command": variant}, config, base, depth + 1, False)
            if found:
                return found
    cwd_arg = (args or {}).get("cwd")
    if _PWD_USE.search(command) and not _pwd_after_cd(command):
        # $PWD before any cd is the folder the command starts in: judged as that path
        # (_abs would read JARVIS's own environment instead).
        start = str(cwd_arg or terminal.cwd())
        filled = re.sub(r"\$\{PWD\}|\$PWD\b|\$env:PWD\b", lambda _: start, command, flags=re.I)
        found = _terminal_denial({**(args or {}), "command": filled}, config, base, depth + 1, False)
        if found:
            return found
    if SECRET_TEXT.search(command) or SECRET_TEXT.search(str(cwd_arg or "")):
        return "JARVIS never reads or sends login, key or token files"
    found = _phone_denial(command)  # before read_only: `cat /dev/video0` only "looks"
    if found:
        return found
    if terminal.read_only(command, cwd_arg):
        return None
    if CREDENTIAL_CMD.search(command):
        return "JARVIS never enters passwords, tokens or keys"
    if PIPE_TO_SHELL.search(command):
        return "JARVIS doesn't run code piped or decoded into a shell"
    if OWN_TEXT.search(command) or _globbed_secret(command):
        return "JARVIS's own config, state and keys are off limits"
    repo = Path(base).resolve() if base else _jarvis_home()
    repo_s = str(repo).replace("\\", "/")
    home = _abs("~", "/")
    vaults = [_abs(v, "/") for v in _vault_paths(config)]
    own = [repo_s + "/jarvis.config.json", repo_s + "/.jarvis", repo_s + "/JARVIS.md",
           repo_s + "/jarvis",  # its own gate code: `tee .../jarvis/gates.py`, `rm -rf .../jarvis`
           repo_s + "/.git",  # David's local history: he commits himself
           home + "/.ssh", home + "/.codex", home + "/.claude"] + [h + "/.jarvis" for h in (home,)]
    here = _abs(cwd_arg, terminal.cwd()) if cwd_arg else _abs(terminal.cwd(), "/")
    # Quotes and backslashes removed: p\asswd, pass''wd and "gh" are what the shell runs.
    plain = re.sub(r"[\\'\"]", "", command)
    if CREDENTIAL_CMD.search(plain):
        return "JARVIS never enters passwords, tokens or keys"
    if PIPE_TO_SHELL.search(plain):
        return "JARVIS doesn't run code piped or decoded into a shell"
    for pattern, why in STRUCTURAL:
        if pattern.search(command) or pattern.search(plain):
            return why
    if _console_port(command, config) or _console_port(plain, config) or \
            LOCAL_CONSOLE.search(_url_path_norm(plain)):
        return _CONSOLE_WHY
    if LINK_MADE.search(plain) and DESTROYS.search(plain):
        return "JARVIS doesn't make a link and delete, move or re-permission in one command"
    if CWD_JUMP.search(command) or CWD_JUMP.search(plain) or _pwd_after_cd(command) or _pwd_after_cd(plain):
        return "JARVIS can't follow cd -, popd, ~-, ~+, $OLDPWD or $PWD; name the folder plainly"
    if re.search(r"\b(?:python3?|perl|ruby|node|php|awk|gawk|deno|bun)\b", command, re.I) and \
            any(n.casefold() in command.casefold() for n in (*PROTECTED_NAMES, "jarvis/", "obsidian", *_vault_paths(config))):
        return "inline code can't touch JARVIS's own files or the vault"

    def judge(step, here, unknown):
        """Why this one command (cwd `here`) is refused, or None."""
        words = terminal._words(_winslash(step)) or [(w, w) for w in _winslash(step).split()]
        if not words:
            return None
        values = [v for v, _ in words]
        inner_env = _split_string(values)
        if inner_env:  # env -S 'rm -rf ~': the value is a command line, judged like bash -c
            found = _terminal_denial({**(args or {}), "command": inner_env}, config, base, depth + 1)
            if found:
                return found
        at, name = _command_name(values)
        if at < len(words) and _dynamic_word(words[at][0], words[at][1], len(words) - at == 1,
                                              words[at + 1][0] if at + 1 < len(words) else ""):
            return _DYNAMIC_WHY
        if not name and _base(values[0]) in WRAPPERS - SHELL_KEYWORDS:
            return "JARVIS can't tell which command that wrapper runs; write it plainly"
        values = values[at:] or values
        if name in SHELLS or name == "eval":  # `bash -c "rm -rf ~"`: judge the inner command
            # From the raw text: the parsed words have lost PowerShell/cmd backslashes.
            raw = step.split()
            inner = next((" ".join(raw[k + 1:]).strip().strip("'\"") for k in range(len(raw) - 1)
                          if raw[k].lower() in ("-c", "-command", "/c", "/k")), None) \
                if name != "eval" else " ".join(raw[raw.index(next(w for w in raw if w.lower() == "eval")) + 1:])
            if inner is None and name != "eval" and name not in ("pwsh", "powershell"):
                # bash -lc 'x', sh -ec 'x': the -c may share its dash with other flags
                inner = next((" ".join(raw[k + 1:]).strip().strip("'\"") for k in range(len(raw) - 1)
                              if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", raw[k])), None)
            if inner is None and name != "eval" and any(re.fullmatch(r"-e(?:c|n\w*)?", v, re.I)
                                                        for v in values[1:]):
                return "JARVIS doesn't run encoded shell commands"
            if inner:
                found = _terminal_denial({**(args or {}), "command": inner}, config, base, depth + 1)
                if found:
                    return found
        targets = []
        for value in values[1:]:
            # option=path forms (--out=~/x), and quoted/spaced pieces
            for piece in {value, value.split("=", 1)[-1]}:
                if piece and not (piece.startswith("-") and "=" not in piece):
                    targets.append(piece)
        protected = own + vaults
        windows = [m.replace("\\", "/") for m in re.findall(r"[A-Za-z]:\\[^\s'\"(),;|]*", step)]
        for t in targets + windows:
            fixed = t.replace("\\", "/")
            if EXPANDS.search(fixed) and not fixed.startswith("-") and ("/" in fixed or fixed.startswith("~")):
                head = EXPANDS.split(fixed, 1)[0]
                if not head:  # "$HOME/x", "$PATH": the base is unknown here; deletes check it below
                    continue
                start = _abs(head, here)
                if len(start) > 1 and any(g.casefold().startswith(start.casefold()) or _under(start, g)
                                          for g in protected):
                    return "a wildcard there could reach JARVIS's own files or the vault"
        if name in ("python", "python3", "perl", "ruby", "node", "php", "awk", "gawk", "deno", "bun") and \
                any(n.casefold() in step.casefold() for n in (*PROTECTED_NAMES, "jarvis/", "obsidian", *vaults)):
            return "inline code can't touch JARVIS's own files or the vault"
        paths = [_abs(t, here) for t in targets] + [here]
        # Windows paths as PowerShell sees them (the parsed words lost their backslashes), found
        # anywhere: -Path:C:\x, [IO.File]::WriteAllText("C:\x"), and > redirect targets.
        paths += [_abs(w, here) for w in windows]
        redirects = re.findall(r">>?\s*([^\s'\";&|>]+)", step)
        paths += [_abs(m, here) for m in redirects]
        # Written paths as typed: judged both resolved and by name below.
        wrote = [m for m in redirects if not re.search(r"[$%`]", m)]
        # $VAR here would be filled from JARVIS's own environment ($PWD): the filled-in variants judge those.
        plainargs = [v for v in values[1:] if not v.startswith("-") and not re.fullmatch(r"/[a-zA-Z]{1,8}", v)
                     and not re.search(r"[$%`]", v)]
        # A destination built from a $variable or (subexpression) can't be read here: refuse.
        dests = [v for v in values[1:] if not v.startswith("-") and not re.fullmatch(r"/[a-zA-Z]{1,8}", v)]
        if (name in WRITE_DEST and dests[-1:] or name in WRITE_ALL and dests) and \
                any(_unresolved(d, command) for d in (dests[-1:] if name in WRITE_DEST else dests)):
            return "JARVIS can't tell where that write lands (variable or subexpression); name the path plainly"
        if name in WRITE_DEST and plainargs:
            wrote.append(plainargs[-1])
        if name in WRITE_ALL or name == "sed" and any(re.match(r"-\w*i|--in-place", v) for v in values[1:]):
            wrote += plainargs
        for k, v in enumerate(values[1:-1], 1):  # -t dir, -Destination x, curl -o x, dd of=x
            if re.fullmatch(r"-t|--target-directory|-destination|-o|--output|--output-document|-d|-P", v, re.I):
                if _unresolved(values[k + 1], command) and name in WRITE_DEST | WRITE_ALL:
                    return "JARVIS can't tell where that write lands (variable or subexpression); name the path plainly"
                if not re.search(r"[$%`]", values[k + 1]):
                    wrote.append(values[k + 1])
        wrote += [v.split("=", 1)[1] for v in values[1:]
                  if re.match(r"(?:of|--target-directory|--output(?:-document)?)=.", v)]
        written = [_abs(w, here) for w in wrote]
        # A symlink in a persistence spot (~/.local/bin/claude -> ~/.local/share/claude/...) is
        # written through by name: resolving it first let `cp x ~/.local/bin/claude` pass (audit
        # 2026-11-07). Lexical paths only add refusals; deletes keep the resolved ones.
        by_name = [_abs(w, here, real=False) for w in wrote]
        looks = name in terminal.READERS | {"ls", "stat", "file", "diff", "less", "more", "wc", "dir", "gci",
                                            "get-childitem", "systemctl", "journalctl"}
        for path in written + by_name + ([] if looks else paths + [_abs(t, here, real=False) for t in targets]):
            if PERSIST.search(path):
                return "JARVIS doesn't write where programs start on their own (autostart, PATH, git hooks)"
        for path in written + by_name:
            if PERSIST_STARTUP.search(path):
                return "JARVIS doesn't write shell or desktop startup files"
        for w in wrote:  # a wildcard or brace list could spell .git/hooks, .local/bin, ~/.ssh...
            if re.search(r"[{}*?\[]", w) and _could_persist(_abs(w, here, real=False)):
                return ("JARVIS can't tell where that write lands (wildcard or brace list) and it could reach "
                        "autostart, PATH, git hooks or key folders; name the path plainly")
        for path in written + by_name:  # the repo's own root: sitecustomize.py, conftest.py, .claude/...
            if path.casefold() == repo_s.casefold() or \
                    posixpath.dirname(path).casefold() == repo_s.casefold() and not os.path.isdir(path) \
                    and path.rsplit("/", 1)[-1].casefold() not in REPO_BUILD or \
                    _under(path, repo_s + "/.claude"):
                return "JARVIS doesn't write files into its own repo's root"
        for path in paths:
            if any(_under(path, v) for v in vaults):
                return "the Obsidian vault is read-only"
            if any(_under(path, o) for o in own) or \
                    re.search(r"(?:^|/)(?:\.jarvis|jarvis\.config\.json|JARVIS\.md|\.ssh|\.codex|\.claude|"
                              r"\.gnupg)(?:/|$)|/JARVIS/jarvis(?:/|$)", path, re.I):
                return "JARVIS's own config, state and keys are off limits"
        if name == "git":
            found = _git_discards(values[1:])
            if found:
                return found
        if name == "git" and "clean" in values and any(re.fullmatch(r"-\w*f\w*|--force", v) for v in values):
            if _under(here, repo_s) or here == home or any(_under(here, v) for v in vaults):
                return "git clean would delete JARVIS's or the vault's untracked files"
        if name == "find" and any(v in ("-delete", "-exec", "-execdir", "-ok") for v in values[1:]):
            start = [v for v in values[1:] if not v.startswith("-")][:1] or ["."]
            name, values = "rm", ["rm", "-r", *start]
            targets = values[2:]
        if name == "rsync" and any(v.startswith("--del") for v in values[1:]):
            name, values = "rm", ["rm", "-r", *[t for t in targets if not t.startswith("-")][-1:]]
            targets = values[2:]
        if name in ("mv", "move", "move-item", "mi", "rename-item", "ren") and targets:
            sources = [t for t in targets if not t.startswith("-")][:-1] or targets[:1]
            for t in sources:  # moving home, the repo or the vault away destroys them as surely
                src = _abs(t, here)
                if any(_under(g, src) for g in [repo_s, home] + vaults) or TOP_FOLDERS.match(src) or \
                        HOME_DIRS.match(src):
                    return "that move would take away JARVIS, its vault or the home folder"
        if name in ("chmod", "chown", "chgrp", "setfacl"):
            recursive = any(re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*|--recursive", v) for v in values[1:])
            spec = name != "setfacl" and \
                not any(v.startswith("--reference") or re.fullmatch(r"-[rwxXst]+", v) for v in values[1:])
            for t in [v for v in values[1:] if not v.startswith("-")][1 if spec else 0:]:
                if EXPANDS.search(t):
                    if recursive:
                        return "recursive chmod/chown must name its target plainly"
                    continue
                path = _abs(t, here)
                # Home, the repo, the vault or a folder above them: even one level locks it out.
                if any(_under(g, path) for g in [repo_s, home, "/"] + vaults) or TOP_FOLDERS.match(path):
                    return "chmod/chown/setfacl can't cover JARVIS's repo, its vault or the home folder"
                if recursive and _under(path, repo_s):
                    return "recursive chmod/chown can't cover JARVIS's repo, its vault or the home folder"
        if name in ("icacls", "cacls", "takeown"):
            changes = name == "takeown" or any(
                re.fullmatch(r"/(?:grant|deny|remove|setintegritylevel|inheritance|reset|setowner|restore|"
                             r"substitute)(?::\w+)?", v, re.I) or v.lower() in ("/e", "/g", "/p", "/r", "/d")
                for v in values[1:])
            for t in values[1:]:
                if not changes or re.fullmatch(r"/\w+(?::\w+)?", t) or (":" in t and not re.match(r"[A-Za-z]:", t)):
                    continue
                path = _abs(t, here)
                if any(_under(g, path) for g in [repo_s, home, "/"] + vaults) or TOP_FOLDERS.match(path) or \
                        HOME_DIRS.match(path):
                    return "icacls/takeown can't change the permissions of home, JARVIS or the vault"
        if name and name not in RM_NAMES and name not in RECURSIVE_OK and \
                any(_RECURSIVE_FLAG.fullmatch(v) or v == "--no-preserve-root" for v in values[1:]):
            # Any other program with rm's flags (a copied or renamed rm, a script): home, the repo,
            # the vault or a folder above them is never its target (audit 2026-11-13).
            for t in targets:
                if t.startswith("-") or EXPANDS.search(t.replace("\\", "/")) and not re.search(r"/\*+$", t):
                    continue  # wildcards that reach protected folders were refused above
                path = _abs(re.sub(r"/\*+$", "", t.replace("\\", "/")) or "/", here)
                if any(_under(g, path) for g in [repo_s, home, "/"] + vaults) or TOP_FOLDERS.match(path) or \
                        HOME_DIRS.match(path):
                    return _GENERIC_WIPE_WHY
        if name in RM_NAMES:
            # -Path:C:\x / -LiteralPath:x carry their target inside the option.
            switch = r"-(?:r\w*|fo\w*|wh\w*|conf\w*|inc\w*|exc\w*):"  # -Recurse:$true is not a path
            targets += [v.split(":", 1)[1] for v in values[1:]
                        if re.match(r"-\w+:.", v) and not re.match(switch, v, re.I)]
            targets += [w.strip("'\"").split(":", 1)[1].strip("'\"") for w in step.split()[1:]
                        if re.match(r"-\w+:.", w.strip("'\"")) and not re.match(switch, w.strip("'\""), re.I)]
            # -Recurse:$true / -r:$true; only an explicit false value is not recursive, `:$x` counts
            bare = [re.sub(r":(?!(?:\$false|false|0)$).*$", "", v) if re.match(r"-[a-zA-Z]+:", v) else v
                    for v in values[1:]]
            recursive = any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*|--r\w*|-recurse\w*|/s", v, re.I)
                            or v == "--no-preserve-root" for v in bare)
            if recursive or name in ("rmdir", "rd"):
                guarded = [repo_s, home, "/"] + vaults
                if unknown and any(not t.startswith(("/", "~")) and not re.match(r"[A-Za-z]:", t) for t in targets):
                    return "a recursive delete after cd into an unresolved place isn't allowed"
                # Unquoted Windows paths too: _words drops their backslashes as bash escapes.
                raw = [w.strip("'\"") for w in step.split()[1:] if "\\" in w and not w.startswith("-")]
                for t in targets[:] + raw:
                    fixed = t.replace("\\", "/")
                    if EXPANDS.search(fixed):
                        # A variable, substitution, ~user or glob: judge the fixed part before it.
                        head = EXPANDS.split(fixed, 1)[0]
                        if "$" in fixed or "`" in fixed or fixed.startswith("~") and not fixed.startswith("~/"):
                            return "recursive deletes must name their target plainly (no variables or ~user)"
                        start = _abs(head, here) if head else here
                        if any(g.casefold().startswith(start.casefold()) for g in guarded):
                            return "that delete's wildcard could cover JARVIS, its vault or the home folder"
                        fixed = head.rsplit("/", 1)[0] if "/" in head else ""
                        if not fixed:
                            return "that recursive delete's target is a wildcard JARVIS can't check"
                    path = _abs(re.sub(r"/\*+$", "", fixed) or "/", here)
                    if any(_under(g, path) for g in guarded) or TOP_FOLDERS.match(path):
                        return "that delete would wipe JARVIS, its vault or the home folder"
                    if _under(path, repo_s) and _repo_keeps(path, repo_s):
                        return "JARVIS doesn't delete folders inside its own repo"
        return None

    unknown = False  # a cd/pushd into a place this check can't resolve (glob, variable)
    pieces, seps, at = [], [], 0
    split_text = command.replace(">|", ">")
    for m in SEPARATORS.finditer(command.replace(">|", ">")):  # `>|` is a redirect, not a pipe
        pieces.append(split_text[at:m.start()])
        seps.append(m.group(0))
        at = m.end()
    pieces.append(split_text[at:])
    seps.append("")
    if DYNAMIC_COMMAND.search(command) or DYNAMIC_COMMAND.search(plain):
        return _DYNAMIC_WHY
    if _runs_what_it_wrote(pieces):
        return _WRITE_RUN_WHY
    # Folders the shell may be in. A cd that fails (`cd /nonexistent; rm -rf x`), runs in a
    # pipeline or subshell, or is backgrounded leaves it where it was, so every cd adds a
    # possibility; only `cd d && ...` (not after ||) is sure to be in d for what follows.
    # Each command is judged in every one of them (audit 2026-11-04).
    grouped = bool(re.search(r"[(){}`]", command))
    heres = [here]
    chain = list(heres)  # where the current &&-chain began
    prev = ";"
    for step, sep in zip(pieces, seps):
        for h in heres:
            found = judge(step, h, unknown)
            if found:
                return found
        words = terminal._words(_winslash(step)) or [(w, w) for w in _winslash(step).split()]
        values = [v for v, _ in words]
        if values:
            at, name = _command_name(values)
            values = values[at:] or values
            if name in terminal.CD | {"pushd"}:
                args_ = [v for v in values[1:] if not v.startswith("-")] if values[1:] else []
                if any(EXPANDS.search(v) for v in values[1:]):
                    unknown = True
                news = [home if not args_ else _abs(args_[-1], h) for h in heres]
                if prev in (";", "\n", "&&") and sep == "&&" and not grouped:
                    heres = news
                else:
                    heres = heres + [n for n in news if n not in heres]
                if len(heres) > 64:
                    return "JARVIS can't follow that many folder changes in one command"
        if sep != "&&":  # the chain ended: it may have stopped before its cd
            heres = chain + [h for h in heres if h not in chain]
            chain = list(heres)
        prev = sep
    return None


def hard_deny(name, args, config, base=None):
    """Why a brain tool call is refused whatever rules.confirm says, or None. These are
    not questions to ask David: credentials are never typed, the vault is read-only, JARVIS
    doesn't edit its own gates or state. Checks: terminal commands that aren't
    terminal.read_only (credential-entry commands; any path naming the vault, repo
    jarvis.config.json, .jarvis/, JARVIS.md or ~/.ssh after ~, ./, //, .. and cd are
    resolved; recursive deletes covering /, home, the repo or the vault); grok_agent
    tasks that agent_risk() denies; browser type/fill/click calls (browser_gate.blocked)."""
    args = args if isinstance(args, dict) else {}
    if name == "terminal":
        return _terminal_denial(args, config, base)
    if name == "grok_agent":
        if agent_risk(str(args.get("task") or "")) == "deny":
            return "JARVIS never handles passwords, 2FA codes or card numbers"
        return None
    if re.match(r"(?:agent_)?browser_", str(name or "")):
        from . import browser_gate
        return browser_gate.blocked(name, args)
    return None


def trusted_rules(config, state=None):
    """The trusted_local rule, defaults filled in from TRUSTED_LOCAL. With `state`, never_ask
    follows rules.confirm as jarvis.config.json says now (asks_now): a switch back to "ask"
    takes effect without a restart, as it does for the brain and browser gates."""
    rule = {**TRUSTED_LOCAL, **((config or {}).get("rules") or {}).get("trusted_local", {})}
    rule["skip"] = [s for s in rule.get("skip") or () if s in SKIPPABLE]
    rule["never_ask"] = not asks_now(config, state)
    return rule


def skips(command, origin, origin_class, rule):
    """The checks this command may skip: none unless it's the owner's own request, runs
    on the PC it came from, is a trusted intent and needs no confirmation."""
    if (not rule or not rule.get("enabled") or origin_class != "owner"
            or command.get("device") != origin
            or command.get("intent") not in (rule.get("intents") or ())):
        return frozenset()
    try:
        if registry.risk(command) != "allow":
            return frozenset()
    except KeyError:
        return frozenset()
    return frozenset(rule["skip"])


def _result(name, ok, why=""):
    return {"name": name, "pass": bool(ok), "why": why}


def check(command, devices, origin_class="owner", reachable=None, ready=None,
          app_exists=None, confirmed=False):
    """Run U1-U10 for one command. Returns {"pass", "risk", "checks", "why"}.

    reachable(device) -> bool, ready(executor, device) -> bool and
    app_exists(device, name) -> bool | None (None = can't tell here: the node decides)
    default to permissive stand-ins so callers opt in to each live check."""
    checks = []

    def fail(name, why):
        checks.append(_result(name, False, why))
        return {"pass": False, "risk": None, "checks": checks, "why": why}

    errors = registry.validate([{**command, "depends_on": []}], devices)  # deps: graph level
    if errors:
        return fail("U1 schema_valid", errors[0])
    checks.append(_result("U1 schema_valid", True))
    intent = registry.get(command["intent"])
    checks.append(_result("U2 device_resolved", True, command["device"]))

    if reachable is not None and not reachable(command["device"]):
        return fail("U3 device_reachable", f"{command['device']} isn't reachable right now.")
    checks.append(_result("U3 device_reachable", True))

    if ready is not None and not ready(intent.executor, command["device"]):
        return fail("U4 executor_ready",
                    f"I can't run {intent.name} on {command['device']} yet.")
    checks.append(_result("U4 executor_ready", True, intent.executor))

    checks.append(_result("U5 args_safe", True))  # enforced by the registry validators

    if intent.name in ("open_app", "close_app", "focus_window") and app_exists is not None:
        found = app_exists(command["device"], command["args"]["name"])
        if found is False:
            return fail("U6 target_exists",
                        f"{command['args']['name']} isn't installed on {command['device']}.")
        checks.append(_result("U6 target_exists", True,
                              "checked" if found else "left to the node"))

    if origin_class in AGENT_ORIGINS and (intent.effect or intent.name == "agent_task"):
        return fail("U9 loop_free", "Actions requested by an agent need David's own request.")
    checks.append(_result("U9 loop_free", True))

    if intent.effect and intent.observer is None:
        return fail("U10 success_defined", f"{intent.name} has no way to confirm it worked.")
    checks.append(_result("U10 success_defined", True))

    risk = registry.risk(command)
    if confirmed and risk != "deny":
        checks.append(_result("policy", True, "owner confirmed"))
        return {"pass": True, "risk": risk, "checks": checks, "why": ""}
    checks.append(_result("policy", risk == "allow", risk))
    if risk != "allow":
        return {"pass": False, "risk": risk, "checks": checks,
                "why": "needs your OK first" if risk == "confirm" else "needs a review first"}
    return {"pass": True, "risk": risk, "checks": checks, "why": ""}


# Plurals and the other names for the same secrets (audit 2026-11-03: "my passwords", "my OTP",
# "the verification code" and "my CVV" all passed). "pin" only as a code, not "pin this tab".
CREDENTIAL_WORDS = re.compile(
    r"\b(?:pass(?:word|code|phrase)s?|2fa(?:\s+codes?)?|mfa\s+codes?|otps?|one[- ]time\s+(?:code|"
    r"password|passcode)s?|verification\s+codes?|security\s+codes?|auth(?:entication)?\s+codes?|"
    r"card\s+numbers?|credit\s+card\s+(?:number|details|info)|cvv2?|cvc2?|(?:my|the|a)\s+pin(?:\s+(?:code|number))?|"
    r"pin\s+(?:code|number)s?|social\s+security\s+number|ssn)\b", re.I)


def always_asks_text(text, config):
    """A task that spends money or messages people as David: asked even under confirm never."""
    from .browser_gate import ALWAYS_ASK
    kinds = ((config or {}).get("rules") or {}).get("always_ask", ["money", "messages"])
    return any(ALWAYS_ASK[k].search(text or "") for k in kinds if k in ALWAYS_ASK)


def agent_risk(text, preflight=None):
    """Risk can only rise; credentials are refused before any model is contacted."""
    if CREDENTIAL_WORDS.search(text):
        return "deny"
    risky = (r"\b(buy|pay|order|book|send|message|reply|text|email|post|tweet|comment|"
             r"delete|remove|unsubscribe|submit|apply|sign|accept|transfer|install|uninstall)\b|"
             r"\bchange\s+settings\b")
    risk = "confirm" if re.search(risky, text, re.I) else "allow"
    if preflight:
        candidate = preflight.get("risk", preflight.get("decision"))
        if candidate == "deny":
            return "deny"
        if candidate == "confirm":
            risk = "confirm"
        task = preflight.get("task")
        if task:
            task_risk = agent_risk(task)
            if task_risk == "deny" or task_risk == "confirm" and risk == "allow":
                risk = task_risk
    return risk
