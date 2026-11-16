"""Read-only authority reporting, distinct from configured transport topology.

An authority answer is diagnostic only: it never installs a successor route or
borrows another hub's credential. A managed PC without a fresh ready lease has
no reported MAIN, even when its old configured Hub is still reachable.
"""
from pathlib import Path


def report(config, state, *, base=None):
    from .integration import continuity_state
    from .console import _fetch
    from jarvis.topology import identity

    configured = (config.get("cluster") or {}).get("main_device")
    local = identity(config)[0]
    try:
        managed = continuity_state(state) == "managed"
    except Exception:
        return {"configured_main_device": configured, "main_device": None,
                "role": "unknown", "authority": {"state": "unavailable"}}
    if not managed:
        return {"main_device": configured, "role": "main" if local == configured else "node"}
    raw = (config.get("continuity") or {}).get("operator_config")
    if not isinstance(raw, str) or not raw:
        authority = {"state": "not_set_up"}
    else:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = Path(base or Path(state).resolve().parent) / path
        # Do not reuse the console's 30-second cache: a lease can expire sooner.
        authority = _fetch(path, None, 3.0)
    ready = (authority.get("state") == "ok" and authority.get("mode") == "V2_ACTIVE"
             and authority.get("lease_valid") is True and authority.get("main_ready") is True)
    main = authority.get("main") if ready else None
    return {"configured_main_device": configured, "main_device": main,
            "role": ("main" if local == main else "node") if ready else "unknown",
            "authority": authority}
