# Codex briefs (2026-10-26)

> Historical task prompts. They assume the Dell was main, the reverse tunnel and the MiniConda interpreter; today the Zenbook is main on a forward tunnel and the interpreter is the project `.venv`. The `jarvis.config.json.pre-*` backups they mention have been removed.

Five self-contained tasks. Paste one brief per Codex session. Background for all of them
is `docs/jarvis-os-architecture.md`.

**Which can run at the same time.** Each brief owns its files, so briefs with disjoint
files can run in parallel:

| Brief | Owns | Can run with |
|---|---|---|
| 1 Confirmation queue | `confirm.py` (new), `gates.py`, `pipeline.py`, `assistant.py`, `history.py`, `ui.py`, `web/flow.*` | all |
| 2 Windows master volume | `observe.py` | all |
| 3 Spotify Liked Songs, play toggle | `actions.py` | all |
| 4 Zenbook live | `cluster*.py`, `topology.py`, `remote.py`, `jarvis.config.json`, `docs/` | all |
| 5 Website bundle | `src/`, `vite.config.ts`, `package.json` | all |

All five own different files, so they can all run at once. What can't be split is the
**full test suite**: each brief runs it, and a run can fail mid-way because of another
session's half-finished work. Re-run before trusting a red result. If a brief has to
edit a file another brief owns, it stops and reports instead. One writer per file, always.

Briefs 1 and 4 also change existing tests; they own only the test files they name.

**Every brief includes these rules** (already in the text below):
- No commits, no branches, no pushes, no CI or deploy changes.
- Never pay-per-token APIs or API keys. The subscriptions and local tools only.
- Edit only the files the brief lists. If another file must change, stop and report why.
- Tests must never touch the real desktop, network, vault or `.jarvis`. Use fakes and
  temp folders.
- Run the full suite at the end with `C:/Users/jdoe/MiniConda3/python.exe -m unittest discover -s tests`
  (MiniConda is the interpreter with JARVIS's dependencies). Report the exact summary line.
- Report: the files changed, what was verified live versus only in tests, and anything
  left undone.

---

## Brief 1: Confirmation queue and risk ratchet

```text
You are working in C:\Users\jdoe\Desktop\JARVIS (Python, stdlib only, Windows + Arch).
Read docs/jarvis-os-architecture.md sections "L3 Gate" (Risk ratchet, The confirmation
queue) and the Intent Registry first, then jarvis/registry.py, jarvis/gates.py,
jarvis/pipeline.py, jarvis/assistant.py, jarvis/history.py, jarvis/web/flow.js.

Goal: commands whose risk is "confirm" (e.g. "close word": close_app of an app not on
registry.SAFE_TO_CLOSE) are parked for David's approval instead of being refused, and
Grok agent tasks get a risk ratchet.

Build:
1. New jarvis/confirm.py: a ConfirmQueue persisted in SQLite under the state dir
   (confirmations table). Each pending item has a short ID ("C7"), a one-line summary
   ("close Word on the dell"), the exact command JSON, a SHA-256 hash of that JSON, the
   origin device, created time, and an expiry (60 s for channel "voice", 10 min
   otherwise). Methods: park(command, origin, channel) -> item; approve(id_or_None,
   origin, hash=None) -> command or raises; reject(id); pending(origin=None); expire().
   approve(None, origin) binds to the only pending item for that origin and raises
   "Which one? C3 or C7" when there are several. An approval only returns the command
   if its hash still matches (a changed command needs a new confirmation). Expired items
   are never approvable. Every approve/reject is recorded in jarvis/history.py's history
   (add a column or a small table there if needed, additive migration only).
2. pipeline.py: where a command is blocked with risk "confirm", park it and reply
   "I need your OK to close Word on the dell. Say 'Jarvis, yes' or press Confirm (C7)."
   Add pipeline.run_confirmed(command, config, state, ...) that runs a previously parked
   command through the same gates (minus the policy check) and executor, with the same
   history recording.
3. assistant.py: before the fast path, recognise approvals: "yes", "confirm", "do it",
   "go ahead", "confirm seven", "confirm c7", and rejections "no", "cancel", "don't".
   Only act on these when there is a pending item for the origin; otherwise treat the
   text normally. Voice approvals still require the wake word (the listener already
   strips it; never add an ungated path).
4. Risk ratchet for agent tasks (the Grok route in assistant.ask): risk = the highest of
   (a) keyword rules (buy, pay, order, book, send, message, reply, text, email, post,
   tweet, comment, delete, remove, unsubscribe, submit, apply, sign, accept, transfer,
   install, uninstall, change settings) and (b) the existing grok preflight. Any
   "confirm" parks the Grok task in the same queue before anything is typed to Grok.
   Nothing can lower a verdict. Credentials (password, passcode, 2fa code, card number)
   are always refused.
5. Console: GET /api/confirm (pending list), POST /api/confirm {id, action:
   approve|reject} on jarvis/ui.py's existing handler (POST requires the X-JARVIS
   header like every other POST). Show pending items with Confirm and Reject buttons in
   the existing connection-tree panel (web/flow.js, flow.css), and publish a
   "turn.confirm" event on the console bus when one is parked.

Rules: no commits/branches/pushes; stdlib only; no API keys or paid APIs. Edit only
jarvis/confirm.py (new), gates.py, pipeline.py, assistant.py, history.py, ui.py,
web/flow.js, web/flow.css, and tests/test_confirm.py (new) plus existing tests that you
must update because behaviour changed (say which and why). Do not touch actions.py,
observe.py, cluster*.py or remote.py. Tests use temp dirs and fake remotes only.

Done when: "close word" parks C1 and replies with the ID; "yes" from the same origin
runs it; "yes" from another device does not; an expired item can't be approved; a
changed command needs re-approval; "reply to sam's last dm" parks before Grok is
contacted; full suite passes. Report the full-suite summary line.
```

## Brief 2: Read the Windows master volume

```text
You are working in C:\Users\jdoe\Desktop\JARVIS. Read jarvis/observe.py (volume_state,
matches, check) and jarvis/registry.py (volume intent).

Problem: on Windows, volume_state() raises LookupError on purpose, because the previous
waveOutGetVolume read the process's own mixer level, not the master volume. So "volume
30" can never be confirmed ("observed") on the Dell.

Build: implement the Windows branch of volume_state() with Core Audio's
IAudioEndpointVolume through ctypes COM (CoInitializeEx, CoCreateInstance of
MMDeviceEnumerator, GetDefaultAudioEndpoint(eRender, eConsole), Activate
IAudioEndpointVolume, GetMasterVolumeLevelScalar, GetMute, Release everything,
CoUninitialize). No third-party packages (no pycaw, no comtypes), no PowerShell. Return
{"percent": round(scalar*100), "muted": bool}. On any COM failure raise LookupError with
a clear message (observe.run already turns that into "Can't observe: ...").

Rules: no commits/branches/pushes; stdlib only. Edit only jarvis/observe.py and
tests/test_observe.py. Unit tests must not need real audio: isolate the COM calls in one
small function you can patch, and test the parsing/mapping around it. Keep the
existing test that pins the old LookupError only if still true; otherwise update it
and say why.

Done when: on the Dell, `C:/Users/jdoe/MiniConda3/python.exe -c "from jarvis.observe
import volume_state; print(volume_state())"` prints the real master volume matching the
Windows volume flyout (check it at two different levels, and muted), and the full suite
passes. Report both the live readings and the suite summary line.
```

## Brief 3: Spotify Liked Songs that really plays, and an idempotent play

```text
You are working in C:\Users\jdoe\Desktop\JARVIS. Read jarvis/actions.py (open_url's
spotify: branch, media), jarvis/pipeline.py (_execute's spotify_play path: open_url
then media "play"), jarvis/observe.py (playback observer).

Problems:
1. On Windows, media("play") presses the play/pause toggle key, so "play" while music
   is already playing PAUSES it. Same for "pause" while paused.
2. "play my liked songs" opens spotify:collection:tracks, sleeps a fixed 2 s, then
   presses play. That most likely resumes whatever Spotify last played, not Liked Songs.

Build (edit only jarvis/actions.py and tests/test_executors.py):
1. Make play and pause idempotent on Windows: read the current SMTC playback status
   first (reuse the PowerShell WinRT query pattern from observe._playback_nt, or import
   it), and only press the toggle when the state needs to change. On Linux, playerctl
   play/pause are already idempotent; keep them.
2. Investigate, live on the Dell, what actually starts Liked Songs: try in order
   (a) the deep link alone, (b) deep link + idempotent play, (c) the context URI
   variants Spotify accepts (e.g. spotify:collection:tracks with a play/autoplay form,
   or spotify:user:<id>:collection), (d) after opening, focusing Spotify and sending
   the space key. Pick the first that reliably starts a Liked Songs track. Replace the
   fixed time.sleep(2) with polling for the Spotify window/SMTC session (max ~6 s).
   If nothing works reliably without UI automation, say so plainly; don't fake it.
Free/local only: never the Spotify Web API or any API key.

Rules: no commits/branches/pushes. Edit only jarvis/actions.py and tests/test_executors.py
(tests patch _run/subprocess/time; never press real keys in tests). If pipeline.py must
change, stop and report what and why instead.

Done when: live on the Dell, "play" while playing keeps playing and "pause" while paused
stays paused; you report exactly what "play my liked songs" plays (track and whether it
is from Liked Songs) for the approach you chose; full suite passes. Report the suite
summary line and the live observations.
```

## Brief 4: Zenbook live through the cluster

```text
You are working in C:\Users\jdoe\Desktop\JARVIS on the Dell (main, Windows) with the
ASUS Zenbook (node, Arch + Hyprland, user jdoe) reachable over SSH with key
~/.ssh/jarvis_ed25519. Read docs/jarvis-os-architecture.md (L4 Dispatch, Transports,
Node offline mode), jarvis/cluster.py, cluster_cli.py, cluster_store.py, topology.py,
remote.py, and jarvis.config.json (devices.zenbook targets, cluster block).

Facts: the main listens on 127.0.0.1:7778 only (no TLS). The Zenbook's LAN IP changes
with DHCP; targets list LAN IPs plus Tailscale 100.64.0.11. known_hosts has the
Zenbook key under the old IP, so SSH to a new IP needs -o HostKeyAlias=172.1.42.215.
The Zenbook's interactive shell is fish, but SSH commands run in bash. Sudo there needs
David's password: never run sudo; hand him the command. Anything long-running on the
Dell must not be started from a Claude/Codex shell that will die: use the Startup
folder / explorer.exe pattern already used for Lan Mouse, or a systemd user unit on Arch.

Build:
1. A tunnel manager on the Dell: one `ssh -N -R 7778:127.0.0.1:7778` session to the
   Zenbook (BatchMode, StrictHostKeyChecking, ServerAliveInterval) that tries each
   configured target in order and reconnects with backoff, like jarvis/clipsync.py's
   reconnect loop. Expose it as `jarvis cluster tunnel start|stop|status` and an
   autostart option.
2. Enrol the Zenbook: run `jarvis cluster enroll zenbook --output <file>` on the Dell,
   copy the pairing file over SSH (scp), run `jarvis cluster join <file>` and
   `jarvis cluster autostart on` on the Zenbook (its checkout is ~/projects/JARVIS; use
   its .venv python; install with `pip install -e .`, no [voice] extra). Delete the
   pairing file on both machines afterwards; it holds a credential.
3. One control path: in remote.py, when the cluster is enabled and the node is online,
   desktop actions go through the cluster; SSH stays only as the fallback when the node
   is offline. Keep host-key pinning.
4. Verify end to end from the Dell: `jarvis ask "open chrome on arch"` runs through the
   fast path, reaches the Zenbook via the cluster, and the observe step reports
   "observed". Then stop the node agent and confirm the SSH fallback still works.

Rules: no commits/branches/pushes; never print or log the pairing token; no sudo; edit
only jarvis/cluster*.py, topology.py, remote.py, jarvis.config.json (back it up first as
jarvis.config.json.pre-cluster-live), docs/, and tests/test_cluster.py /
tests/test_remote.py. Do not touch pipeline.py, gates.py, assistant.py or actions.py.

Done when: `jarvis cluster status` on the Zenbook shows connected after a Zenbook
reboot and after a Wi-Fi change; "open chrome on arch" from the Dell is observed via
the cluster; the SSH fallback works with the node stopped; full suite passes. Report
timings for the cluster path versus the SSH path and the suite summary line.
```

## Brief 5: Website bundle size

```text
You are working in C:\Users\jdoe\Desktop\JARVIS, in the React/Vite website only
(src/, vite.config.ts, package.json; build with `npm run build`). This is the public
site, separate from the jarvis/ Python package; do not touch jarvis/ or tests/.

Problem: `npm run build` warns about a 569 KB main chunk and a 916 KB product-scene
chunk (about 181 KB and 244 KB gzipped). The scene is lazy-loaded in
src/sections/Hero/ProductShot.tsx.

Build: find what makes up both chunks (use `npx vite-bundle-visualizer` or rollup's
output stats), then reduce them: split vendor code with build.rollupOptions.output
.manualChunks, import three.js submodules instead of the whole package where possible,
defer anything below the fold, and keep the scene lazy. No visual or behaviour change
to the site.

Rules: no commits/branches/pushes/deploys; no new runtime dependencies without saying
why; edit only src/, vite.config.ts, package.json (and package-lock.json if a dev
dependency is added).

Done when: `npm run build` finishes with no chunk-size warning or a documented reason
for the remaining one; report before/after sizes (raw and gzip) for every chunk; the
site renders the same in `npm run dev` (check the hero scene loads).
```
