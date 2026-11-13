# Tests

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests
```

Discovery recurses through the subsystem packages; the release runner uses this same command.
Directories below are the ones under `tests/` (plus `__init__.py` and this file). `eval/` is JSONL only and has no `__init__.py`.

| Folder | What it holds |
| --- | --- |
| `audits/` | Audit suites for the brain, data, pipeline, listen/autostart/selftest, and system |
| `cluster/` | Main/node transport, setup, roles, proxy, signed jobs, fleet, membership, pairing, readiness, and doctor |
| `continuity/` | Authority, gateway, ingress, handoff drills, executor, runtime fences, and the phone receiver |
| `data/` | Vault, storage, knowledge, semantic memory, brainsync, second brain, consolidation, and filesystem I/O |
| `desktop/` | Actions, app UI, desktop control, screen, Spotify, terminals, platform backends, and system intents |
| `eval/` | Scorecard datasets: `cases.jsonl`, `grounding.jsonl`, and `judge.jsonl` |
| `intelligence/` | Parse, interpret, inference, brain, decide, Grok routing, pipelines, prompts, and situation |
| `mobile/` | Phone API, phone web harness (`web_phone_test.cjs`), camera, location, fleet, and end-to-end |
| `remote/` | Remote sessions, chaos suites, Lan Mouse, clipboard, dropzone, shared input, overlay, peripherals, and transfer |
| `runtime/` | Archive CLI, history, events, harness, release, rest, settings, usage, workflow, scorecard, and selftest |
| `security/` | Always-ask, confirm, gates, hard deny, permissions, browser gate, sandbox, and hardening |
| `voice/` | Microphone, wake word, speech-to-text, voice print, idle, level, and main-only voice |
| `web/` | Console UI, agent, fleet, meters, sysview, theme, and the Node harnesses beside those tests |

Run a subsystem or an individual module:

```bash
.venv/bin/python -m unittest discover -s tests/mobile
.venv/bin/python -m unittest tests.intelligence.test_parse
node tests/mobile/web_phone_test.cjs
node tests/remote/web_remote_test.cjs
node tests/web/web_voice_test.cjs
.venv/bin/python -m unittest tests.web.test_agent
```

Keep supporting fixtures beside their tests, and update repository-root paths when moving a module.
Each discoverable Python folder has `__init__.py`. Cross-subsystem imports use the complete
`tests.<subsystem>.<module>` path. Do not import suites into `tests/__init__.py`: that would
run tests twice during discovery.
