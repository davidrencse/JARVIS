# JARVIS command reference

*Commands and setup. How the pieces fit is [architecture-guide.md](architecture-guide.md).*

Runbooks: [handbook/browser.md](handbook/browser.md), [cluster.md](handbook/cluster.md), [continuity.md](handbook/continuity.md), [fleet-ui.md](handbook/fleet-ui.md), [lan-mouse.md](handbook/lan-mouse.md), [operations.md](handbook/operations.md), [phone.md](handbook/phone.md), [release.md](handbook/release.md), [remote-control.md](handbook/remote-control.md), [voice.md](handbook/voice.md). Quota behavior is [provider-limits.md](provider-limits.md).

The configured main is the Zenbook (`cluster.main_device`: `zenbook`, Arch/Hyprland). The Dell G15 is a Windows node. Use the project `.venv` (`.venv/bin/python` on Linux, `.venv\Scripts\python.exe` on Windows).

## Talk and plans

```text
jarvis chat
jarvis ask "open spotify"
python -m jarvis ask "..." --speak
python -m jarvis route "build a production recon dashboard"
python -m jarvis orchestrate plan "build ..."
python -m jarvis orchestrate run PLAN_ID
python -m jarvis orchestrate recover PLAN_ID
python -m jarvis orchestrate recover PLAN_ID --apply
python -m jarvis mem search "lan mouse" --kind note
python -m jarvis mem add "fact to keep" --tag project --importance 0.8
```

`recover` without `--apply` only reports task states. `--apply` reconciles terminal tasks and does not launch a provider. A zero exit is `finished`, not verified acceptance. `owned_paths` are ownership hints to the model, not filesystem ACLs. `workspace_roots` in `jarvis.config.json` is launch admission; paths resolve relative to the config file.

```text
python -m jarvis doctor
python -m jarvis add examples/review-kernel.json
python -m jarvis runs
python -m jarvis plan TASK_ID
python -m jarvis engineer TASK_ID
python -m jarvis run TASK_ID --timeout 900
python -m jarvis show TASK_ID
```

`plan` does not launch a model. `engineer` prints a refinement request and does not call one. `run` uses the CLI's existing login and may consume subscription allowance. It defaults to read-only. Workspace writes need `"mode": "workspace-write"` and `"role": "implementer"` in the task. A crash may leave a task `running`; do not retry it blindly.

Web tasks use two browsers, both driven over MCP, with profiles under `.jarvis/`. Pick them with `actions.browser_engines`. Page scripts, uploads, raw CLI flags, other sessions, and screenshot files on disk are refused. See [handbook/browser.md](handbook/browser.md).

## Console

```text
python -m jarvis app          # app window; what start.bat / start.sh runs
python -m jarvis ui --open    # http://127.0.0.1:7777
python -m jarvis overlay      # Windows; own console on port 7790; Alt+1
```

Loopback only. Foreign `Host` headers are rejected. Every POST needs `X-JARVIS`. Closing the app window stops the console server about 15–25 s later; the listener keeps running. On the Zenbook, Super+Shift+J runs `jarvis app` and focuses an open window. The overlay hotkey is Windows-only (`--key ctrl+alt+j` to change it). `overlay --autostart on` installs `JARVIS Overlay.vbs`; logs go to `.jarvis/logs/overlay.log`.

Other processes append `turn.*` and `voice.*` to `.jarvis/events.jsonl`. The console tails that file and serves `/api/events`. Polling is the fallback. After a Python upgrade, restart the listener and the console.

Credits are labelled by source. Live Claude and Codex windows use the existing CLI sign-ins; see [provider-limits.md](provider-limits.md). DeepSeek, OpenRouter, and ElevenLabs use `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, and an ElevenLabs key with User → Read. GLM, Z.AI, Grok, and Muse have no balance endpoint; a figure typed in Credits is shown with the date it was entered. Expired local snapshots are not a refilled quota.

## Vault and tasks

`vault.enabled` and `vault.recall` are true. `vault.write` is false, so capture and append stay off. Paths: `vault.path.windows` and `vault.path.linux` (`~/Documents/obsidian-vault` on the Zenbook). A Windows path is unconfigured on Linux. The code follows the vault's own `CLAUDE.md`.

```text
jarvis vault world
jarvis vault search "page replacement"
jarvis vault recall "when is my OS midterm"
jarvis vault read operating-systems
jarvis vault log "Finished OS assignment 2"
jarvis vault capture "LRU vs FIFO" --body "..." --tag computer-science
jarvis vault append NOTE --section "Next step" --text "- [ ] ..."
jarvis vault health
jarvis vault backlinks emotion
jarvis vault brief [--speak]
jarvis vault open operating-systems
jarvis vault clip https://example.com/post
jarvis vault graph
jarvis vault mcp
jarvis vault hook
```

Recall injects at most 4 notes and 900 characters, or nothing on a weak match. `reference/` is searchable and is not injected. Writes, when `write` is exactly true, are additive: no delete, move, or rewrite. Refused: `calendar.md`, `archive/`, `templates/`, attachments, and files with merge-conflict markers. Each write is appended to `.jarvis/vault-writes.jsonl`.

"Jarvis, brief me", "what's on today", "what's due", and "anything overdue" are answered from `calendar.md`, the task agenda, and the daily note with no model (`briefing.py`). A specific brief ("on the OS midterm") still goes to the brain. "Open my OS note" uses `obsidian://open`. `vault_clip` saves readable text to `reference/<title>.md` and refuses local and private addresses, including through redirects. Pages that need JavaScript are refused.

Tasks are checkbox lines in the vault, Obsidian Tasks format. Checkboxes in `archive/` and `templates/` do not count. A note that is mostly checkboxes is shown as progress, not as hundreds of cards. Edits change one verified line (status, due, or priority). If the line changed since it was read, the edit is refused. Completing an `🔁 every …` task inserts the next occurrence. Task text is never rewritten.

```text
jarvis tasks
jarvis task add "Buy printer paper" --due friday --priority high
jarvis task add "Pick motors" --note drone-blueprint --due +3d
jarvis task done "printer paper"
jarvis task start ID | cancel ID | reopen ID
jarvis task due "pick motors" --to "next monday"
jarvis task priority ID --to highest
jarvis runs
```

Console: `GET /api/vault/tasks`, `POST /api/vault/tasks`, `POST /api/vault/tasks/<id>`, `GET /api/vault/graph`, `GET /api/vault/brief`. `jarvis vault mcp` is stdio. Claude Code's `.mcp.json` entry is read-only; `--write` adds log, capture, and append. Codex: `codex mcp add jarvis-vault -- <project python> -m jarvis vault mcp`. `jarvis vault hook` is an opt-in `UserPromptSubmit` hook and is not registered by default. The index is `.jarvis/vault.sqlite3`.

`jarvis kb` is a separate wiki under `.jarvis/knowledge/` (`knowledge.path` to move it). `raw/` is read-only and SHA-256 hashed. The checker marks claims with no fresh source for `knowledge.stale_days` (30) as `[!unconfirmed]` and never overwrites a contradiction. `knowledge.llm` is `none` in this config; `claude` is accepted only when every citation is one of the retrieved claims.

```text
jarvis kb ingest notes.md paper.pdf https://example.com/post memo.m4a
jarvis kb ingest --text "..."
jarvis kb ask "..."
jarvis kb sync
jarvis kb check
jarvis kb conflicts
jarvis kb resolve CLAIM keep OTHER
jarvis kb merge FROM INTO
jarvis kb schedule on --at 02:00
jarvis kb status | log | sources | page SLUG
```

The console page is `/knowledge.html`. Drops accept text, HTML, PDF, or audio, up to 200 MB each. `/design.html` is the live system map.

## Arch setup and speech

```fish
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e '.[voice]'
mkdir -p .jarvis/voice/kokoro
for f in kokoro-v1.0.onnx voices-v1.0.bin
    curl -fLo .jarvis/voice/kokoro/$f https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f
end
./start.sh
.venv/bin/python -m jarvis voice autostart on
```

Fetch MiniLM once or semantic recall and the decider stay off:

```text
python -c "from huggingface_hub import hf_hub_download as d; M='sentence-transformers/all-MiniLM-L6-v2'; d(M,'onnx/model.onnx'); d(M,'tokenizer.json')"
```

The Zenbook has no CUDA, so Kokoro and Whisper stay on CPU. The mic is read through PipeWire. Playback is `pw-play` or `paplay`, then `espeak-ng`. `open_app` uses `.desktop` files and `gio launch`. `jarvis selftest keyboard` and `jarvis overlay` skip on Linux.

`voice.engine` is `elevenlabs` here. Kokoro is the fallback when the key is missing, the monthly budget is spent, or the network is down. Code defaults: `monthly_char_budget` 30000, `max_spoken_chars` 350, `kokoro_voice` `bm_george` (this config uses `bm_lewis`). Conversational speech uses `voice.model` (`eleven_flash_v2_5`) unless `ELEVENLABS_MODEL_ID` is set. The one-shot `jarvis speak` adapter defaults to `eleven_multilingual_v2`.

```text
jarvis voice demo
jarvis voice use bm_lewis
python -m jarvis voice setup
python -m jarvis speak "JARVIS is ready." --output .jarvis/audio/hello.mp3
jarvis voice agent-setup
jarvis voice agent
```

The key is stored in `.jarvis/secrets.json`. `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` override it. `.env` is not auto-loaded. `speak` writes an MP3 and does not play it. The Live button uses an ElevenLabs agent; the page receives a short-lived WebSocket URL from `/api/agent/session`. The agent needs a client tool `ask_jarvis` (`request`, wait for response) and Security → Overrides → TTS voice. Set `ELEVENLABS_AGENT_ID` or `voice.agent_id` to use one made in the dashboard. Live-call audio goes to ElevenLabs, so the local voiceprint does not apply to it. Listener commands are in [handbook/voice.md](handbook/voice.md).

## Files, clipboard, inventory

```text
jarvis send FILE
jarvis inbox
jarvis fetch NAME
jarvis clipboard start | stop | status | autostart on
jarvis inventory
jarvis inventory --device ID --output .jarvis/inventory/ID.json
```

`send` lands in `~/JARVIS-Drop`. `fetch` pulls back to `~/Downloads/JARVIS-Drop`. Chunks are 2 MB. The receiver publishes a name only when the file is complete. Clipboard sync is text and images, both ways, at most 32 MB, and only new copies. Password-manager secrets stay put (`x-kde-passwordManagerHint: secret` on Linux, `ExcludeClipboardContentFromMonitorProcessing` on Windows). Run `jarvis clipboard start` on each side. Pairing and Lan Mouse are in [handbook/lan-mouse.md](handbook/lan-mouse.md). Machine readings are in [handbook/operations.md](handbook/operations.md).

Live SSH targets are `devices.<id>.target` and `fallback_targets` in `jarvis.config.json`. They move with DHCP. The ASUS host key was checked on the machine; the recorded fingerprint is `SHA256:Up97wSPjhl95DuIoI6bTvYd53OZDfAZ92jnpEQ53QPM`. Remote collection uses Python 3 on Arch, sends the collector on stdin, and never stores a password. Paste Arch commands in fish.
