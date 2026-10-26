"""Console pages as their own windows, and the voice shortcut that opens one.

"Jarvis, check the market" opens the Pro terminal as a borderless app window and says
how the market is doing in one line. "... on the Dell" opens it on that laptop instead,
through the same desktop route as open_app (remote.py), so it works for a showcase from
either machine.

The window shows a console that is already running on that laptop (`jarvis ui`, `jarvis
app` or the overlay); if none is, a quiet `jarvis ui` is started first.
"""

import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PAGES = {"terminal": "terminal.html", "markets": "trade.html", "wiki": "knowledge.html",
         "system": "design.html", "console": ""}
PORTS = (7777, *range(7790, 7800))  # jarvis ui/app, then the overlay's range
REPO = Path(__file__).resolve().parent.parent

# "check the market", "pull up the pro terminal on the dell", "how's the market doing".
# A bare "terminal" is left alone: "open terminal" means the terminal app.
SHOW_MARKET = re.compile(
    r"^\W*(?:hey\s+|ok(?:ay)?\s+)?(?:jarvis\W+)?(?:can\s+you\s+|could\s+you\s+|please\s+)?"
    r"(?:check(?:\s+on)?|show(?:\s+me)?|pull\s+up|bring\s+up|open(?:\s+up)?|launch|"
    r"how(?:'s|\s+is|\s+are))\s+"
    r"(?:the\s+|my\s+)?(?:crypto\s+)?(?:markets?|pro\s+terminal|markets?\s+terminal|"
    r"trading\s+terminal)\b(?:\s+tab)?(?:\s+(?:doing|looking))?(?:\s+(?:today|now|right\s+now))?"
    r"(?:\s+(?:on|over\s+on)\s+(?:the\s+|my\s+)?(?P<device>[a-z]+(?:\s+linux)?)"
    r"(?:\s+(?:laptop|pc|computer))?)?"
    r"(?:\s+(?:please|for\s+me))?\W*$", re.I)


def serves_console(port, timeout=1.5):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/jarvis.css", timeout=timeout) as res:
            return res.status == 200
    except OSError:
        return False


def console_port(ports=PORTS):
    """The port of a JARVIS console already running on this laptop, or None."""
    # Overlay ports are probed in parallel: serial connection timeouts made an
    # already-running console look like a slow cold start on remote machines.
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
    """(ok, text): open one console page as an app window on this laptop."""
    key = (name or "terminal").strip().lower()
    if key not in PAGES:
        return False, f"No console page called {name!r}; pages: {', '.join(PAGES)}."
    from .ui import app_window
    port = console_port() or start_console()
    url = f"http://127.0.0.1:{port}/{PAGES[key]}"
    # Its own profile, like the console's window: the everyday one can swallow it.
    profile = REPO / ".jarvis" / "page-window"
    app_window(url, size=(1600, 980), profile=profile)
    label = {"terminal": "the Pro terminal", "markets": "Markets", "console": "the console"}
    return True, f"Opened {label.get(key, key.title())}."


def market_line(market):
    """One spoken sentence on the market, or None when the data can't be had."""
    try:
        intel = market.intel()
    except Exception:
        return None
    move, breadth = intel.get("cap_weighted_24h"), intel.get("breadth") or {}
    if move is None:
        return None
    mood = "up" if move > 0.25 else "down" if move < -0.25 else "flat"
    line = f"Crypto is {mood} {abs(move):.1f} percent today" if mood != "flat" else \
        "Crypto is about flat today"
    if breadth.get("n"):
        line += f", {breadth.get('up', 0)} of {breadth['n']} big coins up"
    best = (intel.get("gainers") or [None])[0]
    worst = (intel.get("losers") or [None])[0]
    if best and worst and best.get("chg_24h") is not None and worst.get("chg_24h") is not None:
        line += (f". {best['symbol']} leads at {best['chg_24h']:+.1f}, "
                 f"{worst['symbol']} trails at {worst['chg_24h']:+.1f}")
    return line + "."


def answer(text, config, state, remote=None, market=None):
    """The spoken reply when `text` asks to see the market, having opened the Pro terminal;
    None when it doesn't ask that."""
    match = SHOW_MARKET.match(text.replace("’", "'"))
    if not match:
        return None
    from .remote import Remote
    remote = remote or Remote(config, state)
    device = remote.resolve(match.group("device"))  # "arch linux" -> zenbook
    if device and device not in remote.names():
        return f"I don't know a laptop called {device}; I know {', '.join(remote.names())}."
    # Opening the Markets tab is a UI request; don't hold it up on a live market
    # data request. Status/check-market requests still include the spoken summary.
    page = "markets" if re.search(r"\b(?:markets?\s+tab|open\s+(?:up\s+)?markets?)\b", text, re.I) else "terminal"
    try:
        ok, text_ = remote.call(device, "open_page", page)
    except Exception as exc:  # never let the shortcut take the turn down
        ok, text_ = False, str(exc)
    where = f" on the {device.title()}" if device and device != remote.here() else ""
    label = "Markets" if page == "markets" else "Pro terminal"
    opened = f"{label} is up{where}." if ok else f"I couldn't open {'the ' if page == 'terminal' else ''}{label}{where}: {text_}"
    if page == "markets":
        return opened
    if market is None:
        try:
            from .markets import TrueMarkets
            market = TrueMarkets(state, config)
        except Exception:
            market = None
    line = market_line(market) if market is not None else None
    return f"{line} {opened}" if line else opened
