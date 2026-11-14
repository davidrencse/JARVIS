"""Drift alert: an online node that runs other code than the release for too long.

Nodes pull a signed release on their own after a green push to origin/main, so a node that
stays behind for a while is stuck (failed pull, rolled-back code, a dead agent). `drift()` is
pure: given the nodes, the release code and a clock it returns the new state and what to say.
`check(hub)` (called from Hub._keep_up) persists that state and notifies once per node and
release; `notes(state)` feeds `jarvis cluster doctor`. Offline nodes never alert: they pull on
reconnect, so their clock restarts when they come back.
"""

import time
from pathlib import Path

LIMIT = 30 * 60          # seconds an online node may differ from the release before we say so
STATE_FILE = "cluster-drift.json"


def drift(nodes, release, now, state, limit=LIMIT):
    """-> (new_state, report). state = {"since": {node: first_seen}, "told": {node: release}}.
    report = {"behind": [{"id", "minutes", "alert"}], "offline": [ids that differ], "cleared": [ids]}.
    `alert` is true once per node and release, when `limit` has passed."""
    state = state if isinstance(state, dict) else {}
    since, told = dict(state.get("since") or {}), dict(state.get("told") or {})
    report = {"behind": [], "offline": [], "cleared": []}
    if not release:
        return {"since": {}, "told": {}}, report
    seen = set()
    for node in nodes:
        name = node.get("id")
        if not name:
            continue
        seen.add(name)
        if node.get("code") == release:
            if name in since or name in told:
                report["cleared"].append(name)
            since.pop(name, None)
            told.pop(name, None)
        elif not node.get("online"):
            report["offline"].append(name)
            since.pop(name, None)       # clock restarts when it reconnects
        else:
            first = since.setdefault(name, now)
            if now - first >= limit:
                alert = told.get(name) != release
                told[name] = release
                report["behind"].append({"id": name, "minutes": int((now - first) // 60), "alert": alert})
    for gone in set(since) - seen:
        since.pop(gone)
        told.pop(gone, None)
    return {"since": since, "told": told}, report


def _path(state):
    return Path(state) / STATE_FILE


def _load(state):
    from .fsio import read_json
    value = read_json(_path(state), {})
    return value if isinstance(value, dict) else {}


def check(hub, now=None):
    """Hub step: update the persisted clocks and notify once per drifted node."""
    from .fsio import atomic_write
    from .release import current
    import json
    release = current(hub.state).get("code")
    new, report = drift(hub.broker.nodes(), release, now or time.time(), _load(hub.state))
    if new != _load(hub.state):
        atomic_write(_path(hub.state), json.dumps(new))
    for item in report["behind"]:
        if item["alert"]:
            from .confirm import notify
            notify(f"Node {item['id']} behind release for {item['minutes']} min")
    return report


def notes(state, nodes, release, now=None):
    """{node: text} for `jarvis cluster doctor`: online nodes behind for a while, and offline
    nodes that differ. Read-only."""
    _, report = drift(nodes, release, now or time.time(), _load(state))
    out = {item["id"]: f"node {item['id']} behind release for {item['minutes']} min" for item in report["behind"]}
    out.update({name: "offline, will pull" for name in report["offline"]})
    return out
