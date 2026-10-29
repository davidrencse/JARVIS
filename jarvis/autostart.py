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


def system():
    if os.name == "nt":
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def path(name, title):
    kind = system()
    if kind == "windows":
        return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{title}.vbs"
    if kind == "macos":
        return Path.home() / "Library/LaunchAgents" / f"com.jarvis.{name}.plist"
    return Path.home() / ".config/systemd/user" / f"{name}.service"


def install(name, title, argv, enable=True):
    """Turn login startup on or off for argv. Returns {"autostart", "path"}."""
    if not NAME.fullmatch(name):
        raise ValueError("Autostart names are lowercase words joined by dashes.")
    if any("\n" in arg or "\r" in arg for arg in argv):
        raise ValueError("Newlines are not allowed in startup paths.")
    kind, target = system(), path(name, title)
    if kind == "windows":
        if enable:
            argv = list(argv)
            windowless = Path(argv[0]).with_name("pythonw.exe")
            if Path(argv[0]).name.lower() == "python.exe" and windowless.is_file():
                argv[0] = str(windowless)
            line = subprocess.list2cmdline(argv).replace('"', '""')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(vbs(line), encoding="utf-8")
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
        if enable:
            target.parent.mkdir(parents=True, exist_ok=True)
            command = " ".join(json.dumps(arg).replace("%", "%%") for arg in argv)
            target.write_text(f"[Unit]\nDescription={title}\nAfter=graphical-session.target network-online.target\n"
                              f"[Service]\nWorkingDirectory={str(ROOT).replace('%', '%%')}\nExecStart={command}\nRestart=on-failure\nRestartSec=5\n"
                              "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15)
        subprocess.run(["systemctl", "--user", "enable" if enable else "disable", "--now", target.name],
                       check=True, timeout=15)
        if not enable:
            target.unlink(missing_ok=True)
    return {"autostart": enable, "path": str(target)}


def installed(name, title):
    return path(name, title).is_file()
