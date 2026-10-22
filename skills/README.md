# Project-local architecture skills

Downloaded and reviewed on 2026-10-21. These are reference skills, not Python runtime dependencies. Their contents and upstream MIT license notices are preserved. No downloaded executable scripts were run.

| Skill | Why selected | Pinned upstream source |
| --- | --- | --- |
| [System design](system-design/SKILL.md) | Requirements, capacity estimates, reliability and architecture tradeoffs. Registry showed approximately 6,000 installs; repository had 2,478 stars at inspection. | [wondelai/skills, c172996](https://github.com/wondelai/skills/tree/c172996495bed0fcd26896a9416b2093fd7073f0/system-design) |
| [Architecture patterns](architecture-patterns/SKILL.md) | Module boundaries, dependency direction and testable adapters, including Python examples. Registry showed approximately 24,000 installs; repository had 41,426 stars at inspection. | [wshobson/agents, 0874a27](https://github.com/wshobson/agents/tree/0874a27b17a22e2b7c2e2bcd725768e0803cad06/plugins/backend-development/skills/architecture-patterns) |

Popularity was a selection signal, not a security guarantee or proof these are universally the best skills. The actual skill text was reviewed before use. Their general principles were applied to JARVIS's single-owner local workload; distributed-service recipes are not requirements to add infrastructure.

Registries: [system-design](https://www.skills.sh/wondelai/skills/system-design), [architecture-patterns](https://www.skills.sh/wshobson/agents/architecture-patterns).

## Availability

Both skills and all of their local references are downloaded under this writable project folder. The session's global and project agent skill directories are read-only. Consequently this is **not a global installation or automatic Codex skill registration**. They remain available as local references on subsequent turns; ask the agent to read the linked `SKILL.md`.

The standard Skills CLI and installer could not reach their download endpoints from this environment. The downloads used the permitted GitHub CLI, pinned revisions, and the Contents API. See `sources.json` for source revisions and file SHA-256 checksums.

To register globally later from a regular terminal:

```powershell
npx skills add wondelai/skills@system-design -g -y
npx skills add wshobson/agents@architecture-patterns -g -y
```

Those commands install the upstream version available at that time; the copies here remain pinned. Globally registered skills become available to Codex on a subsequent turn.
