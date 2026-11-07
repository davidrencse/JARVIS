"""The agent browser: Vercel's agent-browser CLI, run straight from JARVIS.

The act lane's agent-browser MCP server (actions.browser_mcp) and these CLI calls share
one daemon session, SESSION, with one profile (.jarvis/agent-browser-profile). So:

- `browse` runs simple steps (open a page, scroll, back, forward, reload, close) with no
  model at all, from the parser ("in the agent browser go to instagram and scroll").
- `frame` takes a screenshot of whatever page the agent is on, for the console's
  "Agent screen" box, the way hosted personal agents show their browser. It never
  launches a browser: agent-browser starts Chrome on any command, so it first checks
  that the session's Chrome is already running and otherwise reports "closed".

Every call is one short-lived CLI process (about 30-70 ms once the daemon is up). The
daemon itself is the one the MCP server already uses, with agent-browser's own idle
timeout; nothing new keeps running. Auto-scroll is a thread with the same fail-safes as
desktop scrolling: it ends on "stop scrolling", after SCROLL_MAX, or once the browser
has been closed for SCROLL_MISSES checks in a row.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .platforms import NO_WINDOW, on_windows

PACKAGE = "agent-browser"
VERSION = "0.39.1"  # pinned with actions.AGENT_BROWSER
SESSION = "jarvis"
STATE = Path(__file__).resolve().parents[1] / ".jarvis"
ACTIONS = ("open", "scroll", "back", "forward", "reload", "close")
SCROLL_PX = 700             # one "scroll" in the agent browser, about a screen
SCROLL_EVERY = 10.0
SCROLL_MAX = 30 * 60
SCROLL_MISSES = 3
FRAME_QUALITY = 55          # JPEG quality of live-view frames
FLAGS = NO_WINDOW
_SCROLLER = {"stop": None}
_SCROLLER_LOCK = threading.Lock()
_FRAME_LOCK = threading.Lock()
# The live view polls every 400-700 ms; "is it open" (a CLI spawn plus a /proc scan) and the
# page's url/title (two more spawns) only need refreshing every few seconds.
FRAME_CACHE_S = 3.0
_FRAME_CACHE = {}  # (kind, state_dir) -> (time, value)


def env(state_dir=None, headed=True):
    """The variables that put the CLI and the MCP server in the same session and profile."""
    profile = Path(state_dir or STATE).resolve() / "agent-browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    return {"AGENT_BROWSER_SESSION": SESSION, "AGENT_BROWSER_PROFILE": str(profile),
            "AGENT_BROWSER_HEADED": "true" if headed else "false"}


# A build from the GitHub source (git clone vercel-labs/agent-browser at the pinned tag,
# then `cargo build --release --manifest-path cli/Cargo.toml`), used when its version is the
# pinned one. AGENT_BROWSER_SOURCE points elsewhere; the default is ~/projects/agent-browser.
SOURCE_ENV = "AGENT_BROWSER_SOURCE"
_SOURCE_SEEN = {}


def source_build():
    """The source-built CLI's path when it exists and reports VERSION, else None."""
    root = Path(os.environ.get(SOURCE_ENV) or Path.home() / "projects" / PACKAGE).expanduser()
    exe = root / "cli" / "target" / "release" / (PACKAGE + (".exe" if on_windows() else ""))
    if not exe.is_file():
        return None
    key = (str(exe), exe.stat().st_mtime)
    if key not in _SOURCE_SEEN:
        try:
            out = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                                 timeout=10, creationflags=FLAGS).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        _SOURCE_SEEN[key] = out.strip().split()[-1:] == [VERSION]
    return str(exe) if _SOURCE_SEEN[key] else None


def binary():
    """The pinned CLI: the GitHub source build, else on PATH, else the copy npx cached for
    the MCP server, else npx."""
    built = source_build()
    if built:
        return [built]
    found = shutil.which(PACKAGE)
    if found:
        return [found]
    roots = [Path.home() / ".npm" / "_npx",
             Path(os.environ.get("LOCALAPPDATA", "")) / "npm-cache" / "_npx"]
    for root in roots:
        if not root.is_dir():
            continue
        for package in root.glob(f"*/node_modules/{PACKAGE}/package.json"):
            try:
                if json.loads(package.read_text(encoding="utf-8")).get("version") != VERSION:
                    continue
            except (OSError, ValueError):
                continue
            bins = package.parents[1] / ".bin"
            for name in (PACKAGE + ".cmd", PACKAGE) if on_windows() else (PACKAGE,):
                if (bins / name).is_file():
                    return [str(bins / name)]
    npx = shutil.which("npx")
    return [npx, "-y", f"{PACKAGE}@{VERSION}"] if npx else None


def _stale_daemons(exe):
    """PIDs of daemons of the source build started before the binary was last rebuilt:
    they keep running the old code under the same version, so a fix never shows (seen
    2026-11-01). Linux /proc only; elsewhere, nothing is reported."""
    proc = Path("/proc")
    if not proc.is_dir():
        return []
    try:
        built = Path(exe).stat().st_mtime
        boot = next(float(line.split()[1]) for line in (proc / "stat").read_text().splitlines()
                    if line.startswith("btime "))
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, StopIteration, ValueError, AttributeError):
        return []
    stale = []
    for entry in proc.glob("[0-9]*"):
        try:
            if os.readlink(entry / "exe").removesuffix(" (deleted)") != str(exe):
                continue
            started = boot + int((entry / "stat").read_text().rsplit(")", 1)[1].split()[19]) / ticks
        except (OSError, ValueError, IndexError):
            continue
        if started < built - 2:  # /proc's boot time is whole seconds: allow for it
            stale.append(int(entry.name))
    return stale


def _retire_stale(command):
    """Stop daemons a rebuild left on old code, once per binary build."""
    exe = command[0] if command and len(command) == 1 else None
    if not exe or exe != source_build():
        return
    key = ("retired", exe, Path(exe).stat().st_mtime)
    if key in _SOURCE_SEEN:
        return
    _SOURCE_SEEN[key] = True
    import signal
    stale = _stale_daemons(exe)
    for pid in stale:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    # The next command must not reach a daemon that is still shutting down.
    deadline = time.monotonic() + 3
    while stale and time.monotonic() < deadline:
        stale = [pid for pid in stale if Path(f"/proc/{pid}").exists()]
        time.sleep(0.1)
    for pid in stale:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if stale:
        time.sleep(0.2)


def run(*args, state_dir=None, timeout=30):
    """(ok, data) for one CLI command in the JARVIS session; data is the JSON `data`
    on success and the error text on failure."""
    command = binary()
    if command is None:
        return False, "agent-browser isn't installed (it needs Node's npx)."
    _retire_stale(command)
    try:
        out = subprocess.run([*command, *args, "--json"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=timeout,
                             env={**os.environ, **env(state_dir)}, creationflags=FLAGS)
    except subprocess.TimeoutExpired:
        return False, "agent-browser timed out; the step may have run."
    except OSError as exc:
        return False, f"agent-browser didn't start: {exc}"
    try:
        reply = json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, (out.stderr or out.stdout or "no reply").strip()[:300]
    if not reply.get("success"):
        return False, str(reply.get("error") or "agent-browser failed")[:300]
    return True, reply.get("data") or {}


def running(state_dir=None):
    """True when the JARVIS session's Chrome is open. Read-only: never starts one."""
    ok, data = run("session", "list", state_dir=state_dir, timeout=10)
    if not ok or SESSION not in (data.get("sessions") or []):
        return False
    profile = env(state_dir)["AGENT_BROWSER_PROFILE"]
    try:
        import psutil
    except ImportError:
        if not Path("/proc").is_dir():
            return True  # the daemon is up; without psutil or /proc, trust it
        for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                if profile.encode() in cmdline.read_bytes():
                    return True
            except OSError:
                continue
        return False
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if any(profile in part for part in proc.info["cmdline"] or ()):
                return True
        except (psutil.Error, TypeError):
            continue
    return False


def _page(state_dir=None):
    """{"url", "title"} of the agent browser's page, or {} when it can't say."""
    page = {}
    for field in ("url", "title"):
        ok, data = run("get", field, state_dir=state_dir, timeout=10)
        if ok:
            page[field] = str(data.get(field) or "")
    return page


def _cached(kind, state_dir, compute, clock=time.monotonic):
    key = (kind, str(state_dir))
    now = clock()
    hit = _FRAME_CACHE.get(key)
    if hit and now - hit[0] < FRAME_CACHE_S:
        return hit[1]
    value = compute(state_dir)
    _FRAME_CACHE[key] = (now, value)
    return value


def _is_open(state_dir):
    """running(), but an "open" answer is trusted for FRAME_CACHE_S; "closed" never is."""
    key = ("running", str(state_dir))
    hit = _FRAME_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < FRAME_CACHE_S:
        return True
    if running(state_dir):
        _FRAME_CACHE[key] = (time.monotonic(), True)
        return True
    _FRAME_CACHE.clear()
    return False


def frame(state_dir=None):
    """{"open": False} or {"open": True, "url", "title", "image": JPEG bytes}."""
    if not _is_open(state_dir):
        return {"open": False}
    path = Path(state_dir or STATE).resolve() / "agent-browser-frame.jpg"
    with _FRAME_LOCK:  # two console tabs polling must not write one file at once
        ok, data = run("screenshot", str(path), "--screenshot-format", "jpeg",
                       "--screenshot-quality", str(FRAME_QUALITY), state_dir=state_dir, timeout=10)
        if not ok:
            return {"open": True, "error": str(data)}
        try:
            image = path.read_bytes()
        except OSError as exc:
            return {"open": True, "error": str(exc)}
    return {"open": True, **_cached("page", state_dir, _page), "image": image}


def _scroll_loop(stop, direction, every, state_dir, clock=time.monotonic):
    deadline, misses = clock() + SCROLL_MAX, 0
    while not stop.wait(every) and clock() < deadline:
        if not running(state_dir):
            misses += 1
            if misses >= SCROLL_MISSES:
                break
            continue
        misses = 0
        run("scroll", direction, str(SCROLL_PX), state_dir=state_dir, timeout=15)
    with _SCROLLER_LOCK:
        if _SCROLLER["stop"] is stop:
            _SCROLLER["stop"] = None


def stop_scrolling():
    """End an agent-browser auto-scroll; True when one was running."""
    with _SCROLLER_LOCK:
        running_stop, _SCROLLER["stop"] = _SCROLLER["stop"], None
    if running_stop:
        running_stop.set()
    return running_stop is not None


def browse(spec, state_dir=None):
    """(ok, text). spec: JSON {"action": open|scroll|back|forward|reload|close, "url"
    (open, http(s) only), "direction": down|up, "mode": once|auto|stop, "every": seconds}."""
    try:
        args = json.loads(spec) if isinstance(spec, str) else dict(spec)
    except (TypeError, ValueError):
        return False, "Browse takes a JSON spec."
    action = str(args.get("action") or "").lower()
    if action not in ACTIONS:
        return False, f"The agent browser can {', '.join(ACTIONS)}."
    if action == "open":
        url = str(args.get("url") or "")
        if not re.match(r"https?://", url, re.I):
            return False, "The agent browser only opens http(s) pages."
        ok, data = run("open", url, state_dir=state_dir, timeout=45)
        if not ok:
            return False, f"The agent browser couldn't open {url}: {data}"
        title = str(data.get("title") or "").strip()
        return True, f"Opened {title or url} in the agent browser."
    if action == "scroll":
        direction = str(args.get("direction") or "down").lower()
        mode = str(args.get("mode") or "once").lower()
        if direction not in ("down", "up") or mode not in ("once", "auto", "stop"):
            return False, "Scroll takes direction up or down and mode once, auto or stop."
        was = stop_scrolling()
        if mode == "stop":
            return True, "Stopped scrolling the agent browser." if was else \
                "The agent browser wasn't scrolling."
        if not running(state_dir):
            return False, "The agent browser isn't open; open a page in it first."
        if mode == "auto":
            try:
                every = min(300.0, max(2.0, float(args.get("every") or SCROLL_EVERY)))
            except (TypeError, ValueError):
                every = SCROLL_EVERY
            stop = threading.Event()
            with _SCROLLER_LOCK:  # swap atomically: a scroll started meanwhile must stay stoppable
                replaced, _SCROLLER["stop"] = _SCROLLER["stop"], stop
            if replaced:
                replaced.set()
            threading.Thread(target=_scroll_loop, args=(stop, direction, every, state_dir),
                             name="jarvis-agent-scroll", daemon=True).start()
            return True, (f"Scrolling the agent browser every {every:g} seconds. "
                          "Say \"stop scrolling\" to stop.")
        ok, data = run("scroll", direction, str(SCROLL_PX), state_dir=state_dir, timeout=15)
        return (True, f"Scrolled the agent browser {direction}.") if ok else \
            (False, f"The agent browser couldn't scroll: {data}")
    if action == "close":
        stop_scrolling()
        _FRAME_CACHE.clear()
        if not running(state_dir):
            return True, "The agent browser was already closed."
        ok, data = run("close", state_dir=state_dir, timeout=20)
        return (True, "Closed the agent browser.") if ok else (False, str(data))
    if not running(state_dir):  # back/forward/reload would launch a blank browser
        return False, "The agent browser isn't open."
    ok, data = run(action, state_dir=state_dir, timeout=30)
    return (True, f"Agent browser: {action} done.") if ok else (False, str(data))
