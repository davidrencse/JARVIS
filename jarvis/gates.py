"""L3 gates: the checks every command passes before it is sent (docs/jarvis-os-architecture.md).

Cheapest first, fail fast. Each check returns {"name", "pass", "why"} for the trace.
A command is dispatched only when every applicable check passes and its risk is
"allow". "confirm" commands wait for David; until the confirmation queue exists they
are held back with a reply saying so, never sent.

The checks that need the outside world take it as arguments (reachable, ready,
app_exists), so the gates themselves stay pure and testable.
"""

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


def asks(config):
    """rules.confirm in jarvis.config.json: "ask" (default) parks risky actions for David's
    OK; "never" runs them at once (his choice, 2026-11-01). Refusals (passwords, 2FA codes,
    card numbers; agent-origin actions) stay either way: they are not questions (hard_deny).
    Only exactly "never" (whitespace aside) turns asking off; any other value asks."""
    return str(((config or {}).get("rules") or {}).get("confirm", "ask")).strip() != "never"


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
                _LIVE[str(path)] = cached = (mtime, rules.get("confirm", "ask"))
            return asks({"rules": {"confirm": cached[1]}})
        except (OSError, ValueError, AttributeError):
            pass  # no file there, or it's mid-edit: the config the gate was built with
    return asks(config)


_LOCAL = {}  # config path -> (mtime, config)


def local_config(base=None):
    """This computer's jarvis.config.json (next to the jarvis package), for checks that run
    where no config was handed down (a node's terminal, desktop typing). {} if unreadable:
    hard_deny still refuses everything it can judge without the vault paths."""
    path = Path(base or Path(__file__).resolve().parent.parent) / "jarvis.config.json"
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


def _command_name(values):
    """(index, name) of the word that really runs: wrappers, their options and VAR=x skipped,
    paths cut to the program's name (/usr/bin/rm -> rm, rm.exe -> rm)."""
    i = 0
    while i < len(values):
        word = values[i]
        base = re.sub(r"\.exe$", "", word.replace("\\", "/").rsplit("/", 1)[-1].lower())
        if base in WRAPPERS or word.startswith("-") or re.fullmatch(r"\w+=.*|\d+[smhd]?", word):
            i += 1
            continue
        return i, base
    return len(values), ""


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
LOCAL_CONSOLE = re.compile(r"(?<![\w.])" + _LOOPBACK + r"(?![\w.])|(?:(?<=//)|(?<=@))0+(?=[:/\s]|$)|"
                           r"/api/(?:confirm|ask|settings|send|remote)\b|\bX-JARVIS\b", re.I)

CONSOLE_PORTS = (7777, 7780)  # the console's and the hub's default ports; cluster.listen_port too
_CONSOLE_WHY = "JARVIS doesn't let commands talk to its own console or hub on this computer"


def _console_port(text, config):
    """A command naming the console's or hub's port with any host: a hostname that resolves to
    loopback (localtest.me, *.nip.io) reaches them as well as 127.0.0.1 does."""
    ports = set(CONSOLE_PORTS)
    try:
        ports.add(int(((config or {}).get("cluster") or {}).get("listen_port")))
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
                     r"\.config/git|\.local/share/applications|library/launchagents|"
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
    names = r"\$\{?env:(?:USERPROFILE|HOME)\}?|%(?:USERPROFILE|HOMEPATH|HOME)%|\$\{?HOME\}?"
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
    if terminal.read_only(command, cwd_arg):
        return None
    if CREDENTIAL_CMD.search(command):
        return "JARVIS never enters passwords, tokens or keys"
    if PIPE_TO_SHELL.search(command):
        return "JARVIS doesn't run code piped or decoded into a shell"
    if OWN_TEXT.search(command) or _globbed_secret(command):
        return "JARVIS's own config, state and keys are off limits"
    repo = Path(base).resolve() if base else Path(__file__).resolve().parent.parent
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
        at, name = _command_name(values)
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
        if name in WRITE_DEST and plainargs:
            wrote.append(plainargs[-1])
        if name in WRITE_ALL or name == "sed" and any(re.match(r"-\w*i|--in-place", v) for v in values[1:]):
            wrote += plainargs
        for k, v in enumerate(values[1:-1], 1):  # -t dir, -Destination x, curl -o x, dd of=x
            if re.fullmatch(r"-t|--target-directory|-destination|-o|--output|--output-document|-d|-P", v, re.I):
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
        if name in RM_NAMES:
            # -Path:C:\x / -LiteralPath:x carry their target inside the option.
            targets += [v.split(":", 1)[1] for v in values[1:] if re.match(r"-\w+:.", v)]
            targets += [w.strip("'\"").split(":", 1)[1].strip("'\"") for w in step.split()[1:]
                        if re.match(r"-\w+:.", w.strip("'\""))]  # -Path:C:\x keeps its backslashes raw
            recursive = any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*|--r\w*|-recurse\w*|/s", v, re.I)
                            or v == "--no-preserve-root" for v in values[1:])
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
    for m in SEPARATORS.finditer(command):
        pieces.append(command[at:m.start()])
        seps.append(m.group(0))
        at = m.end()
    pieces.append(command[at:])
    seps.append("")
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
