# JARVIS architecture

## Main pipelines (2026-10-20)

- **A (voice):** mic → speaker check → Whisper (GPU) → JARVIS → **ChatGPT** → streamed
  sentences → Kokoro (local) → speakers.
- **B (text):** `jarvis chat` / `jarvis ask` → JARVIS → **ChatGPT** → printed + Kokoro.

ChatGPT runs through the Codex CLI's `app-server` on David's ChatGPT Plus login
(`jarvis/chatgpt.py`): `gpt-6-luna`, low effort, one warm process, ~1 s answers. It
uses plan usage limits, never API credits; API-key env vars are stripped from the
child. Driving chatgpt.com directly is not done (OpenAI ToS, bot detection).
`routing.brain: "claude"` switches back to the Claude Haiku fast lane.
`routing.auto_escalate: false` keeps all conversation on the brain; the web and
orchestration tiers below run only with an explicit `--tier`.

```
 you ──text──▶ jarvis ask ─┐                    ┌─▶ memory heap (everything)
 you ──voice─▶ jarvis voice listen               │
     mic → VAD → is it David? → whisper → "Jarvis…"│
                           ▼                      │
                    router (free heuristics) ─────┤
        simple ─────────┼──── medium ──────┼───── complex
  one CLI run, cheap  │  own site API     │  lead planner (opus, high)
  tier (haiku/low)    │  → Grok/Muse      │  → specialist subtasks, each at
                      │  → claude web     │    the lowest viable power tier
                      ▼                   ▼  → specialists spawn subagents
                 reply → memory → streamed sentences → Kokoro (local) / cache
```

## Actions: apps and the browser (2026-10-21)

Requests that sound like actions ("open Spotify", "go to github and ...", "use the
browser to ...", anything with a link) go to a second ChatGPT conversation with tools;
everything else stays on the lean chat brain. Both run on the ChatGPT login.

| Tool | Runs where | Guardrail |
|---|---|---|
| `open_app`, `list_apps` | inside JARVIS (Codex dynamic tools); Start-menu apps via `shell:AppsFolder` | launches only; no shell, no killing processes |
| `open_url` | inside JARVIS, default browser | http(s) only |
| `browser_*` (22 tools) | Microsoft Playwright MCP 0.0.83, Chrome, profile `.jarvis/browser-profile` | each state-changing call is approved by JARVIS (`approve_browser`): Playwright only, http(s) navigation only (no `file://`), file upload and page-script tools disabled |

The model is told never to buy, send, post, delete or submit personal/payment forms
without David confirming that exact action first, and never to type passwords: he
signs in himself in the JARVIS browser window. Page content is treated as untrusted.

JARVIS's Codex processes run lean: David's ~400 plugin/connector tools (Gmail,
GitHub writes, JS REPL, computer use...) are switched off with `--disable` flags and
`mcp_servers.<name>.enabled=false`; his normal Codex is unchanged. That cut chat
answers from 3.3-4.3 s to 0.9-1.6 s. MCP servers are passed per process (`-c`), as
per-thread config didn't expose their tools. Built-in web search is off for the
action brain so it uses the real browser. A model "at capacity" error retries once on
`fallback_model`. Measured: open an app ~5-9 s cold; read a page's top headline ~11-16 s.

Considered: browser-use and Stagehand (need their own paid LLM API), Windows-MCP (full
desktop + arbitrary PowerShell), Agent-Reach (reads sites, doesn't operate them).

## Tiers

| Tier | Trigger (router.py) | Runs | Cost |
|---|---|---|---|
| simple | everything else | one Claude/Codex CLI run, `power: low` (Haiku / low effort) | subscription only |
| medium | URLs, named sites, "log in", "check my …" | owner API adapter → browser agent → read-only Claude web | subscription; own APIs free |
| complex | build/ship/production/full-stack, long briefs | planner + ordered specialist subtasks | subscription; high tier only where needed |

Override any time with `--tier`. Classification is regex, not a model call, so
routing costs nothing. Power tiers map to models in `jarvis.config.json → models`:
`low` haiku / low effort, `mid` sonnet / medium, `high` opus / high. Codex tiers
change reasoning effort; set `model` there to pin a Codex model.

## Agent hierarchy (complex)

1. **Lead planner**: one high-power, read-only Claude run returns a JSON plan.
   It sees the specialist roster and is told to pick the lowest power per step.
2. **Specialists** (`.claude/agents/*.md`): architect, implementer, frontend,
   tester, reviewer, researcher, devops, security, scout, prompt-engineer. Each
   has a default power tier and a Claude model. Reviewer, security, researcher,
   architect, and scout are forced read-only. Writers must declare non-overlapping
   `owned_paths`.
3. **Sub-agents**: subtasks at `mid`/`high` power get `allow_subagents`. Claude
   can delegate to the same `.claude/agents` definitions (haiku-tier scout and
   researcher for nested lookups), and Codex to its own subagents.

Plans are saved to `.jarvis/plans/` and memory. Nothing runs until you call
`orchestrate run PLAN_ID`. Steps run one at a time in dependency order, earlier
results are handed to dependents, and the run stops at the first failure.
Parallel execution of independent writers is a later milestone.

## Memory heap

`.jarvis/memory.sqlite3`. Every inbound message, reply, plan, and subtask result is
stored automatically, plus anything added with `mem add`.

- IDs are readable and sortable: `kind:YYYYMMDD:hex6`, e.g. `reply:20261020:858794`.
- Search: SQLite FTS5, porter stemming, prefix matching, BM25 (title weighted 4×)
  blended with `importance` and 30-day recency decay. Filters: kind, tag, since.
- Identical content within a kind is stored once. Typed links (`answers`,
  `part-of`, `related`) connect items.
- Each request gets the top matches as untrusted evidence, so agents get context
  without the whole history.

No embeddings yet. Keyword search is free and exact. Semantic search (local
all-MiniLM, already in the owner's RAG work) can be added as a second ranking
pass without changing IDs.

## Second brain: the Obsidian vault (2026-10-21)

The heap records what JARVIS saw and said; the vault (`vault.path`) is what David
knows and decided. `jarvis/vault.py` adapts obsidian-second-brain's context engine,
bounded recall and vault tools to this vault's own `CLAUDE.md` schema.

| Piece | Where it plugs in | Bound |
|---|---|---|
| World brief (`Vault.world`) | appended to both ChatGPT lanes' developer instructions at thread start; threads with a brief are recycled after an hour | ≤ 3,000 chars |
| Recall (`Vault.context`) | `assistant.ask` puts vault lines ahead of heap lines; the orchestrator planner gets 6 lines | 4 notes / 900 chars, abstains on weak matches, never `reference/` or root map files |
| Tools (`VaultTools`) | the action lane (with desktop tools, or alone if actions are off); `Brain` routes `is_vault_request` there | additive writes only |
| MCP (`vault_mcp.py`) | Claude Code via `.mcp.json`, Codex via `codex mcp add` | read-only unless `--write` |

Search: FTS5 BM25 with title 8×, aliases 5×, tags 3×, path 2×, body 1×. Words of 5+
letters are prefix-matched, so "os" doesn't match "atlas". Results are weighted by
note type (reference 0.55, daily 0.75, root files 0.7) plus a 60-day freshness bonus
on `updated:`. Recall drops anything scoring under 45% of the best hit. A hit must
share a word with the note's title, path or tags, or have two query words in its
snippet. Writes use temp file + `os.replace`, keep each file's line endings, refuse a
file changed since it was read, and bump `updated:`. Upstream's other pieces (the
48-command catalog, scheduled agents, Crew logbook, semantic search) are not
ported: JARVIS already has the heap, events and the orchestrator for those roles.

## Voice pipeline and cost

| Stage | Choice | Cost |
|---|---|---|
| Capture | energy VAD on 30 ms frames, noise-floor calibrated | free |
| Is it David? | sherpa-onnx ERes2Net embedding (26 MB, CPU), cosine vs enrolled profile, cutoff learned from his own clips; runs before transcription. CAM++ failed on this mic (strangers outscored him) | free |
| Speech → text | faster-whisper `large-v3-turbo` on the RTX 4060, CPU `small.en` fallback | free |
| Wake word | transcript must start with "Jarvis" (also accepts Whisper's "Jervis"); applies to the background listener and the console's always-on mic, which defers to the listener when it runs | free |
| Thinking | routed CLI run on subscription | no API spend |
| Text → speech | **Kokoro** 82M (local, CPU fp32, ~0.7 s/sentence, voice `bm_george`), sentences streamed from Claude and synthesized while the previous one plays, phrase cache. ElevenLabs optional via `voice.engine` | free |

## Medium tier: personal agents and own APIs

- **Own APIs** (Instagram, X, …): add to `adapters.apis` in config:
  `{"name": "ig", "url": "http://127.0.0.1:8787/ig", "sites": ["instagram"], "token_env": "JARVIS_IG_TOKEN"}`.
  Contract: POST `{"query", "context"}` → JSON with `answer`. Only https or localhost.
- **Grokbot / Muse**: listed in `adapters.browser_agents`. They are skipped until
  their interface is known (CLI, local API, or browser extension) and
  `personal_agents.<name>.integration` is set.
- **Jev**: reserved name. If you build it, give it the same HTTP contract and it
  slots in as an API adapter or as the orchestrator's planner.
