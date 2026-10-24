"""Decide how much machinery a request needs. Zero-cost heuristics; no model call.

simple  → one Claude/Codex CLI run on a cheap tier
medium  → needs a website/account: owner's own API adapter, else a browser agent
complex → orchestration: lead planner + specialist hierarchy
"""

import re
from pathlib import Path

COMPLEX = re.compile(
    r"\b(build|create|develop|ship|launch|implement|refactor|migrate|redesign|architect)\b.*"
    r"\b(app|system|platform|project|service|pipeline|product|dashboard|backend|frontend|"
    r"website|game|engine|infrastructure|feature set|mvp)\b|"
    r"\b(production|end[- ]to[- ]end|full[- ]stack|from scratch|multi[- ]agent|large project)\b",
    re.I | re.S)
WEB = re.compile(
    r"https?://|\bwww\.|\b(instagram|twitter|x\.com|tiktok|linkedin|reddit|facebook|youtube|"
    r"gmail|inbox|discord|telegram|notion|canvas|brightspace|website|web ?page|browser|log ?in|"
    r"sign ?in|my account|scan (?:my|the)|check (?:my|the) (?:dm|messages|feed|notifications)|"
    r"e-?mails?|mail|outlook|calendar|grok(?:\s*bot)?|"
    r"(?:my|the) (?:dms?|messages|texts|orders?|notifications|subscriptions?|bank|bills?))\b",
    re.I)
# Integrations that can't take a task and hand an answer back.
NO_REPLY = (None, "unconfigured")


def classify(text, override=None):
    if override:
        if override not in ("simple", "medium", "complex"):
            raise ValueError("tier must be simple, medium, or complex.")
        return {"tier": override, "reasons": ["owner override"]}
    reasons = []
    if COMPLEX.search(text):
        reasons.append("multi-part build or production work")
    if len(text) > 600:
        reasons.append("long brief")
    if reasons:
        return {"tier": "complex", "reasons": reasons}
    if WEB.search(text):
        return {"tier": "medium", "reasons": ["needs a website or logged-in account"]}
    return {"tier": "simple", "reasons": ["single self-contained request"]}


def grok_first(text, config):
    """Grok Bot is the orchestrator for work that needs David's accounts or the web:
    such requests go straight to it instead of waiting on the brain to pick a tool.
    routing.orchestrator: "grokbot" (default) or "brain"; routing.grok_scope: "accounts"
    (default: email, logged-in sites, the web, anything naming Grok) or "all"."""
    routing = config.get("routing", {})
    agent = config.get("personal_agents", {}).get("grokbot", {})
    if (routing.get("orchestrator", "grokbot") != "grokbot"
            or agent.get("integration") != "window_automation"
            or not config.get("actions", {}).get("grokbot", True)):
        return False
    return routing.get("grok_scope", "accounts") == "all" or bool(WEB.search(text))


def pick_adapter(text, config):
    """For medium work: the owner's own API if one claims this site, else a browser agent."""
    lowered = text.lower()
    for adapter in config.get("adapters", {}).get("apis", []):
        if any(site.lower() in lowered for site in adapter.get("sites", [])):
            return {"kind": "api", **adapter}
    for name in config.get("adapters", {}).get("browser_agents", []):
        agent = config.get("personal_agents", {}).get(name, {})
        if agent.get("integration") not in NO_REPLY:
            return {"kind": "browser_agent", "name": name, **agent}
    # Fallback that works today: a read-only Claude run with web search/fetch.
    return {"kind": "cli_web", "name": "claude-web"}


def specialists(base):
    """Catalog of .claude/agents/*.md: name, description, power, model, instructions."""
    catalog = {}
    for path in sorted((Path(base) / ".claude" / "agents").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = re.match(r"---\r?\n(.*?)\r?\n---\r?\n(.*)", text, re.S)
        if not match:
            continue
        meta = {k: v.strip() for k, v in re.findall(r"^(\w+):[ \t]*(.+)$", match.group(1), re.M)}
        if "name" in meta:
            catalog[meta["name"]] = {"description": meta.get("description", ""),
                                     "power": meta.get("power", "mid"),
                                     "model": meta.get("model"),
                                     "instructions": match.group(2).strip()}
    return catalog
