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

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .platforms import NO_WINDOW
from . import platforms

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
                    r"_history\b|\.histfile\b", re.I)
# /proc holds every process's environment (tokens) and memory; "environ" catches a cd-ed path.
# /dev/fd/.. and /dev/stdin lead back into /proc/self.
PROC = re.compile(r"(?:^|[/\\])proc(?:[/\\]|$)|environ\b|\benv\b|[/\\]dev[/\\](?:fd|std)", re.I)
# App and browser state where tokens sit under ordinary names (github-copilot/apps.json,
# VS Code's state.vscdb...): reading inside it, or cd-ing into it, needs David's OK.
APP_STATE = re.compile(r"\.config\b|\.local[/\\]share\b|AppData\b|Application Support\b|"
                       r"\.mozilla\b|Library[/\\](?:Keychains|Cookies)\b", re.I)
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
    for chain in command.split("&&"):
        for n, segment in enumerate(chain.split("|")):
            if not _looks(segment.strip()):
                return False
            # PowerShell pipes file objects: gci -r | sls key (or | cat, | type) reads every
            # file the listing found, so only plain filters may follow a pipe.
            if n and segment.split()[0].lower() not in PIPE_OK:
                return False
    return True


def _safe_folder(folder):
    """A cwd argument: a plain path, no variables, wildcards, secrets or /proc."""
    words = _words(folder)
    return (bool(folder.strip()) and len(folder) <= 300 and words is not None
            and folder.isascii() and not re.search(r"~\d", folder) and not APP_STATE.search(folder)
            and not SHELLISH.search(folder) and "%" not in folder and not GLOB.search(folder)
            and not SECRET.search(folder) and not _private(folder)
            and not any(SECRET.search(value) or _private(value) for value, _ in words))


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
    if any(SECRET.search(value) or _private(value) for value in args):
        return False
    if any(FILE_LIST.match(value) for value in args):
        return False
    if name in READERS and any(APP_STATE.search(value) for value in args):
        return False
    # PowerShell doesn't treat \ as an escape: \\host\x reaches it whole, so check raw words too.
    if any(ELSEWHERE.search(word.strip("'\"")) for word in segment.split()[1:]):
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
            if re.match(r"-[^-]*s|--s", a):
                return False
            if not value_next and not a.startswith(("-", "+")):
                return False
            value_next = a in ("-d", "--date", "-r", "--reference")
        return True
    if name == "hostname":  # a name, -b or -F sets the hostname
        return all(a in HOSTNAME_FLAGS for a in args)
    if name == "systeminfo":  # /s HOST /u /p logs in to another computer
        return not args
    if name == "file":  # -C compiles a magic file into the working folder
        return not any(re.match(r"-[^-]*C|--com", a) for a in args)
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
    return [shell, "-c", script]


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
        out = subprocess.run(_argv(command, folder), cwd=folder, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT,
                             stdin=subprocess.DEVNULL,
                             creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, f"Stopped after {TIMEOUT} s (interactive programs can't run here)."
    except OSError as exc:
        return False, f"Couldn't start a shell: {exc}"
    text, _, where = out.stdout.rpartition(MARK)
    if not _:  # the shell died before printing its folder
        text, where = out.stdout, folder
    where = where.strip() or folder
    if Path(where).is_dir() and not SECRET.search(where) and not _private(where) \
            and not APP_STATE.search(where):
        _CWD["path"] = where  # a cd persists, like a real terminal (never into a secret folder)
    body = (text.rstrip() + ("\n" + out.stderr.rstrip() if out.stderr.strip() else "")).strip()
    if len(body) > MAX_OUTPUT:
        body = body[:MAX_OUTPUT] + f"\n... ({len(body) - MAX_OUTPUT} more characters cut)"
    return out.returncode == 0, f"{body or '(no output)'}\n[exit {out.returncode} · cwd {where}]"
