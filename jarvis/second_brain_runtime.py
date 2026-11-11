"""Obsidian second brain, C2 runtime: background drainer, safe status and the `archive` CLI.

The drainer is one in-process thread (like consolidate.start) that calls
`second_brain_queue.drain` on a bounded interval with backoff. It never writes the vault itself
and never decides what is durable: the queue acks an event only after the archive revision is
verified published. This module only decides WHETHER and WHERE a drain may run.

Gates, re-checked every cycle (nothing is written when any fails):
  second_brain.enabled is true (explicit opt-in, default false), this PC is not a cluster node
  (the configured main only; there is NO lease or epoch fence yet, so this is not a
  single-writer guarantee across failover), vault.enabled is not false, the configured vault
  folder (existing per-OS vault.path resolver) is an existing directory, and it carries a
  sentinel (the archive index note or an `.obsidian` folder) so an unmounted mount point is never
  turned into a phantom vault. vault.write is never read or changed: interactive vault editing
  stays off.

Status is read-only and numeric: counts, ages, a reason code. No record bodies, ids or paths. It
never says "saved to Obsidian": `state` is `current` only when nothing is pending, held or
failing, a drain has succeeded, AND every published head this process could open through the
archive loader read back. A chunked note or any note up to 64 KiB is opened; a larger plain note
is not. Stage is C2 transitional: SQLite is still the source and authoritative reads/rebuild
(C3) are not cut over.
"""

import atexit
import importlib
import json
import math
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

STAGE = "c2-transitional"
INTERVAL_S, BATCH, MAX_BACKOFF_S = 30.0, 100, 900.0
CATCHUP_S = 1.0          # a full batch with more waiting drains again soon
DBS = ("memory.sqlite3", "history.sqlite3", "tasks.sqlite3")
FILE_STORES = ("plans", "runs", "knowledge")   # file-backed: no DB trigger sees them, so a bounded
FILES_STATE = "archive-files.json"             # periodic scoped sync keeps them from going stale
FILE_SYNC_S = 600.0

# Keys read from the queue's safe numeric status / drain report (first present wins).
_PENDING = ("pending", "archive_pending", "queued")
_HELD = ("held", "archive_held")
_ERRORS = ("errors", "archive_errors", "failed")
_OLDEST = ("oldest_age_s", "oldest_pending_age_s", "oldest_age", "oldest")
_LAST = ("last_success", "last_drained", "last_archived_at")
_ACKED = ("acked", "published", "archived", "drained", "written")
_REMAINING = ("remaining", "pending")


def settings(config):
    """Normalised second_brain settings. Only a literal true enables it."""
    raw = (config or {}).get("second_brain")
    raw = raw if isinstance(raw, dict) else {}

    def number(key, default, low, high):
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return min(max(float(value), low), high)
    interval = number("interval_s", INTERVAL_S, 1.0, 3600.0)
    return {"enabled": raw.get("enabled") is True, "interval_s": interval,
            "file_sync_s": number("file_sync_s", FILE_SYNC_S, 0.0, 86400.0),   # 0 turns it off
            "batch": int(number("batch", BATCH, 1, 500)),
            "max_backoff_s": number("max_backoff_s", MAX_BACKOFF_S, interval, 3600.0)}


def _state_key(state):
    return str(Path(state).expanduser().resolve())


def resolve(config, state=None):
    """Where a drain may run. Never creates anything. Returns {ok, reason, permanent, root}.

    permanent: the answer won't change without a config/role change, so no thread is kept."""
    if not settings(config)["enabled"]:
        return {"ok": False, "reason": "disabled", "permanent": True}
    try:
        from .topology import is_node
        if is_node(config):
            return {"ok": False, "reason": "not-main", "permanent": True}
    except Exception:  # an unreadable role is not "main": fail closed
        return {"ok": False, "reason": "role-unknown", "permanent": True}
    return vault_root(config)


def vault_root(config):
    """The configured vault folder if it is really there; same resolver the vault uses."""
    from .vault import vault_path
    vault = (config or {}).get("vault")
    vault = vault if isinstance(vault, dict) else {}
    if vault.get("enabled", True) is False:
        return {"ok": False, "reason": "vault-disabled", "permanent": True}
    path = vault_path(vault)
    if not path:
        return {"ok": False, "reason": "vault-not-configured", "permanent": True}
    root = Path(path).expanduser()
    try:
        if not root.is_dir():
            return {"ok": False, "reason": "vault-unavailable", "permanent": False}
        from .second_brain import AREA, INDEX_NOTE
        area = root.joinpath(*AREA)
        sentinel = (root / ".obsidian").is_dir() or any(
            (area / f"{INDEX_NOTE}{ext}").is_file() for ext in (".md",))
    except OSError:
        return {"ok": False, "reason": "vault-unavailable", "permanent": False}
    if not sentinel:
        return {"ok": False, "reason": "vault-sentinel-missing", "permanent": False}
    return {"ok": True, "reason": "", "permanent": False, "root": str(root.resolve())}


def _queue():
    return importlib.import_module(".second_brain_queue", __package__)


def _num(raw, keys):
    """First numeric (non-bool) value under `keys`, else None. Strings/objects are dropped."""
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and value >= 0):
                return value
    return None


def _drain_report(raw):
    """Safe numeric view of a drain report: {ok, busy, acked, baselined, held, failed, remaining}."""
    if not isinstance(raw, dict):
        return {"ok": False, "busy": False, "acked": 0, "baselined": 0, "held": 0, "failed": 1,
                "remaining": None}
    failed = _num(raw, ("failed", "errors")) or 0
    return {"ok": raw.get("ok") is True and not failed,
            "busy": raw.get("busy") is True, "acked": int(_num(raw, _ACKED) or 0),
            "baselined": int(_num(raw, ("baselined",)) or 0),
            "held": int(_num(raw, _HELD) or 0), "failed": int(failed),
            "remaining": _num(raw, _REMAINING)}


def _last_success(raw):
    numeric = _num(raw, _LAST)
    if numeric is not None:
        return numeric
    if isinstance(raw, dict):
        for key in _LAST:
            value = raw.get(key)
            if isinstance(value, str):
                try:
                    stamp = datetime.fromisoformat(value)
                    if stamp.tzinfo is not None:
                        return stamp.timestamp()
                except (ValueError, OverflowError):
                    pass
    return None


def _iso(stamp):
    """Epoch seconds -> UTC ISO-8601 (what status shows; None stays None)."""
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not math.isfinite(stamp):
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


def _archive_brain(root, state):
    """One reader/writer for a permitted sync. Each ledger's overflow cap is carried across;
    a store with no row stays at 0. File stores are unaffected (they never chunk)."""
    from .second_brain import SecondBrain
    policy = {}
    try:
        policy = _queue().overflow_policy(Path(state))
    except Exception:
        policy = {}
    return SecondBrain(root, overflow_by_store=policy)


def file_sync(config, state, root, brain=None):
    """Archive plans/runs/knowledge only (the stores no DB trigger covers) and record the outcome.

    Bounded: the file stores are small and never touch the DB snapshot path. Persists a counts-only
    summary to `archive-files.json` so status can say when this last worked, even across restarts."""
    brain = brain or _archive_brain(root, state)
    rep = brain.sync(state, stores=FILE_STORES)
    summary = {"at": _iso(time.time()), "ok": bool(rep.get("ok")) and not rep.get("failures"),
               "written": rep.get("written", 0), "unchanged": rep.get("unchanged", 0),
               "skipped": len(rep.get("skipped") or []), "failures": len(rep.get("failures") or []),
               "error": _code(rep.get("error"))}
    try:
        from .fsio import atomic_write
        atomic_write(Path(state) / FILES_STATE, json.dumps(summary))
    except OSError:
        pass
    return summary


def _files_state(state):
    try:
        data = json.loads((Path(state) / FILES_STATE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _has_state(state):
    return any((Path(state) / name).is_file() for name in DBS)


_RUNNING, _RUNNING_LOCK = {}, threading.Lock()
_ATEXIT = []


class Drainer:
    """One bounded background drain loop for one state dir."""

    def __init__(self, config, state, log=None, queue=None, interval=None, batch=None,
                 max_backoff=None, first_delay=None, file_sync_s=None, files=None):
        cfg = settings(config)
        self.file_sync_s = cfg["file_sync_s"] if file_sync_s is None else float(file_sync_s)
        self.files = files            # injectable (state, root) -> summary, for tests
        self._files_at = None
        self.config, self.state, self.log = config, Path(state).expanduser(), log
        self.queue = queue
        self.interval = cfg["interval_s"] if interval is None else float(interval)
        self.batch = cfg["batch"] if batch is None else int(batch)
        self.max_backoff = cfg["max_backoff_s"] if max_backoff is None else float(max_backoff)
        self.first_delay = min(self.interval, 5.0) if first_delay is None else float(first_delay)
        self.stopping, self.thread, self.refs = threading.Event(), None, 0
        self._configured = False
        self.info = {"attempts": 0, "failures": 0, "drained_total": 0, "last_attempt": None,
                     "last_success": None, "last_error": None, "reason": "starting"}

    # -- one synchronous cycle (the thread loop and tests both call this) -----------------
    def cycle(self):
        """Run one drain attempt. Returns seconds until the next one, or None to stop."""
        info = self.info
        info["attempts"] += 1
        info["last_attempt"] = time.time()
        gate = resolve(self.config, self.state)
        if not gate["ok"] and gate["permanent"]:
            info["reason"] = gate["reason"]
            return None
        # Capture must be armed even while the vault is temporarily offline.
        if not self.prime():
            return self._failed(info["last_error"] or "capture-unavailable")
        if not gate["ok"]:
            return self._failed(gate["reason"])
        try:
            queue = self.queue or _queue()
        except ImportError:
            return self._failed("queue-unavailable")
        try:
            report = _drain_report(queue.drain(self.state, gate["root"], limit=self.batch))
        except Exception as exc:  # class only: messages can carry paths or record text
            return self._failed(type(exc).__name__)
        info["reason"] = ""
        if report["busy"]:  # another process holds the drain lock: fine, try later
            return self.interval
        if not report["ok"]:
            info["drained_total"] += report["acked"]
            return self._failed("drain-failed")
        info["failures"], info["last_error"] = 0, None
        info["drained_total"] += report["acked"]
        info["last_success"] = time.time()
        self._files_due(gate["root"])
        if (report["acked"] or report["baselined"]) and (report["remaining"] or 0) > 0:
            return min(self.interval, CATCHUP_S)
        return self.interval

    def _files_due(self, root):
        """Every `file_sync_s`: scoped file-store sync. A failure shows in status, never stops drain."""
        if not self.file_sync_s:
            return
        now = time.monotonic()
        if self._files_at is not None and now - self._files_at < self.file_sync_s:
            return
        self._files_at = now
        try:
            (self.files or (lambda st, rt: file_sync(self.config, st, rt)))(self.state, root)
        except Exception as exc:  # class only
            self.info["files_error"] = type(exc).__name__

    def prime(self):
        """Arm transactional capture before accepting new requests, independently of drain."""
        if self._configured:
            return True
        try:
            report = (self.queue or _queue()).configure(self.state, enabled=True)
            if isinstance(report, dict) and report.get("ok") is False:
                raise RuntimeError("capture-unavailable")
            self._configured = True
            return True
        except Exception as exc:
            self.info["last_error"] = "queue-unavailable" if isinstance(exc, ImportError) else type(exc).__name__
            return False

    def _failed(self, code):
        info = self.info
        info["failures"] += 1
        info["last_error"] = code
        info["reason"] = code
        if self.log and info["failures"] in (1, 10, 100):
            try:
                self.log(f"second brain archive drain: {code} (failure {info['failures']})")
            except Exception:
                pass
        return min(self.max_backoff, self.interval * (2 ** min(info["failures"], 12)))

    def _loop(self):
        delay = self.first_delay
        while not self.stopping.wait(delay):
            try:
                delay = self.cycle()
            except Exception:  # nothing may kill the loop; treat as a failed cycle
                delay = self._failed("internal-error")
            if delay is None:
                break
        with _RUNNING_LOCK:
            if _RUNNING.get(_state_key(self.state)) is self:
                del _RUNNING[_state_key(self.state)]

    def begin(self):
        self.stopping.clear()
        self.thread = threading.Thread(target=self._loop, name="jarvis-second-brain-drain",
                                       daemon=True)
        self.thread.start()
        return self

    def stop(self, timeout=10.0, force=False):
        """Release one reference; the thread ends (joined) when the last holder lets go."""
        with _RUNNING_LOCK:
            self.refs = 0 if force else max(0, self.refs - 1)
            if self.refs:
                return False
            self.stopping.set()
            if not self.alive and _RUNNING.get(_state_key(self.state)) is self:
                del _RUNNING[_state_key(self.state)]
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return True

    @property
    def alive(self):
        return bool(self.thread and self.thread.is_alive())


def start(config, state, log=None, queue=None, **overrides):
    """Start (or share) this state's drainer. None unless every permanent gate passes.

    A transient problem (vault not mounted yet) still starts the thread; it writes nothing
    until the gate passes. Each start must be paired with `stop(drainer)`."""
    gate = resolve(config, state)
    if not gate["ok"] and gate["permanent"]:
        return None
    key = _state_key(state)
    with _RUNNING_LOCK:
        live = _RUNNING.get(key)
        if live is not None and live.alive:
            if live.stopping.is_set():
                return None  # a timed-out stop must not overlap a replacement worker
            live.refs += 1
            return live
        drainer = Drainer(config, state, log=log, queue=queue, **overrides)
        drainer.refs = 1
        _RUNNING[key] = drainer
        if not _ATEXIT:
            _ATEXIT.append(True)
            atexit.register(stop_all)
        drainer.prime()
        return drainer.begin()  # publish a started worker before another start can inspect it


def stop(drainer, timeout=10.0):
    return drainer.stop(timeout) if drainer is not None else False


def stop_all(timeout=5.0):
    with _RUNNING_LOCK:
        drainers = list(_RUNNING.values())
    for drainer in drainers:
        drainer.stop(timeout, force=True)


def running(state):
    with _RUNNING_LOCK:
        live = _RUNNING.get(_state_key(state))
    return live if live is not None and live.alive else None


# -- status ------------------------------------------------------------------------------
def status(config, state, queue=None):
    """Read-only, numeric, body-free. Creates no files, DBs or threads."""
    cfg = settings(config)
    gate = resolve(config, state)
    out = {"enabled": cfg["enabled"], "stage": STAGE, "canonical": False,
           "lease_fenced": False, "writer": "configured-main-only",
           "gate": "ok" if gate["ok"] else gate["reason"]}
    drainer = running(state)
    out["running_here"] = drainer is not None
    local = drainer.info if drainer else {}
    out["last_attempt"] = local.get("last_attempt")
    out["last_error"] = local.get("last_error")
    out["drain_failures"] = local.get("failures", 0)
    out["drained_total"] = local.get("drained_total", 0)
    counts = {}
    out["queue"] = "no-state"
    if _has_state(Path(state)):  # never let status create an empty store
        try:
            counts = (queue or _queue()).status(Path(state))
            out["queue"] = "ok" if isinstance(counts, dict) and counts.get("ok") is True else "unreadable"
            counts = counts if isinstance(counts, dict) else {}
        except ImportError:
            out["queue"] = "unavailable"
        except Exception as exc:
            out["queue"] = f"error:{type(exc).__name__}"
    out["archive_pending"] = _num(counts, _PENDING)
    out["archive_held"] = _num(counts, _HELD)
    out["archive_errors"] = _num(counts, _ERRORS)
    out["capture_enabled"] = counts.get("enabled") is True
    out["capture_complete"] = counts.get("capture_complete") is True
    out["archive_unreconciled"] = _num(counts, ("unreconciled",))
    out["archive_source_gone"] = _num(counts, ("source_gone",))
    out["oldest_pending_age_s"] = _num(counts, _OLDEST)
    last = _last_success(counts) or local.get("last_success")
    out["last_success"] = _iso(last)
    drained = None
    if isinstance(counts.get("last_drain"), str):
        drained = _last_success({"last_success": counts["last_drain"]})
    out["last_drain"] = _iso(drained) or _iso(local.get("last_success"))
    out["archive_baseline_pending"] = _num(counts, ("baseline_pending",))
    out["archive_baseline_seeded"] = counts.get("baseline_seeded") is True
    out["archive_excluded"] = _num(counts, ("excluded",))
    out["archive_rows"] = _num(counts, ("rows",))
    out["archive_heads"] = _num(counts, ("heads",))
    # overflow (chunked) publication: owner policy in bytes (0 = off) and the largest event held
    # for size, so a held head can be explained and the policy sized; numbers only
    out["archive_overflow_max_bytes"] = _num(counts, ("overflow_max_bytes",)) or 0
    out["archive_held_oversize_bytes"] = _num(counts, ("held_oversize_bytes",)) or 0
    out["archive_chunk_checked"] = 0
    out["archive_chunk_damaged"] = 0
    if out["enabled"] and out["gate"] == "ok" and out["queue"] == "ok":
        try:
            checked, damaged = _published_integrity(state, gate["root"])
            out["archive_chunk_checked"] = checked
            out["archive_chunk_damaged"] = damaged
        except Exception:
            out["archive_integrity"] = "unverified"  # class only; do not claim the heads read back
    files = _files_state(state)
    out["files"] = {"last_sync": files.get("at"), "ok": files.get("ok"),
                    "failures": files.get("failures"), "skipped": files.get("skipped")} if files else None
    has_files = any((Path(state) / name).is_dir() for name in FILE_STORES)
    out["files_stale"] = bool(cfg["file_sync_s"]) and has_files and (
        not files or files.get("ok") is not True)
    out["state"] = _verdict(out)
    return out


def _verdict(out):
    """One word, never an overclaim: `current` needs zero backlog AND a proven success."""
    if not out["enabled"]:
        return "disabled"
    if out["gate"] != "ok":
        return "blocked"
    if out["queue"] not in ("ok", "no-state"):
        return "unverified"
    if out["archive_held"]:
        return "held"
    if out["archive_pending"]:
        return "pending"
    if out["archive_errors"] or out["drain_failures"]:
        return "error"
    if out["archive_unreconciled"] or out["archive_source_gone"] or out["archive_baseline_pending"]:
        return "pending"
    if not out["capture_complete"] or (out["capture_enabled"] and not out["archive_baseline_seeded"]):
        return "unverified"
    if out.get("archive_integrity") == "unverified":
        return "unverified"
    if out.get("archive_chunk_damaged"):
        return "error"   # a published head did not read back; the queue being empty is not enough
    if out["files_stale"]:
        return "partial"   # database stores are current, plans/runs/knowledge have not been verified
    if ((out["last_drain"] or out["last_success"]) and out["archive_pending"] == 0
            and out["archive_held"] == 0 and out["archive_errors"] == 0):
        return "current"
    return "unverified"


def _published_integrity(state, root):
    """Bounded read-back of acknowledged heads. Counts only. Each opened note goes through
    SecondBrain.published_readable (_load). Heads left unopened are not counted as checked
    and cannot by themselves cancel `current`."""
    from .second_brain import SecondBrain
    brain = SecondBrain(root)
    checked = damaged = 0
    for store, kind, key, digest in _queue().published_heads(Path(state)):
        verdict = brain.published_readable(store, kind, key, digest)
        if verdict is None:
            continue
        checked += 1
        if not verdict:
            damaged += 1
    return checked, damaged


# -- CLI (`jarvis archive ...`); returns (result, exit_code), counts and codes only ------
def _sync_view(rep):
    skipped = {}
    for item in rep.get("skipped") or []:
        reason = item.get("reason") if isinstance(item, dict) else None
        if isinstance(reason, str) and reason.startswith("oversize"):
            reason = "oversize"  # "oversize: N bytes" is the same skip, not a free-text "other"
        elif reason != "secret-like":
            reason = "other"  # free text stays out
        skipped[reason] = skipped.get(reason, 0) + 1
    rows = {name: {"status": info.get("status"), "rows": info.get("rows")}
            for name, info in (rep.get("stores") or {}).items() if isinstance(info, dict)}
    return {"ok": bool(rep.get("ok")), "complete": bool(rep.get("complete")),
            "dry_run": bool(rep.get("dry_run")), "written": rep.get("written", 0),
            "unchanged": rep.get("unchanged", 0), "repaired": len(rep.get("repaired") or []),
            "skipped": skipped, "failures": len(rep.get("failures") or []),
            "tombstoned_skipped": rep.get("tombstoned_skipped", 0),
            "absent_from_source": rep.get("absent_from_source", 0),
            "unclassified_state": len(rep.get("unclassified_state") or []),
            "stores": rows, "error": _code(rep.get("error"))}


def _code(text):
    """Reason prefix only (`vault-unavailable`), never the free text after it."""
    return str(text).split(":", 1)[0][:60] if text else None


def _refuse(code):
    return {"ok": False, "error": code}, 1


def cli(args, config, state, queue=None):
    action = args.action
    if action == "status":
        return status(config, state, queue=queue), 0
    if action == "overflow":   # ledger policy only: needs no vault
        return _cli_overflow(args, state, queue)
    gate = vault_root(config)
    if action == "validate" and gate["reason"] == "vault-sentinel-missing":
        gate = {**gate, "ok": True, "root": str(_root_of(config))}  # reads only: no mount risk
    if not gate["ok"]:
        return _refuse(gate["reason"])
    root = gate["root"]
    from .second_brain import SecondBrain, SecondBrainError
    try:
        if action == "sync":
            files_only = bool(getattr(args, "files", False))
            stores = FILE_STORES if files_only else None
            # A snapshot must not leapfrog queued revisions and then be overwritten by an
            # older event. C2 database publication goes exclusively through ordered drain; the
            # file-backed stores (plans/runs/knowledge) have no queue, so `--files` stays allowed.
            if args.apply and not files_only:
                capture = (queue or _queue()).status(Path(state))
                if capture.get("enabled") or any(capture.get(k) for k in ("pending", "held", "unreconciled")):
                    return _refuse("capture-active-use-drain")
            if args.apply and files_only:
                summary = file_sync(config, state, root)
                return {**summary, "scope": "files"}, 0 if summary["ok"] else 1
            rep = _archive_brain(root, state).sync(state, stores=stores, dry_run=not args.apply)
            view = _sync_view(rep)
            return view, 0 if view["ok"] and not view["failures"] else 1
        if action == "verify":
            return _cli_verify(state, root)
        if action == "drain":
            return _cli_drain(args, state, root, queue)
        if action == "validate":
            rep = SecondBrain(root).validate()
            return ({k: rep.get(k) for k in ("ok", "records", "revisions", "error_count",
                                             "human_edited_count", "stale_tmp",
                                             "link_anomalies")}, 0 if rep.get("ok") else 1)
        if action == "restore":
            return _cli_restore(args, state, root)
    except SecondBrainError as exc:
        return _refuse(_code(exc) or "archive-error")
    except OSError as exc:
        return _refuse(type(exc).__name__)
    return _refuse("unknown-action")


def _root_of(config):
    from .vault import vault_path
    return Path(vault_path((config or {}).get("vault") or {}) or "").expanduser().resolve()


def _cli_verify(state, root):
    """Read-only C3 check: archive structure (validate) + parity against the live data. Counts only.
    `ok` means nothing is missing, stale, skipped or failing; it does NOT make the vault canonical.
    Parity uses each ledger's overflow policy, so an in-policy chunked record is not an oversize skip."""
    brain = _archive_brain(root, state)
    check = brain.validate()
    parity = brain.parity(state)
    out = {"ok": bool(check.get("ok") and parity["in_sync"]), "canonical": False,
           "archive": {k: check.get(k) for k in ("ok", "records", "revisions", "error_count",
                                                 "human_edited_count", "stale_tmp")},
           "parity": {k: parity[k] for k in ("in_sync", "stores", "vault_only", "tombstoned", "failures")},
           "error": _code(parity.get("error"))}
    return out, 0 if out["ok"] else 1


def _cli_drain(args, state, root, queue):
    limit = max(1, min(int(args.limit or BATCH), 500))
    try:
        queue = queue or _queue()
    except ImportError:
        return _refuse("queue-unavailable")
    if getattr(args, "rescan", False):
        try:
            queue.rescan(Path(state))
        except Exception as exc:
            return _refuse(type(exc).__name__)
    try:
        report = _drain_report(queue.drain(Path(state), root, limit=limit))
    except Exception as exc:
        return _refuse(type(exc).__name__)
    return report, 0 if report["ok"] else 1


def _cli_overflow(args, state, queue):
    """`jarvis archive overflow [--mib N]`: show, or (explicit owner operation) set, the chunked
    overflow policy for records over the 16 MiB note bound. 0 turns it off. Counts only."""
    try:
        queue = queue or _queue()
    except ImportError:
        return _refuse("queue-unavailable")
    mib = getattr(args, "mib", None)
    if mib is not None:
        try:
            result = queue.set_overflow(Path(state), int(float(mib) * 1024 * 1024))
        except ValueError:
            return _refuse("overflow-limit-out-of-range")
        except Exception as exc:
            return _refuse(type(exc).__name__)
        if not result.get("ok"):
            return _refuse("overflow-not-saved")
    counts = queue.status(Path(state)) if _has_state(Path(state)) else {}
    out = {"ok": True, "overflow_max_bytes": _num(counts, ("overflow_max_bytes",)) or 0,
           "held_oversize_bytes": _num(counts, ("held_oversize_bytes",)) or 0,
           "held_oversize": int(((counts.get("held_reasons") or {}).get("oversize")) or 0)}
    return out, 0


def _cli_restore(args, state, root):
    if not args.target:
        return _refuse("restore-needs-target")
    target = Path(args.target).expanduser().resolve()
    live, vault = Path(state).expanduser().resolve(), Path(root).resolve()
    for other in (live, vault):
        if target == other or other in target.parents or target in other.parents:
            return _refuse("restore-target-overlaps-live-data")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        return _refuse("restore-target-not-empty")
    from .second_brain import SecondBrain
    rep = SecondBrain(root).restore(target)
    return ({"ok": bool(rep.get("ok")), "restored": rep.get("restored"),
             "tombstoned": rep.get("tombstoned", 0), "errors": len(rep.get("errors") or []),
             "partial": bool(rep.get("partial")),
             "nonterminal_tasks": rep.get("nonterminal_tasks", 0)},
            0 if rep.get("ok") else 1)
