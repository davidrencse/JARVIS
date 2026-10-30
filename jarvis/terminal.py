"""A terminal for the brain: ls, cd, cat and friends run at once; anything else waits for David.

    run('{"command": "ls -la"}') -> (True, "...listing...\\n[exit 0 · cwd /home/jdoe]")

The working folder persists per process (per computer: a node runs its own), so "cd
Desktop" then "ls" behaves like a terminal. Commands run in bash (sh if missing) on Linux
and macOS and in PowerShell on Windows, with no stdin, a timeout and capped output.

read_only() decides what may run without asking: a short allowlist of commands that only
look (ls, pwd, cat, find without -exec/-delete...), chained only with && or |, with no
redirection, substitution or variables, and no argument naming a secret (SSH keys,
.env files, JARVIS's and the CLIs' own state). Everything else is a "terminal" command
parked in the confirmation queue (chatgpt.ConfirmGate, registry.risk). The allowlist is a
heuristic over the command text; it narrows what runs unasked, it isn't a sandbox.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

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
RECURSIVE = re.compile(r"(?:^|\s)(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive|-recurse)\b", re.I)
SHELLISH = re.compile(r"[;&<>`$(){}\n\r]")
# Paths whose contents would leak a credential if read back to the model.
SECRET = re.compile(r"\.ssh\b|\.gnupg\b|\.jarvis\b|\.codex\b|\.claude\b|\.aws\b|\.kube\b|"
                    r"\.docker\b|\.netrc\b|\.git-credentials|\.password-store|keyring|"
                    r"\.env\b|id_(?:rsa|ed25519|ecdsa|dsa)|\.pem\b|\.key\b|\.p12\b|\.pfx\b|"
                    r"credential|secret|token|password|shadow\b|Login Data|Cookies\b|"
                    r"Local State\b", re.I)


def read_only(command):
    """True when `command` only looks around and may run without David's OK."""
    command = str(command or "").strip()
    if not command or len(command) > 500 or SECRET.search(command):
        return False
    for chain in command.split("&&"):
        for segment in chain.split("|"):
            if not _looks(segment.strip()):
                return False
    return True


def _looks(segment):
    if not segment or SHELLISH.search(segment.replace("&&", "")):
        return False
    words = segment.split()
    name = words[0].lower()
    if name not in LOOK:
        return False
    rest = " ".join(words[1:])
    if name == "find" and FIND_WRITES.search(" " + rest):
        return False
    # A recursive grep or content read over a whole tree can sweep up secrets unseen.
    if name in ("grep", "select-string", "sls", "get-content", "gc", "cat") and \
            RECURSIVE.search(" " + rest):
        return False
    if name in ("get-childitem", "gci", "dir", "ls") and re.search(r"-(?:exclude|include)\b.*\|", rest):
        return False
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
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, f"Stopped after {TIMEOUT} s (interactive programs can't run here)."
    except OSError as exc:
        return False, f"Couldn't start a shell: {exc}"
    text, _, where = out.stdout.rpartition(MARK)
    if not _:  # the shell died before printing its folder
        text, where = out.stdout, folder
    where = where.strip() or folder
    if Path(where).is_dir():
        _CWD["path"] = where  # a cd persists, like a real terminal
    body = (text.rstrip() + ("\n" + out.stderr.rstrip() if out.stderr.strip() else "")).strip()
    if len(body) > MAX_OUTPUT:
        body = body[:MAX_OUTPUT] + f"\n... ({len(body) - MAX_OUTPUT} more characters cut)"
    return out.returncode == 0, f"{body or '(no output)'}\n[exit {out.returncode} · cwd {where}]"
