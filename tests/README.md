# Tests

Run from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests
```

Discovery recurses through the subsystem packages; the release runner uses this same command.

| Folder | Coverage |
| --- | --- |
| `mobile/` | Phone API, PWA, camera and location |
| `remote/` | Remote sessions, input sharing, clipboard, transfer and Lan Mouse |
| `cluster/` | Main/node transport, setup, roles and signed jobs |
| `desktop/` | Application actions, Spotify, keyboard/mouse, OS backends and terminals |
| `intelligence/` | Parsing, interpretation, routing, inference and conversation |
| `security/` | Permissions, gates, approvals, browser restrictions and sandboxing |
| `voice/` | Microphone, wake word, speech and voice lifecycle |
| `web/` | Console UI and JavaScript harnesses |
| `data/` | Storage, vault, memory, indexing and filesystem helpers |
| `runtime/` | Launchers, releases, settings, usage and workflow |
| `audits/` | Regression suites spanning several subsystems |
| `eval/` | Existing evaluation datasets |
| `archive/` | Preserved pre-change backup files; excluded from discovery |

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
