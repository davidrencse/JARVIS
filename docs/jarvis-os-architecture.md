# JARVIS OS design

*Decisions with reasons, and what is still open. The running map is [architecture-guide.md](architecture-guide.md).*

## Principles

1. **Cascade.** Rules, then the decider, then an LLM, then David. Each stage runs only when the one before it cannot decide.
2. **One owner per decision.** Other components may only restrict that owner.
3. **Safety ratchet.** After the rules, every stage can only block, clarify, or confirm. When judges disagree, no wins.
4. **Trust boundary.** Direct actions are inside JARVIS's control. Grok Bot is outside it: JARVIS controls the task, not what Grok then does.
5. **Verify before learning.** Only outcomes with a known verification level teach history. "Grok said done" is a claim.
6. **Privacy at the source.** Sensitive data is reduced on the node that produced it, before it is logged or sent.
7. **Thin slices.** Build the smallest end-to-end version. A cost model waits until there are three or more nodes.

| Decision | Owner | May only restrict it |
|---|---|---|
| Mode and commands | interpreter (ChatGPT + `JARVIS.md`); learned templates when it is down | schema, registry, devices |
| Device | interpreter, from `JARVIS.md` and the live menu | must be configured and online |
| Executor | Intent Registry | decider, only when the intent itself is ambiguous |
| Risk | registry for direct intents; ratchet for Grok tasks | LLM review, history novelty, David |
| Dispatch | gates | David, through the confirmation queue |
| Route | fixed preference order plus health | nobody; history only reports |
| Done | node-side observer for direct actions; decider plus David for Grok | nobody |

## Registry

The registry is the one table for executor, risk, observer, and escalation. There is no "run this command" intent. New abilities are new rows. Current rows (`jarvis/registry.py`):

| Intent | Executor | Risk | Observer | Settle |
|---|---|---|---|---|
| `open_app` | direct | allow | a window of the app appears | 8 s |
| `open_url` | direct | allow | a browser title names the site | 6 s |
| `focus_window` | direct | allow | the focused window is the app | 2 s |
| `close_app` | direct | confirm, unless the name is on the safe-to-close list | the app's windows are gone | 5 s |
| `media` | direct | allow | playback state matches; escalate once | 3 s |
| `volume`, `brightness` | direct | allow | the setting reads back | 1 s |
| `scroll` | direct | allow | none; auto ends on stop, the window closing, or 30 min | |
| `browse` | direct | allow | none; the executor reads the page itself | |
| `list_apps` | direct | allow | the result returned | |
| `spotify_play` | direct | allow | Spotify reports playing; escalate once | 6 s |
| `terminal` | direct | confirm, unless `terminal.read_only` | none | |
| `bluetooth` | direct | confirm for pair and forget; otherwise allow | none | |
| `screenshot`, `system_status`, `workspace`, `lock_screen`, `reminder`, `weather` | direct | allow | none | |
| `agent_task` | grokbot | ratchet | optional predicate, otherwise the agent's claim | task deadline |
| `answer` | brain | allow | none | |
| `vault_write` | vault | allow | the note contains the text | 1 s |
| `build` | orchestrator | confirm, because a plan needs an explicit run | none | |

`spotify_play` is direct. A deep link alone could not start Liked Songs, so tool instructions prefer native Spotify search, then accessibility or keyboard, and escalate once if that fails before anything plays.

Outcome states: `pre_effect_failure` (certain, nothing happened), `acknowledged` (likely), `observed` (certain), `unconfirmed` and `uncertain` (not certain), `agent_claimed` (a claim), `user_confirmed` / `user_rejected` (certain). Escalation is allowed only from `pre_effect_failure`, only where the registry says so, and only once. `unconfirmed` and `uncertain` are reported and never retried, so a slow start cannot launch a second copy.

## Language

The strict parser runs first because a wrong fast parse is worse than a model call. It accepts a request only when every word is covered, there is one reading, and every command is a registry intent. Anything else goes to the interpreter untouched. The interpreter returns typed JSON (`converse`, `act`, `agent`, `build`, `mixed`) and a command graph. It cannot set risk, executor, or permissions. Invalid JSON gets one retry with the error attached, then `clarify`.

`JARVIS.md` is the self-model: identity, David, machines, which PC is main and which is the default, and the rules. Facts that also live in config are checked at startup. Config wins, and the mismatch is reported. David edits the file. JARVIS may propose a change and never writes it.

Learned templates are saved only after an interpretation reaches `observed` or `user_confirmed` twice, and only after they pass the labelled set. Confirm-class intents never become templates. If ChatGPT is down or over quota, templates plus the device and app lexicons are the fallback. Anything they cannot cover is reported as the model being unreachable. The same fallback is what a node would use offline.

`converse` uses read-only tools (vault search and read, history, system status). A chat turn that asks for an action is classified as `act`. The brain is ChatGPT on the subscription login. Claude CLI is the failover. A local model is an offline fallback only, and only after VRAM is measured with Whisper loaded. API-key environment variables stay stripped.

The decider has three jobs: a second opinion on the interpreter (`J1`), the Grok handoff checks (`J2`), and whether a Grok answer is complete (`J3`). Until a job's measured precision reaches 0.98 on at least 20 picks, it stays in shadow and the existing rules run (`decisions.shadow` is true; `decisions.enabled: false` is the kill switch). A deadline falls back to rules. Questions for one job are one batched call. Raw probabilities are not trusted; the uncertainty band is wherever measured precision misses the target.

## Gates

U-checks are deterministic and aimed at under 10 ms: schema, exactly one configured device, reachable device, executor ready, safe arguments (http(s), plus an allowlist of app schemes such as `spotify:`), target exists, capacity, quota, loop-free, and a defined success condition for action intents. Lookups such as "youtube" → youtube.com live here, not in the decider.

H-checks are history and are aimed at under 5 ms. History can warn, block, or slow a command. It cannot approve one.

| Check | Owner or scheduled | Agent or system |
|---|---|---|
| Same command failed the same way recently | warn and proceed | block |
| Same command already in flight | merge | block |
| Same command recurring on a rhythm | warn | block and alert David |
| Executor track record, novelty | display, or lower decider confidence | same; novelty never blocks by itself |

Grok risk is the highest of keyword rules (buy, pay, send, post, delete, submit, and the same family), the decider's `has_external_side_effect` at high recall, history novelty, and the LLM review when it runs. None of them can lower another. Credentials are always denied. David signs in himself.

A confirmation is a short id, a one-line summary, a hash of the exact command, the origin device, and an expiry: 60 s for voice, 10 minutes for the console (`confirm.py`). Voice approval is "Jarvis, yes" or "Jarvis, no", and names the id when more than one item is waiting. The approval dies if the command changes. Expired items are dropped and reported.

Vault snippets are fetched at compile time, and only for executors that use context. A direct `open_url` does not pay for recall.

## Dispatch and record

Route order is cluster, then SSH on the LAN, then SSH over Tailscale. The first healthy route wins. Nothing is rewired silently when a route regresses; JARVIS reports it. Long-poll holds `/v1/poll` until a job arrives or about 25 s pass. A node is sent a job only if its poll socket is still connected, it checked in within 10 s, or it is running a job. "Online" at 75 s means seen recently, not eligible for a new job.

Grok is one task at a time per device. JARVIS can guarantee the task passed the checks and that side effects were confirmed. It cannot guarantee what Grok does with computer use, that "ask David first" is honored, or that `jarvis stop` lands mid-action. Results stay `agent_claimed` until observed or confirmed. The proposed hourly cap of 20 is not a named limit in the Grok code.

Node offline mode, as designed, would run only allow-risk direct actions aimed at that node, with no model, Grok, vault, or memory writes, and would replay the journal into history as records rather than running it again. That mode is not implemented. Cognition stays on the main.

Observers run on the node. Window titles never leave it. The reply states what was confirmed and what was not.

The history database is the engine, because the checks need indexed queries and transactions. Learning uses `observed` or `user_confirmed` for templates, `user_confirmed` / `user_rejected` for Grok wording, and verified levels for decider calibration. A redacted vault log (`jarvis/log/YYYY/MM/YYYY-MM-DD.md`, domains instead of URLs, no window titles, excluded from recall) is the human view in the design and is not the store the checks read. The vault is not required for commands to run.

## Still open

- Decider jobs remain in shadow until each one hits the 0.98 target on held-out verified labels.
- Node offline mode is not built.
- The redacted vault log is not the history engine. `history.sqlite3` is.
- A numeric Grok hourly cap was proposed and is not enforced as that number.
- Moving MAIN is not a config edit. The blocked procedure is [handbook/continuity.md](handbook/continuity.md).
- macOS, sway, and X11 backends are unit-tested only. The interpreter has not been proven live end to end.
