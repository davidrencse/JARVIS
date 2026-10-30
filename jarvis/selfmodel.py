"""JARVIS.md, JARVIS's self-model: loaded into every conversation and every interpretation.

David writes JARVIS.md (repo root); JARVIS never edits it. The brief adds live facts from
jarvis.config.json (each machine, which is main and default) after the file, so the model
always has the true machines even if the file goes stale, and any contradiction between the
two is named: the config wins.
"""

import re
from pathlib import Path

FILE = "JARVIS.md"
MAX_CHARS = 6000
# JARVIS.md's "only act on speech that starts with Jarvis" is enforced by the listener, which
# strips the name before the text gets here; read literally, the model refused requests
# ("Say 'Jarvis, how many apps are on my Dell?' so I can check.").
ADDRESSED = ("Every request you receive is already addressed to JARVIS: the listener checked "
             "the wake word and David's voice and removed \"Jarvis\" from the text, and typed "
             "requests need none. Act on it; never ask him to say \"Jarvis\" first.")


def path(base):
    return Path(base) / FILE


def machine_facts(config):
    """One line per configured machine, from config: id, label, role, aliases."""
    devices = config.get("devices") or {}
    main = (config.get("cluster") or {}).get("main_device")
    lines = []
    for key, device in devices.items():
        role = "main PC and default for actions" if key == main else "node"
        aliases = ", ".join(device.get("aliases") or [])
        lines.append(f"- {key}: {device.get('label') or key}; {role}"
                     + (f"; also called {aliases}" if aliases else ""))
    return lines


def mismatches(text, config):
    """Claims in JARVIS.md that contradict the config (which wins)."""
    main = (config.get("cluster") or {}).get("main_device")
    devices = set(config.get("devices") or {})
    found = []
    for line in text.splitlines():
        lowered = line.lower()
        if "main pc" not in lowered:
            continue
        named = {d for d in devices if re.search(rf"\b{re.escape(d)}\b", lowered)}
        if named and main and main not in named:
            found.append(f"JARVIS.md calls {', '.join(sorted(named))} the main PC, "
                         f"but jarvis.config.json says {main}.")
    return found


def brief(config, base):
    """JARVIS.md plus the config's live machine facts, capped. Never raises."""
    try:
        text = path(base).read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    parts = [text[:MAX_CHARS]] if text else []
    facts = machine_facts(config)
    if facts:
        parts.append("Configured machines right now (authoritative):\n" + "\n".join(facts))
    try:  # what this computer really is, detected, not assumed from a label
        if not parts:  # nothing configured, nothing to brief
            raise LookupError
        from .platforms import label
        from .topology import enabled, identity
        here = identity(config)[0] if enabled(config) else None
        parts.append(f"This computer{f' ({here})' if here else ''} runs {label()}.")
    except Exception:
        pass
    if parts:
        parts.append(ADDRESSED)
    problems = mismatches(text, config) if text else []
    if problems:
        parts.append("Note: " + " ".join(problems) + " Follow the configuration.")
    return "\n\n".join(parts)
