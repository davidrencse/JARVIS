# JARVIS architecture guide (complete reference)

This is the entry point for anyone working on JARVIS, human or Claude, on any machine. It
covers what each part does, where it lives, which settings drive it and the rules it must
keep. Deeper design history is in `jarvis-os-architecture.md`; the move to a new main is in
`migration.md`; David's rules, machines and decisions are in the `jarvis-memory/` vault.

Contents: [1 What JARVIS is](#1-what-jarvis-is) · [2 Request flow](#2-request-flow) ·
[3 Modules](#3-modules-by-station) · [4 Main and nodes](#main-and-nodes) ·
[5 OS support](#5-os-support) · [6 Voice](#6-voice) · [7 Models and cost](#7-models-and-cost) ·
[8 Memory](#8-memory-and-the-two-vaults) · [9 Safety](#9-safety-and-checks) ·
[10 Configuration](#10-configuration-reference) · [11 State on disk](#11-state-on-disk) ·
[12 Commands](#12-commands) · [13 Tests](#13-tests) · [14 Known limits](#14-known-limits-and-open-work)

## 1 What JARVIS is

David's personal assistant and operating layer across his computers. He talks or types to
it; it answers, remembers, and acts on any of his machines: opening and closing apps,
links, media, volume, window focus, files, clipboard. It hands judgement tasks (anything in a
logged-in web account or app) to Grok Bot and software work to specialist agents.

Three principles shape everything:
1. **Cheapest path first.** Most requests run without any model (0 tokens). A model is used
   only when the cheap layers can't resolve the request.
2. **Never pay per token.** Only the ChatGPT (Codex) and Claude subscriptions plus free
   local models. No API keys for billed services in the request path.
3. **Checked actions.** Every direct action is gated before it runs and verified on screen after.

JARVIS's own self-description, loaded into every model call, is `JARVIS.md` (David edits it;
JARVIS never does). `selfmodel.py` appends the configured machines and the detected OS, and
flags any line of `JARVIS.md` that contradicts the config (the config wins).

## 2 Request flow

```
voice (daemon.py) / text (ask, chat, console)
  │
  ├─ intake: wake word "Jarvis" (voice only), speaker check before transcription
  ├─ instant.py       fixed phrases: time, date, greetings          0 tokens
  ├─ parse.py         strict grammar → typed intents                  0 tokens
  ├─ registry.py      intent → executor, argument schema
  ├─ gates.py         U-gates: target exists, risk level, confirmation needed?
  ├─ history.py       H1/H3: duplicate or contradicting a recent action?
  ├─ confirm.py       risky steps wait for David's yes (risk ratchet)
  ├─ remote.call      run here, or route to a node through the cluster hub
  ├─ observe.py       read the screen/audio state back to prove it worked
  └─ record           history.sqlite3 + memory + events.jsonl
        │
        └─ not parseable? ─► interpret.py (ChatGPT lane, action-like requests only)
                              modes: act / agent / converse / build / clarify
                                 │
                                 ├─ act/agent ─► back through registry + gates
                                 ├─ converse  ─► brain (chatgpt.py), read-only tools
                                 ├─ build     ─► orchestrator.py specialist hierarchy
                                 └─ clarify   ─► one short question to David
```

- `pipeline.py` runs a list of parsed commands; `DIRECT` holds the intents executed with no
  model. `UNCERTAIN` outcomes ("uncertain", unverifiable) are reported, never claimed done.
- `decide.py` runs beside this in **shadow mode**: an in-process MiniLM classifier predicts
  the route, logs it next to what actually happened, and does not decide anything until
  calibrated (temperature scaling, ≥ 98% precision on ≥ 50 labels).
- `router.py` picks tiers for free-form work: simple → one CLI run (Codex/Claude); web →
  browser agents; complex → `orchestrator.py`.

## 3 Modules by station

The console's system view (`sysview.py`) groups every module into a station; a test fails if
a module is not listed, so add new modules to `sysview.py`.

| Station | Modules | Job |
|---|---|---|
| Hear | listen, daemon, overlay | mic capture, VAD, speaker ID, Whisper, background listener, Windows overlay |
| Decide | router, assistant, instant, briefing, parse, registry, gates, pipeline, confirm, decide, interpret, selfmodel | turn words into checked actions or a conversation |
| Remember | vault, knowledge, memory, vault_mcp, tasks, history, embed | memory heap, semantic recall, David's vault, tasks, action history |
| Think | chatgpt, fastlane, runner, orchestrator, prompts, adapters, claude_auth, grok_preflight | model lanes, failover, agent runs |
| Act | actions, terminal, agent_browser, remote, grokbot, transfer, storage, clipsync, showcase, observe, sharedinput | executors per OS, cross-machine calls, Grok Bot, files, clipboard, Lan Mouse |
| Speak | speech, voice, agent | Kokoro TTS (local), speech queue, ElevenLabs live agent |
| Log | store, events, locking, cluster_store | task ledger, event feed, file locks, cluster job store |
| Console / ops | ui, __main__, sysview, cluster_cli, cluster_proxy, cluster_tunnel, deploy, node, platforms, autostart | CLI, web console, cluster control, code deploy, OS detection, login startup |

Key files in plain words:
- `assistant.py`: one turn end to end; `_interpret`, `_direct_turn`, `_shadow`, `_failover_reply`.
- `chatgpt.py`: the warm ChatGPT lane (Codex CLI), its context (`JARVIS.md` brief + vault
  overview + recall), the usage-limit cooldown (`note_usage_limit`, `usage_limit`).
- `remote.py`: `call(target, action, args)`; a local target runs here (`run_here`), others go
  to the cluster. Also `session_env()` borrows the Linux desktop's DISPLAY/WAYLAND vars for SSH.
- `actions.py` / `observe.py`: per-OS executors and their read-back checks (see §5).
- `agent_browser.py`: JARVIS's own Chrome (agent-browser CLI, pinned). It shares the session
  `jarvis` and `.jarvis/agent-browser-profile` with the act lane's agent-browser MCP server, so
  the console's **Agent screen** box (top of the Assistant panel, `web/agentscreen.js`, polling
  `GET /api/agent-browser`) shows whatever the model or a `browse` command is doing there.
  Saying "in the agent browser" turns open_url/scroll into the direct `browse` intent
  ("in the agent browser go to instagram and keep scrolling"), with no model involved. Without
  the phrase, scrolling still means David's own browser window. `frame()` never launches
  Chrome: it checks the session's Chrome process first. The box's buttons (`POST
  /api/agent-browser`) are back, reload, stop scrolling and close.
- `browser_gate.py`: both browser profiles stay signed in, so on top of `approve_browser` a
  click or key that looks like it commits something (send, post, submit, buy, delete…), Enter
  after typing outside a search box, a typed field with `submit`, or accepting a page dialog
  parks a `browser_action` confirmation and waits up to 180 s for David (console Confirm;
  during a voice request the listener is busy, so use the console). Keyword heuristic over the
  tool's arguments: it narrows prompt-injection risk, it doesn't remove it.
- `terminal.py`: the brain's `terminal` tool on any computer (`device`, like the desktop
  tools): bash, or PowerShell on Windows, 30 s limit, 6000 characters of output, and a
  working folder that persists per process so `cd` then `ls` works. `read_only()` lets
  looking commands run at once (ls, cd, pwd, cat, find without -exec/-delete, du, df,
  Get-ChildItem...; chained only with `&&` or `|`; no redirection, `$`, backticks, recursive
  grep, or paths naming secrets like `.ssh`, `.env`, `.jarvis`). Anything else is a
  `terminal` intent parked by `ConfirmGate` for David's OK and run by `run_confirmed`.
  It's a heuristic over the command text, not a sandbox.
- `cluster.py`: the hub (main) and node client; long polling; liveness; code-mismatch refusal.
- `deploy.py`: code fingerprint, `cluster update` (tar over SSH, keeps the previous copy for `--rollback`).

## Main and nodes

**Exactly one machine is the main**, set by `cluster.main_device` in `jarvis.config.json`.
Because it is a single id, two mains can't be configured. Every other entry in `devices`
is a node.

- **Main**: runs the brain, memory, the vault connection, history and the hub
  (`http://127.0.0.1:<cluster.listen_port>`). It is the default target for actions ("my main PC").
- **Node**: runs `jarvis cluster node`, long-polls the hub (25 s holds), executes jobs locally
  and reports. Nodes refuse jobs if their code fingerprint differs from the main's.
- Liveness: a node is online while a poll is held open and its socket is still connected, or
  it was seen in the last 10 s, or it is running a job. The broker gives up after 75 s.

### Setting up (two commands per machine)

On the **main** (the machine that will run the brain):

```
jarvis cluster init               # registers this PC by hostname, forward tunnel, hub at login
jarvis cluster invite dell        # one secret line: jarvis-invite-1:...
```

The main needs an SSH server (`init` and `doctor` print the per-OS fix if it is off). `invite`
finds this PC's addresses itself (default-route LAN IP, other IPv4s, `tailscale ip -4`, then
the configured targets); add more with `--address`.

On each **node**, in its JARVIS folder:

```
jarvis cluster join "jarvis-invite-1:..."   # or a file holding the code
```

`join` saves the link, starts the tunnel and the node agent, installs both at login
(`--no-autostart` / `--no-start` to skip), and waits for the hub to answer. An invite is
single-use and lasts 24 hours: on joining, the node redeems it (`POST /v1/redeem`) for its own
hub token and SSH key, so the code is dead afterwards. If the main can't write
`authorized_keys` (Windows admin accounts without an elevated shell) only the token rotates.
If the main isn't reachable during `join`, the node agent redeems on its next start. Then on either side:

```
jarvis cluster doctor             # every check in order; each FAIL carries its fix
```

What the invite contains, and why it is safe to paste once:
- the node's hub credential (rotates any earlier one for that node),
- the main's SSH host key, pinned by the node (no trust-on-first-use),
- a fresh ed25519 key made for this node only. On the main it is one line in
  `~/.ssh/authorized_keys` (comment `jarvis-node:<id>`) with
  `restrict,port-forwarding,permitopen="127.0.0.1:<port>",permitlisten="127.0.0.1:1",command="echo jarvis-node-link"`:
  no shell, no forward to anything but the hub, no reverse forwards (checked on OpenSSH 10).
  Windows mains with an admin account use `C:\ProgramData\ssh\administrators_authorized_keys`
  (run `invite` elevated).

The node keeps the link in `.jarvis/cluster-node.json` + `.jarvis/cluster-node.key`, never in
`jarvis.config.json`, so the config is the same committed file on every machine. Treat the
code like a password; `jarvis cluster revoke dell` on the main removes the credential and the
SSH key line, `jarvis cluster leave` on the node stops and forgets its side.

### Delivery: how we know everything made it

Transport integrity is SSH's job (encrypted, MAC per packet, TCP retransmits underneath); a
packet sniffer could not see inside it and would only add a privileged daemon. What matters
is whether each *job* arrived and answered, and that is tracked end to end:
- every job is acknowledged; a result is kept in the node's outbox until the main accepts it,
  and a job is never re-run automatically (at most once);
- `doctor` on the main counts the last 24 h of jobs per node: anything `expired` (never
  picked up) or `uncertain` (picked up, no result) fails the check;
- `doctor` on a node reads `.jarvis/cluster-link-stats.json`: polls that reached the main vs
  failed, results delivered vs resent, and the last error.

### Roles and other commands

```
jarvis cluster roles              # every device, its role, and which one is this PC
jarvis cluster main zenbook       # make zenbook the main; all others become nodes
jarvis cluster status             # this PC's role and the hub's view of the nodes
jarvis cluster update dell        # push code to an SSH node (forward nodes pull it themselves)
```

After switching the main, put the same `jarvis.config.json` on every machine, run
`jarvis cluster init` on the new main and invite the nodes again (their links point at the
old main).

**The SSH link** (`cluster_tunnel.py`) carries hub traffic; nothing listens on the network.
- `cluster.tunnel: "forward"` (what `init` sets): each node runs `ssh -L` to the main. Only the
  main needs an SSH server, so Windows nodes need none.
- `cluster.tunnel: "reverse"` (advanced, manual): the main runs `ssh -R` into each node, which
  needs an SSH server, a `target`, `host_key` and `identity_file` in config, and pairing with
  `jarvis cluster enroll NODE --output FILE` + `jarvis cluster join FILE`.
- Fallback addresses are tried in order. While on a fallback, the tunnel checks the earlier
  targets' port 22 every 60 s (backing off to 30 min if a switch back fails) and returns to the
  first one that answers.

Identity: `cluster.local_device: "auto"` matches this machine's hostname against
`devices.<id>.hostname`; `join` sets an explicit id only if the shared config names another
hostname for this node.

## 5 OS support

Nothing assumes Windows or Linux. `platforms.detect()` returns
`{os, name, version, desktop, label}`, for example `windows / Windows 11 / windows` or
`linux / Arch Linux / hyprland`. Add OS branches behind it, not as scattered `os.name` checks.

| Capability | Windows | macOS | Linux |
|---|---|---|---|
| Login startup (`autostart.py`) | hidden `.vbs` in Startup | LaunchAgent plist + `launchctl` | systemd user unit |
| Windows list/focus/close | Win32 | `osascript` (per app: close = quit) | Hyprland `hyprctl`, sway `swaymsg`, X11/GNOME/KDE `wmctrl` (+ `xdotool`) |
| Open apps / links | Start menu index | `open -a` | `.desktop` files / `gtk-launch` |
| Volume | Core Audio | `osascript` | `wpctl`, else `pactl` |
| Media | media keys | `nowplaying-cli` | `playerctl` |
| Monitors | Win32 | `system_profiler` (no positions) | `hyprctl`, `swaymsg`, `xrandr` |
| Overlay console | yes | no (use `jarvis app`) | no: a compositor key runs `jarvis app` (Hyprland: Super+Shift+J), which focuses an open window |

Plain Wayland compositors other than Hyprland and sway, and GNOME/KDE on Wayland, have no
window control; JARVIS says so instead of pretending. Everything beyond Windows and
Hyprland is unit-tested only so far.

Windows-only gotcha: inside the Claude desktop app, a new folder under AppData is
virtualized, and processes started from Claude's shells die with the shell. Long-running
workers are started through `explorer.exe` (see `cluster_tunnel.start`, `daemon.start`).

## 6 Voice

`daemon.py` runs the background listener (`jarvis voice start|stop|status|autostart`):

1. `listen.record_utterance`: 16 kHz mono; Silero VAD gate (or a loudness gate fitted to
   the room). On Linux it reads through PipeWire/Pulse, not raw ALSA.
2. Speaker check (`listen.VoicePrint`, sherpa-onnx ERes2Net, CPU) runs **in parallel**
   with Whisper (`faster-whisper`, CUDA if present, else CPU int8; `hotwords="Jarvis"`).
   - score ≥ threshold (≈ 0.36, `voiceprint.json`): David.
   - 0.24 ≤ score < threshold: a near miss (far from the mic, quieter, music behind him).
     Accepted only if the transcript starts with the wake word; never used for adaptation.
   - below 0.24: another voice. The transcript is cancelled, never read or logged.
3. Wake word (`assistant.wake_word`): the text must start with Jarvis (or a Whisper
   mishearing of it: Jervis, Jarvas, Javis). "Travis" and "Jarvis's …" don't count. This is
   David's hard rule: no always-on path acts without it.
4. Confident matches with the wake word adapt the voiceprint (enrollment clips stay pinned,
   learned ones rotate, 40 max).
5. Replies are spoken by Kokoro (local, voice `bm_lewis`), streamed sentence by sentence;
   "On it." plays while the brain thinks.

Debugging: `.jarvis/logs/voice.log` shows each utterance's score; "no wake word (score):
N words, not kept" means your voice was accepted but the transcript didn't start with Jarvis
(speech not meant for JARVIS is never written down).
`jarvis voice test` prints live scores; `jarvis voice recalibrate` rebuilds the print from
the saved clips; `jarvis voice enroll` runs the guided voice setup below in the terminal
(do this on a new mic or machine; `jarvis voice enroll "<mic name>"` picks a mic).

Voice setup (`onboard.py`, Settings > Voice, or opens by itself when a machine has no
voiceprint): JARVIS speaks each cue while the server records through the listener's own mic
path. It checks two seconds of room (a mic sending pure silence fails here), measures a
"Hey Jarvis, can you hear me?" to fit the Silero start/hold cutoffs to this mic and room
(`.jarvis/voice/mic.json`, with the chosen device), enrolls the seven phrases into
`enroll.new/` (swapped in only on success, so cancelling changes nothing), scores one test
phrase, then restarts the listener. The listener is paused for the run. On a node the
console answers `/api/voice/setup` itself: a voiceprint belongs to the mic it came from.
The speech gate starts at 0.4 and holds at 0.18 by default (hysteresis); "ignored another
voice (…, near miss)" marks clips that scored above `near_miss` but lacked the wake word.

## 7 Models and cost

| Path | Tokens | Engine |
|---|---|---|
| instant, parser, registry, direct actions | 0 | local code |
| decider (shadow), semantic memory | 0 | MiniLM ONNX in-process, CPU |
| interpreter | ≈ 1.1 k in | ChatGPT lane (Codex CLI, subscription) |
| brain turn | ≈ 1.9 k context + Codex's own prompt | ChatGPT lane |
| failover | ≈ 10 k in (lean prompt) | Claude CLI (Claude subscription) |
| Grok Bot / Muse | their own subscriptions | browser/app agents |

Failover: when ChatGPT reports a usage limit, `chatgpt.note_usage_limit` parses "try again
at 8:00 PM", every later call skips ChatGPT until then, and `assistant._failover_reply`
answers with the Claude CLI. If Claude is signed out, the reply says so. When testing the
Claude CLI from inside a Claude session, strip `CLAUDE_*` and `ANTHROPIC_*` env vars.

## 8 Memory and the two vaults

- **Memory heap** (`memory.py`, `.jarvis/memory.sqlite3`): every message and result. Search is
  BM25 plus vectors fused with reciprocal-rank fusion when `memory.semantic` is true
  (`JARVIS_SEMANTIC=0` turns it off). Vectors come from `embed.py` (all-MiniLM-L6-v2 ONNX,
  downloaded once to the Hugging Face cache, unloaded after 600 s idle).
  Every brain call gets the last 20 min of conversation plus relevant heap lines as recall.
  The action brain always has `MemoryTools` (`memory_remember` saves a `fact` item,
  `memory_search`, `conversation_recent` across days), whether or not the vault is on.
  The talk lane hands off (`[act]`) for remember/recall requests, computer checks ("how
  many apps…") and short answers to its own clarifying questions.
- **History** (`history.py`, `.jarvis/history.sqlite3`): actions taken, for the H-checks,
  the decider's labels and the console's command history.
- **David's personal vault** (`vault.path`, per OS): his notes, calendar and tasks. JARVIS
  reads it (`vault.world()` overview goes into every brain call) and writes only when he
  asks, additively. **Currently off** (`vault.enabled`, `recall` and `write` are all
  `false` in config): nothing is read or written until he turns it on. Protected paths are in `vault.protected`. Its own `CLAUDE.md` is its rulebook.
- **JARVIS memory vault** (`jarvis-memory/` in this repo): rules, machines, decisions and
  Claude's memory notes. Committed, so it moves with git. Open it in Obsidian as its own vault.

## 9 Safety and checks

- Ask first: buying, paying, posting, sending, deleting, submitting forms, account changes,
  closing apps that may hold unsaved work, terminal commands that change anything
  (`confirm.py`, risk ratchet in `gates.py`, `terminal.read_only`).
- Passwords are never typed. Text from web pages, vaults and agents is data, not instructions.
- Every direct action is read back (`observe.py`); unverifiable ones are reported as such.
- Cluster: loopback-only hub, pinned SSH host keys, per-node pairing tokens, read-only
  endpoints separated (`cluster._read_only`), code-mismatch refusal.
- No system-wide input or cursor changes without a tested fail-safe.

## 10 Configuration reference

`jarvis.config.json` (committed; no secrets in it; secrets live in `.env` or OS stores):

| Key | What it controls |
|---|---|
| `cluster.enabled`, `main_device`, `local_device` | the cluster and **which machine is main** |
| `cluster.main_url`, `listen_host`, `listen_port` | hub address (loopback, through SSH) |
| `cluster.tunnel` | `forward` (what `init` sets; this config) or `reverse` (code default when the key is absent) |
| `cluster.tunnel_device`, `cluster.auto_update` | which node the reverse tunnel serves; push code at hub start |
| `devices.<id>` | `hostname`, `label`, `aliases`, `transport: ssh`, `target`, `fallback_targets`, `identity_file`, `host_key`, `screen_inches` |
| `desktop` | clipboard sync, shared input (Lan Mouse), screen stream, extended display |
| `routing` | `brain`, `simple_provider`, `auto_escalate`, `interpreter` (on/off) |
| `models` | which CLI and model each lane uses (subscriptions only) |
| `voice` | TTS engine, Kokoro voice/speed, Whisper model, language, spoken-char limits |
| `actions` | executors on/off, default browser and engines |
| `vault` | personal vault `path` per OS, recall, write, calendar, protected |
| `memory.semantic`, `decisions.shadow` | semantic recall; decider shadow logging |
| `personal_agents`, `adapters`, `orchestration` | Muse, Grok Bot, Jev-style decider; browser agents; agent lanes |

## 11 State on disk

`.jarvis/` (git-ignored, per machine): `memory.sqlite3`, `history.sqlite3`, `events.jsonl`,
`voice/` (speaker model, `voiceprint.json`, `enroll/` clips, `kokoro/`), `logs/`,
pid/lock files, cluster tokens and job store. To move it, snapshot with SQLite's backup API
(safe while running) or stop the daemons first, then copy.

## 12 Commands

`python -m jarvis <command>` (or `jarvis` when pip-installed):
`ask`, `chat`, `route`, `voice`, `ui`, `app`, `overlay`, `cluster`, `desktop`, `clipboard`,
`lanmouse`, `grokbot`, `mem`, `vault`, `tasks`/`task`, `kb`, `orchestrate`, `runs`, `add`,
`doctor`, `inventory`, `selftest`, `credits`, `speak`. `--config` and `--state` point at
another config file or state folder.

Cluster actions: `configure`, `main`, `roles`, `status`, `run` (main), `node`, `enroll`,
`join`, `revoke`, `request`, `autostart on|off`, `tunnel start|stop|status|run`,
`update [node] [--rollback]`.

## 13 Tests

`python -m unittest discover -s tests` with an interpreter that has the voice extras
(the project `.venv`: `.venv/bin/python` on Linux, `.venv\Scripts\python.exe` on Windows). About 880 tests, all mocked: none touch the real
desktop, microphone or accounts. Test configs turn off semantic recall, shadow mode and the
interpreter. Live checks are `jarvis selftest keyboard` and `jarvis doctor`.

## 14 Known limits and open work

- Backends for macOS, sway and X11 desktops are unit-tested only.
- The interpreter has not yet been tested live end to end.
- Lan Mouse fixes are pending (a fail-safe cursor approach first).
- Instant greetings ("hi", "thanks" with no model) were offered, not built.
- The decider stays in shadow mode until it has enough labels to calibrate.
