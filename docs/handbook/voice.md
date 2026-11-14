# Voice

*Wake word, speaker check, and speech output on the main. Setup commands that install models are in [reference.md](../reference.md).*

The listener is `jarvis voice start`. Capture is 16 kHz mono through PipeWire on Linux. Silero VAD (or a loudness gate fitted to the room) cuts one utterance. The speech gate starts at 0.4 and holds at 0.18.

On CUDA, the speaker check and transcription run together. On CPU the check runs first, so a stranger's clip never queues ahead of David. Parakeet is first (`voice.stt_engine`, default). Whisper runs when Parakeet is unsure. A stranger's words are discarded unread unless the first `HEAD_SECONDS` (2.5 s) open with the wake word.

| Score | What happens |
|---|---|
| At or above the learned threshold | Owner. Threshold is clamped to `FLOOR` 0.30 .. `CEILING` 0.36 |
| `near_miss` (default 0.25) up to the threshold | Near miss. Counts only when the clip also starts with "Jarvis". Never used for adaptation |
| `stranger_floor` (default 0.08) up to `near_miss` | First 2.5 s only, and only to look for the wake word |
| Below `stranger_floor` | Ignored. Never transcribed |

`speaker_check: false` means nobody is David. The daemon then requires "Jarvis" on every request and learns nothing. Push-to-talk (`ui.transcribe`) still returns the transcript with `owner=False` and `addressed=False`. That split is intentional until David says to align them.

Accepted wake words: jarvis, jervis, jarvas, javis, jarvus, jarviss, jarves, plus Parakeet's jovers, jarbus, and jovis, plus a fuzzy match that Whisper confirms. A lead-in such as "hey" or "okay" is allowed. "Travis" and "Jarvis's …" are not. `listen.fuzzy_wake` falls back when `WAKE_LEAD` cannot be imported, so a partial install does not crash.

A bare "Jarvis" arms 6 s for the request. A reply window of 8 s after JARVIS stops talking needs no wake word, and only for a full owner match. Approving or cancelling a confirmation always needs "Jarvis". The voiceprint learns only from an owner clip that said "Jarvis" in that clip, and only when `voice.adapt` is not false. Enrollment clips stay pinned. Learned clips rotate, 40 max. Learned entries that do not match the enrollment are ignored, so a poisoned profile cannot move the threshold by itself.

If the mic dies, `reopen_after` closes it and the loop opens it again. Nothing is spoken, and the failure counter does not move. Only real speech resets the idle timer. Failed unload retries wait `IDLE_TICK` (30 s). An in-flight CUDA transcription is not unloaded. The user unit sets `MemoryMax=3G` and `MemorySwapMax=0` and omits `MemoryHigh`, because a 2800M high-water mark throttled a listener that sits near 2.6 GB once models are loaded. ONNX CPU memory arenas are off in `listen.py` and `embed.py` so a long clip or an index batch can return that memory.

Only the spoken lead is voiced: the text before the first blank line, table, list, heading, quote, code fence, or URL. A reply that opens with a table says "The answer is on screen."

| Key | Default | Meaning |
|---|---|---|
| `speaker_check` | `true` | `false` disables owner match and learning |
| `near_miss` | `0.25` | Floor for a near miss |
| `stranger_floor` | `0.08` | Below this, never transcribe |
| `adapt` | `true` | Learn from good clips |
| `stt_engine` | `parakeet` | `parakeet`, `moonshine`, or `whisper` |
| `stt_model` | `large-v3-turbo` | Whisper model |
| `engine` | `kokoro` in code; `elevenlabs` in this config | Speech output |
| `kokoro_voice` | `bm_george` in code; `bm_lewis` here | |
| `monthly_char_budget` | `30000` | ElevenLabs characters per month |
| `max_spoken_chars` | `350` | Spoken length cap |

```text
jarvis voice enroll | test | recalibrate
jarvis voice start | stop | status
jarvis voice autostart on
jarvis voice demo | use bm_george
.venv/bin/python -m jarvis.voice_eval [--quick] [--download] [--no-whisper]
```

`voice test` prints live scores. `recalibrate` rebuilds the print from saved clips. `enroll`, Settings → Voice, or the first-run setup records through the listener's own mic path, fits Silero from a "Hey Jarvis, can you hear me?", and swaps `enroll.new/` in only on success. On a node the console answers `/api/voice/setup` itself, because a voiceprint belongs to the mic it came from. `voice_eval` uses recorded and synthetic clips and never opens the mic. `.jarvis/logs/voice.log` shows each score. "no wake word (score): N words, not kept" means the voice was accepted and the words were not for JARVIS, so they were not written down.

Idle unload, hidden-tab mic polls, and the console caches are in [perf.md](perf.md). Restart `jarvis-voice` after a Python change to this path. Regenerate the unit after a `MemoryMax` change.
