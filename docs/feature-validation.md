# Feature validation

Validation date: 2026-10-21. Independent feature-validation agent, Windows, Python 3.13.11 (MiniConda), Node.js available.

## Results and scope

The initial offline suite passed **196 tests in 38.399 seconds, with no skips**. The complete output is in [feature-validation-baseline.txt](audit-evidence/feature-validation-baseline.txt). These tests exercise real temporary SQLite databases, local HTTP/SSE servers, CLI subprocesses, and fake provider subprocess protocols. Live model inference and physical hardware were not exercised.

Additional checks completed during this review:

- All 30 initial Python source modules parsed successfully; all eight shipped browser JavaScript files passed `node --check`.
- Seven additional HTTP transcription assertions passed against the real local handler with mocked speech models: successful float32 transcription, wake-word stripping, non-owner rejection without transcription, explicit verification override, three invalid clip sizes, and missing-header rejection. See [transcription-validation.json](audit-evidence/transcription-validation.json); the successful transcription/wake-word checks are one assertion group.
- Ten new browser microphone lifecycle and console-cancellation tests pass using Node with mocked browser APIs. No browser permission or real microphone was used.
- A wheel built successfully in a temporary source copy, but running `doctor` from the isolated extracted wheel failed because its default configuration is not packaged. See [package-validation.json](audit-evidence/package-validation.json).

Final integration results and evidence are recorded in [the phase 2 report](pipeline-hardening-phase2.md). The primary agent completed the final run after this feature agent reached the account usage limit.

## Feature coverage matrix

“Covered” describes the tested boundary. It does not certify an external account, remote device, physical microphone, or browser rendering engine.

| Feature | What was exercised | Remaining validation |
| --- | --- | --- |
| Task ledger and runner | SQLite persistence, lifecycle transitions, exclusive task claims, dead-owner recovery, real child-process success/failure/timeout, artifact logs, missing executable cleanup | Real Codex/Claude task execution; simultaneous independent OS processes beyond test cases |
| CLI | Real subprocesses for task add/list, prompt engineering, memory add/search/link/stats, routing dry run, instant answer, doctor, error exits; local `ui` process serving its API and page | Interactive chat terminal, actual background listener startup, startup-folder integration in a real Windows login |
| Conversation and routing | Simple/medium/complex selection, conversation default, owner API priority, local clock fast path, fallback behavior, recall, linked request/reply persistence | Real remote inference accuracy and latency |
| Provider transports | Fake JSON-streaming Claude and Codex app-server processes, reusable lanes, streaming sentences, recycle/recovery, deadline enforcement, stale-output isolation, subprocess reaping, stripped API-key environment | Authentication, changing upstream protocol behavior, provider tools, actual usage consumption |
| Plans and specialists | Shipped specialist parsing, malformed plan rejection, dependency order, failure blocking, generated task scope, result checkpointing, atomic plan-file replacement, prevention of replay after uncertain dispatch | Real specialist output quality and production acceptance criteria; new recovery tests are covered by the final combined run |
| Memory, investigations and graphs | Dedupe rules, hostile query input, links/tags, case creation and duplicate detection, capture/filter/export, history pairing, module graph completeness, no dangling graph edges | Large retained histories under sustained use; browser graph interaction/rendering |
| HTTP console and SSE | Actual loopback request/response handling, background jobs, one conversation at a time, plan jobs, static assets, traversal rejection, local-host checks, CSRF header checks, malformed/oversized requests, SSE replay/live events, lifecycle event relaying | Browser end-to-end navigation, accessibility, reconnect behavior under real network interruption, slow or disconnected clients |
| Push-to-talk | HTTP PCM ingestion with mocked transcription/owner verification; actual shipped JavaScript controller under mocked browser APIs; startup failure cleanup, retry, concurrency, cancellation, resampling, silence | Real browser permission prompts, AudioWorklet execution, physical recording and playback, browser UI cancellation wiring |
| Always-on voice | Mocked complete owner/wake-word/transcription/answer/speech loop; stranger rejection, continued listening after failed turn, single-instance locking, enrollment threshold/adaptation with synthetic arrays | Microphone acoustics, speaker false-accept/false-reject rates, GPU Whisper performance, real recognition quality |
| Speech output | Mocked Kokoro and ElevenLabs synthesis, cache reuse, character budget, output no-overwrite checks, bad content cleanup, ordered speech queue | Model downloads, actual local model compatibility, speaker output, paid ElevenLabs responses |
| Desktop actions/browser bridge | App matching, mocked launch calls, safe URL handling, browser tool approval policy, real fake-server request/approval round trip | Actual app launch and browser control; confirmation behavior for consequential actions remains a separate product limitation |
| Overlay/app lifecycle | Hotkey parsing, mocked Windows focus APIs, refusal to trap focus, mocked Chromium app-window launch, last-window shutdown logic | Real global hotkey registration, actual window focus across apps, desktop scaling, compositor behavior |
| Device inventory and Lan Mouse | SSH argument validation, strict trust flags, collector parsing, certificate fingerprinting, peer config validation, probe verdicts, log/self-test marker matching | Live remote connectivity, keyboard/mouse handoff, clipboard, screen streaming, stress behavior |
| File browser and transfers | Temporary local paths, traversal/symlink boundaries, actual remote-helper script under local Python, fake SSH/SCP copy directions, filename handling, streamed upload HTTP validation | Real SSH/SFTP compatibility, large interrupted transfers, remote permissions/filesystems; new staging tests are covered by the final combined run |
| Credits | Existing local Codex event parser, Claude usage tally/deduplication, manual entries, malformed provider responses and masked key-saving behavior | Current account quota retrieval is separately implemented and documented by the provider-limits agent; token tally alone is not a plan quota |
| Prompts/security boundaries | Input normalization and limits, read-only role prompts, evidence isolation, ownership/acceptance fields, allowed workspace/mode validation | Provider enforcement and prompt-injection resistance during live tool use |
| Packaging and launcher | Source-checkout import/CLI, all Python/JS syntax, actual isolated wheel build and launch attempt, inspection of `start.bat` | Wheel defaults are incomplete; batch launcher was not executed because it starts voice and a real app window |

## Defect found and corrected

`Mic.start()` acquired a microphone stream before loading its AudioWorklet. A rejected worklet load, failed audio-context creation, or failed connection left that stream open. `active` remained false, so the previous `stop()` returned without releasing anything. A Node reproduction against the original code observed zero stopped tracks and zero closed contexts, even after `stop()`.

The controller now owns each startup attempt separately, releases acquired tracks/nodes/context on failure, shares concurrent startup attempts, supports cancellation while permission/worklet loading is pending, and keeps late completions from replacing a newer recording. Normal recording still returns resampled 16 kHz audio. Regression tests exercise the shipped `voice.js` through mocked browser APIs.

Run the focused tests with:

```powershell
python -m unittest discover -s tests -p test_web_voice.py -v
```

## Packaging findings

The source-checkout workflow documented in the README works in the offline tests. A standalone wheel has a narrower supported surface:

1. `pyproject.toml` includes the Python package and browser assets but excludes `jarvis.config.json`. `__main__.HOME` expects that file beside the installed package, so an isolated `python -m jarvis doctor` exits 1 with a missing-file error.
2. Specialist definitions under `.claude/agents` are also absent from the wheel. A packaged deployment needs explicit resource/config handling before promising the orchestration workflow outside a checkout.
3. The `voice` extra lists the recorder, Whisper, and speaker-verification dependencies but omits `kokoro-onnx`, even though Kokoro is the default speech engine. The README separately documents its manual installation.

These are existing deployment limitations, not evidence that source-checkout behavior regressed. No installed package or user configuration was replaced during validation.

## Highest-value remaining checks

1. Add browser end-to-end tests for the main conversation, file, case, credits, and microphone permission/error controls; syntax checks alone cannot catch DOM integration mistakes.
2. Keep real-device acceptance tests separate from routine offline CI: audio capture/playback, overlay focus, Lan Mouse release, and cross-machine copy need the real devices.
3. Add documented minimal live provider smoke tests with explicit account/source reporting and bounded usage. The offline tests establish protocol handling, not a usable authenticated account.
4. Decide whether distribution remains source-checkout-only. If so, state that clearly; if wheels are intended, ship default resources without personal configuration or secrets and add a clean-install test.

No live paid inference, physical microphone recording, desktop launch, real remote copy, or message to another person was performed by this feature-validation work.
