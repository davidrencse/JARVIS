# QA parity checkpoint

Read-only audit of the test harness on the dirty working tree of
`implementation/ui-parity-checkpoint-20261111` at
`845b55800a2243e826fa5096bbb8520c1ac8dd86`. No repo file other than this report
was written. No full suite, no server, no `jarvis eval`, no install, no device,
no vault, no config dump.

Verdict: the tree is a useful fake-backed suite with a real Node DOM layer, and
it is not a production-ready or current-green claim. The last recorded full run
(3434 tests, OK, 7 skipped, 1 expected failure) is older than
`jarvis/web/app.js`, `jarvis/settings.py`, `jarvis/web/settings.js`, and
`jarvis/fleet_management.py`. Open consolidation defects remain. One documented
Node command fails on this tree. Future MAIN lease, gateway, safe handoff,
distributed compute, and Obsidian C3 (vault as the primary read store) are
unimplemented. Obsidian C2 is the active writer; newer repairs are not
activated. Battery checks use Linux and Windows fixtures. The Dell pack was
not read.

## Commands this audit ran

| Command | Result |
| --- | --- |
| `git rev-parse --abbrev-ref HEAD` and `git rev-parse HEAD` | `implementation/ui-parity-checkpoint-20261111`, `845b55800a2243e826fa5096bbb8520c1ac8dd86` |
| `git status --porcelain \| wc -l` and status-code counts | 231 lines: 147 deleted, 50 modified, 34 untracked. The brief said 230. This count is from before this report existed. `docs/design/` is already one untracked line, so this file does not add a line. |
| `google-chrome-stable --version` | Google Chrome 157.0.8103.38. The binary is a shell script at `/usr/bin/google-chrome-stable`. |
| `node --version` | v24.13.0 at `/home/jdoe/.var/app/com.visualstudio.code/config/nvm/versions/node/v24.13.0/bin/node` |
| Tool presence (`type`, `command -v`, `.venv` import check) | See the availability table. |
| `idevice_id -l` | `ERROR: Unable to retrieve device list!` |
| Static hash compare of `HEAD:tests/test_*.py` against the working tree | 143 HEAD modules, 176 working modules, 100 identical moves, 43 content changes, 33 new modules, 0 HEAD modules missing a successor. |
| Static `ast` count of `test_*` methods | 3450 methods on classes in `tests/**/test_*.py`. This is not a `Ran N` figure. The suite was not loaded. |
| Node DOM scripts below | Fleet 36, phone 42, remote 17, meters 16, meters card 1, all exit 0. Voice file exit 1. Agent file with no scenario name exit 1; each named scenario the Python wrappers call exited 0. |
| `/tmp` probe of `python -m unittest` argv, then deleted | `sys.argv[0]` is the literal `python -m unittest`. Importing `jarvis` set `JARVIS_NO_MODELS=1` and the audit hook. |

Node stderr on the passing scripts was one warning: `NO_COLOR` is ignored because `FORCE_COLOR` is set. Exit status stayed 0.

Not run, on purpose: `python -m unittest discover -s tests`, any Chrome or Playwright launch, `jarvis eval`, `adb`, simulators, the consolidation repro script, and the `/tmp` fleet browsers.

## The tree discover must use

`unittest discover -s tests` walks the working tree, not the git commit.

| Layout | What discover loads |
| --- | --- |
| Commit `845b558` | Flat `tests/test_*.py` (143 modules in that commit). Phone, fleet, second-brain, hostmeter, and the package `__init__.py` files are absent from the commit. |
| This working tree | Those flat files are deleted. Suites live in untracked packages: `tests/{audits,cluster,data,desktop,intelligence,mobile,remote,runtime,security,voice,web}/`. `tests/README.md` is untracked. `tests/eval/` and `tests/archive/` stayed. |

A clean checkout of the commit is a different suite. The checkpoint command is the dirty tree, from the repo root, with `.venv/bin/python`.

`tests/archive/` holds `*.pre-*` backups. Discovery's default `test*.py` pattern does not load them. Verified by listing: no `test_*.py` there.

`tests/__init__.py` sets `JARVIS_NO_NOTIFY=1` and does not import suites, so discovery does not double-run them. `jarvis/__init__.py:17-20` and `:96-98` arm the model audit hook when `sys.argv[0]` is `python -m unittest` or the file name starts with `test_`. Both entry points were probed. The hook blocks `codex` and `claude`. It does not make the suite free of loopback servers.

## Availability (no installs)

| Tool | This machine | Suite effect |
| --- | --- | --- |
| `.venv/bin/python` | 3.14.9 | Required runner. |
| numpy | 2.5.4 | Voice and workflow `skipUnless` cases run. |
| Pillow | 13.0.0 | Screen and clipboard image cases run. |
| node | the nvm path above | `shutil.which("node")` succeeded in this shell. A shell without that nvm bin skips every Node wrapper. |
| ssh-keygen | `/usr/bin/ssh-keygen` | Cluster invite and job-signing tests run. |
| bwrap | `/usr/bin/bwrap` | Terminal-sandbox tests that `skipTest` without bwrap will run. |
| npx | same nvm prefix | Browser-MCP tests that `skipTest` without npx will run. |
| Chrome | `/usr/bin/google-chrome-stable`, 157.0.8103.38 | Present, so `RemoteRenderChaos` is not skipped. |
| playwright-core | `/home/jdoe/.npm/_npx/381cec31605a419a/node_modules/playwright-core`, version `1.64.0-alpha-1790635538000` | The chaos renderer imports this directory. The pip package `playwright` is absent. |
| adb, emulator, xcrun, simctl, appium | not on `PATH` | No Android emulator and no iOS Simulator path. |
| idevice_id | `/usr/bin/idevice_id` | Present. Device list failed. No phone was attached for this audit. |

## How counts work

`Ran N tests` counts collected Python methods. A Node file that the wrapper runs as one subprocess counts as one method even when it prints `ok 36`. `tests/mobile/test_phone_web.py` runs each phone scenario as a `subTest` of one method. Adding the Node PASS lines on top of 3434 double-counts.

The Python wrappers do not call every scenario in the `.cjs` files. The voice gap below is the one that fails today.

`tests/runtime/test_live.py:20` reads `jarvis.config.json` at import and then disables vault, cluster, semantic memory, and the interpreter in the in-memory copy. This audit did not import that module and did not print the file.

Several modules bind `127.0.0.1` with `ThreadingHTTPServer` during a full discover: `tests/web/test_ui_security.py`, `tests/web/test_ui_body_rejection.py`, `tests/cluster/test_fleet_management.py`, `tests/cluster/test_cluster_proxy.py`, `tests/data/test_storage.py`, `tests/mobile/test_phone_e2e.py`, `tests/remote/test_remote_view.py`, `tests/remote/test_remote_chaos_browser.py`. The phone end-to-end module uses a real `phone.py` server, a hub with no nodes, and fakes for Tailscale, the screen, input, ffmpeg, location, and the brain (`tests/mobile/test_phone_e2e.py:1-8`).

## Baseline commands

Run from `/home/jdoe/projects/JARVIS`. Keep the nvm `node` on `PATH`. Prefix a full discover with `JARVIS_NO_MODELS=1` as well; the hook also sets it, and the explicit variable covers child processes.

| Gate | Command | What a pass means |
| --- | --- | --- |
| Full suite | `.venv/bin/python -m unittest discover -s tests -v` | Historical bar: `OK (skipped=7, expected failures=1)`. Use `-v` so the seven skips are named. The 15:27 log does not name them. Expect on the order of four minutes. This audit did not run it. |
| One package | `.venv/bin/python -m unittest discover -s tests/mobile` | Same discovery rules, one package. |
| One module | `.venv/bin/python -m unittest tests.intelligence.test_parse` | Dotted package path. The old `tests.test_parse` path is gone from the worktree. |
| Fleet DOM | `node tests/web/web_fleet_test.cjs` | `ok 36`, exit 0. Ran here. |
| Phone DOM | `node tests/mobile/web_phone_test.cjs` | 42 `PASS` lines, exit 0. Ran here. |
| Remote DOM | `node tests/remote/web_remote_test.cjs` | 17 `PASS` lines, exit 0. Ran here. |
| Meters DOM | `node tests/web/web_meters_test.cjs` | 16 `PASS` lines, exit 0. Ran here. |
| Meter card | `node tests/web/web_app_meters_card_test.cjs` | `ok 1`, exit 0. Ran here. Windows CIM battery kept when a Linux probe reports `power_supply` missing. |
| Voice DOM, all scenarios | `node tests/web/web_voice_test.cjs` | Must exit 0. It exited 1 here. See finding V1. |
| Voice DOM, one scenario | `node tests/web/web_voice_test.cjs <name>` | The Python wrapper's names passed here. `always_on_sends_only_requests_that_start_with_jarvis` failed. |
| Agent DOM | `node tests/web/web_agent_test.cjs <name>` | A name is required. The ten names the wrappers call passed. The bare file threw. See finding V2. |
| Chrome remote renderer | included in discover via `tests.remote.test_remote_chaos_browser.RemoteRenderChaos` | Skipped only when Chrome or playwright-core is missing. Both exist here, so a full discover launches headless Chrome against a stub on a random localhost port and writes `~/.cache/jarvis-breakit`. Viewports in source: 1440×900, 1180×800, 720×450, 320×640 (`test_remote_chaos_browser.py:721`). Not run here. |
| Free scorecard | `JARVIS_NO_MODELS=1 .venv/bin/python -m jarvis eval` | Parser, registry, decider, vault, grounding, judge. Appends `.jarvis/eval/runs.jsonl` (`jarvis/scorecard.py:20-21`). `--brain` spends subscription quota. Not run here. Handbook `docs/handbook/perf-2026-11.md` records an older 211-case run. Those figures are not this tree's baseline. |
| Perf behavior | `.venv/bin/python -m unittest tests.web.test_ui_perf tests.intelligence.test_latency_fixes tests.voice.test_voice_idle` | TTL, ETag, app-list cache, focus timeout, idle unload. They assert behavior with a fake clock. They do not print milliseconds. |

`tests/README.md` matches the package commands. It lists `node tests/web/web_voice_test.cjs` and does not say that `unittest tests.web.test_web_voice` covers only a prefix of that file.

## Checkpoint matrix

Use this before UI implementation. A row is green only when its own command was run on this tree after the latest edit to the files it loads.

| Checkpoint | Expected on a good tree | Evidence now | State |
| --- | --- | --- | --- |
| Worktree, not the commit | Discover sees `tests/mobile`, `tests/web`, `tests/cluster`, … | Flat files deleted; packages untracked | Required. A clean `845b558` fails this. |
| Full discover | OK, 7 skips, 1 expected failure, no quota use | `/tmp/jarvis-all-audit-repairs-final.log` line 261: `Ran 3434 tests in 252.354s`, line 263: `OK (skipped=7, expected failures=1)`. Log mtime 15:27. `app.js` 15:42. `settings.py`, `settings.js`, `fleet_management.py` 15:48. | Stale. Do not cite 3434 for the current tree. |
| Earlier full runs | Same shape, smaller totals | Fleet final log: 3334 in 239.543s. Battery combined log: 3415 in 253.787s. Both OK, 7 skips, 1 expected failure. | Historical only. |
| Expected failure | One xfail | The only decorator is `tests/remote/test_lanmouse_fuzz.py:235`, `GuardSweep.test_manual_stop_sticks_until_boot`. Comment at 237-240: a hand stop before the first post-boot reading is undone. `jarvis/sharedinput.py` is unmodified, and the fuzz module matches its HEAD blob. | Inferred still failing, from the 15:27 count plus unchanged files. The 7000-sequence class was not re-run. |
| CR counter label | The label says "expected failure" | `test_lanmouse_fuzz.py:35` and `:405`. `jarvis/sharedinput.py:432` escapes `\r` as `&#13;`. The comment at line 395 still says the CR bug exists. | The label is a print string. It is not `@expectedFailure`. The CR method was not re-run. |
| Fleet DOM | 36 pass | Ran. `ok 36`, exit 0. | Pass on this tree. |
| Phone DOM | 42 pass | Ran. 42 PASS, exit 0. | Pass on this tree. |
| Remote DOM | 17 pass | Ran. exit 0. | Pass on this tree. |
| Meters | card keeps a Windows battery across a blind Linux probe | Both scripts exit 0. | Pass on fixtures. Dell hardware unread. |
| Voice DOM file | exit 0 | exit 1 on `always_on_sends_only_requests_that_start_with_jarvis` | Fail. Finding V1. |
| Agent DOM names | each wrapper name exits 0 | 10 names exit 0 | Pass when named. Bare file fails. Finding V2. |
| Model hook | `JARVIS_NO_MODELS=1` under `python -m unittest` | Probed outside the repo. Hook true. | Pass for this interpreter. |
| Physical phone or emulator | a device command | adb and xcrun absent; idevice list failed | No device gate exists. |
| C3, lease, gateway, handoff, distributed compute | absent, and tests keep them absent | `tests/runtime/test_harness_baseline.py` pins today's gaps. Fleet DOM asserts the client does not POST `/api/fleet/main-transfer`. Phone DOM asserts no owner write URL. | Unimplemented. A new UI must not present them as working. |

## Coverage by screen

Console hashes come from `jarvis/web/app.js:119-125`. `#graph` redirects to `knowledge.html`. `#run` has no rail key.

| Route | Screen | Action the UI takes | API | Test evidence | Gap |
| --- | --- | --- | --- | --- | --- |
| `#console` (default) | Console | Type a request, confirm, watch the trace, hold Space to talk | `POST /api/ask`, `POST /api/confirm`, `GET /api/events`, `POST /api/listen` | `tests/web/test_ui.py`, `test_ui_audit.py`, `test_ui_security.py`, `tests/intelligence/test_pipeline.py`, voice DOM | Always-on wake query is red outside the Python wrapper (V1). Headless Chrome denying the mic is expected (`CLAUDE.md`). |
| `#graph` | Leaves the console | `location.replace("knowledge.html")` | knowledge page | `tests/web/test_sysview.py`, `tests/data/test_knowledge.py` | The rail id `graph` is a redirect, not a graph canvas. |
| `#system` | System design | Read the station map | `GET /api/system/design`, `GET /api/system/components` | `tests/web/test_sysview.py` | A module added without a station fails `test_every_module_belongs_to_a_station`. That failure is in `/tmp/jarvis-battery-integration-focused.log` and was later fixed for hostmeter. Re-run after any new module. |
| `#credits` | Credits | Refresh usage | `GET /api/credits`, `GET /api/usage` | `tests/web/test_credit_dashboard.py`, `tests/runtime/test_usage.py` | Dashboard Node test is behind `skipUnless(node)`. |
| `#memory` | Memory | Search and open an item | `GET /api/memory`, `GET /api/memory/item` | `tests/data/test_vault.py`, `test_semantic.py`, second-brain modules | C3 canonical read is not implemented. SQLite remains the live store. |
| `#run` | Run | Open a plan from the trace | `GET /api/plans/<id>`, `GET /api/jobs/<id>` | `tests/intelligence/test_plan_recovery.py`, `test_pipelines.py` | No rail link. Easy to drop in a redesign. |
| `#files` | Files | List, thumbnail, copy, send | `GET /api/fs`, `GET /api/fs/thumbnail`, `POST /api/fs/copy`, `POST /api/send` | `tests/data/test_storage.py`, `test_fsio.py`, `tests/web/test_thumbnails.py`, `tests/remote/test_transfer_cluster.py` | Storage tests bind loopback. |
| `#devices` | Connections | Fleet inventory, invite, revoke, capabilities, archive and Git status | `GET /api/fleet`, `GET /api/fleet/vault`, `POST /api/fleet/preview`, `POST /api/fleet/invite`, `POST /api/fleet/revoke`, `POST /api/fleet/capabilities` | `node tests/web/web_fleet_test.cjs` (36, passed), `tests/cluster/test_fleet_management.py`, `test_surface_liveness.py` | Backend findings 1, 2, and 4 are outside these DOM checks. See the open-defect table. |
| `#devices` transfer | Same screen | Preview only | `POST /api/fleet/main-transfer/preview`. `POST /api/fleet/main-transfer` stays unimplemented | Fleet scenario `main_transfer_is_preview_only_and_shows_what_is_missing` passed. `tests/web/test_web_fleet.py:26` asserts the client source has no POST to the bare transfer path. | Settings files changed at 15:48, after the 3434 log. The parent's separate 10-check settings run was not repeated here. |
| `#remote` | Remote | Connect, pointer, keys, wheel, disconnect releases held input | `POST /api/remote/start`, `POST /api/remote/input`, `POST /api/remote/stop`, `GET /api/remote/frame` | `node tests/remote/web_remote_test.cjs` (17, passed), `tests/remote/test_remote_view.py`, chaos modules | Chrome layout pass not run. Chaos comments still name BR findings; several are asserted as the desired end state inside the Node VM, so a pass means the VM script matched, which needs a fresh read of each test when the UI changes. |
| `#settings` | Settings | Read and write fields. Loosening a safety net requires `confirmed: true`. | `GET /api/settings`, `POST /api/settings` | `tests/runtime/test_settings.py`, `tests/security/test_permissions.py` | `settings.py` and `settings.js` are newer than the full-suite log. |
| `knowledge.html` | Knowledge | Upload and read pages | `POST /api/kb/upload`, `GET /api/kb/page` | `tests/data/test_knowledge.py`, `tests/intelligence/test_knowledge_launch_policy.py` | Launch policy is a P2 guard, covered by its own module. |

Phone shell, from `jarvis/web/phone/index.html` and `jarvis/phone.py`. Location UI and `navigator.geolocation` are removed; `tests/mobile/test_phone_web.py` asserts that.

| Route | Screen | Action | API | Test evidence | Gap |
| --- | --- | --- | --- | --- | --- |
| `#/devices` | Devices | Pick a computer. One Manage entry. Remote stays first. | `GET /phone/api/overview` | Phone scenario `devices_screen_has_one_manage_entry_and_remote_stays_first` passed | No physical iPhone safe-area or keyboard run. The DOM scenario `installed_ios_uses_full_height_and_measured_keyboard_height` is a fake viewport. |
| `#/ask` | Ask | Send text to the selected computer, poll, show cost as not a subscription charge | `POST /phone/api/ask`, `GET /phone/api/ask/<job>` | Ask scenarios in the 42 passed | Live brain not called. |
| `#/phone` | Settings | Pair, log out | `POST /phone/pair`, `POST /phone/pair/status`, `POST /phone/api/logout`, `GET /phone/api/me` | Pairing scenarios passed in the DOM file. `tests/mobile/test_phone.py` | Pairing needs a tailnet address. Tests fake it. |
| `#` fleet section | Manage computers | Read-only list. No invite, revoke, or transfer. | `GET /phone/api/fleet` only | `fleet_screen_reads_only_the_phone_fleet_endpoint_and_has_no_write_controls` and `fleet_source_has_no_owner_write_endpoint` passed. `tests/mobile/test_phone_fleet.py` | Write controls belong on the main console. |
| session screen | Screen control | Move, click, type, release on hide | `POST /phone/api/session/start`, `POST /phone/api/session/input`, `GET /phone/api/session/frame`, `POST /phone/api/session/stop` | Phone input scenarios passed. `tests/mobile/test_phone_e2e.py` (not run; starts loopback) | No real pointer and no Dell session. |
| session camera | Camera | Viewfinder only. No input and no release. | same session routes with kind camera | `a_camera_session_sends_no_input_and_its_end_sends_no_release` passed. `tests/mobile/test_camera.py` | ffmpeg is faked in the e2e module. |
| (removed) | Locate | Must stay absent | no `/phone/api/locate` | `test_phone_web.py` shell assertions; `tests/mobile/test_locate.py` still covers the Python helper | Do not put a radar tab back. |

## Coverage by package

Static `test_*` method counts. They include methods on helper base names in the same file and are not the `Ran` total.

| Package | Methods | What a pass protects | What it does not touch |
| --- | --- | --- | --- |
| `tests/intelligence` | 546 | Parser, interpreter, pipeline, inference, Grok routing, plans | Live ChatGPT, Claude, or Grok calls. The model hook blocks those CLIs. |
| `tests/runtime` | 486 | Settings, usage, workflow, archive CLI, hostmeter, harness baseline, second-brain runtime | `jarvis eval` scorecard run. Real systemd units. |
| `tests/remote` | 450 | Remote view, clipboard, Lan Mouse guard, overlay, chaos | A physical mouse, Lan Mouse process, or Dell injector. Fakes only. |
| `tests/desktop` | 433 | Apps, Spotify, keys, terminals, platform backends | The live Hyprland pointer. |
| `tests/cluster` | 393 | Hub, invite, roles, signed jobs, fleet API, privacy, liveness projection | A real node join, revoke, or SSH to Dell or Oracle. |
| `tests/data` | 344 | Vault, index, second brain, storage | The live vault and the live SQLite files. Tests use temp dirs. |
| `tests/security` | 214 | Gates, confirm, browser proxy, sandbox | A real browser profile. |
| `tests/mobile` | 192 | Phone server, camera, locate helper, fleet read-only, DOM | Hardware camera, GPS, and an installed PWA. |
| `tests/web` | 176 | Console HTTP, fleet DOM, meters, theme contrast ratios computed in Python | A headed browser, except the remote chaos class when discover reaches it. `tests/web/test_theme_contrast.py` measures token contrast in process. It does not screenshot. |
| `tests/voice` | 109 | Wake, mic, print, idle, reliability | The microphone. Headless Chrome has no mic. |
| `tests/audits` | 107 | Cross-cutting regressions | They are not a second full suite. |
| `tests/eval` | data | `cases.jsonl`, `grounding.jsonl`, `judge.jsonl` feed `jarvis eval` | Not collected by unittest. |

### New modules (no HEAD blob)

`tests/cluster/test_config_writers.py`, `test_fleet_contracts.py`, `test_fleet_management.py`, `test_fleet_role_routing.py`, `test_node_console_privacy.py`, `test_surface_liveness.py`, `tests/data/test_second_brain.py`, `test_second_brain_overflow.py`, `test_second_brain_queue.py`, `test_second_brain_storage.py`, `test_vault_record_exclusion.py`, `tests/intelligence/test_chatgpt_started_tools.py`, `test_inference_started_tools.py`, `test_knowledge_launch_policy.py`, `test_shorthand.py`, `tests/mobile/test_camera.py`, `test_locate.py`, `test_phone.py`, `test_phone_e2e.py`, `test_phone_fleet.py`, `test_phone_web.py`, `tests/remote/test_dropzone.py`, `test_remote_kinds.py`, `tests/runtime/test_archive_cli.py`, `test_archive_history_faults.py`, `test_harness_baseline.py`, `test_harness_boundaries.py`, `test_hostmeter.py`, `test_second_brain_runtime.py`, `tests/web/test_meters_web.py`, `test_theme_contrast.py`, `test_ui_body_rejection.py`, `test_web_fleet.py`.

43 moved modules differ in bytes from their HEAD blobs, including `test_ui.py`, `test_settings.py`, `test_remote_view.py`, `test_inference.py`, and `test_clipsync.py`. A diff against `845b558` is required before treating a failure as new.

## Open defects the green log does not close

Merged with `AUDIT-SUMMARY.md` and `AUDIT-CONSOLIDATION-BACKEND.md`. This audit did not re-run `/tmp/jarvis-consolidation-backend-audit.py`.

| Item | Status in the audits | What the suite actually pins |
| --- | --- | --- |
| Stale full-config write during invite | High, still open. `fleet_management.py` device write can put an older config back. | `test_main_flipped_during_device_write` covers the flip before `_write_device` reads. The backend audit says that test does not reach this window. |
| Role flip during `code_key` | High, still open. A flip inside key generation still returns a code. | `tests/cluster/test_config_writers.py` has `test_role_flip_while_the_signing_key_is_made_stops_before_any_credential`. The audit says a later window still issues credentials. Treat the audit repro as the open case. |
| Held oversize task blocks a later smaller revision | High, still open. | `tests/data/test_second_brain_overflow.py` covers publish and rollback of a payload over the cap. The audit's shrink-then-drain case is the one still open. |
| Invite compensation restores a token and leaves jobs `uncertain` | Medium, still open. | Signing-key failure before authorize is tested and described as resolved. The job-state compensator is the remaining case. |
| Oversized HTTP body | An earlier full log (`/tmp/jarvis-independent-audit-suite.log`) died with `BrokenPipeError` in `test_oversize_body_gets_413_not_reset`. The 3434 log was OK. `AUDIT-SUMMARY.md` still records a 1-in-20 connection reset before 413. | `tests/web/test_ui_security.py:66` posts `MAX_AUDIO + 1000` to a loopback `/api/listen`. One green run does not retire a 1-in-20 reset. |
| Settings MAIN mutation | Parent blocked it and recorded 10 checks after the 3434 log. | Those 10 checks were not repeated here. The settings files are newer than the log. |
| Liveness, archive partial, invite wording | Surfaces audit listed them. | Today's fleet DOM passed `a_node_busy_past_the_online_window_reads_busy_not_online_or_offline`, `a_partly_verified_archive_never_claims_complete_capture`, and `a_non_retryable_invite_failure_shows_the_server_reason_and_survives_a_label_edit`. That is fixture-DOM evidence for those strings. `test_surface_liveness.py` checks one Python projection for connections, owner fleet, and phone fleet. It does not open the three rendered pages together. |
| Windows battery overwritten by a Linux probe | Surfaces audit. | `web_app_meters_card_test.cjs` passed: a CIM battery survives `power_supply` missing. Fixture only. |
| Lan Mouse hand-stop across boot | Expected failure, still the documented bug | `test_lanmouse_fuzz.py:235`. |
| MAIN lease, gateway, safe handoff, distributed compute, C3 primary | Unimplemented on purpose | Baseline pins and the blocked transfer UI. Not regressions. |
| Obsidian | C2 write-through is the live path. C3 is not. SQLite is still the source of truth. | Second-brain tests use temp vaults. Live 832-record parity was not re-opened. |
| Repairs after the last restart | Not activated, per `IMPLEMENTATION-PROGRESS.md` and `AUDIT-SUMMARY.md` | A green suite does not mean the running `jarvis-cluster` process loaded these files. |

## Findings

| Id | Severity | Where | What happened | Resolution |
| --- | --- | --- | --- | --- |
| Q1 | High | `/tmp/jarvis-all-audit-repairs-final.log:261` versus file mtimes | The OK 3434 run finished at 15:27. `jarvis/web/app.js` was written at 15:42. `jarvis/settings.py`, `jarvis/web/settings.js`, and `jarvis/fleet_management.py` were written at 15:48. | Re-run discover on this worktree before calling the checkpoint green. Publish the log path and the seven skip names (`-v`). |
| Q2 | High | `git status` | 231 dirty lines. Discover on a clean `845b558` loads the flat suite and misses the untracked packages. | Run and review against the dirty tree. Land the package move with the product files so the commit and the suite match. |
| V1 | Medium | `tests/web/web_voice_test.cjs:242` and `jarvis/web/app.js:1163-1171`, `:1200-1209`, `:1248`, `:1311` | `node tests/web/web_voice_test.cjs` exit 1. `always_on_sends_only_requests_that_start_with_jarvis` expected `['/api/listen?wake=1']` and got `['/api/listen']`. Reproduced alone, same mismatch. Space calls `startMic(false, true)`. Push-to-talk stops a waiting clip and starts with `wait` false, so `stopMic` omits `?wake=1`. Page load uses `startMic(true)`, which would keep the wait. `push_to_talk_needs_no_wake_word` and `waiting_keeps_only_a_preroll` passed when named. `tests/web/test_web_voice.py` stops at line 67 and never calls `always_on`, `mic_button`, `enter_sends`, `push_to_talk`, or `waiting`. | Drive the wake=1 case from the page-load always-on path. Keep Space as the no-wake path. Call both from `BrowserMicrophoneTests` so discover fails if they break. The product comment at 1165-1166 already says Space during a wait is push-to-talk. |
| V2 | Low | `tests/web/web_agent_test.cjs:275-276` | `node tests/web/web_agent_test.cjs` with no argument throws `TypeError: scenarios[name] is not a function`. The wrappers pass a name. All ten wrapper names exited 0: handshake, protocol, start_failure, change_voice, corrupt audio, microphone fallback, audio controls, all-mics select, clearest mic, multi-mic fallback. | Guard a missing `process.argv[2]` with the scenario list. The README does not tell anyone to run this file bare. |
| Q3 | Low | `tests/remote/test_lanmouse_fuzz.py:395` | The CR test's comment still says `_task_xml` lets a CR become a LF. The function escapes CR. The unittest expected-failure count is the hand-stop test, not this one. | Update the comment when the CR method is next run. Do not add a second `@expectedFailure` for it without a failing run. |
| Q4 | Low | Node stderr | `NO_COLOR` is ignored because `FORCE_COLOR` is set. Scripts still exited 0 when the assertions passed. | Unset one of the two in the checkpoint shell if the warning should disappear. It did not fail a test. |

## Flaky and historical logs

These files are on this machine's `/tmp`. They are not a durable suite. A reboot drops them.

| Log | What it recorded |
| --- | --- |
| `/tmp/jarvis-all-audit-repairs-final.log` | 3434 OK, 7 skips, 1 expected failure, 252.354s. ResourceWarnings for unclosed sqlite connections and a cookie jar. Not test failures. |
| `/tmp/jarvis-repair-battery-combined.log` | 3415 OK, same skip and xfail shape, 253.787s. |
| `/tmp/jarvis-fleet-final-integration.log` | 3334 OK, same shape, 239.543s. |
| `/tmp/jarvis-independent-audit-suite.log` | `ERROR` `BrokenPipeError` in `test_oversize_body_gets_413_not_reset` under the old import path `web.test_ui_security`. |
| `/tmp/jarvis-battery-integration-focused.log` and `/tmp/jarvis-harness-obsidian-integration.log` | `test_every_module_belongs_to_a_station` failed before hostmeter was registered. Later logs are OK. |
| `/tmp/jarvis-ui-presence-followup.log` | Three `ReleaseReadinessTests` failures in `tests.remote.test_remote_view`. Later full logs are OK. |
| `/tmp/jarvis-organized-full-tests.log` | `test_bad_target_never_reaches_spotify` failed during the move, under `desktop.test_spotify`. |
| `/tmp/jarvis-mobile-integration-tests.log` | `test_real_child_sees_scrubbed_env` failed. `docs/handbook/perf-2026-11.md` says the child saw Codex harness variables and passed after they were unset. This audit's shell had none of `CODEX_APPLY_PATCH_PRESERVE_LINE_ENDINGS`, `CODEX_CI`, `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`, `CODEX_SESSION_ID`, `CODEX_THREAD_ID`, `CODEX_TOOL_CALL_ID`, `CODEX_VERSION`. A shell that has them can fail that test again. |
| `/tmp/jarvis-no-location-python.log` | `test_phone_js_never_parses_server_text_as_html` failed during the location removal. The phone DOM file passes now. |

## Harnesses outside the suite

| Path | Role | Limit |
| --- | --- | --- |
| `/tmp/jarvis-fleet-ux.mjs` and `/tmp/jarvis-fleet-ux/report.json` | One-shot Playwright script. Fake HTTP server, fake fleet payload, screenshots at desktop and 390-wide phone sizes. `report.json` records overflow 0 for the scenes it captured. | Not collected by unittest. Not re-run. The script contains a fake invite code. Do not treat the JSON as a regression test. |
| `/tmp/jarvis-fleet-ux-grok.mjs` and `/tmp/jarvis-fleet-ux-grok/` | Same kind of pass, second copy. | Same limit. |
| `/tmp/jarvis-fleet-ui-qa/` | `serve.py`, `phone.html`, shots. | Ad-hoc. Not a named test. |
| `/tmp/jarvis-consolidation-backend-audit.py` | Parent's temp-dir repro for the four open backend findings. | Not part of discover. This audit did not execute it. |

## Performance method

Measure again on this tree. Do not copy numbers forward.

| Measurement | Method | This audit |
| --- | --- | --- |
| Scorecard | `JARVIS_NO_MODELS=1 .venv/bin/python -m jarvis eval`. Writes `.jarvis/eval/runs.jsonl` and compares with the previous run. Cases live in `tests/eval/`. | Not run. Handbook figures from 2026-11-07 stay in that handbook. |
| Brain | `jarvis eval --brain` | Out of bounds. It spends subscription quota. |
| UI cache | `tests/web/test_ui_perf.py` uses a fake clock for TTL, ETag 304, and the file tree cache. | Not run. No millisecond claim. |
| Latency fixes | `tests/intelligence/test_latency_fixes.py` asserts cache and timeout behavior. | Not run. |
| Voice idle | `tests/voice/test_voice_idle.py` | Not run. |
| Render overflow | The `/tmp` fleet script recorded overflow fields. Chrome remote chaos checks horizontal scroll at four viewports when discover runs it. | Neither was run here. |
| Host battery | `tests/runtime/test_hostmeter.py` reads fixture trees, including `test_linux_fixture_tree_skips_mains_and_absent_packs` and Windows WMI cases. | Not run. The meter card DOM script did pass. No Dell reading. |

## Acceptance bar for the UI work

Preserve every row in the screen tables. A redesign may change layout and must keep the route, the API, and the refusal.

- Phone Manage stays read-only. Remote stays the first device action. Locate stays gone.
- Console transfer stays a preview. The client keeps refusing `POST /api/fleet/main-transfer`.
- Invite secrets stay out of the DOM text dump. The fleet scenario `issues_the_invite_shows_the_secret_privately_and_clears_it` passed and is the check.
- Disconnect and hide still release held keys and buttons. Remote DOM `blur_lets_go_of_everything_held` passed.
- Archive status must be able to say partial or pending. The partly-verified fleet scenario passed.
- Always-on listen must send `?wake=1` when the clip was a wait, and must omit it for Space push-to-talk. V1 says the all-scenario file does not currently pass.
- New screens do not add a gateway, a lease, a handoff button, or a C3 "vault is primary" claim.
- After the UI change, re-run the Node DOM commands in the baseline table, then discover. The 3434 log does not cover that change.
