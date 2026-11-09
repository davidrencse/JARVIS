"""Start a JARVIS process at login on any OS: one place instead of one per feature.

- Windows: a hidden .vbs in the user's Startup folder (runs under Explorer, so it outlives
  the shell that installed it).
- macOS: a LaunchAgent plist in ~/Library/LaunchAgents, loaded with launchctl.
- Linux (any distribution with systemd): a user unit in ~/.config/systemd/user, enabled
  and started with systemctl --user.

    install("jarvis-cluster", "JARVIS main or node agent", argv, enable=True)
"""

import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path

NAME = re.compile(r"[a-z][a-z0-9-]{1,40}\Z")
# Every startup runs `python -m jarvis`, which needs the checkout as its working folder
# (JARVIS isn't pip-installed). Login managers start elsewhere (systemd: $HOME).
ROOT = Path(__file__).resolve().parent.parent


def vbs(line):
    """A hidden Windows launcher for one command line (already "" -escaped), run from ROOT."""
    folder = str(ROOT).replace('"', '""')
    return (f'Set shell = CreateObject("WScript.Shell")\nshell.CurrentDirectory = "{folder}"\n'
            f'shell.Run "{line}", 0, False\n')


def interpreter(argv):
    """argv with the project's own .venv Python when there is one and argv runs whichever
    Python happened to call install(): the venv has the voice deps, a bare system Python
    may not (installing from such a shell once left the main without voice input)."""
    argv = list(argv)
    venv = [ROOT / ".venv/Scripts/python.exe", ROOT / ".venv/bin/python"]
    found = next((v for v in venv if v.is_file()), None)
    if found and argv and Path(argv[0]).resolve() == Path(sys.executable).resolve() \
            and not Path(sys.executable).resolve().is_relative_to((ROOT / ".venv").resolve()):
        argv[0] = str(found)
    return argv


def _word(arg):
    """One ExecStart word: quoted only when it has to be; % is systemd's specifier sign and
    $ starts a variable even inside quotes (so $$ for a literal one)."""
    arg = str(arg).replace("%", "%%").replace("$", "$$")
    return json.dumps(arg) if not arg or any(c in arg for c in ' \t"\'\\;$') else arg


def systemd_unit(title, argv, comment=None, after="graphical-session.target network-online.target",
                 wanted="default.target", service=()):
    """The text of a systemd user unit that runs argv from ROOT, restarting it on failure but
    giving up after 5 crashes in 5 minutes. service: extra [Service] lines (Environment=...)."""
    lines = [f"# {comment}"] if comment else []
    lines += ["[Unit]", f"Description={title}", f"After={after}",
              "StartLimitIntervalSec=300", "StartLimitBurst=5", "",
              "[Service]", "Type=simple", f"WorkingDirectory={str(ROOT).replace('%', '%%')}",
              f"ExecStart={' '.join(_word(arg) for arg in interpreter(argv))}",
              "Restart=on-failure", "RestartSec=5", *service, "",
              "[Install]", f"WantedBy={wanted}", ""]
    return "\n".join(lines)


def system():
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def startup_file(file_name):
    """An entry in the current user's Windows Startup folder."""
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / file_name)


def unit_file(file_name):
    """Where systemd looks for a user unit ($XDG_CONFIG_HOME/systemd/user)."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "systemd" / "user" / file_name


def write_launcher(target, argv, comment=None, windowless=False):
    """Write a hidden Windows Startup launcher for argv, run from ROOT. windowless: use
    pythonw.exe beside python.exe when there is one."""
    argv = interpreter(argv)
    windowless_exe = Path(argv[0]).with_name("pythonw.exe")
    if windowless and Path(argv[0]).name.lower() == "python.exe" and windowless_exe.is_file():
        argv[0] = str(windowless_exe)
    line = subprocess.list2cmdline(argv).replace('"', '""')
    head = "".join(f"' {text}\n" for text in (comment or "").splitlines())
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(head + vbs(line), encoding="utf-8")
    return target


def path(name, title):
    kind = system()
    if kind == "windows":
        return startup_file(f"{title}.vbs")
    if kind == "macos":
        return Path.home() / "Library/LaunchAgents" / f"com.jarvis.{name}.plist"
    return unit_file(f"{name}.service")


def install(name, title, argv, enable=True):
    """Turn login startup on or off for argv. Returns {"autostart", "path"}."""
    if not NAME.fullmatch(name):
        raise ValueError("Autostart names are lowercase words joined by dashes.")
    if any("\n" in arg or "\r" in arg for arg in argv):
        raise ValueError("Newlines are not allowed in startup paths.")
    kind, target = system(), path(name, title)
    argv = interpreter(argv)
    if kind == "windows":
        if enable:
            write_launcher(target, argv, windowless=True)
        else:
            target.unlink(missing_ok=True)
    elif kind == "macos":
        if enable:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as stream:
                plistlib.dump({"Label": f"com.jarvis.{name}", "ProgramArguments": list(argv),
                               "WorkingDirectory": str(ROOT),
                               "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}}, stream)
            subprocess.run(["launchctl", "unload", str(target)], capture_output=True, timeout=15)
            subprocess.run(["launchctl", "load", "-w", str(target)], check=True, timeout=15)
        else:
            subprocess.run(["launchctl", "unload", "-w", str(target)], capture_output=True, timeout=15)
            target.unlink(missing_ok=True)
    else:
        existed = target.is_file()
        if enable:
            target.parent.mkdir(parents=True, exist_ok=True)
            unit = systemd_unit(title, argv)
            changed = not existed or target.read_text(encoding="utf-8") != unit
            target.write_text(unit, encoding="utf-8")
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
            subprocess.run(["systemctl", "--user", "enable", "--now", target.name],
                           check=True, timeout=15)
            if existed and changed:  # already running on the old ExecStart
                subprocess.run(["systemctl", "--user", "restart", target.name],
                               check=True, timeout=15)
        elif existed:  # never turned on: nothing to disable
            subprocess.run(["systemctl", "--user", "disable", "--now", target.name],
                           check=True, timeout=15)
            target.unlink(missing_ok=True)
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
    return {"autostart": enable, "path": str(target)}


def installed(name, title):
    return path(name, title).is_file()
