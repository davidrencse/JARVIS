# JARVIS OS: architecture v2 (design, not built)

Status: revised 2026-10-26 after a design review (17 findings; see
[Review traceability](#review-traceability)). Uncommitted work in progress; the suite is
green. **Built and verified live** so far: Intent Registry, strict parser, U-gates, the
direct fast path with node-side `observe`, command history (H1/H3) with replay in the
console, the confirmation queue and risk ratchet, Core Audio volume on Windows, and the
Zenbook as a live cluster node (see [Built status](#built-status-2026-10-26)).

## The vision in one paragraph

David speaks or types one command. JARVIS parses it for free into atomic commands and
grounds them in his second brain. Each command is decided by the cheapest thing that can
decide it: rules first, a local Jev-type decider only where there is real ambiguity or
judgement, an LLM review only when the decider is unsure, and David for anything with
consequences. Commands go out over the cluster link (SSH underneath) as typed direct
actions, or to Grok Bot when a step needs judgement. JARVIS then confirms what it can
observe, reports honestly what it can't, and records everything. The record makes it
faster over time without ever making it less safe.

## Principles

Every design choice below follows from these seven rules.

1. **Cascade: rules → decider → LLM → David.** Each stage runs only when the stage
   before it cannot decide. Most commands never reach a model.
2. **One owner per decision.** Every decision has exactly one component that makes it
   (see [Decision ownership](#decision-ownership)). Other components may only *restrict* it.
3. **Safety ratchet.** After the rules, every stage can only move a command toward
   safer: block, clarify, confirm. Nothing downstream can approve what an earlier
   stage stopped. When judges disagree, "no" wins.
4. **Know the trust boundary.** Direct actions are inside JARVIS's control; JARVIS can
   guarantee what they do. Grok Bot is **outside** it: JARVIS controls what Grok is
   asked, never what Grok then does.
5. **Verify before you learn.** Only outcomes with a known verification level feed the
   history's learning. "Grok said done" is a claim, not a fact.
6. **Privacy at the source.** Sensitive data is reduced where it is produced, on the
   node, before it travels or gets logged.
7. **Thin slices.** Build the smallest version that works end to end, then add.
   Machinery that only pays off with more machines or more data waits for them.

## Decision ownership

| Decision | Owner | Who may only restrict it |
|---|---|---|
| Mode and commands the request contains | the interpreter (ChatGPT + `JARVIS.md`); learned templates when it's down | validation (schema, registry, devices) |
| Which device | the interpreter, using `JARVIS.md` and the live menu | validation: must be a configured, online device |
| Which executor | the [Intent Registry](#the-intent-registry) | decider (direct vs Grok, only when the intent itself is ambiguous) |
| Risk | Registry (direct intents); rules + decider ratchet (Grok tasks) | LLM review, history novelty, David |
| Dispatch or not | gates | David (confirmation queue) |
| Route | fixed preference order + health | none; history only reports |
| Done or not | node-side observer (direct); decider + David (Grok) | none |

## Layers

```
 voice / text / console / node proxy
        │
 L0  INTAKE      wake word, speaker check (exists) → Envelope {id, text, origin_device, origin_class}
        │
 L1  INTERPRET   instant phrases → strict parser → one warm LLM call (with JARVIS.md):
        │        mode (converse / act / agent / build) + Command Graph → strict validation
 L2  RESOLVE     Intent Registry fixes executor and risk; decider only where the registry says "ambiguous"
        │
 L3  GATE        U-checks → H-checks (origin-aware) → risk ratchet → confirmation queue
        │        → COMPILE (vault snippets fetched here, only for executors that use context)
 L4  DISPATCH    cluster long-poll (SSH tunnel) · SSH fallback · Grok handoff · node offline mode
        │
 L5  VERIFY      ack → observe with settle time (on the node) → outcome state
        │
 L6  RECORD      history DB (SQLite engine) → redacted Obsidian log (human view)
```

## The Intent Registry

The registry is the core of v2: one table that says, for every intent, everything the
pipeline needs. Gates, risk, verification and escalation all read it, so their rules
never disagree.

| Intent | Args | Executor | Risk | Observer (runs on the node) | Settle | On certain failure |
|---|---|---|---|---|---|---|
| `open_app` | name | direct | allow | a window of that app's class appears | 8 s | report |
| `open_url` | url, browser? | direct | allow | a browser window's title contains the site name | 6 s | report |
| `spotify_play` | target: `liked` / `playlist:<id>` / `album:<id>` / `track:<id>` | **grokbot** (tested live 2026-10-26: a freshly opened Spotify has no media session, so neither the media key nor Space starts Liked Songs; it needs the page's Play button) | allow | playback state reports Spotify playing | 6 s | n/a |
| `focus_window` | name | direct | allow | the focused window's class matches | 2 s | report |
| `close_app` | name | direct | **confirm** (unsaved work) unless the app is on a safe-to-close list | the app's windows are gone | 5 s | report |
| `media` | play / pause / next | direct | allow | playback state (MPRIS on Linux, SMTC on Windows) | 3 s | escalate to `agent_task` once (e.g. no player is running) |
| `volume` | level | direct | allow | the sink volume reads back | 1 s | report |
| `list_apps` | query | direct | allow | result returned | n/a | report |
| `scroll` | direction, mode (once/auto/stop), target, app, url, every | direct | allow | none (auto: presses only while the window is focused; ends on stop, window gone, or 30 min) | n/a | report |
| `agent_task` | free text | grokbot | **ratchet** (see L3) | optional predicate; else agent claim | per task deadline | never re-run |
| `answer` | question | brain | allow | n/a: no observable effect | n/a | report |
| `vault_write` | text, target | vault tools (additive) | allow | the file contains the text | 1 s | report |
| `build` | goal | orchestrator | confirm (plans already need an explicit run) | plan created | n/a | report |

New abilities are added as new rows: a typed intent, its observer and its risk. There is
never a "run this command" intent.

### Outcome states

| State | Meaning | Certain? |
|---|---|---|
| `pre_effect_failure` | the executor refused before doing anything (app not found, validation failed) | yes, nothing happened |
| `acknowledged` | the executor reported success | likely |
| `observed` | the observer matched within the settle time | yes |
| `unconfirmed` | acknowledged, but the observer didn't match within the settle time | **no** |
| `uncertain` | no acknowledgement before the deadline | **no** |
| `agent_claimed` | Grok's answer says it's done | a claim |
| `user_confirmed` / `user_rejected` | David approved or corrected it ("Jarvis, that's wrong") | yes |

**Escalation rule:** only `pre_effect_failure`, only where the registry allows it, and
only once. `unconfirmed` and `uncertain` are reported, never retried or escalated, so a
slow Spotify start can never cause a second Spotify.

## L0 Intake (exists)

Wake word, speaker verification and the node proxy stay as they are. Each request becomes
an **Envelope**: `id`, `text`, `origin_device` (the cluster already threads `ORIGIN`),
and `origin_class`:

| origin_class | Examples | Why it matters |
|---|---|---|
| `owner` | voice with the wake word, console, CLI, push-to-talk | David's own repeats are warned about, not blocked |
| `scheduled` | David's timers and routines | treated as owner, rate-limited |
| `agent` | Grok answers, orchestrator steps | repeats are blocked, loops alerted |
| `system` | health checks, probes | never triggers actions |

## L1 Interpret: one ChatGPT call understands the request

**Decided (David, 2026-10-26, replacing the rigid parser):** an AI reads the request and
says what it means. Rules no longer parse; they only **check** what the AI returns.

### Step 1: instant phrases (no model)

A small exact-match table answers trivial phrases instantly: "hello", "thanks",
"what time is it" (`instant.py`, exists). This is a lookup table, not grammar rules.

### Step 2: the fast parser (no model, microseconds)

**Decided (David, 2026-10-26, final):** the parser runs first for speed; the AI handles
everything else. The parser is deliberately **strict**: it answers only when it is sure,
and otherwise passes the request on untouched. It never guesses.

- It tags devices (aliases from config and `JARVIS.md`, including "main", "default",
  "here", "the other one"), action verbs, apps (each device's cached app list) and sites.
- It accepts a request only if **every word is covered, there is exactly one reading, and
  every command is a Registry intent**, e.g. "open spotify", "open chrome on arch and
  spotify on the dell", "play my liked songs", "pause", "volume 30".
- Learned templates (below) are part of the parser, so it covers more over time.
- Anything else (chat, questions, pronouns, conditions, unknown words, ambiguity) goes to
  the interpreter. A wrong fast parse is worse than a 1 s AI call, so precision beats
  coverage, and the labelled test set enforces that.

### Step 2b: actions the parser can't run go to Grok Bot (built)

**Decided (David, 2026-10-26):** a command that can't be done algorithmically is sent to
Grok Bot. When the parser recognizes an action but can't run it itself (it wasn't certain:
"play that song kevin sent me", "open the thing"; or the intent has no direct executor yet:
`spotify_play`), or the request starts with an agent verb (buy, reply, send, check, search…),
`pipeline.needs_agent()` routes it to Grok Bot. It still goes through the Grok preflight
(draft + ten checks), which can answer or ask instead of forwarding. Conversation and
questions ("hello", "how do i open chrome") stay with the ChatGPT brain. Off with
`routing.agent_fallback: false`, or when Grok Bot isn't configured.

### Step 3: the interpreter (one warm LLM call)

The interpreter is a dedicated, always-warm ChatGPT `app-server` lane
(`gpt-6-luna`, low effort, subscription login, no API credits, ~1 s). It gets:

- the request text and envelope (origin device, channel)
- **`JARVIS.md`**, JARVIS's self-model (see [below](#jarvismd-the-self-model)): who
  JARVIS is, who David is, the machines, which PC is "main" or "default", and the conventions
- the live menu: online devices, each device's app list summary, the Registry's intents
- vault **tags** found in the text (titles and aliases matched cheaply; no note bodies)

It returns typed JSON only. In that one call it does two jobs.

**Job 1: mode triage.** Which kind of request this is:

| Mode | Means | Goes to |
|---|---|---|
| `converse` | talk, questions, opinions, "how are you", "what do you think about…" | the conversation brain: persona, memory, vault **read-only**, no actions |
| `act` | one or more desktop or app actions | the command graph → gates → executors |
| `agent` | needs judgement in a UI or a logged-in account | `agent_task` → Grok checks → Grok Bot |
| `build` | multi-step software work | the orchestrator (plan, then explicit run) |
| `mixed` | several of these ("open spotify and tell me a joke") | one command per part |

**Job 2: the command graph.** For `act`, `agent` and `build`: each command's intent,
device and args, plus dependencies. The AI resolves "my default pc", "the other one",
"it", and verbs left out ("open spotify … and chrome on my other pc"), using
`JARVIS.md` and the menu.

```json
{"mode": "act", "commands": [
  {"id": "c1", "intent": "open_app", "device": "dell", "args": {"name": "Spotify"},
   "why_device": "JARVIS.md: main/default PC = dell", "depends_on": []},
  {"id": "c2", "intent": "open_app", "device": "zenbook", "args": {"name": "Chrome"},
   "why_device": "'other pc' = the PC that isn't main", "depends_on": []}
]}
```

### Step 4: deterministic validation (for both parser and interpreter output)

- JSON schema valid; every intent is in the Registry; every device exists in config
- if the interpreter named a device, it must be in the menu; `why_device` is kept for the trace
- anything invalid → **one** retry with the error attached, then `clarify` ("which PC did you mean?")
- the interpreter **cannot** set risk, executor or permissions; the Registry and gates own those

So the AI is flexible about language, and the code stays strict about what it is
allowed to do. A badly parsed command still has to pass every gate.

### Speed and resilience

- The lane is kept warm, so a parse costs about 1 s. Requests run on the subscription,
  so it's free in money, but it uses plan quota (checked by U8).
- **Learned shortcuts:** when an interpretation reaches `observed` or `user_confirmed`
  twice, it is saved as a slotted template ("open {app} on {device}"). Next time a
  matching request skips the call. Templates go live only after passing the labelled
  test set, and never for confirm-class intents.
- **If ChatGPT is down or over quota:** a fallback runs the learned templates plus the
  device and app lexicons. Anything they can't cover gets "I can't reach my language model
  right now". The node offline mode uses the same fallback.
- Quality is measured on a labelled set of real commands, by correctness of the graph.

### JARVIS.md: the self-model

A Markdown file in the repo (`JARVIS.md`), loaded into the interpreter and the
conversation brain. It says:

- **who JARVIS is**: personality, tone, how it talks to David
- **who David is**: the stable facts he wants JARVIS to know (the vault's world brief
  adds the current ones: projects, deadlines, today)
- **the system**: each machine, what it's for, its aliases; which PC is **main** and which
  is the **default** for actions (the main unless David says otherwise); "here", "this PC"
  and "the other one"
- **its own rules**: wake word only, direct actions before Grok, confirm before anything
  with consequences, read-only in conversation
- **what it can and can't do**: the Registry's intents, in plain words

Facts in `JARVIS.md` that also exist in config (devices, main) are **cross-checked at
startup**. If the file says the main is `dell` but config says `zenbook`, config wins and
JARVIS reports the mismatch, so the self-model can't drift from reality. David edits the file
by hand; JARVIS can propose changes but never writes it itself.

### Conversation mode: memory and personality

`converse` requests go to the existing ChatGPT chat lane with:
- `JARVIS.md` as its identity
- the vault world brief and bounded recall (exists), the memory heap's recent context
  (exists), and the history DB for "what did you do earlier?"
- **read-only tools**: vault search and read, history queries, system status. No desktop,
  browser or Grok tools, so a chat can never turn into an action by accident. If David
  asks for an action mid-conversation, the interpreter classifies that turn as `act`.
- replies are spoken with Kokoro (exists) and remembered in the heap

"Sentience" here means continuity: the same identity, memory of past conversations, and
knowledge of its own state, not a separate model.

## The brain: provider-agnostic, always on

**Decided (David, 2026-10-26):** ChatGPT (Codex `app-server` on the Plus login,
`gpt-6-luna`, ~1 s warm) **is JARVIS's brain**: conversation, the interpreter, and
drafting Grok tasks. Claude CLI and a local model stay as failovers only.

JARVIS's personality, memory and conversation need an LLM running all the time. It is
one **brain interface** with interchangeable providers, so JARVIS isn't tied to ChatGPT:

```python
brain.ask(messages, tools=read_only|actions, stream=on_sentence) -> reply
brain.interpret(envelope, self_model, menu) -> {mode, commands}
```

| Provider | How | Cost | Speed | Role |
|---|---|---|---|---|
| ChatGPT | Codex `app-server` on the Plus login (exists: `chatgpt.py`) | subscription quota | ~1 s, warm | **default** brain and interpreter |
| Claude | Claude Code CLI on the subscription (exists: `fastlane.py`) | subscription quota | ~1-3 s | second provider; failover when ChatGPT is limited |
| Local | an OpenAI-compatible local server (Ollama or llama.cpp) on the RTX 4060 | free, unlimited | depends on model size | offline and over-quota fallback; private chat |

- **Config chooses:** `brain.provider`, `brain.fallbacks` (for example ChatGPT → Claude →
  local), per mode if wanted (e.g. the interpreter on ChatGPT, casual chat on local).
- **Failover is automatic** on rate limits, outages or timeouts, and the turn says which
  brain answered. Same `JARVIS.md`, same memory, same tools on every provider, so JARVIS
  is the same "person" whichever model is speaking.
- **Local model limits:** the 4060 has 8 GB, already shared with Whisper (and later the
  decider). That fits roughly a 7-8B model at 4-bit, which is fine for chat and simple
  interpretation but weaker than ChatGPT at hard reasoning. Measure VRAM with everything
  loaded before making local the default.
- **Never pay-per-token APIs** (no-API-credits rule): API-key env vars stay stripped.

## L2 Resolve: the registry decides, the decider only breaks ties

The registry fixes the executor and the risk for every intent. The decider is consulted
only in its three jobs:

| Job | When | Question | Replaces |
|---|---|---|---|
| **J1** interpretation check | the interpreter's output, in shadow at first | `mode_correct`, `device_correct`: a cheap second opinion; disagreement → `clarify` | nothing (new) |
| **J2** Grok handoff checks | every `agent_task` | the ten checks + `achievable_by_grok` + `has_external_side_effect` | the ChatGPT review pass, except when the decider is unsure |
| **J3** completion | every `agent_task` answer | `complete` / `verify_more` / `incomplete` | nothing (new) |

Device, executor and risk for parsed direct intents involve no model at all. In your
example, the decider is never called.

**Before the decider exists**, J1 falls back to ChatGPT, J2 to the existing two-pass
ChatGPT preflight (exists), and J3 to reporting Grok's answer as `agent_claimed`. Rules
mode has no probabilities, only "decided" or "not decided".

## L3 Gate: checks and balances

Cheapest first, fail fast. Each check writes `{name, pass, prob?, why}` to the trace.

### U-checks: deterministic, every command, target < 10 ms

| # | Check | Applies to |
|---|---|---|
| U1 | `schema_valid`: intent in the registry, args typed | all |
| U2 | `device_resolved`: exactly one configured device | all |
| U3 | `device_reachable`: heartbeat fresh (long-poll window), else SSH fallback probe | remote |
| U4 | `executor_ready`: capability enrolled; Grok installed, window, daemon, computer use | per executor |
| U5 | `args_safe`: http(s) only, plus app deep-link schemes on an allowlist (`spotify:`), length limits, no control chars, data never code | all |
| U6 | `target_exists`: the app is in that device's `list_apps` cache; the site is known or a valid domain | `open_app`, `open_url` |
| U7 | `capacity_ok`: per-device concurrency; **Grok: one task at a time per device and an hourly cap** | per executor |
| U8 | `quota_ok`: Claude/ChatGPT headroom (`credits.py`, exists); **Grok: cooldown after a rate-limit error** | brain, orchestrator, grokbot |
| U9 | `loop_free`: no JARVIS- or Grok-originated echo | all |
| U10 | `success_defined`: the registry has an observer, or the intent is marked "no observable effect" | action intents only |

`target_exists` and URL mapping (youtube → youtube.com) are lookups, so they live here
and not in the decider.

### H-checks: history, origin-aware, target < 5 ms

| # | Check | `owner` / `scheduled` origin | `agent` / `system` origin |
|---|---|---|---|
| H1 | same command failed the same way recently | **warn and proceed** ("this failed 2 min ago because X; trying again") | block, report |
| H2 | track record of this executor for this intent on this device | prior and display only | same |
| H3a | the same command is **in flight** | merge ("already opening Spotify"), not duplicate | block |
| H3b | the same command keeps recurring on a rhythm | warn | **block and alert David** (the Grok-Instagram loop) |
| H4 | a learned template matches | used by the parser (L1), not a gate | n/a |
| H5 | a compiled Grok task like this one was `user_confirmed` or `user_rejected` before | reuse the good wording; avoid the bad | n/a |
| H6 | novelty: an action class never seen from this origin on this device | lowers decider confidence; never blocks | same |

History is evidence, not authority: it can warn, block or slow JARVIS down, never approve.

### Risk ratchet

- **Direct intents:** risk comes from the registry. It is fixed and reviewable.
- **`agent_task`:** risk = the **highest** of:
  1. keyword rules: buy, pay, order, book, send, message, reply, text, email, post,
     tweet, comment, delete, remove, unsubscribe, submit, apply, sign, accept, transfer,
     change settings, install…
  2. decider J2 `has_external_side_effect`, with a **high-recall** threshold (it may flag
     too much, never too little)
  3. history novelty (H6)
  4. the LLM review, when it runs

  Any of them saying "confirm" means confirm. None of them can lower another's verdict.
- **Credentials:** always deny. David signs in himself.

**LLM review only when unsure.** If a decider job lands in its measured uncertainty band
(see [Calibration](#calibration)), the ChatGPT review runs. It can only restrict:
block, clarify or confirm. If it and the decider disagree, the stricter verdict wins.

### The confirmation queue

When a command needs David's OK:

1. It is parked as a **pending confirmation**:
   - a short ID ("C7")
   - a one-line summary ("Grok will reply to Sam's last DM on the Dell")
   - a hash of the exact compiled command
   - the origin device
   - an expiry: 60 s for voice, 10 min for console
2. JARVIS says or shows the summary.
3. David approves or rejects through:
   - **voice:** "Jarvis, yes" / "Jarvis, no". This respects the wake-word rule. It binds to
     the only pending item on that device; if more than one is pending, he names it
     ("Jarvis, confirm seven").
   - **console:** buttons on the pending list
   - **overlay:** a hotkey
4. The approval is bound to the hash. If anything about the command changes after
   approval (re-compiled, a different device), it needs a new confirmation.
5. Expired items are dropped and reported. Every approval is recorded with who, how and when.

### Compile

A passed command becomes its executor's exact instruction from a fixed template. **Vault
snippets are fetched here**, and only for executors that use context (brain, Grok,
orchestrator). A direct `open_url` never pays for vault recall. The Grok template
carries: the task, the target device, the success condition, the owner's constraints,
"ask David before buying, sending, posting or submitting" (exists), and the reply address.

## L4 Dispatch

### Grok Bot is outside the trust boundary

| JARVIS guarantees | JARVIS cannot guarantee |
|---|---|
| Grok only gets tasks that passed J2 and the risk ratchet | what Grok does with its computer-use access |
| side-effect tasks were confirmed by David first | that Grok honours "ask David first" |
| one task at a time per device, with an hourly cap | that a Grok-to-Grok handoff (Dell Grok driving Arch Grok) stays inside JARVIS |
| `jarvis stop` asks every Grok Bot to stop | that the stop lands mid-action: it is best-effort |

So Grok is used only where judgement is needed (decided: direct first), and its results
count as `agent_claimed` until observed or confirmed.

### Transports

```
                    ┌──────────── main PC (dell) ─────────────┐
                    │ parser · registry · gates · decider      │
                    │ brain · history · vault · hub :7778      │
                    └───────┬─────────────────────────┬───────┘
        cluster: long-poll over the SSH tunnel   SSH (host key pinned):
        typed, capability-gated, at-most-once    tunnel carrier · fallback
                    │                                 │
            ┌───────┴────────────────────────┐   ┌────┴────────────┐
            │ node (zenbook)                  │   │ future nodes    │
            │ direct actions · observer       │   │ jarvis-node     │
            │ Grok typing · offline mode      │   └─────────────────┘
            └─────────────────────────────────┘
```

- **Long-poll, not short-poll.** Today nodes poll once a second when idle, back off to
  15 s after errors, and write a heartbeat to SQLite on every poll ([cluster.py:422](../jarvis/cluster.py)).
  That adds an average of 0.5 s, and up to 1 s, to every remote command. v2 changes this:
  - `/v1/poll` holds the request open until a job is enqueued or about 25 s pass
    (a condition variable, signalled by `enqueue`)
  - the hub waits for results on a condition too, instead of polling SQLite every 50 ms
  - the online window becomes about 45 s, longer than the long-poll

  Jobs then arrive within milliseconds, with almost no idle traffic.
- **Route selection is a fixed preference order with health checks:** cluster → SSH on
  the LAN → SSH over Tailscale. The first healthy option is taken. There is no cost
  optimiser until there are three or more nodes.
- **Cheap active probes** (a TCP or SSH no-op every few minutes) measure the routes not
  in use, so the visualisation and regression alerts have data for every route, not just
  the chosen one.
- **Regression alerts:** for example, "every Zenbook command has used Tailscale since its
  LAN address changed; update `devices.zenbook.target`". It surfaces this and never
  rewires anything silently.
- **Moving the main:** `cluster handover` refuses while jobs are in flight. It moves the
  history, memory, ledger and plans, re-issues node tokens, and runs `vault connect` on
  the new main. Nodes refuse results that belong to an old main (exists).

### Node offline mode

If a node can't reach the main for more than a short grace period:

- it runs **its own** parser, registry and direct executor
- only for commands **targeting itself**, with **allow** risk
- no models, no Grok, no vault, no memory writes
- every action goes into a local journal

When the main is back, the journal is replayed into the history **as records only**,
never re-executed. The main still owns all cognition, so "one brain" holds. A sleeping
Dell no longer stops the Zenbook from opening its own apps.

## L5 Verify

1. **Acknowledge:** the executor's result returns, or Grok's inbox file arrives before the deadline.
2. **Observe on the node:** the main sends the registry's predicate (for example "a
   window of class `spotify`"). The node polls every 250 ms until the settle time runs
   out, then returns `{match: true/false, class}`. **Window titles never leave the node.**
3. **Assign the outcome state** (see [Outcome states](#outcome-states)) and apply the escalation rule.
4. **Grok answers:** decider J3 judges `complete` / `verify_more` / `incomplete`. The
   outcome stays `agent_claimed` unless an observer matched or David confirms.

The reply to David says what is known: "Spotify is open on the Dell. I opened YouTube on
the Zenbook but couldn't confirm the page loaded."

## L6 Record

**The history DB (SQLite, beside the memory heap)** is the engine. It has `commands` and
`hops` tables holding the envelope, parse, registry row, every check, every decider
answer, the route, per-hop timings, the outcome state and the **verification level**.
Learning uses only these levels:

| Feature | Learns from |
|---|---|
| parser templates (H4) | `observed` or `user_confirmed`, at least twice |
| Grok wording (H5) | `user_confirmed` / `user_rejected` |
| track record (H2) | all states, weighted by certainty |
| decider evaluation and calibration | `observed`, `user_confirmed`, `user_rejected` |

**The Obsidian vault log** is the human view. It is generated from the DB, batched, rebuildable and **redacted**:

- **Daily note** `jarvis/log/YYYY/MM/YYYY-MM-DD.md`, with the vault's required
  frontmatter. A summary first (commands, failures, unconfirmed, slowest route,
  regressions), then one line per command: time, intent, device, executor, outcome, latency.
- **Redaction:**
  - URLs reduced to their domain
  - Grok task text reduced to its intent and target ("reply on Instagram")
  - no window titles, ever
  - the full trace stays in the DB, and each line links to a console replay
- **Project links:** commands whose vault tags matched a project link that note, so the
  project's backlinks show what JARVIS did for it.
- **Frontmatter fields** (`device`, `executor`, `outcome`, `latency_ms`) for Dataview.
- **Writes:** additive, audited in `.jarvis/vault-writes.jsonl` (exists), batched every
  few minutes. Never written into a file with merge-conflict markers.
- **Excluded from recall,** so logs never come back as knowledge. "What did JARVIS do
  yesterday?" is answered from the DB.

Why the vault isn't the database: the history checks need millisecond indexed queries
and at-most-once needs transactions, which Markdown can't provide. Writing every command
into the vault would also flood recall and the graph, and fill obsidian-git with
commits and conflicts.

## Visualization: A → B, measured

This extends the live connection tree (`web/flow.js` fed by `events.py`, exists) with
`turn.parse`, `turn.gate`, `turn.hop`, `turn.confirm` and `turn.outcome` events.

- **Route view:** one lane per command. Each hop appears as A → B with its measured time
  and state. Gates appear as a checklist with reasons. Where a real choice was made (J1,
  route fallback, direct vs Grok), the alternatives are shown with the reason they lost.
  Nothing is drawn for choices that never happened.
- **Waterfall:** measured time per layer, not estimates.
- **Topology:** machines and links with live health and probe latencies.
- **Pending confirmations:** the queue, with approve and reject buttons.
- **Replay:** any past command from the history DB, with outcome state and verification level.

## Connecting Obsidian (first run)

`jarvis vault connect`, plus a console page:

1. **Discover** vaults from Obsidian's registry, `obsidian.json`, which has a `vaults` map of `{path, ts, open}`:
   - Windows: `%APPDATA%\obsidian\obsidian.json` (verified on the Dell)
   - Linux: `~/.config/obsidian/obsidian.json`
   - macOS: `~/Library/Application Support/obsidian/obsidian.json`

   Also offer to type a path, or to create a minimal new vault.
2. **Inspect, read-only:**
   - frontmatter keys
   - daily-note format (`.obsidian/daily-notes.json`)
   - plugins (`.obsidian/community-plugins.json`)
   - a rulebook (`CLAUDE.md` / `AGENTS.md`)
   - whether it's git-backed, and any merge conflicts
3. **Propose, then confirm:** what JARVIS reads, where it writes (the log folder,
   additive), and which files are protected. Nothing is written before a yes.
4. **Configure:** write the per-OS `vault.path`, build the index, run a health check.
5. **Optional:** register the read-only vault MCP (`vault_mcp.py`, exists).
6. **No vault:** everything still works, and the log is back-filled when a vault is connected later.

## Your example, end to end (targets, to be measured)

> "Jarvis, open chrome and go to youtube on my archlinux machine and go spotify on my dell windows 11 pc"

| t | Layer | What happens |
|---|---|---|
| 0 | L0 | wake word ✓, speaker ✓ → envelope: origin `dell`, class `owner` |
| < 5 ms | L1 | strict parser: one certain reading, mode `act`, `c1 open_url(zenbook, youtube.com, chrome)`, `c2 open_app(dell, Spotify)`; validation ✓. No model |
| < 1 ms | L2 | the registry fixes `direct` + `allow` for both. No decider |
| < 10 ms | L3 | U-checks ✓ (Spotify in the Dell's app cache, the Zenbook heartbeat fresh), H-checks ✓, nothing to confirm. No vault fetch: direct intents use no context |
| ~0.1-0.3 s | L4 | c1 via long-poll to the Zenbook; c2 locally. In parallel |
| ~1-5 s | L5 | observers settle: a browser title with "YouTube" on the Zenbook, a Spotify-class window on the Dell (cold starts take longer) |
| | reply | "YouTube is open on the Zenbook and Spotify on the Dell." |

Variant: "...and play my liked songs on spotify on the dell". The interpreter returns
`spotify_play(dell, liked)`. JARVIS opens `spotify:collection:tracks`, sends `media play`,
and the playback state confirms it: about 2 s, no Grok. (The deep link is untested; if it
fails before anything plays, it escalates once to Grok.)

## Decider backend

**Interface first**, so backends can be swapped:

```python
decide.run(job, state, questions) -> {question: {"winner", "probs": {opt: p}, "confidence"}}
```

- **Batched:** all of a job's questions are answered in one model run that shares the
  prompt prefix (KV cache reuse). J2's twelve questions are one call, not twelve.
- **VRAM budget:** Whisper `large-v3-turbo` already uses the RTX 4060. Measure both
  together, give the decider a fixed budget, use a small quantized model, and keep a CPU
  fallback. The deadline (`decisions.deadline_ms`) falls back to rules mode, never to a guess.
- **Candidates** (accuracy claims are the authors' own, so judge them on JARVIS's set):
  logit scoring over a small local LLM first, Laya (Apache 2.0, 421M) second. The paid
  Jev API is excluded by the no-API-credits rule.

### Calibration

Raw model probabilities aren't trusted. For each job:

1. **Collect** labelled cases from the history (verified levels only) in shadow mode.
2. **Fit** a calibration map (temperature or isotonic) and plot reliability.
3. **Set thresholds from measured precision.** A job may auto-pass only where measured
   precision meets its target (for example, J2's "pass" side ≥ 98%). The **uncertainty
   band** is wherever precision falls short. It is computed, not guessed.
4. **Switch on** per job only after it meets the target on held-out data. `decisions.enabled: false` is the kill switch.

## Security model

1. **Cascade and ratchet:** rules decide, later stages can only restrict, and the stricter verdict wins.
2. **One owner per decision,** and the registry is the single source of executor and risk.
3. **Trust boundaries are explicit:** direct actions are guaranteed; Grok is constrained
   only at the prompt, and its results are claims.
4. **One brain:** cognition, memory and the vault are on the main. Nodes run typed
   actions and re-validate them. Offline mode is limited to allow-risk actions on the node itself.
5. **No arbitrary shell:** new abilities are new registry rows.
6. **Least privilege:** per-node capabilities and revocable tokens, host-key-pinned SSH,
   and a loopback-only hub unless TLS is configured.
7. **Untrusted input is data:** vault text, web pages and Grok answers are never instructions.
8. **At-most-once:** idempotency keys, outcome states, and escalation only on `pre_effect_failure`.
9. **Confirmation is bound** to the exact command hash, expires, and respects the wake word.
10. **Privacy at the source:** observers match on the node, and the vault log is redacted.
11. **Loops and runaways:** origin classes, in-flight merging, rhythm detection, Grok caps and cooldowns, and `jarvis stop`.
12. **Kill switches** per layer: `decisions.enabled`, per-executor flags, cluster off.
13. **Audit:** every check, decision, confirmation and outcome, with its verification level.

## How this maps onto the code

| Piece | Today | v2 |
|---|---|---|
| `assistant.ask` | routes and runs everything | thin: envelope → pipeline |
| `router.py` | regex tiers | fallback in rules mode |
| `grok_preflight.py` | 2 ChatGPT passes, 10 checks | J2's rules-mode fallback; later the LLM review inside the band |
| `remote.py` / `actions.py` | typed desktop actions | direct executor + node-side `observe(predicate)` |
| `grokbot.py` | typing + handoff inbox | grokbot executor + hourly cap and cooldown |
| `cluster.py` | short-poll, main/node | **long-poll**, condition-based waits, `handover`, offline-mode journal |
| new `registry.py` | none | the Intent Registry and outcome states |
| new `brain.py` | `chatgpt.py`, `fastlane.py` separate | one provider interface: ChatGPT, Claude, local; failover |
| new `interpret.py` | none | warm ChatGPT interpreter lane, mode triage, validation, learned templates, offline fallback |
| new `JARVIS.md` | none | self-model: identity, owner, machines, main/default, rules; cross-checked with config |
| new `gates.py` | none | U- and H-checks, risk ratchet, confirmation queue |
| new `history.py` | none | `commands` / `hops`, verification levels, H-checks' queries |
| new `pipeline.py` | none | Command Graph runner (parallel, deps, outcome states, trace) |
| new `decide.py` | none | J1-J3, batching, calibration, shadow logging |
| `vault.py` | additive notes, recall | + redacted log writer, `connect`, log folder excluded from recall |
| `web/flow.js`, `events.py` | live connection tree | + route lanes, gates, confirmations, waterfall, topology, replay |

## Build order: thin slices

Each slice works end to end before the next starts.

0. **Stabilise:** green test suite, commit the Codex work (on David's go-ahead), update `architecture.md`.
1. **Local direct path:**
   - registry, parser (with its labelled test set), U-checks
   - direct executor on the Dell, `observe` with settle times, outcome states
   - history with H1 (warn) and H3, and the route view

   Your example works on the Dell with no model.
2. **Remote direct path:**
   - long-poll cluster, tunnel manager, enrol the Zenbook
   - fixed route order with probes, node offline mode

   Your full example works across both PCs.
3. **Consequences:** the confirmation queue and risk ratchet (rules mode); Grok
   `agent_task` through the existing preflight, with caps.
4. **Records:** the redacted vault log and `vault connect`.
5. **Decider:** J1-J3 in shadow, then calibration. Switch each job on when it meets its
   target, after which the Grok review only runs inside the band.
6. **Later, with more machines:** a route cost model, and placement in the orchestrator.

## Review traceability

| # | Finding | Fixed by |
|---|---|---|
| 1 | Verification failure escalated, causing duplicates | outcome states; escalation only on `pre_effect_failure`; settle times |
| 2 | Grok outside the guarantees | trust-boundary table; Grok results are `agent_claimed` |
| 3 | Rules can't classify Grok risk; risk had two owners | registry risk for direct intents; ratchet (max of rules, decider, novelty, review) for Grok |
| 4 | No confirmation protocol | confirmation queue: ID, hash binding, expiry, wake-word voice approval |
| 5 | U10 and H1/H3 blocked normal use | U10 scoped to action intents; origin-aware H-checks (warn for owner, block for agents) |
| 6 | Privacy contradictions | node-side predicates; redacted vault log; full trace only in the DB |
| 7 | Uncalibrated probabilities | calibration per job; thresholds and band from measured precision; shadow until met |
| 8 | Sparse or biased learning signals | verification levels; active route probes; templates instead of exact phrases |
| 9 | Device-attachment errors and lexicon collisions | prefix, suffix and carry-forward attachment; tagging priority; superseded by the ChatGPT interpreter (#20); labelled correctness suite |
| 10 | The Dell as a single point of failure | node offline mode (allow-risk, self-only, journal replayed as records) |
| 11 | Scope too big | thin slices; cost model and placement deferred |
| 12 | Decider used where rules decide | the registry decides; the decider has only three jobs |
| 13 | Too many model calls | batched questions per job; VRAM budget; deadline falls back to rules |
| 14 | Short-poll latency | long-poll and condition waits; online window about 45 s |
| 15 | Cost optimiser overkill | fixed preference order + health; optimiser only with three or more nodes |
| 16 | Vault fetched for every command | tag at parse time; fetch snippets at compile time only for context executors |
| 17 | Grok quota unchecked | U7 hourly cap and single-flight; U8 cooldown after rate-limit errors |
| 18 | No route for plain conversation | interpreter mode triage; `converse` mode with persona, memory and read-only tools |
| 19 | "Default PC" undefined | `JARVIS.md` self-model, cross-checked against config at startup |
| 20 | Rigid parser rules (verb-less objects, phrasing) | ChatGPT interpreter; rules only validate |
| 21 | Liked Songs needed Grok | `spotify_play` deep-link intent, no Grok |

## Open decisions for David

1. ~~Direct actions or always Grok?~~ **Decided 2026-10-26:** direct first; Grok for judgement.
2. ~~ChatGPT or an algorithm for splitting?~~ **Decided 2026-10-26 (final):** instant phrases →
   strict parser (only certain, single readings) → warm LLM interpreter with `JARVIS.md`
   for everything else; rules validate both.
3. **Confirm list:**
   - the registry's direct risks (only `close_app` and `build` confirm)
   - the Grok keyword list in the [risk ratchet](#risk-ratchet)

   Add or remove anything?
4. **Precision target** for auto-passing a decider job (proposed 98%). This replaces
   guessing the uncertainty band.
5. **Vault log folder:** `jarvis/log/`. Your vault's `CLAUDE.md` would need to list it.
6. **Offline-mode scope:** only allow-risk direct actions on the node itself, as proposed, or narrower?
7. **Confirmation expiry:** 60 s for voice, 10 min for console?
8. **Grok cap:** how many Grok tasks per hour per device (proposed 20)?

## Built status (2026-10-26)

| Piece | State | Notes |
|---|---|---|
| Registry, parser, U-gates, fast path, `observe` | built, live-verified | "open notepad" observed in ~1.4 s on the Dell |
| History (H1 warn / H3 loop) and console replay | built, live-verified | `/api/history/commands`; `history.sqlite3` in the state dir |
| Confirmation queue + risk ratchet | built, live-verified | "close word" parks C1; "Jarvis, yes" runs it through the gates |
| Volume on Windows | built, live-verified | set and read exactly through Core Audio (IAudioEndpointVolume); "mute" no longer toggles |
| Idempotent play/pause on Windows | built | reads the media session first; never toggles blind |
| `spotify_play` | routed to Grok Bot | direct deep link can't start Liked Songs (tested live) |
| Zenbook as a cluster node | built, live-verified | tunnel `ssh -R 7780` (LAN, Tailscale fallback), node as a systemd user service; "open chrome on arch" observed in 1.8 s via the cluster; SSH fallback in 0.4 s when the node is stopped |

**Ports.** The cluster hub listens on `127.0.0.1:7780`. Port 7778 is the Grokbot tunnel's
forward on the Zenbook (to the Dell's relay on 7779); the two must not share a port.

**Node liveness.** A node is sent a job only if it holds a poll whose socket is still
connected, checked in within 10 s, or is running a job. A stopped node therefore falls
back to SSH within ~10 s instead of a 45 s timeout. "online" (75 s) only means seen recently.

**Resilience, verified live 2026-10-26:** after a Zenbook reboot the node rejoined on its own
(38 s offline; next action observed in 0.6 s). After a Wi-Fi change (iPhone data, then back)
the tunnel cycled through its targets and recovered over Tailscale (48 s offline; next action
observed in 0.6 s). Fixed on the way: the tunnel worker died when its status file was read
during a write (Windows refuses the replace); writes now retry and skip instead.
