"""Always-on voice JARVIS: background process, login autostart, one instance only.

`jarvis voice start` launches the listener hidden (no console window) with output in
.jarvis/logs/voice.log. `stop` / `status` manage it via a PID file; an exclusive file
lock guarantees two listeners never share the mic. `autostart on` adds a hidden
launcher to the Windows Startup folder (same pattern as Lan Mouse), or on Linux a
systemd user unit that runs `voice start` at login.
"""

import atexit
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ACK = "On it."
STARTUP_NAME = "JARVIS Voice.vbs"
UNIT_NAME = "jarvis-voice.service"


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}", file=sys.stderr, flush=True)


class InstanceLock:
    """Exclusive lock on .jarvis/voice.lock; released automatically if the process dies."""

    def __init__(self, state_dir):
        self.path = Path(state_dir).resolve() / "voice.lock"
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("JARVIS is already listening (see: jarvis voice status).") from None
        return self

    def __exit__(self, *_):
        self.handle.close()


def listen_loop(state, config, base, threshold=None):
    from .assistant import ask, wake_word
    from .chatgpt import make_lane
    from concurrent.futures import ThreadPoolExecutor

    from .events import Turn, emit, feed_sender
    from .instant import answer as instant_answer
    from .listen import Transcriber, VoicePrint, _np, record_utterance
    from .memory import Memory
    from .speech import SpeechQueue, Voice, spoken
    from .store import Store, lanes_from

    from .topology import is_node
    node = is_node(config)
    store = None if node else Store(state, lanes_from(config))
    from .memory import semantic_recall
    memory, voice = (None if node else Memory(state, semantic=semantic_recall(config))), Voice(state, config)
    speaker = SpeechQueue(voice)
    ears, stt = VoicePrint(state, config), Transcriber(config)
    lane = make_lane(config, base, state)
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ears")
    if not ears.path.is_file():  # fail once, clearly, instead of on every utterance
        log(f"error: {NOT_ENROLLED}")
        raise SystemExit(2)
    with InstanceLock(state):
        foreground_priority()
        pid_file = Path(state) / "voice.pid"
        pid_file.write_text(str(os.getpid()))
        # A stale pid file can name an unrelated process once Windows reuses the number.
        atexit.register(lambda: pid_file.unlink(missing_ok=True))
        # Warm up so the first real request isn't slow: load Whisper, cache the ack line.
        stt(_np().zeros(16000, dtype="float32"))
        voice.synth(ACK)
        lane.warm()
        if not node:
            from .pipeline import warm
            warm()  # the installed-app list, for the direct-action fast path
            memory.warm_vectors()  # meaning-based recall index, in the background
        log(f"JARVIS listening on {stt.device}. Say \"Jarvis, ...\".")
        emit(state, "voice.ready", device=stt.device)
        failures = 0
        while True:
            try:
                audio = record_utterance()
                if audio is None:  # 30 s of silence: keep the brain warm
                    lane.warm()
                    continue
                # Voice check (CPU) and transcription (GPU) run side by side to save ~0.3 s.
                # A stranger's transcript is discarded unread: never logged or answered.
                check, text = pool.submit(ears.verify, audio, threshold), pool.submit(stt, audio)
                is_owner, score = check.result()
                if not is_owner:
                    text.cancel()
                    log(f"ignored another voice (similarity {score})")
                    emit(state, "voice.ignored", score=score)
                    continue
                request = wake_word(text.result())
                if not request:  # not addressed to JARVIS: never logged or answered
                    continue
                ears.learn()  # passed the voice check AND said "Jarvis": adapt to live audio
                log(f"you ({score}): {request}")
                emit(state, "voice.heard", text=request, score=score)
                if not instant_answer(request):  # instant answers need no "On it."
                    voice.say(ACK, wait=False)  # cached, free; plays while ChatGPT thinks
                budget = [voice.limit]
                turn = Turn(feed_sender(state), "voice", request)

                def speak_sentence(sentence, turn=turn, budget=budget):
                    # Queue streamed sentences as they land, up to the spoken-length cap.
                    turn.sentence(sentence)
                    if budget[0] > 0:
                        budget[0] -= len(sentence)
                        speaker.put(sentence)

                started = time.time()
                try:
                    result = ask(request, store, memory, config, base, lane=lane,
                                 on_sentence=speak_sentence, on_start=turn.start,
                                 on_step=turn.step, channel="voice")
                except BaseException as exc:
                    turn.error(exc)
                    raise
                turn.done(result)
                log(f"jarvis [{result['tier']}, {time.time() - started:.1f}s]: {result['reply']}")
                if not result.get("streamed"):
                    speaker.put(spoken(result["reply"], voice.limit))
                speaker.drain()  # finish talking before listening again
                failures = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # keep listening through any single failed turn
                failures += 1
                log(f"error: {type(exc).__name__}: {exc}")
                if failures == 1:  # a dead mic must not apologise out loud every second
                    try:
                        voice.say("Sorry, that one failed. The details are in the log.")
                    except Exception:
                        pass
                time.sleep(min(30, 2 ** min(failures, 5)))


def foreground_priority():
    """Hidden background processes get Windows EcoQoS throttling and normal priority;
    opt this one out so a spoken request is handled at full speed."""
    if os.name != "nt":
        return
    import ctypes

    class PowerThrottling(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                    ("StateMask", ctypes.c_ulong)]

    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # 64-bit pseudo-handle, not int
    kernel32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                               ctypes.c_void_p, ctypes.c_ulong]
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    handle = kernel32.GetCurrentProcess()
    # ProcessPowerThrottling = 4; control EXECUTION_SPEED (1), state 0 = never throttle.
    state = PowerThrottling(1, 1, 0)
    kernel32.SetProcessInformation(handle, 4, ctypes.byref(state), ctypes.sizeof(state))
    kernel32.SetPriorityClass(handle, 0x8000)  # ABOVE_NORMAL_PRIORITY_CLASS


def _alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value == 259  # STILL_ACTIVE


def _pid(state):
    try:
        pid = int((Path(state) / "voice.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _alive(pid) else None


def status(state):
    log_path = Path(state).resolve() / "logs" / "voice.log"
    tail = []
    if log_path.is_file():
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
    pid = _pid(state)
    voice = Path(state).resolve() / "voice"
    return {"listening": pid is not None, "pid": pid,
            "enrolled": (voice / "voiceprint.json").is_file(),
            "autostart": _startup_file().is_file(),
            "log": str(log_path), "recent": tail}


NOT_ENROLLED = ("No voiceprint on this machine, so every voice would be rejected. "
                "Run: jarvis voice enroll  (each laptop needs its own)")


def start(state):
    if _pid(state):
        return {"started": False, **status(state)}
    if not (Path(state).resolve() / "voice" / "voiceprint.json").is_file():
        return {"started": False, "note": NOT_ENROLLED, **status(state)}
    logs = Path(state).resolve() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             if os.name == "nt" else 0)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with open(logs / "voice.log", "a", encoding="utf-8") as out:
        subprocess.Popen([sys.executable, "-m", "jarvis", "--state", str(Path(state).resolve()),
                          "voice", "listen"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         creationflags=flags, start_new_session=os.name != "nt", env=env,
                         close_fds=True)
    for _ in range(60):  # model loading takes a few seconds
        time.sleep(0.5)
        if _pid(state):
            return {"started": True, **status(state)}
    return {"started": False, "note": "Listener did not come up; check the log.",
            **status(state)}


def stop(state):
    pid = _pid(state)
    if not pid:
        return {"stopped": False, "note": "Not running."}
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, 15)
    return {"stopped": True, "pid": pid}


def _startup_file():
    if os.name != "nt":
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return config / "systemd" / "user" / UNIT_NAME
    return (Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / STARTUP_NAME)


def _systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _systemd_autostart(state, enable):
    """`voice start` detaches the listener, so the unit is a oneshot that stays active;
    `voice stop` still works through the PID file."""
    path = _startup_file()
    if not enable:
        _systemctl("disable", UNIT_NAME)
        path.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        return {"autostart": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    state = Path(state).resolve()
    path.write_text(
        "# JARVIS: start the voice listener at login. `jarvis voice autostart off` removes it.\n"
        "[Unit]\nDescription=JARVIS voice listener\n"
        "After=pipewire.service pipewire-pulse.service\n\n"
        f"[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"WorkingDirectory={Path(__file__).resolve().parent.parent}\n"
        # systemd's PATH lacks ~/.local/bin, where the claude CLI usually lives.
        f'Environment="PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin")}"\n'
        f'ExecStart="{sys.executable}" -m jarvis --state "{state}" voice start\n'
        f'ExecStop="{sys.executable}" -m jarvis --state "{state}" voice stop\n\n'
        "[Install]\nWantedBy=default.target\n", encoding="utf-8")
    enabled = _systemctl("daemon-reload") and _systemctl("enable", UNIT_NAME)
    return {"autostart": True, "unit": str(path), "enabled": enabled}


def autostart(state, enable):
    if os.name != "nt":
        return _systemd_autostart(state, enable)
    path = _startup_file()
    if not enable:
        path.unlink(missing_ok=True)
        return {"autostart": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    command = f'"{sys.executable}" -m jarvis --state "{Path(state).resolve()}" voice start'
    path.write_text(
        "' JARVIS: start the voice listener hidden at login. Delete this file to disable.\n"
        f'CreateObject("WScript.Shell").Run "{command.replace(chr(34), chr(34) * 2)}", 0, False\n',
        encoding="ascii")
    return {"autostart": True, "launcher": str(path)}
