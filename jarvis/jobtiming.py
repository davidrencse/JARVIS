"""Step timing for one node job: where a slow job spends its time.

The node agent calls start() when a job arrives and finish() after its result is sent.
Code on the job's path calls mark(name) between steps. Marks from other threads, or
outside a job, are ignored. Each job appends one JSON line to logs/node-timing.log.
"""
import json
import os
import threading
import time

_local = threading.local()
_MAX_LOG = 1_000_000  # bytes; the log starts over past this


def start():
    _local.marks = [("start", time.monotonic())]


def mark(name):
    marks = getattr(_local, "marks", None)
    if marks is not None and len(marks) < 200:
        marks.append((str(name)[:60], time.monotonic()))


def finish(state, **extra):
    """Write this thread's job timing (ms per step, in order) and clear it. Never raises."""
    marks = getattr(_local, "marks", None)
    _local.marks = None
    if not marks:
        return None
    try:
        steps = [[name, round((at - marks[i][1]) * 1000)] for i, (name, at) in enumerate(marks[1:])]
        line = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "total_ms": round((marks[-1][1] - marks[0][1]) * 1000), "steps": steps, **extra}
        folder = os.path.join(str(state), "logs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "node-timing.log")
        mode = "w" if os.path.exists(path) and os.path.getsize(path) > _MAX_LOG else "a"
        with open(path, mode, encoding="utf-8") as out:
            out.write(json.dumps(line) + "\n")
        return line
    except Exception:
        return None
