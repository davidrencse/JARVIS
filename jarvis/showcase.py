"""Open JARVIS console pages as standalone windows."""

import os
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PAGES = {"wiki": "knowledge.html", "system": "design.html", "console": ""}
PORTS = (7777, *range(7790, 7800))
REPO = Path(__file__).resolve().parent.parent


def serves_console(port, timeout=1.5):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/jarvis.css", timeout=timeout) as res:
            return res.status == 200
    except OSError:
        return False


def console_port(ports=PORTS):
    """The port of a JARVIS console already running on this laptop, or None."""
    with ThreadPoolExecutor(max_workers=min(8, len(ports) or 1)) as pool:
        return next((port for port, ready in zip(ports, pool.map(
            lambda p: serves_console(p, timeout=0.35), ports)) if ready), None)


def start_console(port=7777, wait=25):
    """Start `jarvis ui` in the background (no window) and wait until it serves."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen([sys.executable, "-m", "jarvis", "ui", "--port", str(port)], cwd=REPO,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
                     start_new_session=os.name != "nt")
    end = time.time() + wait
    while time.time() < end:
        if serves_console(port, timeout=0.5):
            return port
        time.sleep(0.4)
    raise RuntimeError("the JARVIS console didn't start")


def open_page(name):
    """(ok, text): open a supported console page as an app window."""
    key = (name or "console").strip().lower()
    if key not in PAGES:
        return False, f"No console page called {name!r}; pages: {', '.join(PAGES)}."
    from .ui import app_window
    port = console_port() or start_console()
    url = f"http://127.0.0.1:{port}/{PAGES[key]}"
    profile = REPO / ".jarvis" / "page-window"
    app_window(url, size=(1600, 980), profile=profile)
    label = {"console": "the console", "wiki": "Knowledge", "system": "System"}
    return True, f"Opened {label.get(key, key.title())}."
