"""A terminal for the brain: ls, cd, cat and friends run at once; anything else waits for David.

    run('{"command": "ls -la"}') -> (True, "...listing...\\n[exit 0 · cwd /home/jdoe]")

The working folder persists per process (per computer: a node runs its own), so "cd
Desktop" then "ls" behaves like a terminal. Commands run in bash (sh if missing) on Linux
and macOS and in PowerShell on Windows, with no stdin, a timeout and capped output.

read_only() decides what may run without asking: a short allowlist of commands that only
look (ls, pwd, cat, find without -exec/-delete...), chained only with && or |, with no
redirection, substitution, variables or wildcards (a quoted find -name / grep pattern
excepted), no argument or cwd naming a secret (SSH keys, .env files, JARVIS's and the
CLIs' own state) or /proc, checked on the words as the shell will see them once quotes
and backslashes are removed, and none of the flags that turn a looker into a writer
(sort -o, uniq's output file, tree -o, date -s...) or show environment variables (ps e).
A cd into a secret folder runs but isn't remembered. Everything else is a "terminal" command
parked in the confirmation queue (chatgpt.ConfirmGate, registry.risk). The allowlist is a
heuristic over the command text; it narrows what runs unasked, it isn't a sandbox.
"""

from .home import home as _jarvis_home
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
from pathlib import Path

from .fsio import read_json
from .platforms import NO_WINDOW, on_windows, on_macos
from . import platforms
from .cluster_store import OWNER_TOKEN

TIMEOUT = 30          # seconds a command may run
MAX_OUTPUT = 6000     # characters of output handed back to the brain
MARK = "__JARVIS_CWD__"
_CWD = {"path": None}  # this process's working folder; None = the home folder

# Commands that only read. Windows names are PowerShell cmdlets and their usual aliases.
LOOK = frozenset({
    "ls", "dir", "pwd", "cd", "cat", "head", "tail", "wc", "find", "du", "df", "stat",
    "file", "tree", "which", "whereis", "type", "echo", "whoami", "id", "uname", "hostname",
    "date", "uptime", "ps", "free", "lsblk", "grep", "sort", "uniq", "cut", "realpath",
    "basename", "dirname", "readlink", "nproc", "lscpu",
    "get-childitem", "gci", "get-location", "gl", "set-location", "sl", "chdir",
    "get-content", "gc", "get-item", "gi", "test-path", "get-process", "gps",
    "select-string", "sls", "measure-object", "measure", "select-object", "select",
    "sort-object", "format-table", "ft", "format-list", "fl", "get-date", "resolve-path",
    "get-psdrive", "get-computerinfo", "where.exe", "systeminfo"})
FIND_WRITES = re.compile(r"(?:^|\s)-(?:exec|execdir|ok|okdir|delete|fprint\w*|fls)\b", re.I)
# --rec: GNU tools take any unambiguous prefix of a long option (--rec = --recursive).
RECURSIVE = re.compile(r"(?:^|\s)(?:-[a-zA-Z]*[rR][a-zA-Z]*|--rec\w*|-recurse)\b", re.I)
SHELLISH = re.compile(r"[;&<>`$(){}\n\r]")
# Paths whose contents would leak a credential if read back to the model.
SECRET = re.compile(r"\.ssh\b|\.gnupg\b|\.jarvis\b|\.codex\b|\.claude\b|\.aws\b|\.kube\b|"
                    r"\.docker\b|\.netrc\b|\.git-credentials|\.password-store|keyring|"
                    r"\.env\b|id_(?:rsa|ed25519|ecdsa|dsa)|\.pem\b|\.key\b|\.p12\b|\.pfx\b|"
                    r"credential|secret|token|password|shadow\b|Login Data|Cookies\b|"
                    r"Local State\b|Local Storage|leveldb|logins\.json|key4\.db|\.kdbx\b|"
                    # token files with ordinary names (gh, npm, PyPI, rclone, git remotes) and
                    # shell histories (bash/zsh/fish, PowerShell's ConsoleHost_history.txt)
                    r"hosts\.yml\b|\.npmrc\b|\.pypirc\b|rclone\.conf\b|\.git[/\\]config\b|"
                    r"_history\b|\.histfile\b|\.viminfo\b|\.gitconfig\b", re.I)
# /proc holds every process's environment (tokens) and memory; "environ" catches a cd-ed path.
# /dev/fd/.. and /dev/stdin lead back into /proc/self.
# /dev/zero and friends are endless reads that cat would print until the timeout.
PROC = re.compile(r"(?:^|[/\\])proc(?:[/\\]|$)|environ\b|\benv\b|"
                  r"[/\\]dev[/\\](?:fd|std|zero|u?random|full)", re.I)
# App and browser state where tokens sit under ordinary names (github-copilot/apps.json,
# VS Code's state.vscdb...): reading inside it, or cd-ing into it, needs David's OK.
APP_STATE = re.compile(r"\.config\b|\.local[/\\]share\b|AppData\b|Application Support\b|"
                       r"Local Settings\b|Application Data\b|"
                       r"\.mozilla\b|\.thunderbird\b|\.var[/\\]app\b|Library[/\\](?:Keychains|Cookies)\b", re.I)
# Commands that print what they read, and the only ones a pipe may feed: in PowerShell any
# other name after | (cat, gc, type, sls...) may read each file object it is handed.
READERS = frozenset({"cat", "head", "tail", "grep", "sort", "uniq", "cut", "gc", "get-content",
                     "sls", "select-string", "type", "cd", "chdir", "set-location", "sl"})
PIPE_OK = frozenset({"grep", "wc", "sort", "uniq", "head", "tail", "cut", "measure-object",
                     "measure", "select-object", "select", "sort-object", "format-table", "ft",
                     "format-list", "fl"})
# Options that read file names from a file or stdin (find -print0 | sort --files0-from=-
# prints every found file), or reach another computer (Get-Process -ComputerName).
FILE_LIST = re.compile(r"--fil|--from|--ra|-(?:cn|comp\w*|cim\w*|session\w*)$", re.I)
# PowerShell takes any unambiguous parameter prefix, also as -Name:value: Get-Process -C dell
# is -ComputerName dell. On a PowerShell command every -c... is treated as remote.
PS_REMOTE = re.compile(r"-c\w*(?::|$)|-(?:comp\w*|cim\w*|ses\w*)(?::|$)", re.I)
PS_ALIASES = frozenset({"gci", "gc", "gi", "gps", "gl", "sl", "sls", "ft", "fl", "measure",
                        "select", "chdir"})
CD = frozenset({"cd", "chdir", "set-location", "sl"})
GLOB = re.compile(r"[*?\[]")
# PowerShell provider paths (HKCU:, cert:, variable:, Registry::...) reach the registry and
# session state; a UNC path (\\\\host\\x or //host/x) makes Windows send an NTLM hash to host.
ELSEWHERE = re.compile(r"^[A-Za-z][\w.]+:|::|^[\\/]{2}")
# grep's other ways to recurse: -d recurse, -drecurse, --directories=recurse (or --dir rec),
# --dereference-recursive. Any -d/--d... is refused; skip/read aren't worth the parsing.
GREP_DIRS = re.compile(r"-[^-]*d|--d", re.I)
# find tests whose quoted value is a pattern, not a path the shell or cmdlet expands.
FIND_PATTERNS = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename",
                           "-regex", "-iregex", "-lname", "-ilname"})


def read_only(command, cwd=None):
    """True when `command` (run in `cwd`, if given) only looks and may run without David's OK."""
    command = str(command or "").strip()
    # Non-ASCII: PowerShell reads curly quotes as quotes (.s‘’sh is .ssh to it, not to us).
    # ~1: a Windows 8.3 short name (SSH~1 is .ssh) that no name check can see through.
    if not command or len(command) > 500 or not command.isascii() or \
            re.search(r"~\d", command) or SECRET.search(command):
        return False
    if cwd is not None and not _safe_folder(str(cwd)):
        return False
    # Where each && step runs: `cd .local && cd share && cat x` reads ~/.local/share/x.
    here = _join(globals()["cwd"](), str(cwd)) if cwd else globals()["cwd"]()
    for chain in _outside_quotes(command, "&&"):
        for n, segment in enumerate(_outside_quotes(chain, "|")):
            if not _looks(segment.strip()):
                return False
            if n == 0:
                here = _walk(segment.strip(), here)
                if here is None:
                    return False
            # PowerShell pipes file objects: gci -r | sls key (or | cat, | type) reads every
            # file the listing found, so only plain filters may follow a pipe.
            if n and segment.split()[0].lower() not in PIPE_OK:
                return False
    return True


def _outside_quotes(text, sep):
    """`text` split on `sep` where it isn't inside quotes: `grep -E 'a|b' notes` is one step
    (audit 2026-11-01: it was cut in two and asked for an OK)."""
    parts, start, quote, i = [], 0, None, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and quote != "'":  # \' or \" is a literal quote, not one that opens
            i += 2  # (audit 2026-11-03: `echo \' && rm -rf x` hid the rm inside a "quote")
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return parts


# Folders one step above app state (.local/share, .var/app, Library/Keychains): a cd that
# stops there makes the next relative read land inside it. On another PC JARVIS can't see
# where an earlier command left the shell (audit 2026-11-01: `cd .local` on the Dell, then
# `cat share/...`), so stopping there is not "only looks".
STATE_PARENTS = frozenset({".local", ".var", "library", "appdata", "application support"})


def _norm(value):
    """The path as the OS will resolve it: ./ and // and .. folded away (~/.local/./share),
    so a name check can't be dodged by spelling. Not a filesystem lookup."""
    text = str(value).replace("\\", "/")
    return posixpath.normpath(text) if text.strip() else text


def _join(here, arg):
    """`arg` as an absolute, normalised path run from `here` (~ expanded)."""
    text = os.path.expanduser(str(arg).replace("\\", "/"))
    if not (text.startswith("/") or re.match(r"[A-Za-z]:", text)):
        text = posixpath.join(str(here).replace("\\", "/"), text)
    return posixpath.normpath(text)


def _walk(segment, here):
    """The folder after this step, or None when it names (or cds into) a secret or app-state
    folder once joined to where earlier cd steps left us."""
    words = _words(segment)
    if not words:
        return None
    name = words[0][0].lower()
    paths = [_join(here, value) for value, _ in words[1:] if not value.startswith("-")]
    for path in paths:
        if SECRET.search(path) or _private(path) or (name in READERS and APP_STATE.search(path)):
            return None
    if name in CD:
        if any(value == "-" for value, _ in words[1:]):
            return None
        dest = paths[-1] if paths else str(Path.home())
        if posixpath.basename(dest.rstrip("/")).lower() in STATE_PARENTS:
            return None
        return dest
    return here


def _safe_folder(folder):
    """A cwd argument: a plain path, no variables, wildcards, secrets or /proc."""
    words = _words(folder)
    return (bool(folder.strip()) and len(folder) <= 300 and words is not None
            and folder.isascii() and not re.search(r"~\d", folder) and not APP_STATE.search(folder)
            and not SHELLISH.search(folder) and "%" not in folder and not GLOB.search(folder)
            and not SECRET.search(folder) and not _private(folder)
            and not any(SECRET.search(value) or _private(value) or APP_STATE.search(value)
                        for value, _ in words)
            and not any(SECRET.search(v) or _private(v) or APP_STATE.search(v)
                        for v in (_norm(folder), *(_norm(w) for w, _ in words))))


def _private(path):
    return bool(PROC.search(str(path)) or ELSEWHERE.search(str(path).strip()))


def _words(segment):
    """[(value, bare)] per word as bash sees it: value with quotes and backslashes removed,
    bare = only the unquoted characters (where a wildcard would expand). None if unbalanced."""
    words, value, bare, quote, started, i = [], "", "", None, False, 0
    while i < len(segment):
        c = segment[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < len(segment):
                i += 1
                value += segment[i]
            else:
                value += c
        elif c in "'\"":
            quote, started = c, True
        elif c == "\\" and i + 1 < len(segment):
            i += 1
            value += segment[i]
            started = True
        elif c.isspace():
            if started:
                words.append((value, bare))
            value, bare, started = "", "", False
        else:
            value, bare, started = value + c, bare + c, True
        i += 1
    if quote:
        return None
    if started:
        words.append((value, bare))
    return words


def _looks(segment):
    if not segment or SHELLISH.search(segment.replace("&&", "")):
        return False
    words = _words(segment)
    if not words:
        return False
    name = words[0][0].lower()
    if name not in LOOK:
        return False
    args = [value for value, _ in words[1:]]
    # Quote splitting ('.s''sh', id_\rsa) hides a name from the raw-text check; recheck each word.
    spelled = [v for value in args for v in (value, _norm(value))]
    if any(SECRET.search(value) or _private(value) for value in spelled):
        return False
    if any(FILE_LIST.match(value) for value in args):
        return False
    if (name in PS_ALIASES or "-" in name) and any(PS_REMOTE.match(value) for value in args):
        return False
    # PowerShell doesn't treat \ as an escape: AppData\Local and \\host\x reach it whole, so
    # check the raw words too, split on its array commas and with a -Path: style prefix off.
    raw = [piece for word in segment.split()[1:]
           for piece in re.sub(r"^-\w+:", "", word.strip("'\"")).split(",") if piece]
    if name in READERS and any(APP_STATE.search(value)
                               for value in spelled + [r.replace("\\", "/") for r in raw]):
        return False
    if any(ELSEWHERE.search(piece) for piece in raw):
        return False
    if not _no_wildcards(name, words[1:]):
        return False
    rest = " ".join(args)
    if name == "find" and FIND_WRITES.search(" " + rest):
        return False
    # A recursive grep or content read over a whole tree can sweep up secrets unseen.
    if name in ("grep", "select-string", "sls", "get-content", "gc", "cat") and \
            RECURSIVE.search(" " + rest):
        return False
    if name == "grep" and any(GREP_DIRS.match(a) for a in args):
        return False
    if name in ("get-childitem", "gci", "dir", "ls") and re.search(r"-(?:exclude|include)\b.*\|", rest):
        return False
    return _no_writes(name, args)


def _no_wildcards(name, words):
    """No wildcard in any argument: ~/.s?h/id_* would slip past SECRET. The one exception is
    a fully quoted pattern for find -name/-path... or grep, which the tool matches itself."""
    pattern_next, grep_pattern = False, False
    for value, bare in words:
        is_pattern = pattern_next or (name == "grep" and not grep_pattern and not value.startswith("-"))
        grep_pattern = grep_pattern or (name == "grep" and is_pattern)
        pattern_next = (name == "find" and value.lower() in FIND_PATTERNS) or \
                       (name == "grep" and value in ("-e", "--regexp"))
        if not GLOB.search(value):
            continue
        if not is_pattern or GLOB.search(bare):
            return False
    return True


HOSTNAME_FLAGS = frozenset({"-s", "-f", "-i", "-I", "-d", "-a", "-A", "-y", "--short", "--fqdn",
                            "--long", "--ip-address", "--all-ip-addresses", "--domain",
                            "--alias", "--all-fqdns"})


def _no_writes(name, args):
    """Flags that make a looking command write a file, change the system or show env vars."""
    if name == "sort":  # -o FILE writes; --compress-program runs a program
        return not any(re.match(r"-[^-]*o|--o|--co", a) for a in args)
    if name == "uniq":  # uniq [OPTION]... [INPUT [OUTPUT]]: a second operand is overwritten
        operands, skip, options = 0, False, True
        for a in args:
            if skip:
                skip = False
            elif options and a == "--":
                options = False
            elif options and a.startswith("-") and a != "-":
                skip = a in ("-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars")
            else:
                operands += 1
        return operands <= 1
    if name == "tree":  # -o FILE writes; -R re-runs tree with -o 00Tree.html in each folder
        return not any(re.match(r"-[^-]*[oR]|--o", a) for a in args)
    if name == "date":  # -s/--set, or an MMDDhhmm operand, sets the clock; +FORMAT only prints
        value_next = False
        for a in args:
            if re.match(r"-[^-]*[sf]|--s|--f", a):  # -f/--file prints each line of a file
                return False
            if not value_next and not a.startswith(("-", "+")):
                return False
            value_next = a in ("-d", "--date", "-r", "--reference")
        return True
    if name == "hostname":  # a name, -b or -F sets the hostname
        return all(a in HOSTNAME_FLAGS for a in args)
    if name == "systeminfo":  # /s HOST /u /p logs in to another computer
        return not args
    if name == "file":  # -C compiles a magic file into the working folder; -f reads names from a file
        return not any(re.match(r"-[^-]*[Cf]|--com|--fil", a) for a in args)
    if name == "ps":  # e/E (ps e, ps eww, ps -auxe, macOS ps -E) prints environments;
        # only UNIX-style -e (every process) and its usual companions are plain
        return not any("e" in a.lower() and not re.fullmatch(r"-e[fFlyLjH]*", a) for a in args)
    return True


def cwd():
    path = _CWD["path"]
    if path and Path(path).is_dir():
        return path
    return str(Path.home())


def _argv(command, folder):
    if platforms.is_windows():
        shell = shutil.which("pwsh") or shutil.which("powershell.exe") or "powershell.exe"
        script = (f"Set-Location -LiteralPath '{folder.replace(chr(39), chr(39) * 2)}'\n"
                  f"{command}\n$code = if ($?) {{ 0 }} else {{ 1 }}\n"
                  f"if ($LASTEXITCODE) {{ $code = $LASTEXITCODE }}\n"
                  f"Write-Output \"`n{MARK}$((Get-Location).Path)\"\nexit $code")
        return [shell, "-NoProfile", "-NonInteractive", "-Command", script]
    shell = shutil.which("bash") or "/bin/sh"
    script = f"{command}\n__code=$?\nprintf '\\n{MARK}%s' \"$PWD\"\nexit $__code"
    return [*sandbox(folder), shell, "-c", script]


# Enforced by the OS, not by reading the command (audit 2026-11-03: quoting, //paths, variables
# and globs got past the text checks). Audit 2026-11-04 (#4) inverted it to an allowlist: the whole
# filesystem is read-only and only WRITABLE below can change. A writable home let a command plant
# ~/.local/bin/claude, a systemd unit, ~/.gitconfig hooks or a json.py in the repo root, all run
# later outside the sandbox. Hidden: secrets, browser profiles, app sign-ins and JARVIS's state.
HIDDEN = ("~/.ssh", "~/.codex", "~/.claude", "~/.gnupg", "~/.password-store", "~/.local/share/keyrings",
          "~/.config/google-chrome", "~/.config/chromium", "~/.mozilla", "~/.zen", "~/.config/zen",
          "~/.config/BraveSoftware", "~/.config/gh", "~/.config/github-copilot", "~/.docker", "~/.aws",
          "~/.kube", "~/.azure", "~/.config/gcloud", "~/.android", "~/.vnc", "~/.pki", "~/.cache/huggingface",
          "~/.cache/google-chrome", "~/.cache/chromium", "~/.cache/mozilla", "~/.cargo/registry/credentials",
          "~/.local/share/fish", "~/.config/Code/User/globalStorage", "~/.config/discord", "~/snap",
          "~/.var/app", "~/.local/share/TelegramDesktop", "~/.config/Signal",
          "~/.config/jarvis",  # relay tokens (grokbot-<device>.json) reach other laptops' desktops
          # audit 2026-11-04 (#4): agent and app sign-ins, wallets, clipboard history, CA keys
          "~/.grok", "~/.grokbot", "~/.hermes", "~/.openclaw", "~/.gemini", "~/.copilot", "~/.cursor",
          "~/.ollama", "~/.camoufox", "~/.agent-desktop", "~/.zoom", "~/csaw-certs",
          "~/.config/openclaw", "~/.config/Grok Bot", "~/.config/Codex", "~/.config/Cursor",
          "~/.config/cursor", "~/.config/gogcli", "~/.config/google-chrome-for-testing",
          "~/.config/microsoft-edge", "~/.config/opera", "~/.config/vivaldi", "~/.config/Slack",
          "~/.config/Notion", "~/.config/Electron", "~/.config/Vencord", "~/.config/Mullvad VPN",
          "~/.config/Windscribe", "~/.config/TradingView", "~/.config/browser-harness", "~/.config/opencode",
          "~/.config/eve", "~/.config/lan-mouse", "~/.config/lanshare", "~/.config/instabox-listener",
          "~/.config/figma-linux-next", "~/.local/share/kwalletd", "~/.local/share/com.vercel.cli",
          "~/.local/share/gogcli", "~/.local/share/opencode", "~/.local/share/wifidoc-chrome",
          "~/.local/share/undetected_chromedriver", "~/.local/share/Windscribe", "~/.local/share/mkcert",
          "~/.local/share/klipper", "~/.local/share/instabox-listener", "~/.local/share/tgfs",
          "~/.cache/Codex", "~/.cache/codex-runtimes", "~/.cache/claude", "~/.cache/claude-cli-nodejs",
          "~/.cache/opencode", "~/.cache/com.vercel.cli", "~/.cache/google-chrome-headless",
          "~/.cache/cliphist", "~/.cache/gh",
          "/dev/input", "/dev/snd/by-id")  # input devices: no keylogging from a terminal command
HIDDEN_FILES = ("~/.git-credentials", "~/.netrc", "~/.pgpass", "~/.claude.json", "~/.my.cnf", "~/.s3cfg",
                "~/.authinfo", "~/.wakatime.cfg", "~/.cargo/credentials.toml", "~/.cargo/credentials",
                "~/.npmrc", "~/.pypirc", "~/.bash_history", "~/.zsh_history", "~/.python_history",
                "~/.mysql_history", "~/.psql_history", "~/.lesshst", "~/.viminfo", "~/.Xauthority")
# The only places a command may write (audit 2026-11-04 #4). Scratch space plus David's ordinary
# work folders: where a download, a convert or a build in one of his projects belongs. Not $HOME
# itself, ~/.config, ~/.local, ~/.cache or ~/src (the Lan Mouse build): all of those hold things
# that run later outside the sandbox. A command's folder outside these stays read-only.
WRITABLE = ("/tmp", "/var/tmp", "~/Downloads", "~/Desktop", "~/Documents", "~/Pictures", "~/Music",
            "~/Videos", "~/projects", "~/JARVIS-Drop")
# Read-only again inside WRITABLE: the JARVIS repo (whole: a json.py or .claude/settings.json in
# its root runs at the next start), the vault (from config), and executables JARVIS runs.
PROTECT = ("~/Desktop/grok-bot-local",)  # grokbot.py starts this binary outside the sandbox
# Read-only already through the read-only root; kept as names for the console and older checks.
PERSIST = ("~/.config/systemd", "~/.config/autostart", "~/.config/hypr", "~/.config/uwsm",
           "~/.config/fish", "~/.bashrc", "~/.bash_profile", "~/.profile", "~/.zshrc", "~/.zprofile",
           "~/.config/environment.d", "~/.pam_environment")
# $XDG_RUNTIME_DIR is replaced by an empty tmpfs: its sockets (D-Bus -> systemd-run, Hyprland IPC
# -> dispatch exec, gpg/ssh agents, keyrings, agent-browser, Lan Mouse, and since audit #4 Wayland,
# PipeWire and Pulse: wtype typed into David's shell, grim took screenshots, the mic recorded)
# run or see things outside bwrap. Nothing comes back.
RUNTIME_KEEP = ()
RUNTIME_ENV = ("DBUS_SESSION_BUS_ADDRESS", "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "HYPRLAND_INSTANCE_SIGNATURE",
               "WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY", "PULSE_SERVER", "PIPEWIRE_REMOTE")
# Host sockets outside $XDG_RUNTIME_DIR, found when the sandbox is built and covered with
# /dev/null: /run/docker.sock (docker group = root), tailscale, mullvad, the system bus (reboot,
# NetworkManager), X11 (/tmp/.X11-unix) and Chrome's SingletonSocket in /tmp.
SOCKET_DIRS = ("/run", "/tmp", "/var/tmp")


def _sockets(top, depth=4):
    found, stack = [], [(Path(top), 0)]
    while stack:
        here, level = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if level + 1 < depth:
                        stack.append((Path(entry.path), level + 1))
                elif stat.S_ISSOCK(entry.stat(follow_symlinks=False).st_mode):
                    found.append(entry.path)
            except OSError:
                pass
    return sorted(found)


# seccomp filter for bwrap (audit 2026-11-13). Classic BPF built by hand, so there is no libseccomp
# dependency. Denied with EPERM: mount, umount2, pivot_root, chroot, unshare, setns and the new
# mount API (open_tree, move_mount, fsopen, fsconfig, fsmount, fspick, mount_setattr), plus clone
# with any CLONE_NEW* flag (a nested user+mount namespace). clone3 reads its flags from memory a
# filter can't see, so it gets ENOSYS and libc falls back to clone. Other architectures,
# including 32-bit and x32 calls, are refused outright.
_SECCOMP_ARCHES = {  # machine: (AUDIT_ARCH, {name: syscall number}, x32-style bit or 0)
    "x86_64": (0xC000003E, {"mount": 165, "umount2": 166, "pivot_root": 155, "chroot": 161, "unshare": 272,
                            "setns": 308, "open_tree": 428, "move_mount": 429, "fsopen": 430,
                            "fsconfig": 431, "fsmount": 432, "fspick": 433, "mount_setattr": 442},
               {"clone": 56, "clone3": 435}, 0x40000000),
    "aarch64": (0xC00000B7, {"mount": 40, "umount2": 39, "pivot_root": 41, "chroot": 51, "unshare": 97,
                             "setns": 268, "open_tree": 428, "move_mount": 429, "fsopen": 430,
                             "fsconfig": 431, "fsmount": 432, "fspick": 433, "mount_setattr": 442},
                {"clone": 220, "clone3": 435}, 0),
}
_CLONE_NEW = 0x00020000 | 0x02000000 | 0x04000000 | 0x08000000 | 0x10000000 | 0x20000000 | 0x40000000 | 0x80
_RET_ALLOW, _RET_EPERM, _RET_ENOSYS = 0x7FFF0000, 0x00050000 | 1, 0x00050000 | 38


def seccomp_program(machine=None):
    """The BPF program (bytes, sock_filter structs) bwrap loads with --seccomp. Raises OSError
    on an architecture it has no syscall table for: never run without the filter."""
    import platform
    import struct
    machine = machine or platform.machine()
    if machine == "arm64":
        machine = "aarch64"
    if machine not in _SECCOMP_ARCHES:
        raise OSError(f"The terminal sandbox has no seccomp filter for {machine!r}; refusing to run unfiltered.")
    arch, denied, special, x32 = _SECCOMP_ARCHES[machine]
    LD, JEQ, JGE, JSET, RET = 0x20, 0x15, 0x35, 0x45, 0x06  # BPF_LD|W|ABS, JMP|K variants, RET|K
    # Layout: arch check, [x32 check], syscall numbers, clone flags check, then the verdicts.
    steps = [("ld", 4)]                                        # seccomp_data.arch
    steps += [("jeq", arch, None, "eperm"), ("ld", 0)]           # a foreign ABI fails; then .nr
    if x32:
        steps += [("jge", x32, "eperm", None)]
    steps += [("jeq", nr, "eperm", None) for nr in denied.values()]
    steps += [("jeq", special["clone3"], "enosys", None), ("jeq", special["clone"], "clonecheck", None),
              ("ret", _RET_ALLOW), ("label", "clonecheck"), ("ld", 16),  # args[0] low word = clone flags
              ("jset", _CLONE_NEW, "eperm", None), ("ret", _RET_ALLOW),
              ("label", "eperm"), ("ret", _RET_EPERM), ("label", "enosys"), ("ret", _RET_ENOSYS)]
    index, position = {}, 0
    for step in steps:
        if step[0] == "label":
            index[step[1]] = position
        else:
            position += 1
    out, position = [], 0
    for step in steps:
        if step[0] == "label":
            continue
        position += 1
        kind = step[0]
        if kind == "ld":
            out.append(struct.pack("HBBI", LD, 0, 0, step[1]))
        elif kind == "ret":
            out.append(struct.pack("HBBI", RET, 0, 0, step[1]))
        else:
            _, value, true_to, false_to = step
            jump = lambda label: 0 if label is None else index[label] - position
            code = {"jeq": JEQ, "jge": JGE, "jset": JSET}[kind]
            out.append(struct.pack("HBBI", code, jump(true_to), jump(false_to), value))
    return b"".join(out)


def _sandbox_stdin(argv):
    """(stdin, close) for a sandboxed command. bwrap's `--seccomp 0` reads the filter from stdin,
    so that stdin is a pipe holding the program (the command then sees end-of-file, like
    /dev/null). Anything else keeps /dev/null. Raises OSError when the filter can't be built."""
    for i, arg in enumerate(argv[:-1]):
        if arg == "--seccomp" and argv[i + 1] == "0":
            blob = seccomp_program()
            read, write = os.pipe()
            try:
                os.write(write, blob)  # a few hundred bytes: far below the pipe buffer
            except OSError:
                os.close(read)
                raise
            finally:
                os.close(write)
            return read, lambda: os.close(read)
    return subprocess.DEVNULL, lambda: None


# Hooks and config of every git repo inside a writable folder run at David's next git command
# outside the sandbox (core.hooksPath, filter and alias entries run code too), so they stay
# read-only. Found by a bounded walk that never follows symlinks and skips build/dependency
# trees; each folder is walked again for every command, so the budget keeps that quick.
_GIT_SKIP = {"node_modules", ".venv", "venv", "__pycache__", "target", ".cache", "site-packages",
             ".tox", "dist", "build", ".next", ".gradle", ".m2", ".npm", ".cargo", ".mypy_cache"}
GIT_DEPTH = 6        # folders below a writable root searched for .git
GIT_BUDGET = 20000   # folders scanned per writable root before giving up on the rest


def _gitdir_guards(gitdir, inside, guards, _seen=None):
    """Add the read-only targets of one git dir: hooks/, config, config.worktree, the shared
    dir of a linked worktree (commondir) and the same for each submodule under modules/."""
    gitdir = Path(gitdir)
    seen = _seen if _seen is not None else set()
    if gitdir in seen or not inside(gitdir):
        return
    seen.add(gitdir)
    for name in ("hooks", "config", "config.worktree"):
        target = gitdir / name
        if not target.is_symlink() and target.exists():
            guards.add(str(target))
    try:  # a linked worktree: its hooks and config live in the main repo's git dir
        common = (gitdir / "commondir").read_text().strip()
        if common and len(common) < 4096:
            _gitdir_guards(os.path.realpath(gitdir / common), inside, guards, seen)
    except OSError:
        pass
    modules = gitdir / "modules"
    if modules.is_dir() and not modules.is_symlink():
        stack = [(modules, 0)]
        while stack:
            here, level = stack.pop()
            try:
                entries = list(os.scandir(here))
            except OSError:
                continue
            if (here / "HEAD").is_file():  # a submodule's git dir (nested ones sit inside modules/)
                _gitdir_guards(here, inside, guards, seen)
            for entry in entries:
                try:
                    if level < 4 and entry.is_dir(follow_symlinks=False) and entry.name not in ("objects", "refs"):
                        stack.append((Path(entry.path), level + 1))
                except OSError:
                    pass


def _git_guards(roots, depth=GIT_DEPTH, budget=GIT_BUDGET):
    """Sorted hooks/config paths of every git repo found under `roots` (writable folders)."""
    roots = [Path(r) for r in roots]
    real = [str(Path(os.path.realpath(r))) for r in roots]
    def inside(path):
        path = os.path.realpath(path)
        return any(path == r or path.startswith(r.rstrip("/") + "/") for r in real)
    guards = set()
    for root in roots:
        stack, scanned = [(root, 0)], 0
        while stack and scanned < budget:
            here, level = stack.pop()
            scanned += 1
            try:
                entries = list(os.scandir(here))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.name == ".git":
                        if entry.is_dir(follow_symlinks=False):
                            _gitdir_guards(entry.path, inside, guards)
                        elif entry.is_file(follow_symlinks=False):  # worktree or submodule: "gitdir: <path>"
                            line = Path(entry.path).read_text(errors="replace")[:4096].splitlines()[0]
                            if line.startswith("gitdir:"):
                                target = Path(line[7:].strip())
                                target = target if target.is_absolute() else Path(here) / target
                                _gitdir_guards(os.path.realpath(target), inside, guards)
                    elif level + 1 < depth and entry.name not in _GIT_SKIP and entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), level + 1))
                except (OSError, IndexError):
                    pass
    return sorted(guards)


def sandbox(folder=None, repo=None, config=None):
    """The bwrap prefix for a terminal command on Linux, or [] where it can't run (Windows,
    macOS, no bwrap, or terminal.sandbox: false in jarvis.config.json)."""
    if on_windows() or on_macos() or not shutil.which("bwrap"):
        return []
    repo = Path(repo) if repo else _jarvis_home()
    if config is None:
        config = read_json(repo / 'jarvis.config.json', {})
    if (config.get("terminal") or {}).get("sandbox", True) is False:
        return []
    home = str(Path.home())
    expand = lambda p: Path(home + str(p)[1:]) if str(p).startswith("~") else Path(p)
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    # Order matters: a later mount covers an earlier one. Read-only root, then the writable
    # folders, then read-only again inside them, then the hidden things on top of everything.
    # --cap-drop ALL + --seccomp 0 (audit 2026-11-13): under pasta the command is root with every
    # capability inside a user namespace, so `umount ~/.ssh` lifted the tmpfs that hides it and
    # exposed the real folder. No capabilities, and mount/umount/pivot_root/unshare/setns/clone
    # with namespace flags/the new mount API fail with EPERM. The filter is read from stdin (pasta
    # closes every other fd), which _run_shell feeds; without a filter the sandbox won't start.
    args = ["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc",
            "--unshare-pid", "--new-session", "--die-with-parent", "--cap-drop", "ALL", "--seccomp", "0"]
    writable = []
    for path in WRITABLE:
        if expand(path).is_dir() and not expand(path).is_symlink():
            args += ["--bind", str(expand(path)), str(expand(path))]
            writable.append(expand(path))
    vault = (config.get("vault") or {}).get("path")
    vaults = list(vault.values()) if isinstance(vault, dict) else [vault]
    hooks = [p for p in _git_guards(writable) if p != str(repo) and not p.startswith(str(repo) + "/")]
    for path in (repo, repo / "jarvis", repo / ".git", repo / ".venv",  # repo root covers all; subpaths for older checks
                 *[expand(v) for v in vaults if v], *[expand(p) for p in PROTECT], *hooks):
        if Path(path).exists():
            args += ["--ro-bind", str(path), str(path)]
    strays, secrets = _stray_state(repo)
    for path in (*HIDDEN, str(repo / ".jarvis"), *strays):
        if expand(path).is_dir():
            args += ["--tmpfs", str(expand(path))]
    for path in (*HIDDEN_FILES, *secrets):
        if expand(path).is_file():
            args += ["--ro-bind", "/dev/null", str(expand(path))]
    devices = [*sorted(Path("/dev").glob("hidraw*")), Path("/dev/uinput"), *sorted(Path("/dev").glob("video*")),
               *sorted(Path("/dev/snd").glob("pcmC*D*c"))]  # ALSA capture nodes are 0660 audio: the mic
    for raw in devices:
        if raw.exists():  # raw keyboard/mouse devices, synthetic input, cameras, microphones
            args += ["--ro-bind", "/dev/null", str(raw)]
    for top in SOCKET_DIRS:
        for sock in _sockets(top):
            if not sock.startswith(str(runtime) + "/"):
                args += ["--ro-bind", "/dev/null", sock]
    if runtime.is_dir():
        args += ["--tmpfs", str(runtime)]
        for path in (p for pattern in RUNTIME_KEEP for p in sorted(runtime.glob(pattern))):
            args += ["--bind", str(path), str(path)]
    for name in RUNTIME_ENV:
        args += ["--unsetenv", name]
    cache = f"/tmp/jarvis-sandbox-cache-{os.getuid()}"  # ~/.cache and ~/.npm are read-only
    args += ["--setenv", "XDG_CACHE_HOME", cache, "--setenv", "npm_config_cache", cache + "/npm"]
    if folder:
        args += ["--chdir", str(folder)]
    # No host loopback (audit 2026-11-03): the console on 7777, the hub, the relay and browser
    # debug ports trust any local caller. pasta gives the command its own network namespace with
    # internet but no route to the host's 127.0.0.1 (-T/-U none: no namespace->host port
    # forwards; --no-map-gw: the gateway isn't the host). Without pasta: no network at all.
    # --unshare-pid: when the shell ends, everything it started (setsid -f, nohup, &) dies with it.
    pasta = shutil.which("pasta")
    if pasta:
        return [pasta, "--quiet", "--config-net", "-t", "none", "-u", "none", "-T", "none",
                "-U", "none", "--no-map-gw", "--", *args, "--"]
    return [*args, "--unshare-net", "--"]


# Files that are JARVIS secrets wherever a copy of them lands (audit 2026-11-07: a stray
# 2.4 GB copy of the repo inside it, "C:\\Program Files/JARVIS Lan Mouse", carried a live
# .jarvis/ with the code-signing key, tokens and browser cookies the sandbox didn't hide).
SECRET_NAMES = ("code-signing.key", OWNER_TOKEN, "chrome-extension-token",
                "secrets.json", "cluster-node.json")
_SKIP_WALK = {".git", ".venv", "venv", "node_modules", "__pycache__", "target"}


def _stray_state(repo, depth=6):
    """(.jarvis folders, secret files) anywhere under the repo besides repo/.jarvis, found by
    a bounded walk that skips environments and build output (they hold no state)."""
    repo = Path(repo)
    strays, secrets = [], []
    top = len(repo.parts)
    for folder, dirs, files in os.walk(repo):
        here = Path(folder)
        if len(here.parts) - top >= depth:
            dirs[:] = []
        for name in list(dirs):
            if name == ".jarvis":
                if here != repo:
                    strays.append(str(here / name))
                dirs.remove(name)  # hidden whole (repo/.jarvis already is)
            elif name in _SKIP_WALK:
                dirs.remove(name)
        secrets += [str(here / f) for f in files
                    if f.startswith("code-signing.key") or f in SECRET_NAMES]
    return strays, secrets


def _kill_tree(proc):
    """Kill the shell and everything it started (builds, servers, grandchildren)."""
    try:
        if on_windows():
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           timeout=10, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


OUTPUT_CAP = 1_000_000  # characters kept from a command; the rest is read and thrown away (audit 2026-11-04)
KEEP_TAIL = 4096        # characters kept from the end: the MARK line with the final folder


def _drain(pipe, keep, total, proc):
    """Read `pipe` to its end keeping only the head (2*MAX_OUTPUT) and a short tail, so
    `cat big.mkv` or `yes` can't fill memory. Past OUTPUT_CAP nothing more is kept but the tail;
    the pipe is still drained to its end so the command can finish (only the timeout kills)."""
    head, tail, dropped = "", "", 0
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            with total[2]:
                total[0] += len(chunk)
                if total[0] > OUTPUT_CAP:
                    total[1] = True
                over = total[1]
            room = 2 * MAX_OUTPUT - len(head)
            if room > 0 and not over:
                head, chunk = head + chunk[:room], chunk[room:]
            if chunk:
                tail += chunk
                if len(tail) > KEEP_TAIL:
                    dropped += len(tail) - KEEP_TAIL
                    tail = tail[-KEEP_TAIL:]
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass
    keep.append(head + (f"\n... ({dropped} characters dropped)\n" if dropped else "") + tail)


def _run_shell(argv, folder):
    """(returncode, stdout, stderr, timed_out). On timeout the whole process tree dies and
    the pipes are drained with a bounded wait, so a grandchild holding them can't hang us.
    Output is read in bounded pieces (_drain); past OUTPUT_CAP the rest is drained and discarded."""
    import threading
    extra = ({"creationflags": NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if on_windows()
             else {"start_new_session": True})
    stdin, release = _sandbox_stdin(argv)
    try:
        proc = subprocess.Popen(argv, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                stdin=stdin, **extra)
    finally:
        release()
    total, out, err = [0, False, threading.Lock()], [], []
    readers = [threading.Thread(target=_drain, args=(pipe, keep, total, proc), daemon=True)
               for pipe, keep in ((proc.stdout, out), (proc.stderr, err))]
    for reader in readers:
        reader.start()
    import time
    deadline, timed_out = time.monotonic() + TIMEOUT, False
    try:
        proc.wait(timeout=TIMEOUT)
        for reader in readers:  # like communicate(): wait for whatever still holds the pipes
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        timed_out = any(reader.is_alive() for reader in readers)
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        _kill_tree(proc)
        for reader in readers:
            reader.join(timeout=5)  # a grandchild still holding a pipe can't hang us
        try:
            proc.wait(timeout=5)  # reap the killed shell
        except subprocess.TimeoutExpired:
            pass
    stdout, stderr = "".join(out), "".join(err)
    if total[1]:
        stderr += f"\n[the command printed over {OUTPUT_CAP} characters; the rest was discarded]"
    if timed_out:
        return None, stdout, stderr, True
    return proc.returncode, stdout, stderr, False


def run(spec):
    """(ok, text) for one command. spec: JSON {"command", "cwd"?} or the command itself."""
    try:
        data = json.loads(spec) if str(spec).lstrip().startswith("{") else {"command": spec}
    except ValueError:
        data = {"command": spec}
    command = str(data.get("command") or "").strip()
    if not command:
        return False, "No command."
    folder = cwd()
    if data.get("cwd"):
        wanted = Path(os.path.expandvars(str(data["cwd"]))).expanduser()
        wanted = wanted if wanted.is_absolute() else Path(folder) / wanted
        if not wanted.is_dir():
            return False, f"No such folder: {wanted}"
        folder = str(wanted.resolve())
    try:
        code, stdout, stderr, timed_out = _run_shell(_argv(command, folder), folder)
    except OSError as exc:
        return False, f"Couldn't start a shell: {exc}"
    if timed_out:
        return False, (f"The command was stopped after {TIMEOUT} s and everything it started was "
                       "killed. Long-running commands (builds, servers, watchers) and interactive "
                       "programs can't run through this tool.")
    text, _, where = stdout.rpartition(MARK)
    if not _:  # the shell died before printing its folder
        text, where = stdout, folder
    where = where.strip() or folder
    if Path(where).is_dir() and not SECRET.search(where) and not _private(where) \
            and not APP_STATE.search(where):
        _CWD["path"] = where  # a cd persists, like a real terminal (never into a secret folder)
    body = (text.rstrip() + ("\n" + stderr.rstrip() if stderr.strip() else "")).strip()
    if len(body) > MAX_OUTPUT:
        body = body[:MAX_OUTPUT] + f"\n... ({len(body) - MAX_OUTPUT} more characters cut)"
    return code == 0, f"{body or '(no output)'}\n[exit {code} · cwd {where}]"
