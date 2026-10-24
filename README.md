# JARVIS

A local assistant and workflow layer for David's Dell Windows laptop and ASUS
Arch/Hyprland laptop, with a Python CLI, web console, desktop overlay and voice loop.

See the [workflow review and optimization results](docs/pipeline-architecture-review.md)
and [independent audit](docs/pipeline-independent-audit.md) for the current architecture,
measured improvements, verification and prioritized follow-up work. Downloaded
[architecture skills](skills/README.md) are available as project-local references.

The follow-up [pipeline hardening report](docs/pipeline-hardening-phase2.md),
[feature coverage matrix](docs/feature-validation.md), and
[provider quota report](docs/provider-limits.md) record the latest changes and checks.

## Talk to it

```powershell
jarvis chat                                                         # type; ChatGPT answers, read aloud
jarvis ask "open spotify"                                           # actions: apps + JARVIS browser
jarvis ask "go to news.ycombinator.com and read me the top headline"
python -m jarvis ask "what did I decide about the clipboard?"     # one-shot, remembered
python -m jarvis ask "..." --speak                                  # + spoken reply
python -m jarvis route "build a production recon dashboard"         # dry run: which tier
python -m jarvis orchestrate plan "build ..."                       # complex: plan only
python -m jarvis orchestrate run plan-1a2b3c4d                      # then execute it
python -m jarvis mem search "lan mouse" --kind note                 # memory heap
python -m jarvis mem add "fact to keep" --tag project --importance 0.8
jarvis voice enroll                                                 # record your voiceprint
jarvis voice listen                                                 # "Jarvis, ..." loop (foreground)
jarvis voice start | stop | status                                  # background listener
jarvis voice autostart on                                           # listen from login (on)
jarvis voice test | recalibrate                                     # check / rebuild voiceprint
```

Tiers, agent hierarchy, memory and voice-cost design: [docs/architecture.md](docs/architecture.md).

Web tasks get two browsers, both driven by ChatGPT over MCP with their own Chrome
profiles under `.jarvis/`: Playwright MCP (`browser_*`, primary) and Vercel's
[agent-browser](https://github.com/vercel-labs/agent-browser) (`agent_browser_*`, core
tools only; `agent_browser_read` fetches a page as text without opening Chrome). Pick
with `actions.browser_engines` in `jarvis.config.json`. Page scripts (`eval`), uploads,
raw CLI flags, other sessions and screenshot files on disk are refused.

## Second brain (Obsidian)

JARVIS reads and adds to your Obsidian vault (`vault.path` in `jarvis.config.json`, now
`C:\Users\jdoe\Documents\Vault`). It follows the vault's own `CLAUDE.md` rules, not a
layout of its own. Built on the ideas in
[obsidian-second-brain](https://github.com/eugeniughelbur/obsidian-second-brain) (MIT);
the code is new, uses only the standard library, and lives in `jarvis/vault.py`.

- **It knows what's going on.** Each brain conversation starts with a short brief built
  from the vault: owner facts (`MEMORY.md`), current courses, active projects with
  their next step, deadlines in the next 14 days, today's and tomorrow's agenda from
  `calendar.md`, and today's priorities. "When is my OS midterm?" is answered from
  your notes on the fast chat brain.
- **It recalls the right notes.** Every request gets up to 4 relevant notes (900
  chars) as evidence, or nothing when the match is weak. `reference/` transcripts are
  searchable but never injected automatically. The orchestrator's planner gets the
  same recall.
- **It remembers when you ask.** "Remember that…", "note that…", "log that…", or
  anything about "my notes" or "my vault" goes to the tool brain. It can search, read,
  log a dated line in today's daily note, create a note in `notes/`, `projects/` or
  `people/`, or append to a section of an existing note.
- **Writes are additive only.** JARVIS never deletes, moves or rewrites text. It
  refuses `calendar.md` (generated), `archive/`, `templates/`, attachments, and any
  file with git merge-conflict markers. New notes get the vault's frontmatter schema
  and a link from today's log, so none are orphaned. Every write goes to
  `.jarvis/vault-writes.jsonl`, and obsidian-git's history is the undo. Set
  `"write": false` to make it read-only.

```powershell
jarvis vault world                          # the brief the brain starts with
jarvis vault search "page replacement"      # ranked notes (BM25 + type + freshness)
jarvis vault recall "when is my OS midterm" # exactly what a request would be given
jarvis vault read operating-systems         # by path, [[link]], filename, title or alias
jarvis vault log "Finished OS assignment 2"
jarvis vault capture "LRU vs FIFO" --body "Belady's anomaly hits FIFO." --tag computer-science
jarvis vault append drone-blueprint --section "Next step" --text "- [ ] Pick a flight controller"
jarvis vault health                         # schema, filenames, broken links, orphans, conflicts
jarvis vault backlinks emotion
jarvis vault brief [--speak]                # the local morning briefing (below)
jarvis vault open operating-systems         # show a note in Obsidian; also: today, tasks
jarvis vault clip https://example.com/post  # save a web page into reference/
jarvis vault graph                          # notes and [[links]]; most-linked notes
```

**Briefings without the brain** (`jarvis/briefing.py`). "Jarvis, brief me", "good
morning", "what's on today?", "what do I have tomorrow?", "what's due?" and "anything
overdue?" are answered from `calendar.md`, the task agenda and the daily note in
about 20 ms. Like the clock answers, they skip ChatGPT entirely, so they keep working
offline and when the plan's usage limit is reached. Anything more specific ("brief me
on the OS midterm") still goes to the brain.

**Obsidian from voice.** "Open my OS note" or "open today's note" uses `vault_open`,
an `obsidian://open` link the Obsidian app handles. Today's daily note is made from
your template if it doesn't exist yet. "Save this article to my vault" uses
`vault_clip`: the page's readable text goes to `reference/<title>.md` with its source
URL, and today's log links to it. Clipped text is quoted material, never
auto-recalled, never treated as instructions. Local and private addresses
(127.0.0.1, localhost, 10.x, 192.168.x) are refused, including through redirects.
Pages that need JavaScript yield too little text and are refused; use the browser
for those.

The console API also has `GET /api/vault/graph` (notes, links, degree and an
`obsidian://` link per note) and `GET /api/vault/brief`.

### Tasks

One task list, kept in the vault (`jarvis/tasks.py`). A task is a checkbox line in the
note it belongs to, in the Obsidian Tasks plugin format your notes already use:
`- [ ] Pick motors ⏫ 📅 2026-10-23`. Tick it in Obsidian and JARVIS sees it. Tell
JARVIS "I finished assignment 2" and it ticks the same line, adding `✅ 2026-10-22`.
Dataview reads the same fields, so the daily note's Due query sees them too.

- **Where tasks go:** in their project or course note, under its Tasks/Next step
  section. Tasks with no home go to the Inbox in `tasks.md` at the vault root, which
  also has live Tasks-plugin queries for Overdue, Today, Next 7 days and In progress.
- **What counts:** checkboxes in every note except `archive/` and `templates/`. From
  daily notes, only today's Priorities and lines with a date or priority marker
  count: routine checklists and copied backlog aren't tasks. Leftovers in a
  `status: completed` course are ignored. Notes that are mostly checklists (study
  plans, 15+ boxes) show as progress (123/266) instead of hundreds of cards.
- **Fields:** `📅` due, `⏳` scheduled, `🛫` start, `🔺⏫🔼🔽⏬` priority, `🔁 every …`
  repeat, `✅`/`❌` done/cancelled dates. Dataview `[due:: …]` is read too, and so is
  the older prose "due October 13, 2026".
- **Edits are one verified line:** status (done, start, cancel, reopen), due date or
  priority. If the line changed since it was read, the edit is refused. Completing
  an `🔁 every week` task adds the next occurrence above it, like the plugin does.
  Task text is never rewritten and lines are never deleted. Every edit is audited.
- **The brain** gets overdue, today and this-week tasks in its brief, so "what's due?"
  is answered fast. "Remind me to…", "add a task…", "mark … done" and "I finished…"
  go to the tool brain (`task_list`, `task_add`, `task_update`).

```powershell
jarvis tasks                                    # agenda: overdue, today, this week, anytime, checklists
jarvis task add "Buy printer paper" --due friday --priority high
jarvis task add "Pick motors" --note drone-blueprint --due +3d
jarvis task add "Water the plant" --due today --every "every week"
jarvis task done "printer paper"                # by its words, or by the id `jarvis tasks` prints
jarvis task start a397dfd8 | cancel ID | reopen ID
jarvis task due "pick motors" --to "next monday"
jarvis task priority ID --to highest
jarvis runs                                     # agent runs (the old `jarvis tasks`)
```

The console has `GET /api/vault/tasks` (the agenda), `POST /api/vault/tasks`
(`{text, due, priority, note}`) and `POST /api/vault/tasks/<id>`
(`{action, value}`). It announces `vault.changed` on the live stream when any note
changes, from Obsidian or from JARVIS.

**Other agents.** `jarvis vault mcp` is a stdio MCP server with the same tools. Its
read-only form is registered for Claude Code in `.mcp.json`; approve `jarvis-vault`
the first time Claude Code asks. Add `--write` to offer log, capture and append.
For Codex: `codex mcp add jarvis-vault -- C:\Users\jdoe\MiniConda3\python.exe -m jarvis vault mcp`.
`jarvis vault hook` is an opt-in Claude Code `UserPromptSubmit` hook that adds recall
to every prompt. It is not registered by default.

The index is `.jarvis/vault.sqlite3` (SQLite FTS5). It updates incrementally on every
query: about 0.2 s to build for 221 notes, and 3–25 ms per search.

## Knowledge engine (a wiki that writes itself)

Karpathy's second-brain pattern, run by three bots that are plain scripts. The vault
above is David's own writing; this is JARVIS's wiki of what its sources say.
It lives in `.jarvis/knowledge/` (`knowledge.path` to move it).

- **Reader collects.** `raw/` keeps every file, link, note or voice memo exactly as it
  arrived, read-only and SHA-256 hashed, with a text extract (`pdftotext` for PDFs,
  Whisper for audio). Nothing edits `raw/`.
- **Librarian writes.** It pulls declarative claims out of a source, files each into
  the page it's about (TF-IDF routing) or into new topic pages (a greedy cover over
  noun-like terms), reconfirms claims a new source repeats, relinks related pages, and
  logs every atomic write with its diff. One source usually touches 5–20 pages.
- **Checker doubts.** Nightly it re-hashes `raw/`, looks for contradictions across the
  whole wiki, marks claims with no fresh source for 30 days `[!unconfirmed]`, and lists
  orphan pages and broken links. A contradiction is never overwritten: both claims stay,
  both get `[!conflict …]`, the page goes to `status: review`, and you get a
  notification.
- **You rule.** `keep` one claim, `drop` it, or say `both` are true. The loser is
  marked `[!superseded …]`: it stays on the page as history but is no longer evidence.
  Each bot keeps its own memory in `bots/`. The checker remembers your rulings so it
  never raises the same pair again. The librarian remembers merged pages, so future
  claims about a merged topic go where you put them.

Every claim shows the claim, its sources and the date it was last confirmed:

```
- The review routine runs daily at 02:00. ^ca8117b — source: [[source-001]] · confirmed: 2026-09-01
```

Contradictions are found without a model. Claims about the same subject (shared
content words) that disagree on a facet are flagged: how often (daily ≠ weekly), a
quantity with units (16 GB ≠ 32 GB; 90 s and 1.5 min agree), a clock time, a port or
version, on/off, or a negation.

Answers come from claims, with citations. An answer that cites two or more claims is
filed back as `wiki/answer-NNN.md`, so the wiki grows from use as well as reading.
Asking the same question again refreshes that page. Set `knowledge.llm` to `claude`
(or pass `--llm claude`) to let Claude phrase the answer. Its text is only accepted
if every citation is one of the retrieved claims; otherwise JARVIS uses the plain
extractive answer. Wiki claims are also part of what `jarvis ask` and the voice
listener recall.

```fish
jarvis kb ingest notes.md paper.pdf https://example.com/post memo.m4a
jarvis kb ingest --text "Lan Mouse uses UDP port 4242 on both laptops."
jarvis kb ask "how often does the review routine run?"
jarvis kb sync                       # read your Obsidian vault in (changed notes only;
                                     # one wiki page per note; daily/, templates/, archive/ skipped)
jarvis kb check                      # the nightly audit, now
jarvis kb conflicts                  # what's waiting on you
jarvis kb resolve c5513ad keep ca8117b   # or drop / both; the last id limits it to one pair
jarvis kb merge later review         # fold a page into another; the librarian remembers
jarvis kb schedule on --at 02:00     # systemd user timer (Task Scheduler on Windows)
jarvis kb status | log | sources | page review-routine
```

The console's **Wiki** link (`/knowledge.html`) shows it live: the graph turns,
and each reader/librarian/checker event pulses the node it touched (`LOCKED`, `PATCH`,
`SAVED`, `CONFLICT`). Eight panels show the shared chat, write queue, page inspector,
graph index, raw sources with hash checks, the latest diff, a page × source citation
matrix and the nightly checker. When the checker has flagged something, the inspector
shows the two claims side by side with **Keep A / Keep B / Both true**. Drop files
anywhere on the page (or use **+ FILE**) to hand them to the reader: text, HTML, PDF
or audio, up to 200 MB each. The command line takes `ingest URL`, `note TEXT`,
`ask QUESTION`, `check`, `resolve CLAIM keep|drop|both [AGAINST]` and `merge FROM INTO`.

## System design page

The console's **System** link (`/design.html`) explains how JARVIS works and checks it,
live:

1. **What happens when you say "Jarvis, …"**: seven stations (hear, decide, remember,
   think, act, speak, log). Each is described in plain words, shows a health dot, and
   opens to the real modules behind it. Type a request into **Try a request** to light up
   the path it would take.
2. **Is everything working?**: every requirement (voiceprint, microphone, vault, CLIs,
   browser, voice files, nightly check) with the exact command that fixes it.
3. **Where your data lives**: each store, what it holds, how big it is.
4. **Under the hood**: modules grouped by station, with their imports.

## Console

Double-click **`start.bat`**: it starts the voice listener if it isn't running, then
opens the console as its own app window (Edge or Chrome `--app`: no tabs or address
bar). Closing the window stops the console server about 15–25 s later; the listener
keeps running. If the console is already up, it just opens another window onto it.

```powershell
C:\Users\jdoe\MiniConda3\python.exe -m jarvis app         # app window (what start.bat runs)
C:\Users\jdoe\MiniConda3\python.exe -m jarvis ui --open   # plain server + browser tab, http://127.0.0.1:7777
```

A local web console over the same `.jarvis/` state: prompt bar with tier choice,
push-to-talk (hold Space; audio goes only to this laptop's voiceprint check and
Whisper), the always-on listener toggle, date and Open-Meteo weather, provider
credits, a force-directed knowledge graph (memory heap plus the module graph read
from real imports), a system-design diagram, memory search, the task ledger, and
devices. Use the MiniConda interpreter; the PATH `python` lacks the voice packages.

Credits are labelled by source. **Live**: DeepSeek, OpenRouter and ElevenLabs
balance endpoints (set `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`; the ElevenLabs
key needs the *User → Read* permission). Claude and Codex subscription windows
are also read live using existing CLI sign-ins. Their direct usage endpoints are
internal compatibility interfaces; unavailable readings fall back to local logs.
**Local**: Codex window snapshots from `~/.codex/sessions` and a Claude Code token
tally from `~/.claude/projects`. Expired snapshots do not imply a refilled quota.
See [provider limits](docs/provider-limits.md) for sources and supported credentials.
**Manual**: GLM, Z.AI, Grok and Muse have no public balance endpoint; enter
the figure in the Credits view and it is shown with the date you entered it.
Binds to loopback only, rejects foreign `Host` headers, and requires an
`X-JARVIS` header on every POST.

**Live wiring** (`jarvis/events.py`). Every request is one *turn*, whichever
channel it came from: the console, the voice listener, or `jarvis ask`/`chat`. Console
asks run on the same warm brain lane as voice (ChatGPT by default, ~1–2 s) instead of a
cold one-shot run, and replies stream into the Session thread sentence by sentence;
read-aloud starts with the first sentence. Other processes append `turn.*`/`voice.*`
events to `.jarvis/events.jsonl`; the console tails it and also watches memory, the task
ledger, plans and the listener PID. Everything goes to the browser as Server-Sent Events
on `/api/events`; the LIVE chip shows the stream is connected. Polling remains only as
a fallback. Restart the listener (`jarvis voice stop; jarvis voice start`) and the
console after upgrading so both run the new code.

**Overlay:** `python -m jarvis overlay` keeps the console as a full-screen,
always-on-top, translucent window that Alt+1 shows and hides from any app
(`--key ctrl+alt+j` for another key). Hiding never closes the page. It hosts its
own console on port 7790. `overlay --autostart on` starts it hidden at login via
`JARVIS Overlay.vbs` in the Startup folder (delete the file, or `--autostart off`,
to stop); a windowless run logs to `.jarvis/logs/overlay.log`.

The Machines panel checks the link live every minute (`jarvis/probe.py`): the
Dell's Lan Mouse process and its UDP 4242 socket and peer, and on the Zenbook a
fixed read-only script over the configured SSH key (Lan Mouse units, socket,
free memory, swap, Wi-Fi power save, current IP). It warns when the Zenbook's
DHCP address no longer matches the paired one. "Daemons up" means both are
running and holding the port, not that input has been seen crossing.

## Working now

- Persistent SQLite task ledger and append-only lifecycle events.
- Provider-specific task prompts, a researched prompt-engineer definition, and a
  Claude Code subagent in `.claude/agents/prompt-engineer.md`.
- Local Codex and Claude Code launch adapters using their installed native CLIs.
- Read-only mode by default; explicit workspace-write mode for implementation.
- Saved prompts, raw output, errors, exit codes, and bounded execution times.
- One running task per ledger to avoid concurrent edits. Finished jobs cannot
  accidentally be run again.
- Windows/Linux hardware collectors, a fixed remote collector over trusted SSH,
  and a configured but unverified Zenbook endpoint.
- ElevenLabs text-to-speech adapter that writes MP3 files.
- Explicit integration states for clipboard, input, video and personal agents.

## Start

Python 3.10+; the kernel uses the standard library, with no pip installation.
Run from this directory:

```powershell
python -m jarvis doctor
python -m unittest discover -s tests -v
python -m jarvis add examples/review-kernel.json
python -m jarvis runs
```

The `add` command prints the new task ID. Substitute it below:

```powershell
python -m jarvis plan TASK_ID
python -m jarvis engineer TASK_ID
python -m jarvis run TASK_ID --timeout 900
python -m jarvis show TASK_ID
```

`plan` shows the invocation and task prompt without launching a model. `engineer`
prints a research/refinement request for a model; it does not itself call a model
or research the internet. The prompt-engineering subagent can be invoked from
Claude Code in this project. Its output is a proposed task contract.

`run` makes a real agent call using the CLI's existing authentication and may
consume subscription usage or API credits. It defaults to read-only. Set
`"mode": "workspace-write"` and `"role": "implementer"` in a task specification
when edits are intended. Claude uses `acceptEdits` for that mode; tool actions
requiring additional permission are denied in this noninteractive runner.
Codex uses its workspace-write sandbox. Neither adapter bypasses permission checks.

For another project, add its directory to `workspace_roots` in
`jarvis.config.json`, then set the task's `workspace` to that path. Paths are
resolved relative to the config file. This is launch admission control, not an
OS sandbox: the provider's tools and runtime enforce access during execution.
`owned_paths` communicate ownership to the model; they are not filesystem ACLs.
Use one state directory for this kernel; separate ledgers do not share locks.

State is in `.jarvis/`, excluded from version control. Agent logs may contain
project content. A zero exit code produces `finished`, not `verified`: acceptance
criteria still need checking. A machine or runner crash may leave a task marked
`running`; do not automatically retry it because it may already have changed files.
Dead-owner task claims can be marked failed by the ledger. Plans checkpoint results
and refuse automatic replay of a previously dispatched step without a result;
inspect its recorded task and artifacts before recovery. A per-plan OS lock excludes
overlapping runs and recovery on the same local filesystem. Inspect a plan with
`python -m jarvis orchestrate recover PLAN_ID`; add `--apply` to reconcile terminal
tasks and authorize reuse of original queued task IDs. Recovery never launches a
provider. Running or missing tasks require inspection; they are not guessed safe
to retry. Queued scheduling and live cancellation are not implemented.

Current offline validation and commands are recorded in the
[pipeline review](docs/pipeline-architecture-review.md#measurement-and-verification).
Tests include local child processes and mocked HTTP/SSH interfaces; they do not
establish live provider authentication, inference or physical hardware behavior.

## Running on Arch (Zenbook)

The same checkout runs on the ASUS. Commands below are fish-safe.

```fish
uv venv .venv --python 3.12                         # Arch's python refuses system pip
uv pip install --python .venv/bin/python -e '.[voice]'
mkdir -p .jarvis/voice/kokoro                         # local voice model, ~350 MB
for f in kokoro-v1.0.onnx voices-v1.0.bin
    curl -fLo .jarvis/voice/kokoro/$f https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f
end
./start.sh                                            # voice listener + console window
.venv/bin/python -m jarvis voice autostart on         # systemd user unit, starts at login
```

What differs from the Dell:

- **Devices.** The config is written from the Dell's side. On the machine whose
  hostname matches a device (`archmain`), that device becomes local, and the
  Dell is left out, since there is no SSH route to Windows. Drop-folder transfers
  therefore run from the Dell.
- **Apps.** `open_app` lists `.desktop` entries (including Flatpak) and launches
  them with `gio launch`; links open in the default browser.
- **Console window.** `jarvis app` uses `google-chrome-stable`, Chromium, Brave,
  Edge or Vivaldi in `--app` mode. The hotkey overlay is Windows-only; bind a
  Hyprland key to `start.sh` instead.
- **Voice.** Kokoro and Whisper run on CPU (no CUDA on the Zenbook, so the GPU
  model is never downloaded). The mic is read through PipeWire, because without
  `pipewire-alsa` ALSA's default device is the raw DSP mic and reads silence.
  Playback uses `pw-play`/`paplay`; the fallback voice is `espeak-ng`.
- **Vault.** `vault.path` takes `{"windows": ..., "linux": ...}`. A Windows path is
  treated as unconfigured on Linux. On the Zenbook it is `~/Documents/obsidian-vault`.
- **Not ported.** The Windows keyboard self-test (`selftest keyboard`) and the
  overlay. Both say so and skip.

## Hardware and SSH

See [the inventory](docs/hardware.md) for confirmed versus supplied specifications.

```powershell
python -m jarvis inventory --output .jarvis/inventory/local.json
python -m jarvis inventory --device zenbook --output .jarvis/inventory/zenbook.json
```

The ASUS endpoint is `jdoe@172.1.42.215` (Wi-Fi DHCP, so the address can
change). Its ED25519 host key was verified against the fingerprint read on the
laptop itself, and JARVIS logs in with the dedicated key `~/.ssh/jarvis_ed25519`.
Remote collection needs Python 3 on Arch, never prompts for or stores a password,
and sends the collector over stdin without installing anything. The ASUS login
shell is fish, so commands to paste on Arch must use fish syntax.

The live PowerShell tool probe succeeded for Dell hardware. In this restricted
agent environment, the Python collector's child PowerShell process was denied
CIM access; it reports that section unavailable instead of inventing values.
Run the collector in your regular terminal for a fresh full snapshot.

## Voice output

Default is **ElevenLabs** (set up below); whenever it can't speak (no key yet, over
`monthly_char_budget`, offline, out of credits) the reply falls back to **Kokoro**, a
local voice model: free, offline, no credits. Files live in
`.jarvis/voice/kokoro/` (`kokoro-v1.0.onnx`, `voices-v1.0.bin`, from the
kokoro-onnx `model-files-v1.0` release); package `pip install kokoro-onnx`.

```powershell
jarvis voice demo            # hear bm_george, bm_lewis, bm_daniel, bm_fable
jarvis voice use bm_lewis    # switch, then: jarvis voice stop; jarvis voice start
```

## ElevenLabs (paid)

`"engine": "elevenlabs"` under `voice` in `jarvis.config.json`; set it to `"kokoro"`
for local only.

Run `python -m jarvis voice setup` once: paste the key at a hidden prompt, pick a
voice from the list. Saved to `.jarvis/secrets.json` (git-ignored); environment
variables `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` override it.
Listening runs locally: faster-whisper large-v3-turbo on the RTX 4060 (≈0.3 s per
sentence warm) and sherpa-onnx speaker verification (≈0.5 s, CPU). Optionally
set `ELEVENLABS_MODEL_ID`; the default is `eleven_multilingual_v2`.
`.env.example` lists the names; `.env` files are not auto-loaded.

```powershell
python -m jarvis speak "JARVIS is ready." --output .jarvis/audio/hello.mp3
```

The one-shot `jarvis speak` adapter makes an ElevenLabs request and writes an MP3
without overwriting existing files. That command does not play or capture audio;
the separate `chat` and `voice` pipelines provide conversational playback/listening.
The adapter's HTTP behavior is covered with mock responses.
API contract: [ElevenLabs create speech](https://elevenlabs.io/docs/api-reference/text-to-speech/convert).

## ElevenLabs voice agent (live calls)

The **Live** button in the console dock starts a real-time voice call with an
[ElevenLabs agent](https://elevenlabs.io/docs/agents-platform/overview): ElevenLabs
handles speech recognition, turn-taking, barge-in (talk over it to interrupt) and the
voice. Anything beyond small talk goes back to JARVIS through the agent's `ask_jarvis`
client tool, so memory, routing, vault and actions are unchanged. The API key stays on
the server: the page gets a short-lived signed WebSocket URL from `/api/agent/session`.

```bash
jarvis voice setup          # once: ElevenLabs key + default voice -> .jarvis/secrets.json
jarvis voice agent-setup    # once: creates the "JARVIS" agent, saves ELEVENLABS_AGENT_ID
jarvis voice agent          # status
```

The key needs the ElevenLabs Agents and Voices permissions. To use an agent you made in
the dashboard instead, set `ELEVENLABS_AGENT_ID` (or `voice.agent_id` in
`jarvis.config.json`); it needs a client tool named `ask_jarvis` (string parameter
`request`, wait for response) and **Security → Overrides → TTS voice** enabled.

**Switching voices.** The voice picker next to the speaker button lists this account's
ElevenLabs voices. A pick is saved as `ELEVENLABS_VOICE_ID`, applies to live calls
(an active call reconnects with the new voice), and to read-aloud replies when
`voice.engine` is `elevenlabs`. Live calls bill ElevenLabs agent minutes.

Live-call audio goes to ElevenLabs, so the local voiceprint check doesn't apply to it;
push-to-talk (**Voice**) and the always-on listener still run locally.

## Markets (True Markets recon)

**Markets** in the console nav opens `trade.html`, the simple view: a plain-English
market pulse with a mood gauge and a market-cap heatmap (click a coin for its chart),
the pipeline drawn live (market data → strategies → signals → you decide → real
trading **off**, with a funnel line), each strategy's result as "$1,000 → $X vs
holding", and a three-tap builder (style, coins, how careful) with an animated result.
`/api/markets/pipeline` serves the pipeline (cached 60 s; templates on BTC/ETH/SOL run
until you save your own). If True Markets blips, the last good data (up to an hour
old) is shown instead of an error.

**Pro terminal** (`terminal.html`, linked from that page) is the Bloomberg-style view over
[True Markets](https://truemarkets.co) data. Monitor (top assets by market cap, 1h/24h
change, range, sparkline), chart with windows from 1H to 365D, security description,
intel (movers, breadth, BTC dominance, volatility) and a **strategy lab**.

```bash
jarvis markets setup ~/Desktop/truemarkets-org-api-key-*.json   # once per laptop
jarvis markets status | quote SOL | intel | monitor
jarvis markets strategies ; jarvis markets backtest <id>
```

The key file is copied to `.jarvis/truemarkets-key.json` (git-ignored, owner-only).
Sign-in is True Markets' ES256 challenge, done in pure Python, so no extra package.

**Recon only.** The client issues GETs against an allowlist of market-data paths and
contains no order, transfer or wallet code. This key's account endpoints answer 401
anyway.

**Strategies are data, not code:** tickers, window, bar size, entry rules (all must
hold), exit rules (any one), stop loss and take profit. A rule is `TERM OP TERM`, for
example `sma(20) crosses_above sma(50)`, `rsi(14) < 30` or `price > high(20)`.
Indicators are sma, ema, rsi, roc, zscore, high, low and vol. Backtests are long-only;
a signal on a bar's close fills on the next bar, so there is no look-ahead. They report
return vs buy-and-hold, Sharpe, max drawdown, trades, win rate, exposure and the live
signal (BUY/SELL/HOLD/FLAT). Five templates are included; saved strategies live in
`.jarvis/strategies.json`.

Terminal keys: `/` command line (`BTC`, `ETH GP 30D`, `DES SOL`, `TOP`, `NEW`, `BT`,
`TPL 3`, `HELP`), F1-F5 panes, ↑/↓ steps through the monitor.

## Unified desktop

See [desktop setup and acceptance checks](docs/unified-desktop.md).
Clipboard, input sharing, video streaming, and display extension are separate
connections. **Clipboard:** `jarvis clipboard start` on the Dell syncs text and images
with the Zenbook over the existing SSH link (`autostart on` to keep it running). Lan Mouse 0.11.0 is installed on the Dell; JARVIS generates each
side's config so each laptop trusts only the other's certificate fingerprint:

```powershell
python -m jarvis lanmouse fingerprint                  # this laptop
python -m jarvis lanmouse fingerprint --device zenbook # computed on Arch over SSH
python -m jarvis lanmouse config --peer-name archmain --peer-ip 172.1.42.215 `
  --position right --peer-fingerprint <zenbook fp> --output $env:LOCALAPPDATA\lan-mouse\config.toml
```

Only the certificate block is hashed; the private key in `lan-mouse.pem` never
leaves its laptop. The generated release binding is Left Ctrl + Left Shift +
Left Super (Lan Mouse calls this `KeyLeftMeta`) + Left Alt. On the Arch laptop,
press that chord to return control to the Dell; moving off the Dell's left edge
also returns control to Arch.

The Dell once appeared to lock when `L` was pressed, likely because its internal
keyboard emitted a hidden Windows key press with `L`. If it recurs, tap the
Windows key once; if it persists, check Dell SupportAssist warranty coverage
for the internal keyboard.

Status: paired and verified in both directions (ASUS sits left of the Dell).
Both daemons start at login: the Dell via `JARVIS Lan Mouse.vbs` in the Startup
folder, the ASUS via the `hyprland.start` hook in `hyprland.lua` (backup saved
as `hyprland.lua.pre-lan-mouse`). Pairing uses the Wi-Fi DHCP addresses, so if
either laptop's IP changes, regenerate that side's config.
Executable detection checks PATH, not every possible app installation.

Muse is owner-reported in logged-in Zen browser sessions. Grokbot is owner-reported
installed on both machines. Jev is planned, possibly as a custom agent. Their
product URLs and supported interfaces are still needed. No cookie extraction or
unverified app-control adapter is implemented.

OpenClaw, Hermes, gatewaylab, Switchboard, AI Chief of Staff, and n8n are not part
of the owner's stack.

## Next implementation milestones

1. ~~Verify the SSH endpoint and collect the ASUS inventory.~~ Done.
2. ~~Pair input sharing.~~ Done. Pair clipboard sharing; test both directions in real applications.
3. Pair screen streaming, then test a virtual extended display.
4. If paid speech is desired, configure ElevenLabs and verify its live audio output;
   Kokoro already supplies local speech.
5. Identify Muse/Grokbot interfaces and implement explicit task handoffs.
6. Harden the existing console, conversational voice loop and usage reporting;
   add scheduling and verified remote workers as needed. See the
   [prioritized architecture backlog](docs/pipeline-architecture-review.md#what-to-add-next-in-priority-order).
"# JARVIS" 
