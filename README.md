# JARVIS

**A personal assistant and OS layer that runs across David's computers.** Say "Jarvis, …"
or type a request. JARVIS works out what you meant, does it on the right machine, checks
that it happened and remembers it.

- Won the SpaceX Super Intelligence Track at Tech@NYU Shipyard

![The JARVIS console: machines, the live request engine, the trace of a request and the assistant thread](docs/images/console.png)

<sub>The console at `http://127.0.0.1:7777`. Left: machines and links. Middle: the
request engine and the trace of "what time is it", answered free in 0 s by the
instant-phrase step. Right: the assistant thread, showing what each reply cost.
"Mic blocked" appears only because headless Chrome refuses the microphone.</sub>

**Contents:** [Highlights](#highlights) · [Quick start](#quick-start) ·
[Architecture](#architecture) · [The console](#the-console) · [Proof](#proof-real-output) ·
[Safety rules](#safety-rules) · [Repository layout](#repository-layout) · [Docs](#docs)

## Highlights

| | |
|---|---|
| **Free first** | Most requests never reach a model. Instant phrases and a strict rule parser handle them on the PC at no cost. The parser scores 0.990 and has made 0 wrong calls on the 199-case scorecard (the case file is real voice transcripts, so it stays private). |
| **Subscriptions only** | When a model is needed, JARVIS uses ChatGPT through the Codex subscription and fails over to the Claude CLI. It never uses pay-per-token API credits. |
| **Asks before it spends or sends** | Buying, paying and messaging wait in a confirm queue. A hard gate refuses credentials, wipes and edits to its own files outright. |
| **Sandboxed** | Brain terminal commands on Linux run in bubblewrap. Secrets, browser profiles and `.jarvis/` are hidden, and JARVIS's own code is read-only. |
| **One assistant, many PCs** | One main, light nodes. Nodes long-poll the main's hub over an SSH tunnel and only run code that passed the full test suite. |
| **Drive the other PC** | The Remote view streams a node's screen into the console and sends your mouse and keyboard back. No new port is opened. |
| **Knows your voice** | An ERes2Net voiceprint checks it's the owner. Parakeet transcribes, with Whisper as backup. Strangers' speech is dropped unread. |
| **Memory that cites** | SQLite memory combines BM25 and vector search. It reads an Obsidian vault (never writes to it) and keeps a knowledge engine whose claims carry sources. |

It runs on two laptops:
- **Main:** an ASUS Zenbook on Arch Linux and Hyprland. Holds the brain, memory, voice and console.
- **Node:** a Dell G15 on Windows 11. Runs jobs, can be remote-controlled from the main, stays silent.

## Quick start

Requires Python 3.10+ (3.12 recommended) and [uv](https://github.com/astral-sh/uv).

On Linux (fish shell shown):

```fish
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e '.[voice]'
./start.sh                                        # voice listener + console window
.venv/bin/python -m jarvis voice autostart on     # start at login
```

On Windows, double-click `start.bat`.

Talk to it:

```fish
.venv/bin/python -m jarvis ask "turn bluetooth off"
.venv/bin/python -m jarvis ask "open spotify on the dell"
.venv/bin/python -m jarvis chat                   # type; replies are read aloud on the main
.venv/bin/python -m jarvis route "build a dashboard"   # dry run: which path it would take
.venv/bin/python -m jarvis voice enroll           # record your voiceprint
```

Cluster:

```fish
.venv/bin/python -m jarvis cluster roles          # who is main, which nodes
.venv/bin/python -m jarvis cluster doctor         # diagnose either side
.venv/bin/python -m jarvis cluster update dell    # optional: releases go out automatically after a green push
```

Setting up a new cluster: `cluster init` and `cluster invite <node>` on the main,
then `cluster join <code>` on the node. See the [cluster handbook](docs/handbook/cluster.md).

## Architecture

![JARVIS architecture: input, free fast path, model path, confirm queue, memory and cluster](docs/images/architecture.svg)

Every request, from voice, the console, the CLI or a node, takes the same path:

1. **Free fast path, on the main.** Instant phrases → strict parser (`parse.py`) →
   intent registry (`registry.py`) → gates (`gates.py`) → history checks
   (`history.py`) → direct executor (`remote.call`, on this PC or a node) → on-screen
   check (`observe.py`) → record.
2. **Model path, only for what the parser can't resolve:**
   - the interpreter (`interpret.py`);
   - then the brain (`inference.py`): ChatGPT, failing over to the Claude CLI;
   - Grok Bot for judgement inside apps and accounts.

   `brain.judge` sends multi-job or hard requests to a planner (`brain.py`, levels L1–L3).
   Whatever the brain decides becomes registry intents and passes through the same gates.
3. **Confirm queue** (`confirm.py`). Anything risky waits until the owner answers in the
   console gate or says "Jarvis, yes".

The full reference is in [docs/architecture-guide.md](docs/architecture-guide.md).

## The console

### System page

The System page reads every check live from the laptop. Anything amber or red comes
with the command that fixes it.

![System design: live health checks](docs/images/design-health.png)

**Under the hood** shows each running component with its live numbers: brain levels,
the scorecard, gate policy, the terminal sandbox (143 paths hidden, 61 read-only), the
cluster release, and the Lan Mouse guard. Below them, every Python module is grouped by
the step it belongs to, with its line count and imports.

![Under the hood: live components and every module by step](docs/images/design-code.png)

The request path is drawn as a live circuit board. Type a request and the board lights
the route it would take.

![System design: request path board](docs/images/design-flow.png)

### Remote control

![Remote view: the Dell's Windows desktop live in the console at 9.2 fps](docs/images/remote-live.png)

<sub>A real session to the Dell: Live, 9.2 fps, 1920×1080 source. Folder and chat
contents are blurred for privacy. The session was view-only and ended with Disconnect.</sub>

- **No new port.** Frames go node → hub and input goes hub → node over the existing
  SSH tunnel. Only the main's console can start a session; the brain has no tool for it.
- **Nothing gets stuck.** However a session ends (Disconnect, closed tab, lost link,
  `jarvis control stop` on the node, restart), held keys and buttons are released.
- **Your mouse stays home.** While you drive a node, `ControllerPause` pauses the
  main's Lan Mouse so the pointer can't slip across mid-session.
- Quality presets: low (6 fps), medium (10 fps), high (15 fps).
  Ctrl+Alt+Shift+Esc releases the keyboard.

Details: [docs/handbook/remote-control.md](docs/handbook/remote-control.md).

### Machines and connections

![Connections: both laptops, hub, code version, Lan Mouse and clipboard](docs/images/devices.png)

Both machines, the hub's code release, whether each node runs the same code, and
Lan Mouse and clipboard state on each side.

### Knowledge engine

![Knowledge engine: 653 records in 11 clusters](docs/images/knowledge.png)

Memory and the vault drawn as one graph. A reader ingests sources, a librarian
commits claims with their citations, and a checker runs nightly.

### Memory, credits, files and settings

| Memory | Credits |
|---|---|
| ![Memory search](docs/images/memory.png) | ![Subscription usage windows](docs/images/credits.png) |
| Everything JARVIS has seen, said or done, searchable. | Live Claude and Codex subscription windows. No API balances are spent. |
| **Files** | **Settings** |
| ![Files across both laptops](docs/images/files.png) | ![Settings](docs/images/settings.png) |
| Browse both laptops side by side and drag files between them. Copies never overwrite or delete. | Machines, voice, calls, intelligence, memory and input in one place. |

## Proof: real output

![Real CLI output: scorecard, parser, cluster status, Lan Mouse, test suite](docs/images/under-the-hood.png)

| Claim | Evidence |
|---|---|
| Requests resolve without a model | `jarvis eval`: parser 0.990 (197/199, **0 wrong**), registry 1.000, vault 1.000, grounding 1.000 |
| Bluetooth, brightness, lock and node apps parse directly | Strict parser output above |
| Nodes run only tested code | Each release passes the full suite before nodes accept it |
| Lan Mouse only links on the same network | `lanmouse nearby` reads the peer's network; Tailscale addresses are refused in the patched build |
| The codebase is tested | `python -m unittest discover -s tests`: **2569 tests OK** |

Rerun these yourself. All of them are free:

```fish
.venv/bin/python -m jarvis eval                  # scorecard; --brain spends quota
.venv/bin/python -m jarvis cluster status
.venv/bin/python -m jarvis lanmouse nearby
.venv/bin/python -m unittest discover -s tests   # about 3 min
```

## Safety rules

- No pay-per-token API credits. Only the ChatGPT and Claude subscriptions and free local models.
- Money, messages, pairing and forgetting devices, and shell commands it can't read
  all wait for the owner's OK.
- Hard refusals never run: credentials, wipes and edits to JARVIS's own files.
- Text from web pages, the vault and agents is data, never instructions.
- No system-wide cursor or input change without a tested fail-safe. Stop desktop
  control with **Super+Shift+Esc** or `jarvis control stop`.

## Repository layout

| Path | What |
|---|---|
| `jarvis/` | The Python package. CLI entry: `python -m jarvis <cmd>` |
| `jarvis/web/` | The console: vanilla JS/CSS, no build step, served by `jarvis ui` on :7777 |
| `tests/` | unittest suite. The scorecard's case file (`tests/eval/cases.jsonl`) is private; `jarvis eval` skips it when absent |
| `docs/` | Handbook, architecture guide, command reference, images |
| `src/`, `index.html` | A separate React/Vite project website, not the console |
| `jarvis.config.json` | Cluster, voice, safety and integration settings |

Runtime state (memory, history, logs, node links) lives in `.jarvis/` and is never committed.

## Docs

| Read | For |
|---|---|
| [Docs index](docs/INDEX.md) | Everything below, plus design notes |
| Handbook: [voice](docs/handbook/voice.md) · [cluster](docs/handbook/cluster.md) · [remote control](docs/handbook/remote-control.md) · [Lan Mouse](docs/handbook/lan-mouse.md) · [phone](docs/handbook/phone.md) · [operations](docs/handbook/operations.md) | Day-to-day guides |
| [Architecture guide](docs/architecture-guide.md) | The full reference, module by module |
| [Command and setup reference](docs/reference.md) | Every CLI command, Obsidian, the knowledge engine, Arch setup, hardware, the unified desktop |
| [OS architecture](docs/jarvis-os-architecture.md) · [Provider limits](docs/provider-limits.md) | Design and build status, quota sources |
