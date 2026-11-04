"""Always-on voice JARVIS: background process, login autostart, one instance only.

`jarvis voice start` launches the listener hidden (no console window) with output in
.jarvis/logs/voice.log. `stop` / `status` manage it via a PID file; an exclusive file
lock guarantees two listeners never share the mic. `autostart on` adds a hidden
launcher to the Windows Startup folder (same pattern as Lan Mouse), or on Linux a
systemd user unit that runs the listener itself and restarts it if it crashes; with
that unit installed, `start`/`stop` go through systemctl.
"""

import atexit
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ACK = "On it."
UNCLEAR = "Sorry, I didn't catch that."  # a transcript Whisper was unsure of (listen.unsure)
STARTUP_NAME = "JARVIS Voice.vbs"
UNIT_NAME = "jarvis-voice.service"


def log(message):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}", file=sys.stderr, flush=True)


def _try_lock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


class InstanceLock:
    """Exclusive lock on .jarvis/voice.lock; released automatically if the process dies.
    `wait` covers a restart: the old listener may still be exiting when the new one starts."""

    def __init__(self, state_dir, wait=0.0):
        self.path = Path(state_dir).resolve() / "voice.lock"
        self.handle, self.wait = None, wait

    def __enter__(self):
        if self.handle and not self.handle.closed:
            return self  # already held (taken early so a busy lock exits cleanly)
        self.handle = open(self.path, "a+")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                _try_lock(self.handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise RuntimeError(
                        "JARVIS is already listening (see: jarvis voice status).") from None
                time.sleep(0.5)
        return self

    def __exit__(self, *_):
        self.handle.close()


def lock_held(state):
    """True while a live process holds voice.lock. The OS drops the lock when its holder
    dies, so a pid file left behind by a stopped listener never passes for a live one."""
    path = Path(state).resolve() / "voice.lock"
    if not path.is_file():
        return False
    try:
        handle = open(path, "a+")
    except OSError:
        return False
    try:
        _try_lock(handle)
    except OSError:
        return True
    finally:
        handle.close()  # closing releases the probe's own lock
    return False


FOLLOW_UP = 6.0  # seconds after a bare "Jarvis" in which the next utterance is the request
REPLY_FOLLOW_UP = 8.0  # seconds after JARVIS finishes talking in which David can answer it
# What answering JARVIS sounds like ("Then check it." "Yes please." "No, stop."). Anything
# else right after a reply is the room talking, and still needs "Jarvis".
REPLY_WORDS = frozenset("""yes yeah yep yup sure ok okay alright please no nope nah do it go
    ahead for check then sounds good that's fine cancel stop never mind thanks thank you
    jarvis right now that one this""".split())


def answers_jarvis(text):
    """A short reply to what JARVIS just said or offered."""
    words = re.findall(r"[a-z']+", text.lower().replace("\u2019", "'"))
    return 0 < len(words) <= 6 and all(w in REPLY_WORDS for w in words)


def confirmation_waiting(config, state):
    """A confirmation is pending for this device, or might be: a node's confirmations live
    on the main, out of sight. Approving one always takes "Jarvis" (the reply window must
    not turn a "Yes" said to someone in the room into an approval)."""
    from .topology import is_node
    if is_node(config):
        return True
    try:
        from .assistant import confirmation_origin
        from .confirm import ConfirmQueue
        return bool(ConfirmQueue(state).pending(confirmation_origin(config, state)))
    except Exception:
        return True  # can't tell: treat as pending


def opens_reply_window(result):
    """Only a reply that went through invites an answer; a failure ends the exchange."""
    return not result.get("error") and not any(
        isinstance(o, dict) and o.get("ok") is False for o in result.get("outcomes") or ())


# Live mic scores dropped to 0.11-0.28 on 2026-10-26, so near misses started at 0.10. Every one
# is transcribed on the CPU before the wake word can reject it: 603 of 2,259 rejected clips by
# 2026-10-30 (median 0.205), and none became a request. The voice print's own cutoff is
# 0.30-0.36, so 0.25 keeps the band just under it and skips Whisper for ~3/4 of the strangers.
BORDERLINE = 0.25
# David far from the mic has scored 0.11-0.28, so a clip between this floor and BORDERLINE
# is not dropped outright: only its opening seconds are transcribed (cheap next to the whole
# clip), and it counts as a near miss when they start with "Jarvis". Below the floor, noise.
STRANGER_FLOOR = 0.08
HEAD_SECONDS = 2.5


def warm_quietly(lane):
    """Warm the brain. Codex not up yet (at login) or down is no failed turn: logging it
    is enough. Uncaught, it killed the listener at startup until systemd gave up, and a
    short noise (a None clip warms the brain) said "Sorry, that one failed"."""
    try:
        lane.warm()
    except Exception as exc:
        log(f"brain warm-up failed: {type(exc).__name__}: {exc}")


def listen_loop(state, config, base, threshold=None, config_path=None):
    from .assistant import ask, confirmation_words, wake_word
    from .chatgpt import make_lane
    from concurrent.futures import ThreadPoolExecutor

    from .events import Turn, emit, feed_sender
    from .instant import answer as instant_answer
    from .listen import RATE, LiveMic, Transcriber, VoicePrint, _np, mic_settings, record_utterance, unsure
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
    lock = InstanceLock(state, wait=15)
    try:
        lock.__enter__()
    except RuntimeError as exc:
        log(f"error: {exc}")
        raise SystemExit(3) from None  # the unit doesn't restart on 3: someone else listens
    with lock:
        foreground_priority()
        pid_file = Path(state) / "voice.pid"
        pid_file.write_text(str(os.getpid()))
        # A stale pid file can name an unrelated process once Windows reuses the number.
        atexit.register(lambda: pid_file.unlink(missing_ok=True))
        # `voice stop` sends SIGTERM, whose default action skips atexit and leaves the pid file.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        # Warm up so the first real request isn't slow: load Whisper, cache the ack line.
        for name, warm in (("Whisper", lambda: stt(_np().zeros(16000, dtype="float32"))),
                           ("speech", lambda: voice.synth(ACK)), ("brain", lambda: warm_quietly(lane))):
            try:  # a failed warm-up costs the first request some speed, never the listener
                warm()
            except Exception as exc:
                log(f"{name} warm-up failed: {type(exc).__name__}: {exc}")
        if not node:
            from .pipeline import warm
            warm()  # the installed-app list, for the direct-action fast path
            memory.warm_vectors()  # meaning-based recall index, in the background
            from .consolidate import start as consolidate_daily
            consolidate_daily(config, memory, base, log=log)  # memory.consolidate: once a day
        log(f"JARVIS listening on {stt.device}. Say \"Jarvis, ...\".")
        emit(state, "voice.ready", device=stt.device)
        failures, mic, armed, armed_by_owner, chatting = 0, None, 0.0, False, 0.0
        pending = None  # a cut-off command ("open up") waiting for its rest (fragments.py)
        from .fragments import complete as complete_fragment, completes as completes_fragment, \
            judge as judge_fragment
        from .parse import parse_for
        from .topology import enabled as cluster_on, identity
        here = identity(config)[0] if cluster_on(config) else next(iter(config.get("devices") or {"pc": 0}))

        def understood(text):
            """The strict parser or an instant answer handles it: never a fragment."""
            if instant_answer(text):
                return True
            try:
                return parse_for(text, config, here)["commands"] is not None
            except Exception:
                return False
        live = [None]  # the open mic, for the console's level wave (post_levels)
        start_level_poster(state, lambda: live[0])
        while True:
            try:
                mic = mic or LiveMic(tuning=mic_settings(state))  # device + cutoffs from setup
                live[0] = mic
                audio = record_utterance(mic=mic)
                if audio is None:  # silence or a short noise: keep the brain warm
                    warm_quietly(lane)
                    continue
                # When this utterance began, so a long follow-up still lands in the window.
                began = time.monotonic() - len(audio) / RATE
                said_at = time.time() - len(audio) / RATE  # wall clock, before Whisper runs
                # On a GPU the voice check (CPU) and transcription run side by side to save
                # ~0.3 s. On CPU Whisper takes seconds and can't be stopped once started, so
                # every TV line or passer-by would hog the cores and make David's next
                # request queue behind it: there the check (~0.2 s) goes first.
                # A stranger's transcript is discarded unread: never logged or answered.
                check = pool.submit(ears.verify, audio, threshold)
                text = pool.submit(stt, audio) if stt.device == "cuda" else None
                is_owner, score = check.result()
                # Only now does the pass count as working: a voice check that keeps raising
                # (corrupt voiceprint) must back off, not apologise for every noise (audit
                # 2026-11-01: the counter was reset as soon as the mic delivered audio).
                failures = 0
                # A near miss (David further from the mic, quieter, music behind him) is
                # accepted only when it also starts with "Jarvis"; strangers score far lower.
                settings = config.get("voice", {})
                near = not is_owner and score >= settings.get("near_miss", BORDERLINE)
                heard, stats_of = None, audio  # stats_of: the array Whisper heard
                if not is_owner and not near:
                    head, clip = None, audio[:int(HEAD_SECONDS * RATE)]
                    if score >= settings.get("stranger_floor", STRANGER_FLOOR):
                        try:  # its words stay unread unless they open with the wake word
                            head = text.result() if text is not None else stt(clip)
                        except Exception:
                            head = None
                    if head is None or wake_word(head) is None:
                        if text is not None:
                            text.cancel()
                        log(f"ignored another voice (similarity {score})")
                        emit(state, "voice.ignored", score=score)
                        continue
                    near = True
                    if text is not None or len(audio) <= HEAD_SECONDS * RATE:
                        heard = head  # already the whole clip
                        if text is None:
                            stats_of = clip  # its confidence was stored under the clip it heard
                if heard is None:
                    heard = text.result() if text is not None else stt(audio)
                request = wake_word(heard)
                if request == "":  # a bare "Jarvis" and a pause: the request comes next
                    armed, armed_by_owner = began + len(audio) / RATE + FOLLOW_UP, is_owner
                    log(f"wake word ({score}): waiting for the request")
                    emit(state, "voice.wake", score=score)
                    continue
                # The follow-up has no wake word of its own, so one of the two clips must
                # pass the full voice check: two near misses are not enough.
                follow_up = False
                if request is None and heard.strip() and began < armed and (
                        is_owner or armed_by_owner):
                    request = heard.strip()  # the rest of "Jarvis, ... <pause> ..."
                    follow_up = True
                # Answering JARVIS ("I can check it if you want." "Then check it.") needs
                # no wake word, but only a full voice match and a short reply count.
                # Wall clock: the reply's end is read once, after JARVIS stops talking, and
                # the utterance's start before transcribing, so Whisper's time doesn't count.
                if request is None and is_owner and answers_jarvis(heard) and said_at < chatting:
                    if confirmation_words(heard) and confirmation_waiting(config, state):
                        log("reply window: approving or cancelling a confirmation needs \"Jarvis\"")
                    else:
                        request = heard.strip()
                        follow_up = True
                if request or is_owner:  # a near miss (TV, another voice) closes no window
                    armed = chatting = 0.0
                if not request:  # not addressed to JARVIS: never answered
                    if near:  # still unconfirmed as David: discarded unread
                        log(f"ignored another voice (similarity {score}, near miss)")
                    elif heard:  # David's own voice without the wake word: say so, never what
                        log(f"no wake word ({score}): {len(heard.split())} words, not kept")
                    continue
                if unsure(stt.stats(stats_of) if hasattr(stt, "stats") else None):
                    # Probably misheard: ask again rather than answer a guess, and keep the
                    # follow-up window open so the repeat needs no "Jarvis".
                    log(f"unclear ({score}): asked to repeat")
                    emit(state, "voice.unclear", score=score)
                    armed, armed_by_owner = time.monotonic() + FOLLOW_UP + 3, is_owner
                    voice.say(UNCLEAR, wait=True)
                    mic.flush()
                    continue
                if pending and began < pending[1] and completes_fragment(pending[0], request, understood):
                    request = complete_fragment(pending[0], request)  # "open up" + "Spotify"
                pending = None
                verdict, ask_back = judge_fragment(request, follow_up=follow_up, understood=understood)
                if verdict == "overheard":  # David talking to someone else: never answered
                    log(f"overheard ({score}): {len(request.split())} words, not kept")
                    emit(state, "voice.fragment", verdict=verdict, score=score)
                    continue
                if verdict in ("unclear", "incomplete"):
                    log(f"{verdict} ({score}): {request!r}")
                    emit(state, "voice.fragment", verdict=verdict, score=score)
                    armed, armed_by_owner = time.monotonic() + FOLLOW_UP + 3, is_owner
                    pending = (request, armed) if verdict == "incomplete" else None
                    voice.say(ask_back or UNCLEAR, wait=True)
                    mic.flush()
                    continue
                if not near:
                    ears.learn()  # passed the voice check AND said "Jarvis": adapt to live audio
                # Seconds from the end of David's speech to these words: the silence wait,
                # the voice check and Whisper. The reply line adds the brain's own time.
                words_in = time.monotonic() - (began + len(audio) / RATE)
                via = getattr(stt, "last_engine", None)
                log(f"you ({score}, words in {words_in:.1f}s{' via ' + via if via else ''}): {request}")
                emit(state, "voice.heard", text=request, score=score)
                if not instant_answer(request):  # instant answers need no "On it."
                    voice.say(ACK, wait=False)  # cached, free; plays while ChatGPT thinks
                budget = [voice.limit]
                turn = Turn(feed_sender(state), "voice", request)

                first = []  # when the first streamed sentence arrived, for the timing line

                def speak_sentence(sentence, turn=turn, budget=budget, first=first):
                    # Queue streamed sentences as they land, up to the spoken-length cap.
                    if not first:
                        first.append(time.time())
                    turn.sentence(sentence)
                    if budget[0] > 0:
                        budget[0] -= len(sentence)
                        speaker.put(sentence)

                if config_path:  # console saves (vault.write...) since the last request
                    try:
                        from .settings import refresh
                        refresh(config, config_path)
                    except Exception as exc:
                        log(f"settings refresh failed: {exc}")
                started = time.time()
                try:
                    result = ask(request, store, memory, config, base, lane=lane,
                                 on_sentence=speak_sentence, on_start=turn.start,
                                 on_step=turn.step, channel="voice")
                except BaseException as exc:
                    turn.error(exc)
                    raise
                turn.done(result)
                lead = f", first words {first[0] - started:.1f}s" if first else ""
                log(f"jarvis [{result['tier']}, {time.time() - started:.1f}s{lead}]: {result['reply']}")
                if not result.get("streamed"):
                    speaker.put(spoken(result["reply"], voice.limit))
                speaker.drain()  # finish talking before listening again
                mic.flush()  # and don't treat JARVIS's own reply as something heard
                if opens_reply_window(result):
                    chatting = time.time() + REPLY_FOLLOW_UP
                failures = 0
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # keep listening through any single failed turn
                failures += 1
                log(f"error: {type(exc).__name__}: {exc}")
                if mic is not None:  # a dead or stalled stream is reopened on the next pass
                    try:
                        mic.close()
                    except Exception:
                        pass
                    mic = None
                if failures == 1:  # a dead mic must not apologise out loud every second
                    try:
                        speaker.clear(timeout=10)  # never over the failed turn's half-spoken reply
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
        except OSError:
            return False
        return True
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
    return pid if _alive(pid) and _is_listener(pid) and lock_held(state) else None


def _is_listener(pid):
    """After a reboot a stale voice.pid can name an unrelated process: on Linux, check it
    really is the listener (and not a zombie) before reporting it running or stopping it."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return True  # no /proc (Windows, macOS): the liveness probe is all there is
    if stat.rpartition(")")[2].split()[:1] == ["Z"]:
        return False
    return b"jarvis" in cmdline and b"listen" in cmdline


LEVEL_EVERY = 0.1   # seconds between posts of the mic level
LEVEL_STALE = 1.5   # older than this, the console shows the listener as not hearing


def level_file(state):
    """Where the listener posts its live mic level for the console's wave. A tmpfs
    ($XDG_RUNTIME_DIR) when there is one, so ten writes a second never touch the disk."""
    import hashlib
    state = Path(state).resolve()
    run = os.environ.get("XDG_RUNTIME_DIR")
    if run and os.path.isdir(run):
        tag = hashlib.sha1(str(state).encode()).hexdigest()[:10]
        return Path(run) / f"jarvis-voice-level-{tag}.json"
    return state / "voice" / "level.json"


def post_level(path, peak, now=None):
    """Write one reading atomically (the console must never read half a file)."""
    import json
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"at": now or time.time(), "pid": os.getpid(),
                               "peak": round(float(peak), 5)}), encoding="utf-8")
    os.replace(tmp, path)


def start_level_poster(state, current_mic, every=LEVEL_EVERY):
    """A daemon thread posting the mic's peak level every `every` seconds. Display only:
    any failure is swallowed so it can never cost the listener a request."""
    import threading
    path = level_file(state)

    def run():
        while True:
            time.sleep(every)
            mic = current_mic()
            if mic is None:
                continue
            try:
                post_level(path, mic.take_peak())
            except Exception:
                pass
    atexit.register(lambda: path.unlink(missing_ok=True))
    threading.Thread(target=run, name="voice-level", daemon=True).start()


def level(state, now=None):
    """The listener's latest mic level for the console: {"live": False} when the
    listener is off or hasn't posted lately."""
    import json
    try:
        data = json.loads(level_file(state).read_text(encoding="utf-8"))
        at, peak = float(data["at"]), float(data["peak"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"live": False}
    if (now or time.time()) - at > LEVEL_STALE:
        return {"live": False}
    return {"live": True, "peak": peak, "at": at}


def status(state):
    log_path = Path(state).resolve() / "logs" / "voice.log"
    tail, ignored, last_ignored = [], 0, None
    if log_path.is_file():
        # Background voices log a line each and would push everything else out of a plain
        # tail: keep the last lines that say something, and count the ignored ones.
        for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]:
            if "ignored another voice" in line:
                ignored, last_ignored = ignored + 1, line[:16]
            elif line.strip():
                tail.append(line)
        tail = tail[-8:]
    pid = _pid(state)
    voice = Path(state).resolve() / "voice"
    return {"listening": pid is not None, "pid": pid,
            "enrolled": (voice / "voiceprint.json").is_file(),
            "mic_tuned": (voice / "mic.json").is_file(),
            "autostart": _startup_file().is_file(),
            "log": str(log_path), "recent": tail,
            "ignored": {"count": ignored, "last": last_ignored}}


NOT_ENROLLED = ("No voiceprint on this machine, so every voice would be rejected. "
                "Run: jarvis voice enroll  (about a minute; each laptop needs its own)")


def _under_systemd_unit():
    """True when this process runs inside a systemd service (Linux), whose cgroup would
    take the listener down with it on restart."""
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("INVOCATION_ID"):
        return True
    try:
        return ".service" in Path("/proc/self/cgroup").read_text()
    except OSError:
        return False


def _detach_from_unit(command):
    """Inside a systemd unit, launch the listener in its own transient scope so restarting
    the unit (jarvis-cluster.service) does not kill it. Otherwise the command is unchanged."""
    if _under_systemd_unit() and shutil.which("systemd-run"):
        return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                f"--unit=jarvis-voice-{os.getpid()}-{int(time.time())}", *command]
    return command


def _supervised():
    """The Linux unit runs the listener itself (Type=simple), so systemd restarts it after
    a crash. Older units ran `voice start` as a oneshot and watched nothing."""
    if os.name == "nt" or not shutil.which("systemctl"):
        return False
    try:
        return "voice listen" in _startup_file().read_text(encoding="utf-8")
    except OSError:
        return False


def _wait_listening(state):
    for _ in range(60):  # model loading takes a few seconds
        time.sleep(0.5)
        if _pid(state):
            return {"started": True, **status(state)}
    return {"started": False, "note": "Listener did not come up; check the log.",
            **status(state)}


def start(state):
    if _pid(state):
        return {"started": False, **status(state)}
    if not (Path(state).resolve() / "voice" / "voiceprint.json").is_file():
        return {"started": False, "note": NOT_ENROLLED, **status(state)}
    if _supervised():
        _systemctl("reset-failed", UNIT_NAME)
        if not _systemctl("start", UNIT_NAME):
            return {"started": False, "note": f"systemctl --user start {UNIT_NAME} failed; "
                    f"see: journalctl --user -u {UNIT_NAME}", **status(state)}
        return _wait_listening(state)
    logs = Path(state).resolve() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
             if os.name == "nt" else 0)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    command = [sys.executable, "-m", "jarvis", "--state", str(Path(state).resolve()),
               "voice", "listen"]
    command = _detach_from_unit(command)
    with open(logs / "voice.log", "a", encoding="utf-8") as out:
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         creationflags=flags, start_new_session=os.name != "nt", env=env,
                         close_fds=True)
    return _wait_listening(state)


def stop(state):
    pid = _pid(state)
    if not pid:
        return {"stopped": False, "note": "Not running."}
    if _supervised() and _systemctl("is-active", "--quiet", UNIT_NAME):
        _systemctl("stop", UNIT_NAME)  # a plain kill would leave the unit to restart it
        return {"stopped": True, "pid": pid}
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
    """The unit runs `voice listen` in the foreground so systemd sees it die and restarts
    it. Exit 2 (no voiceprint) and 3 (another listener holds the mic) aren't retried."""
    path = _startup_file()
    if not enable:
        _systemctl("disable", UNIT_NAME)
        path.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        return {"autostart": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    state = Path(state).resolve()
    from .autostart import systemd_unit
    log = state / "logs" / "voice.log"
    path.write_text(systemd_unit(
        "JARVIS voice listener", [sys.executable, "-m", "jarvis", "--state", str(state), "voice", "listen"],
        comment="JARVIS: start the voice listener at login. `jarvis voice autostart off` removes it.",
        after="pipewire.service pipewire-pulse.service",
        service=["RestartPreventExitStatus=2 3",
                 # systemd's PATH lacks ~/.local/bin, where the claude CLI usually lives.
                 f'Environment="PATH={os.environ.get("PATH", "/usr/local/bin:/usr/bin")}"',
                 'Environment="PYTHONIOENCODING=utf-8" "PYTHONUNBUFFERED=1"',
                 f"StandardOutput=append:{log}", f"StandardError=append:{log}"]), encoding="utf-8")
    (state / "logs").mkdir(parents=True, exist_ok=True)
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
