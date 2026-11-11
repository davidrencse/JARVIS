# Fleet backend audit

Independent read of fleet onboarding, phone/node auth, and started-tool replay.
No production edits, commits, service restarts, live DB writes, real node enrollment, or model CLIs.
SSH and key effects in the repros were mocked onto temp files. The real Dell was not controlled.

Repro: `/tmp/jarvis-fleet-backend-audit.py` (11/11 expected outcomes, exit 0).

## Evidence

| Class | What |
|---|---|
| Verified fake | Targeted suites below, plus the temp-dir repro. Cloud invite through real `cluster_setup.invite` stayed `["inventory"]`. Phone projection omitted `locate`, hostnames, and addresses. A MAIN change already on disk at call entry returned `not_main` and enrolled nothing. |
| Verified live | None this run. No live HTTP and no service status check. |
| Previously recorded, not re-checked | `/tmp/jarvis-independent-live-audit.json`: `/v1/status`, `/api/fleet`, `/api/fleet/vault` were 200; zenbook MAIN and dell online; main transfer blocked; archive capture complete, pending/held/errors 0, state current; cluster and voice active. That file does not prove join, remote input, camera, or MAIN failover. |
| Failed | Nothing in the commands below. The parent full suite was not repeated (3334 tests, 1 `BrokenPipeError` in `test_oversize_body_gets_413_not_reset`; see AUDIT-INTEGRATION.md). |
| Unverified | Mid-revoke race after the disk re-read. Physical phone. Real `~/.ssh/authorized_keys` (authorize was mocked). A second JARVIS process sharing one ledger. |

Existing green tests do not cover the five holes below. The 107 passing tests use a fake `cluster_setup.invite` or a single-threaded lane whose `tool_log` is still intact when the caller reads it.

Not bugs, and not done: stable installation identity, mode guard, tickets, Oracle authority/gateway, lease fencing, distributed compute, safe MAIN handoff, phone fleet admin, C3 canonical read cutover. Plans/runs/knowledge remain backfill/snapshot only. Main transfer still returns `blocked` and does not rewrite `cluster.main_device`.

## Findings

### 1. Medium — a finished tool step can be forgotten, and one turn can write into another turn's tool list

`jarvis/assistant.py:835`, `jarvis/chatgpt.py:403`, `jarvis/chatgpt.py:648`, `jarvis/inference.py:236`

`_orchestrated` records a successful step from `lane.tool_log` after `lane.ask` returns. `ChatGPTLane.ask` keeps the real copy on a thread-local (`turn_log`, set at `chatgpt.py:418` before the lock drops) and the next ask's first locked act is `self.tool_log = []`. `Failover.tool_log` and `Brain.tool_log` follow that shared list on the success path. Two console turns share one lane (`assistant.py:907`).

Repro, same thread's snapshot versus the shared list: after `ask("first")` recorded `open_app`, a second thread's `ask("second")` ran before the first thread read. `tool_log` was `[]`. `turn_log()` was still `[("open_app", True, "opened")]`. Feeding that cleared list to `_orchestrated` made the planner-failure path return `None` with no `state: partial`. The same planner with the list left intact returned the partial reply `open_app ran`. `assistant.ask` treats `None` as "the brain lane should answer" (`assistant.py:229`), which runs the tools again.

`Brain._act` then does `log[:0] = earlier` on whatever object `act.tool_log` currently is (`chatgpt.py:646`). That finally holds no lane lock. Pointing `tool_log` at another turn's list before the finally ran prepended `open_app` onto `[("other_request", True, "stolen")]`.

The lane's own `serverOverloaded` retry still stops when `_tool_pending` or `tool_log` is non-empty. That path is in the 7 ChatGPT tests and was not the failure.

Minimal fix: before releasing the lane lock, store this turn's finished calls on the calling thread, including the first turn `Brain._act` already saved in `earlier`. Publish that snapshot from `Brain.turn_log` and `Failover.turn_log`. `_orchestrated` must append `lane.turn_log()`, and the finally must prepend only onto the list object this turn created.

Regression: two threads on one `ChatGPTLane` (mocked `_ask`, no CLI). Thread B's ask completes in the gap after thread A's ask returns. Thread A's `turn_log()` still lists `open_app`, and a planner failure after that step returns `state: partial` and does not call the brain lane. A second case binds `tool_log` to another list before `Brain._act`'s finally; that other list stays unchanged.

### 2. Medium — a MAIN change during the locked invite still issues a credential

`jarvis/fleet_management.py:561`, `jarvis/fleet_management.py:592`, `jarvis/cluster_setup.py:338`

The lock re-reads the config and requires this PC to be MAIN (`fleet_management.py:561`). `_write_device` reads the file again (`fleet_management.py:494`) and writes `{**fresh, "devices": ...}`, so a newer `main_device` on disk is preserved. `invite()` is then called with `copy.deepcopy` of the first snapshot (`fleet_management.py:592`) and accepts it when that snapshot is still MAIN (`cluster_setup.py:338`).

Repro: inside the lock, the file's `main_device` was set to `dell` before `_write_device` read it. `issue_invite` returned a `jarvis-invite-1` code, the temp broker had an `oracle1` row, and the file's main stayed `dell`. Order: disk flip, authorize, enroll, code_key. A flip done before the call returned `not_main` and did not enroll. The before-call test in `test_fleet_management.py` still matches that second case.

Minimal fix: after `_write_device`, read the file again, run `_require_main` and the tunnel check on that read, and pass that object to `cluster_setup.invite`. If the new read is not MAIN, return `not_main` without calling `invite`. This remains a config check, not a lease.

Regression: temp config and temp broker, authorize/keypair/sshd mocked. Flip `main_device` on disk in the `_write_device` wrapper. Assert `FleetError not_main`, no `nodes` row, and no authorize call.

### 3. Medium — the invite credential is stored before the code exists, and that failure is not retryable

`jarvis/cluster_setup.py:365`, `jarvis/cluster_setup.py:371`, `jarvis/cluster_setup.py:375`, `jarvis/fleet_management.py:598`

`invite()` writes the SSH key (`authorize`), then `Broker.enroll`, and only then calls `code_key()` while building the code. `issue_invite` turns `ValueError` and `OSError` into `invite_failed` (`retryable: true`) and writes the ledger only after `invite()` returns. `RuntimeError` is uncaught.

Repro: `code_key` raised `RuntimeError` after the mocked authorize and the real temp enroll. The caller saw `runtime:Couldn't make the code-signing key: timeout`. The ledger was empty. The same `request_id` then got `invite_pending`. The row was `["inventory"]` with an invite expiry. The code was never returned, and the config file did not contain it. The temp key file had already been written.

The UI tells a `retryable` failure to reuse the request id (`jarvis/web/fleet.js:611`). This failure is not that code, and the follow-up is `invite_pending`, so the same id cannot finish.

Minimal fix: create the signing key and read the host key before `authorize` and `enroll`. If anything fails after `enroll`, `revoke` that node and remove the key line before raising. Catch `RuntimeError` only once that rollback has left no row, and then return `invite_failed` with `retryable: true`.

Regression: mock `code_key` to raise `RuntimeError` after enroll. Assert no `nodes` row, no key line, and the same `request_id` can issue once. Assert the HTTP body is `invite_failed`, not a bare 500.

### 4. Low — the same request id issues a second code after the ledger is gone, when replace is set

`jarvis/fleet_management.py:567`, `jarvis/fleet_management.py:579`

The ledger is an in-memory `OrderedDict` (`fleet_management.py:95`). A repeat of the same id in-process returns `already_issued` (covered by the suite). After `_LEDGER.clear()`, with the pending row still in sqlite:

- the same id without `replace_pending` returned `invite_pending` and did not enroll again
- the same id with `replace_pending: true` enrolled again and returned a second code (2 invites)

FLEET-API says the same `request_id` never issues a second code, and that a replacement uses a new id. The console does allocate a new id when the replace box is checked (`fleet.js:593`). A replay of the original POST body after a process restart does not.

Minimal fix: persist `request_id -> node` in the state directory, with no code in that record. `already_issued` applies to a known id even when `replace_pending` is true. Replacing the pending invite requires a new id.

Regression: issue once, clear the process ledger, leave the sqlite invite, call again with the same id and `replace_pending: true`. Assert one enroll and `already_issued` or `invite_pending`.

### 5. Low — the fleet payload calls a cloud node a gateway

`jarvis/fleet_management.py:324` and `jarvis/fleet_management.py:51`

For a device with `profile: cloud`, `main_eligible_why` is "Cloud servers are gateway-only and never become the main." The same inventory's cloud profile says enrollment does not install a gateway service. `invite()` writes a device entry, one restricted key, and one hub credential. It does not install a gateway. Oracle authority/gateway is still unimplemented (IMPLEMENTATION-PROGRESS.md). `main_eligible: false` for cloud is accurate.

Minimal fix: use the profile sentence (inventory only, never MAIN, no gateway service). Regression: `inventory` for a cloud device, assert `main_eligible_why` does not contain `gateway`.

FLEET-API also says the phone projection has no invite state. `GET` inventory for `audience: phone` still returns `status: invite_pending` and the matching step text. `tests/mobile/test_phone_fleet.py` asserts that status, so this audit does not treat it as a defect. The sentence in FLEET-API is the part that is wrong. Addresses, hostnames, `locate`, write paths, and `can_write` were absent on that projection.

## Commands

Targeted suite, from `/home/jdoe/projects/JARVIS`:

```sh
JARVIS_NO_MODELS=1 .venv/bin/python -m unittest \
  tests.cluster.test_fleet_management \
  tests.mobile.test_phone_fleet \
  tests.intelligence.test_inference_started_tools \
  tests.intelligence.test_chatgpt_started_tools \
  tests.intelligence.test_inference_races -q
```

107 tests in 9.168s, OK. Counts: fleet 59, phone fleet 7, inference started-tools 22, ChatGPT started-tools 7, inference races 12. stderr showed the injected `RuntimeError: boom /home/jdoe/secret` from `test_unexpected_exceptions_do_not_leak`; that test expects the 500 and passed.

```sh
JARVIS_NO_MODELS=1 .venv/bin/python /tmp/jarvis-fleet-backend-audit.py
```

11/11, exit 0. Temp directories only.

Not run: `unittest discover -s tests`, live `/api/fleet` against the running hub, `jarvis cluster invite` on the Dell, and any write to the real `authorized_keys`.
