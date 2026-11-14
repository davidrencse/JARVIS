# JARVIS architecture guide

*What each part does, where it lives, which settings drive it, and the rules it must keep.*

Design decisions are in [jarvis-os-architecture.md](jarvis-os-architecture.md). Commands are in [reference.md](reference.md). Moving the main is in [migration.md](migration.md). Runbooks are under `docs/handbook/`: browser, cluster, continuity, drag-drop-design, fleet-ui, lan-mouse, operations, perf, phone, release, remote-control, voice. David's rules and machines are in the `jarvis-memory/` vault.

Contents: [1 What JARVIS is](#1-what-jarvis-is) · [2 Request flow](#2-request-flow) ·
[3 Modules](#3-modules-by-station) · [4 Main and nodes](#main-and-nodes) ·
[5 OS support](#5-os-support) · [6 Voice](#6-voice) · [7 Models and cost](#7-models-and-cost) ·
[8 Memory](#8-memory-and-the-two-vaults) · [9 Safety](#9-safety-and-checks) ·
[10 Configuration](#10-configuration-reference) · [11 State on disk](#11-state-on-disk) ·
[12 Commands](#12-commands) · [13 Tests](#13-tests) · [14 Known limits](#14-known-limits-and-open-work) ·
[15 Pipeline invariants](#15-pipeline-invariants)

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
  browser agents; complex → `orchestrator.py`. `routing.auto_escalate: false` keeps
  conversation on the brain; web and orchestration run only with an explicit `--tier`.
  `routing.brain: "claude"` selects the Claude lane. `models` maps `low` / `mid` / `high`
  to Haiku or low effort, Sonnet or medium, Opus or high. Codex tiers change reasoning
  effort; set `model` there to pin a Codex model.
- ChatGPT runs through the Codex CLI `app-server` on the ChatGPT subscription. API-key
  environment variables are stripped from that child. Driving chatgpt.com is not the path.
- `adapters.apis` entries are `{name, url, sites, token_env}`. The contract is POST
  `{"query", "context"}` → JSON `answer`, https or localhost only. A listed browser agent
  stays unused until `personal_agents.<name>.integration` is set.
- Specialists are `.claude/agents/*.md`. Reviewer, security, researcher, architect, and
  scout are read-only. Writers declare non-overlapping `owned_paths`. A plan is saved and
  does nothing until `jarvis orchestrate run PLAN_ID`. Steps follow dependencies and stop
  at the first failure. Grokbot handoff checks are in [handbook/browser.md](handbook/browser.md).

## 3 Modules by station

The console's system view (`sysview.py`) groups every module into a station; a test fails if
a module is not listed, so add new modules to `sysview.py`.

| Station | Modules | Job |
|---|---|---|
| Hear | listen, daemon, overlay | mic capture, VAD, speaker ID, Whisper, background listener, Windows overlay |
| Decide | router, assistant, instant, briefing, parse, registry, gates, pipeline, confirm, decide, interpret, selfmodel | turn words into checked actions or a conversation |
| Remember | vault, knowledge, memory, second_brain, second_brain_queue, second_brain_runtime, brainsync, vault_mcp, tasks, history, embed | memory heap, semantic recall, David's vault, tasks, action history |
| Think | chatgpt, fastlane, runner, orchestrator, prompts, adapters, claude_auth, grok_preflight | model lanes, failover, agent runs |
| Act | actions, control, terminal, agent_browser, remote, remote_view, grokbot, transfer, dropzone, storage, clipsync, showcase, observe, sharedinput, fleet_management | executors per OS, cross-machine calls, Grok Bot, files, clipboard, Lan Mouse |
| Speak | speech, voice, agent | Kokoro TTS (local), speech queue, ElevenLabs live agent |
| Log | store, events, locking, cluster_store, pathsafe | task ledger, event feed, file locks, cluster job store, path checks on names that arrive from another machine |
| Console / ops | ui, __main__, phone, sysview, cluster_cli, cluster_proxy, cluster_tunnel, deploy, node, platforms, autostart, rest | CLI, web console, cluster control, code deploy, OS detection, login startup |

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
- `deploy.py`: code fingerprint, `cluster update` (tar over SSH, keeps the previous copy for `--rollback`); optional, since nodes pull releases automatically.
- `drift.py`: alerts when an online node stays on other code than the release for over 30 min; shown in `cluster doctor`.
- `phone.py`: the phone PWA's server. It binds 127.0.0.1:7787 by default; David publishes that
  port with `tailscale serve`. If funnel publishes the port, the server stays shut. Each
  request must match this PC's tailnet name and `tailscale whois`, and `/phone/api/*` also
  needs the cookie of a phone paired at the main's terminal. It does not serve console routes.
- `second_brain.py`: the archive storage engine. Nothing runs on import. Changed personal
  records are appended as immutable revision notes under `<vault>/reference/jarvis-records/`;
  an existing note is never opened for writing. `validate()` checks the notes. `restore()`
  rebuilds memory, history, tasks, plans, runs and knowledge files into a new empty state
  directory. SQLite stays the reader unless `second_brain.canonical_read` is literal true
  (this config does not set it).
- `second_brain_queue.py`: the write-through outbox for memory, history and tasks. The
  personal mutation and its full revision commit in the same SQLite transaction. `drain()`
  publishes that revision and acknowledges it only after the note was written, fsynced and
  read back.
- `second_brain_runtime.py`: the drainer the voice daemon starts, and `jarvis archive`. A
  cycle writes nothing unless `second_brain.enabled` is true (it is, in this config), this
  PC is the configured main, `vault.enabled` is not false, and the vault directory exists
  and carries a sentinel. It never reads `vault.write`. Status `state` is `current` only
  when nothing is pending, held or failing, a drain has succeeded, every published head was
  opened through the archive loader, and no published record has a revision gap or duplicate.
- `brainsync.py`: a private git snapshot (`<base>/.jarvis/brain-git`, not the Obsidian vault)
  and a SQLite memory handoff. Off unless `brainsync.enabled` is true; this config sets it
  true. Safe vault files are copied in with exclusive create. The same path with
  different bytes becomes a side-by-side `conflict-<sha256>` copy. Nothing is overwritten
  or deleted. Secret-shaped names and `state/memory.sqlite3` are not pushed unless that
  exact path is on `remote_allowlist` (empty by default).
- `continuity/`: MAIN continuity prototype. `authority` is the single-writer lease store;
  `clock` is the PC-side cached-validity view. Tested on loopback, not deployed.
  `cluster.main_device` stays the live authority. `gateway` binds nothing on import, and
  `executor` is not wired into `Remote.call`. This is not failover. Lazy hooks in
  `cluster_cli`, `cluster`, `remote`, `rest` and `phone` stay default-off until a cutover
  closes them. The hub does have owner routes `/v1/continuity/main` and
  `/v1/continuity/recover`, and a node will take a `continuity` job. `run_main` refuses
  when `configured_handoff` returns None. `run_recover` refuses when
  `hub.continuity_recovery` is not callable. `execute_job` refuses with
  `managed_unbound` when no participant is bound. `participant`, `brain_channel`,
  `hub_control` and `recovery` are those adapters. Nothing in `jarvis/` calls
  `participant.bind` or assigns `hub.continuity_recovery`.
- `continuity/participant.py`: transition-scoped RPCs on the signed cluster job channel
  (`kind` `continuity`). Actions are `readiness`, `drain`, `release`, `accept`,
  `snapshot`, `fence`, `probe`, `brain`, `canary` and `activate_return`. A callback does
  not take a key path, a gateway URL, a readiness flag or a shell command; the
  installation client and runtime are built on this machine. Each call re-reads this
  installation's authenticated status and must match that incarnation and epoch. Drain
  and release belong to the source. The other actions belong to the candidate, except
  `probe` (the requester must be the live main when the lease is valid, otherwise this
  device), `fence` (any enrolled executor may acknowledge), and `brain`. Incoming
  `brain_stage`, `put` and `brain_receive` run only on the candidate; `brain_export`,
  `get` and `brain_freeze` run only on the source. The path stays inside this device's
  `brain_transfer_dir`, outside Git, and a file name must match `brain-<32 hex>.bundle`.
  A `GRANTED_NOT_READY` transition also requires `lease_valid`,
  `main_ready` false, and `main` equal to the candidate. Except for `readiness` and
  `snapshot`, the state directory must already hold a production binding whose runtime,
  executor and issuer are this participant's; otherwise the code is `managed_unbound`.
  The node runs `execute_job` for a `continuity` job, and an empty registry is the same
  refusal. `RemoteRuntime` forwards SafeHandoff calls on that job transport.
  `dispatch` is a separate maintenance queue (code match, the `files` capability, poll
  signatures, deadlines, at-most-once). It is not a fallback that admits an ordinary job.
- `continuity/brain_channel.py`: brain bundles over owner-only cluster transfer path
  operations. No shell and no SSH. Actions are `brain_receive`, `brain_export`,
  `brain_freeze` and `brain_stage`, and the payload must be exactly the keys that action
  requires. `devices.<id>.brain_transfer_dir` has no default. The directory must already
  exist inside that peer's storage roots and outside every Git checkout. Ordinary
  transfer admission still applies; this module does not bypass a drained runtime's
  fence. `run_here` freezes through `brainsync.freeze`, answers `brain_stage` once the
  path is a directory, receives only when the file is not a directory, is within
  `brainsync.MAX_BUNDLE_BYTES` and matches `sha256` (then deletes it only if receive
  returned `ok`), and exports a mode-0600 `brain-<uuid>.bundle` with its size and digest.
  `ClusterBundleTransport` is what `brainsync` selects when any `devices` entry has
  `brain_transfer_dir`. Push checks `brain_stage`, sends the bytes with `transfer.push`
  (`timeout` 180 s), then `brain_receive` with the digest. Pull exports, and when that
  returns `ok` true it reads `get` chunks until the declared size and checks the digest.
  `ConnectionError`, `OSError` and `TimeoutError` come back as `source_offline`.
  `ValueError`, `RuntimeError`, `KeyError` and `TypeError` come back as `transport_failed`.
  On `/v1/action` and `/v1/control` the hub rejects these actions unless the caller has
  the owner credential.
  `TransitionBundleTransport` refuses to construct unless this PC's remote status is
  `GRANTED_NOT_READY`, with `lease_valid` true and `main_ready` false. Every later chunk
  is sent with that transition id, incarnation and epoch; the receiver checks the live
  transition again. Nothing in `jarvis/` constructs it.
- `continuity/hub_control.py`: owner-only bridge on the hub socket that is already
  listening. It is not an authority fallback and it opens no new port. `request_main`
  and `request_recover` refuse unless `cluster.main_url`'s host is loopback, then post
  as the local owner to `/v1/continuity/main` (`{"device"}`) or `/v1/continuity/recover`
  (`{"device", "request_id"}`), each with `timeout` 600 s. The hub requires the owner
  capability and a loopback peer address before it calls `run_main` or `run_recover`.
  `run_main` accepts exactly a device-id string and refuses with "Safe handoff has no
  bound managed runtime." when `configured_handoff` returns None, so `set_main` cannot
  recurse into this same server. `run_recover` accepts only the local device id plus a
  request id that passes the identity check, and calls `hub.continuity_recovery` only
  when that attribute is callable. Otherwise it refuses and no lease is requested.
  `RuntimeRefusal`, `LinkError` and `AuthorityError` come back as
  `{ok: false, status: unconfirmed, code}` and are not turned into `no_main` or retried.
  `operator_configured` reads an optional `continuity.operator_config` file locally and
  returns true only when that file's device id is this PC; it does not contact the
  authority. `cluster_cli.set_main` uses that check. `jarvis continuity recover` (`lease`
  is the same action) calls `request_recover`.
- `continuity/recovery.py`: `ManualRecovery` is a manual initial-lease coordinator. No
  timer, no election, no force flag and no automatic recover. It takes a port, the
  candidate runtime (the one that must outlive the CLI, normally inside the hub) and a
  callable canary. The device must be that candidate, and the client key must pass the
  identity check. A valid lease is `main_alive` unless this same candidate is already in
  a `recover` transition at `GRANTED_NOT_READY`. The mode must be `V2_ONLY_NO_MAIN` or
  `HANDOVER`. With no transition it requests `action` `recover`, `profile` `control` and
  caller `manual-recovery`. It then submits the candidate's own readiness, drains (a
  `not_ready` or `profile` refusal cancels the transition and re-raises), fences, grants
  and accepts the lease. A lost grant is not rebuilt from status. After
  `GRANTED_NOT_READY` the candidate snapshot must still match a fresh status
  (`lease_unavailable` otherwise). The canary must return true; a failure or any
  exception is false, and that value is what `complete` is called with. The result is
  `{ok, status, transition_id, main, incarnation, epoch}`. `status` is `ready` only when
  the canary was true, the mode is `V2_ACTIVE`, the lease is valid and `main_ready`,
  `main` is this device, incarnation and epoch match the snapshot, and no transition is
  open. Otherwise `status` is `no_main`.
- `fleet/`: P1 facades over today's single main. `legacy_topology` names the config-driven
  roles and does not treat `cluster.main_device` as a lease. `readiness` is a read-only
  report on a possible MAIN candidate and does not authorize promotion, failover, sleep or
  cutover.
- `fleet_management.py`: node onboarding, and a read-only fleet picture for the console and
  a safe subset of it for the paired phone. Invite, revoke and capability changes wrap the
  existing cluster calls. The invite code is returned once and is not stored. MAIN is never
  an enroll, revoke or capability target.
- `rest.py`: `jarvis sleep` on Linux. It stops this PC's JARVIS user units (the IP watcher
  first, then Lan Mouse, clipboard, voice and the hub) and any other user unit holding a
  block sleep lock, asks logind for `suspend-then-hibernate` by default, then starts exactly
  those units again. `--no-suspend` only pauses; `jarvis wake` restores. Nodes stay up.
- `control.py`: the `computer` tool (screenshot, click, drag, type, key, scroll) on Hyprland,
  Win32 or macOS. `jarvis control stop` refuses further actions for 10 minutes. A spoken
  "stop controlling my computer" hits the same stop (`assistant.STOP_CONTROL`). A pointer
  JARVIS did not leave where it is pauses control. At most 120 actions per 10 minutes. An
  error releases the mouse buttons and modifier keys.
- `remote_view.py`: David drives a node, or the main, from the console. Frames and input ride
  the cluster hub; nothing new listens. The node runs one signed `remote` job. Only the
  main's console can open a session, and the brain has no tool for it. Ending the session
  releases every button and key the node held.
- `sharedinput.py`: Lan Mouse for the console, and `jarvis lanmouse start|stop|pair`.
  `guard()` stops sharing after two apart readings in a row, and starts it again only if the
  guard was what stopped it. While this console drives another computer, `ControllerPause` stops
  Lan Mouse here (`remote-controlling.json`). The captured-input fail-safe is the daemon
  `release_bind`, which `desktop.lan_mouse_config` writes next to `authorized_fingerprints`.
- `hostmeter.py`: read-only RAM, swap, disk, CPU load and battery health for one computer
  (`sysview` lists it on the Machines side). Health is full-charge capacity divided by
  design capacity, and only when both numbers exist in the same unit. The same summary
  rides inventory and the node heartbeat.
- `dropzone.py`: a thin Wayland strip just inside the Lan Mouse edge. A file dropped on it
  is copied to the other laptop through `transfer.push`. It accepts COPY only, so the file
  manager does not delete the original.
- `pathsafe.py`: stdlib checks for paths, SSH names and addresses that arrive from another
  machine. Callers keep their own destination roots. It will not follow a final symlink,
  publish over a name that already exists, or treat a Tailscale address as a Lan Mouse
  peer. `open_leaf` opens one regular-file child of a folder (`rb`, `xb` or `ab`); it uses
  `dir_fd` and `O_NOFOLLOW` when the platform allows it, and on Windows it rejects a
  reparse point and opens with `O_BINARY`. `xb` and `ab` are mode 0600. `link_new`
  hard-links a regular file only when the destination name is absent (`follow_symlinks`
  false where that flag exists). `publish_new` creates an exclusive placeholder and
  replaces that placeholder, not a file or symlink a peer already planted. `ensure_private_dir`
  creates a real directory (0700, except on Windows) and refuses a symlink.
  `write_text_private` writes a regular file and will not open an existing symlink.
  `scp_remote_filename` is one component of at most 255 characters; shell metacharacters
  are refused and one ` (digits)` collision marker is allowed. `ssh_destination` is
  `user@host` only. `is_tailscale_ip` matches 100.64.0.0/10 and fd7a:115c:a1e0::/48.
  `lan_mouse_ip_blocked` is that check, or any text that starts with `100.`. `release_key`
  is one `[A-Za-z0-9_]{1,64}` token. `has_parent_segment` is true for a non-string, a NUL
  or a `..` segment. `sysview` lists it on Log. `transfer`, `clipsync`, `desktop` and
  `sharedinput` call it.
- `transfer.py`: files between the laptops, over chunked hub `transfer` jobs, or over SSH
  when the cluster is off or the node is offline. `jarvis send` lands in `~/JARVIS-Drop`;
  `jarvis fetch` pulls a file back to `~/Downloads/JARVIS-Drop`. The receiver keeps a hidden
  `.part` file until the last chunk is in. Opens, part files and the published name go
  through `pathsafe`. Brain bundle actions are checked and run by `continuity/brain_channel`.

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
jarvis cluster update dell        # optional: nodes get code automatically after a green push to origin/main; this only speeds it up
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
2. Speaker check (`listen.VoicePrint`, sherpa-onnx ERes2Net, CPU). On CUDA it runs beside
   speech-to-text; on CPU it runs first so a stranger's clip never queues ahead of David.
   Parakeet (`voice.stt_engine`, default) first; Whisper (`faster-whisper`, CUDA if present,
   else CPU int8, `hotwords="Jarvis"`) when Parakeet is unsure. The learned threshold is
   clamped to 0.30–0.36. `near_miss` defaults to 0.25 and `stranger_floor` to 0.08.
   Bands, wake variants, and learning rules are in [handbook/voice.md](handbook/voice.md).
3. Wake word: an always-on request must start with Jarvis (or an accepted mishearing).
   "Travis" and "Jarvis's …" do not count. Push-to-talk with the speaker check off does
   not apply that rule; that difference is still David's to close.
4. Confident matches with the wake word adapt the voiceprint (enrollment clips stay pinned,
   learned ones rotate, 40 max).
5. Replies use `voice.engine` (ElevenLabs in this config, Kokoro when that cannot speak),
   streamed sentence by sentence. "On it." plays while the brain thinks. Kokoro's voice
   here is `bm_lewis`.

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
at 8:00 PM", every later call skips ChatGPT until then, and Claude answers instead. If
Claude is signed out, the reply says so. When testing the Claude CLI from inside a Claude
session, strip `CLAUDE_*` and `ANTHROPIC_*` env vars.

The inference layer (`inference.py`) is the one place that picks the model for each part.
Add new model calls through it, never as a bare `ChatGPTLane`/`FastLane`:

- Roles `talk`, `tools`, `interpret`, `grok_check` and `consolidate`, each with a Claude tier
  and a timeout. Override them in config under `inference.roles.<role>` (`tier`, `timeout`).
  `inference.failover: false` turns the brain's failover off.
- `Failover` wraps the brain: ChatGPT first, then Claude while ChatGPT is limited. It also
  fails over when the limit hits mid-answer, but only if nothing was spoken yet and no tool
  ran. Starting the stand-in closes ChatGPT's lanes (one Chrome per browser profile); the
  stand-in closes again on ChatGPT's next answer.
- `claude_brain` is the stand-in brain. It has the same instructions, context and tools:
  the toolbox comes over MCP (`tools_mcp.py`, `jarvis brain-tools`) behind the same
  ConfirmGate. The signed-in browsers come through `browser_proxy.py` (`jarvis
  browser-proxy <server>`), a relay that runs `approve_browser` and `browser_gate` on every
  call. Codex does that for ChatGPT itself; the Claude CLI has no per-call approval hook.
- `ask()` is for stateless questions (consolidation, Grok preflight), with the same failover.
- The interpreter gives ChatGPT `PATIENCE` (12 s), then reads the request with Claude:
  ChatGPT turns sometimes stall for about 60 s.

## 8 Memory and the two vaults

- **Memory heap** (`memory.py`, `.jarvis/memory.sqlite3`): every message and result. Ids are
  `kind:YYYYMMDD:hex6`. Search is FTS5 (porter stemming, prefix match, title weighted 4×)
  blended with importance and 30-day recency; identical content within a kind is stored once.
  BM25 plus vectors are fused with reciprocal-rank fusion when `memory.semantic` is true
  (`JARVIS_SEMANTIC=0` turns it off). Vectors come from `embed.py` (all-MiniLM-L6-v2 ONNX,
  downloaded once to the Hugging Face cache, unloaded after 600 s idle).
  Every brain call gets the last 20 min of conversation plus relevant heap lines as recall.
  The action brain always has `MemoryTools` (`memory_remember` saves a `fact` item,
  `memory_search`, `conversation_recent` across days), whether or not the vault is on.
  The talk lane hands off (`[act]`) for remember/recall requests, computer checks ("how
  many apps…") and short answers to its own clarifying questions.
- **Consolidated memory** (`consolidate.py`, kinds `fact`, `preference`, `lesson`, `loop`):
  when `memory.consolidate` is true, the voice daemon or console checks every 30 min and,
  once a day after 04:00, sends everything said before local midnight (since the last run)
  plus the active known items to ChatGPT in one tool-free call. Each returned item must cite
  ids it was shown; it is linked (`from`) to its source messages. `same_as` or near-identical
  wording merges into the known item (`meta.seen` +1), `replaces` retires the older item as
  kind `replaced` (link `replaces`), `closed` retires loops. Retired kinds (`forgotten`,
  `replaced`, `closed`) stay in the file but search skips them. A failed call records
  nothing and retries; a file lock keeps two processes from both running it.
  `jarvis mem consolidate` runs it now; `jarvis mem knowledge` lists the items.
- **Corrections and the memory pack**: a request like "no, I meant the Dell"
  (`memory.is_correction`) saves a `lesson` linked to the exchange it corrects, for free.
  Every brain call's recall starts with the pack (`Memory.pack`): open loops from the last
  14 days, then matching facts/preferences (3) and lessons (2), ranked by meaning when
  semantic recall is on. The interpreter's prompt gets the matching lessons too.
- **History** (`history.py`, `.jarvis/history.sqlite3`): actions taken, for the H-checks,
  the decider's labels and the console's command history.
- **David's personal vault** (`vault.path`, per OS): his notes, calendar and tasks. JARVIS
  reads it (`vault.world()` overview goes into every brain call) and writes only when he
  asks, additively. **On, read-only** in `jarvis.config.json`: `enabled` and `recall` are
  true, `write` is false. `Vault.writable` is true only when `write` is exactly true, so
  capture and append stay off and the brain is told vault writes are off
  (`chatgpt.VAULT_READ_ONLY`). Protected paths are in `vault.protected`. Its own `CLAUDE.md` is its rulebook.
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
- Paths, SSH destinations and Lan Mouse peer addresses that arrive from another machine
  go through `pathsafe.py`: no symlink follow, no publish over an existing name, no
  Tailscale address as a peer (100.64.0.0/10, fd7a:115c:a1e0::/48, or any text that
  starts with `100.`).
- Shell and desktop startup files (shell, X, fish, hypr, uwsm, environment.d, PowerShell
  profile) may be read. Writes are refused: "JARVIS doesn't write shell or desktop startup files".
- No system-wide input or cursor changes without a tested fail-safe.
- `rules.confirm: never` still parks money and messages (`rules.always_ask`). A terminal
  command runs at most 30 s (`terminal.TIMEOUT`) and, on Linux, inside bubblewrap unless
  `terminal.sandbox` is false.

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

`python -m unittest discover -s tests` with the project `.venv`
(`.venv/bin/python` on Linux, `.venv\Scripts\python.exe` on Windows). Set
`JARVIS_NO_MODELS=1` and unset `CODEX_*`. The suite is mocked: it does not touch the real
desktop, microphone, or accounts, and an audit hook refuses to start `codex` or `claude`.
Test configs turn off semantic recall, shadow mode, and the interpreter. Live checks are
`jarvis selftest keyboard` (Windows only; it skips elsewhere) and `jarvis doctor`.
`jarvis eval` is free; `jarvis eval --brain` spends subscription quota.

## 14 Known limits and open work

- Backends for macOS, sway and X11 desktops are unit-tested only.
- The interpreter has not yet been tested live end to end.
- Lan Mouse is in service (`lan_mouse.enabled` is true). `sharedinput.guard` stops it after two apart readings, and the captured-input fail-safe is the daemon `release_bind` written by `desktop.lan_mouse_config` (with `authorized_fingerprints`).
- Instant greetings ("hi", "thanks" with no model) were offered, not built.
- The decider stays in shadow (`decisions.shadow` is true) until a job's measured precision
  reaches 0.98 on at least 20 picks (`decide.TARGET`). A deadline falls back to rules, never a guess.
- Node offline mode (allow-risk actions on that node only, journal replayed as records and
  never re-executed) is a design, not a running mode.
- Parallel plan writers stay off. Owned paths are collision checks, not filesystem permissions.

## 15 Pipeline invariants

Plans, the task ledger, and the memory database are separate stores.

- `jarvis/locking.py` takes a nonblocking OS file lock before loading a plan and holds it
  through execution or recovery. A second process gets a busy message. Exit releases the
  lock; the file stays. The lock covers one local filesystem, not separate state directories.
- `python -m jarvis orchestrate recover PLAN_ID` reports recorded task states and launches
  nothing. `--apply` adopts terminal artifacts, keeps failed work blocked, and marks the
  original queued task ids for a later `orchestrate run`. It never creates a replacement
  task for an uncertain dispatch. A running or missing task stops recovery for inspection.
- The result checkpoint is written before memory indexing. Indexing can be repaired from
  the artifact. `finished` means the process exited 0, not that acceptance criteria passed.
  A crash after dispatch and before the checkpoint is uncertain: do not clear the task
  mapping to make Run succeed.
- Plan validation rejects absolute paths, traversal, and empty or ambiguous Windows names,
  and compares case, separators, dot segments, and nested directories the same way.
  Immediately before dispatch, owned paths are resolved inside the plan workspace, which
  catches symlink aliases that leave it, including links added after the plan was saved.
  A workspace writer with no owned paths cannot run. A link can still change between that
  check and the edit. Do not enable parallel writers until overlap detection, whole-plan
  claims, separate workspaces, and acceptance checks exist.
- Warm provider lanes use monotonic deadlines. An abandoned turn discards its process and
  output queue so the next request cannot receive the previous answer. Cleanup waits on
  the direct child; descendant MCP and browser processes are not covered by that helper.
- `Store.overview()` reads totals and fourteen recent task projections on one connection.
- A full subscriber queue closes that SSE connection. The browser's EventSource retry
  receives a fresh `hello` and reloads history and overview. The watcher compares the
  feed file's identity, so a replaced `events.jsonl` is read from the start even when the
  new size is at least the old offset. The feed is a bounded transient stream.
- File copies check the exact candidate path. Directories and broken symlinks count as
  taken. A permission error fails closed. Names are normalized before the collision check,
  including Windows reserved names, and stay inside the inbox parser's 200-character limit.
  Picking a free name and performing the copy are separate steps; two writers can still race.
- A failed microphone startup releases the tracks, nodes, and audio context it acquired.
  Concurrent starts share one request. Escape, the microphone button, or releasing a held
  Space cancels a pending start. A quick Space tap keeps its previous behavior.
- Restart the console, listener, or overlay to load Python. The task timestamp index is
  created on the next store open and can stay if code is rolled back. Do not restore or
  delete `.jarvis` to undo a code change.
