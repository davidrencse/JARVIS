# Main session handoff — Team B

Date: 2026-11-13
Scope: implementation and readiness for Arch Zenbook → Dell → Arch main transfer.

## Guardrails

- Do not change the live main role, restart fleet services, release/update nodes, suspend
  hosts, or control mouse/keyboard. Root coordinates live acceptance after both teams integrate.
- Do not edit the existing dirty phone paths. Do not commit, push, change CI, spend API credits,
  edit the vault, or expose secrets.
- Treat loopback/fixture tests as code evidence only, never proof of live transfer.
- Use `.venv/bin/python` for tests.

## Starting evidence

`CLAUDE.md`, `docs/architecture-guide.md` and `docs/migration.md` were read. The architecture
guide describes continuity as a prototype, with the ordinary `cluster.main_device` still the
authority. It specifically documents missing production binding for the configured handoff,
participant, and recovery callback; it also states that the executor is not wired into
`Remote.call` and continuity hooks remain default-off. Brain bundle transport additionally
requires configured `devices.<id>.brain_transfer_dir` paths. These are repository statements,
not live inspection results; they may be stale and will be checked against implementation and
read-only live reconnaissance before being treated as blockers.

`docs/migration.md` describes the historical Dell → Zenbook move and ordinary cluster join flow;
it is not a runbook for the requested return trip or managed safe handoff.

## Worker allocation

Ten Grok 4.7 workers were launched with disjoint file/test scopes: CLI and journal, startup
binding/fencing, gateway, repoint/tunnel lifecycle, brain return, release parity, dispatch
integration tests, read-only live reconnaissance, operational test repair, and read-only
integration review. Results and focused test commands will be appended after integration.

## Live acceptance

Not run by this team. No live role, service, release, or host state was changed.

## Read-only live reconnaissance

The read-only reconnaissance worker reported on 2026-11-13 that this host is the Zenbook
(`archmain`), the cluster service is active, and the legacy main remains `zenbook`.
The gateway status was `LEGACY_ACTIVE`, `main` null, `lease_valid` false, with no transition.
The Dell was recently heartbeating the served release; Oracle was also online but is not
main-eligible. No continuity managed/endpoint/handoff/successor markers were found in the
checked state paths. The operator config exists locally, but the running hub has no managed
binding because the marker/operator-config prerequisites are not both satisfied.

The live configuration lacks `continuity.operator_config`, `storage.roots`, and per-PC
`devices.<id>.brain_transfer_dir` entries. The Zenbook SSH identity path is absent; Dell has
no SSH target. The existing constrained node key cannot serve as the successor route. The
Dell disk/operator file and staging directory remain unchecked. No secret values or key
material are included here.

The live run is blocked before any mutation. In addition to missing binding, the reported
source-level gaps are: production wiring for SafeHandoff/participant/runtime/issuer and
recovery; readiness wiring for role/tunnel switch; successor routing; both staging directories;
and owner proof that remains valid after Dell becomes main. Do not infer that fixture tests
prove the live transition. The safe rollout sequence and final commands remain pending the
implementation workers and root's integration review.

## Independent read-only integration review

The reviewer independently confirmed the live gateway remains `LEGACY_ACTIVE` at version 1,
epoch 0, with no main or lease, and the live hub's `configured_handoff` and
`operator_configured` checks are false. The live hub process uses the working tree, while the
Dell and Oracle heartbeats report the older served release. Thus no shared tested release is
available for an operational transfer.

The reviewer identified a safety-sensitive partial activation: do not create the managed
marker by hand or set `continuity.operator_config` until a complete production composition is
installed in the serving process. `bind_managed` can mark state managed after configuring
runtime/executor/issuer, while it currently omits SafeHandoff; that can change dispatch fencing
without making cluster role switching/recovery usable. The hub still has no production
`hub.continuity_recovery`, `RoleLifecycle`, SafeHandoff, or `TransitionBundleTransport`
composition. The ordinary role writer refuses without bound handoff; `recover` refuses without
the hub recovery coordinator.

The worker's safe read-only checks were `jarvis cluster roles` and
`jarvis continuity --config ~/.config/jarvis-continuity/operator.json status`. The reviewer
provided a future protocol outline, but it is intentionally not an executable live runbook yet:
it depends on code composition, Dell-side operator/staging verification, controlled release
parity, and a reviewed rollback path. The fixture drills are in-process/loopback fakes and are
not live proof. Exact final commands and preconditions remain for root's post-integration
acceptance review.

## Dispatch integration worker

The dispatch worker changed only `tests/continuity/test_managed_dispatch.py`. It added
coverage for a managed `transfer.call` inbox bulk job: the continuity envelope digest matches
the body, the node executor receives the body once, replay does not execute again, and the
test confirms SSH was not used. `integration.py`, `participant.py`, `executor.py`, and the listed
cluster production sources were inspected and left unchanged.

Focused result: `JARVIS_NO_MODELS=1` with `CODEX_*` unset,
`.venv/bin/python -m unittest tests.continuity.test_managed_dispatch
tests.continuity.test_participant_channel tests.continuity.test_executor_concurrency
tests.cluster.test_fleet_role_routing tests.cluster.test_integrated_fleet_gaps` — 105 tests
passed in 20.2s.

Unresolved from this track: an offline node raises `NodeOffline` after authority succeeds;
`send_file` may use SSH without a sealed ticket; brain handoff via `TransitionBundleTransport`
uses `transfer.run_here` inside the participant rather than `transfer.call`. Production source
was not changed. These require integration review and must not be treated as covered by the
new tests.

## Release parity worker

The release worker confirmed `jarvis.release.code_parity` is read-only and never releases,
deploys, updates, or authorizes by itself. A match requires a tested release, a live tree equal
to that release, a peer fingerprint equal to it, and no local rollback record. The `served_code`
fallback to the live tree when there is no release snapshot is not parity.

No caller on the transfer path uses `code_parity`: cluster dispatch/capability checks and
continuity participant validation use `served_code`; fleet inventory and doctor have separate
checks; `main_transfer` does not check parity. Therefore the parity helper's result currently
does not block the managed transfer path when no release exists. The worker did not wire these
callers because they were outside its assigned ownership. This remains an actionable integration
blocker before root may release/update either node.

The worker modified only `tests/runtime/test_release.py`, moving its `__main__` block below
`FingerprintTests` so script execution collects those tests. `tests/remote/test_release_audit.py`
was left unchanged. Focused result with `JARVIS_NO_MODELS=1` and `CODEX_*` unset:
`.venv/bin/python -m unittest tests.runtime.test_release tests.remote.test_release_audit` —
34 tests passed in about 10.5s. No release, deploy, or node update was run.

## Operational E2E test worker

The test worker reports no production code edits. Focused command:

```text
JARVIS_NO_MODELS=1 .venv/bin/python -m unittest \
  tests.continuity.test_operational_e2e \
  tests.continuity.test_drill_gateway \
  tests.continuity.test_drill_handoff \
  tests.continuity.test_safe_handoff
```

Result: 48 tests in 66.817s, OK, with two expected failures (`test_brain_transfer_is_a_ws3_seam`
and `test_phone_cookie_is_a_ws4_seam`). The drills use temp configs, loopback gateways, fake
clocks, and in-process adapters; the in-process phone adapter is not the listener on port 7787.
They do not involve the Dell, Oracle, or Tailscale, and do not establish live readiness.

The report highlights an unresolved source behavior: `ContinuityIntegration._blockers` currently
lets a device id named `oracle` clear the gateway-host-unverified blocker using a name-only
record. The test explicitly does not treat that result as live gateway verification. The
two expected failures remain outstanding seams. No live config or services were changed.

## Brain return worker

The worker changed `jarvis/continuity/return_brain.py`. Repeating a receive after an approval
is already `received` now re-parses and validates the bundle source, device, memory hash, and
freeze. It returns `already: true` without re-import when the approved live database matches;
a missing or changed database raises `memory_conflict` and leaves approval/freeze unchanged.
`activate` and `refreeze` likewise require that same approved snapshot before moving the freeze
or reporting memory preserved. Finished `activated`/`refrozen` approvals continue to archive;
interrupted/review phases still require `retire_interrupted`, and activation records the grant
before moving the freeze.

Focused result with `JARVIS_NO_MODELS=1`:
`.venv/bin/python -m unittest tests.continuity.test_roundtrip_adapters
tests.continuity.test_handoff_recovery` — 21 tests passed in 42.6s. No live role, service, or
config changed. `retire_interrupted` is still not wired to the continuity CLI (`cli.py` is
outside this worker's scope), so interrupted approval recovery remains an operational gap.

## CLI and journal worker

The worker changed `jarvis/continuity/cli.py`, `console.py`, `hub_control.py`,
`tests/continuity/test_cli.py`, and `test_handoff_surfaces.py`. The new
`jarvis continuity main DEVICE --hub-config HUB --state STATE` path (with existing `--config`
and `--yes`) writes a metadata-only mode-0600 receipt before posting. A lost response leaves
`outcome_unknown` and the prior transition id for same-target retry. An open receipt for a
different target, corrupt/symlink receipt, or local operator identity mismatch refuses before
posting. The CLI does not take `fleet-manage.lock`, dispatch a job, or rewrite the role file.
The hub's existing `cluster_cli.set_main` journals `main-handoff.json` only when a bound hub
receives the request. Console gateway status now shares one in-flight read per operator path;
a timed-out waiter cannot overwrite the owner's cached result.

Focused result: `JARVIS_NO_MODELS=1` with `CODEX_*` unset, running the CLI, console gateway,
bootstrap adapter, manual recovery CLI, and handoff-surface modules — 68 tests passed in 1.433s
(one non-failing pre-existing unclosed-SQLite ResourceWarning). The worker confirms evidence is
fixture/loopback only. A retry posts again and does not itself publish `cluster.main_device`;
terminal receipts can be replaced for a different target; a failed directory fsync can leave
`outcome_unknown` without posting. No live role change, restart, release, or commit was made.

## Repoint and tunnel lifecycle worker

The worker changed `jarvis/cluster_tunnel.py`, `jarvis/cluster_recovery.py`, and the owned
tunnel/recovery tests. A held `cluster-tunnel.lock` is treated as owned even when its note has
a stale PID; explicit start signals stop and waits up to 40 × 0.25s before spawning one
replacement. A hung SSH is reaped with a five-second timeout, reports `SSH did not exit`,
signals stop, and releases the lock. Continuity refusal exits before/inside SSH without a
traceback or second dial. Signed peer pin changes drop the active SSH session. A stop during
discover leaves the lock released. `repoint.py` was inspected and left unchanged; it validates
the signed link before reading handoff configuration.

Focused tests (with `CODEX_*` unset and `JARVIS_NO_MODELS=1`):

- `tests.cluster.test_tunnel_failback`, `tests.continuity.test_repoint_lifecycle`,
  `tests.continuity.test_repoint_verify_cache`: 41 passed in 14.834s.
- `tests.cluster.test_link_recovery`, `test_link_reconnect`, `test_doctor_operator_stop`,
  `test_link_supervisor`, `tests.cluster.test_cluster.TunnelTests`: 53 passed in 1.816s.
- `tests.cluster.test_ws2_membership.TunnelLifetimeTests`: 8 passed in 22.562s.

Actionable gaps remain: `lifecycle.switch_node` still publishes the endpoint and
`cluster-node.json` before tunnel activation; `prepare_successor` writes `cluster-node.json`
without the signed link, so a linked successor fails `effective_config` with `bad_state`.
`cluster_setup` still owns doctor refusal text. Ruff reports 18 issues on the four edited files,
mostly pre-existing style/import-order issues; the worker notes one intentional `BLE001` for
re-raising discover-thread failure on the main thread. No live config, service, SSH, or role
was changed.

## Gateway and transport worker

The worker changed `jarvis/continuity/gateway.py`, `gateway_deploy.py`, and `brain_channel.py`,
plus gateway/deploy/failover tests. For an ineligible disabled participant, status, metadata
relay, and transient forwarding now return `unauthorized` while acknowledgement retains the
store's `revoked` result; same-installation re-enroll does not clear revocation. Client-only
directory entries are removed on disable, and repeated disable returns `unknown_participant`.
Relay reads/effects that complete after the MAIN tuple or pinned origin changes return
`no_relay`; an already-sent effect is not posted again, while a new operation id uses the new
origin.

`backup_gateway` now takes the same database writer lock, copies through a memory image, and
publishes using an `O_EXCL|O_NOFOLLOW` directory fd. It refuses symlink races and active writers
without following/unlinking replacements. Brain push reports `uncertain` and discards the
staged name after a successful stage followed by a dropped upload. Deployment script swap now
renames the old tree aside, restores it if staging rename fails, and requires the unit to stay
active through a 15-second poll. No deploy was run against Oracle.

Focused result: 62 passed in `test_gateway`, `test_gateway_deploy`, `test_gateway_failover`,
and `test_console_gateway`; additional listed gateway frame-timeout, transient-relay,
BrainChannel, transition-bound brain transport, and restore-publication cases passed. Exact
combined count for the additional cases was not reported. Environment `JARVIS_NO_MODELS=1`,
`CODEX_*` unset. No phone/core modules or live deployment were touched.

Remaining issues from the worker: HTTP dispatch still calls metadata relay rather than
`forward_transient`, and `MAX_BODY` remains 8192. It also reports a console status cache-lock
race; however the shared tree now contains the CLI worker's `_INFLIGHT` single-flight change in
`console.py` with owner publication under `_LOCK`. Root should verify this apparent report/code
discrepancy before accepting the console race as open or fixed. Unresolved unrelated dispatch
and payload-size findings remain actionable.

## Startup binding and fencing worker

The worker changed `jarvis/continuity/startup.py`, `integration.py`, and the owned startup and
production-fence tests. `SelfRecovery` now refuses a missing canary. If `bind_managed` has no
`NodeMainServices`, it sets `_configured_main_canary` only when the runtime has started and its
control probe still meets; it does not start console or phone servers. A node with services
still uses `services.activate`. The stale-issuer fence remains: a signer other than the current
ready MAIN raises `IntegrationError("stale")` before signing.

Focused result: `JARVIS_NO_MODELS=1` with `CODEX_*` unset,
`.venv/bin/python -m unittest tests.continuity.test_startup_binding
tests.continuity.test_production_fences -v` — 30 passed in 25.536s. The simulated sequence
binds Dell as MAIN, then Zenbook after failback; Zenbook becomes `main_ready`, Dell's server
closes, Zenbook's issuer signs, and Dell's issuer is stale. This is a test transfer, not live
acceptance.

Adjacent tests `tests.continuity.test_participant_channel` and
`tests.continuity.test_roundtrip_adapters` ran 35 tests in 62.568s but failed one:
`test_signed_fence_ack_is_idempotent_and_does_not_cover_the_new_epoch` expected
`AuthorityError("bad_state")` on a second `transition_step(..., "fence")`, but got no exception.
The worker reports this path does not call `production_issuer`. Roundtrip adapters passed. This
is an unresolved integration/test failure requiring root review; do not claim all focused tests
are green.

## Consolidated acceptance status and safe command sequence (initial worker state)

All ten worker outcomes have been collected. These changes improve retry receipts, stale-main
fencing, return idempotency, tunnel supervision, gateway backup/relay behavior, and test
coverage. They do not establish a runnable live handoff. The live observations above remain
unchanged because this team made no live mutation. The current code still has no production
composition of `SafeHandoff`, `ManualRecovery`, and `RoleLifecycle` (searched in `jarvis/` after
the worker merges), no `hub.continuity_recovery` assignment, and no production
`TransitionBundleTransport` construction. The configured managed marker/operator path is absent
on the running Zenbook, and the Dell operator/staging/route configuration remains unverified.
Do not enable the marker or point `continuity.operator_config` at the existing operator file
until root closes those composition and dispatch-fencing gaps.

Other explicit blockers to close before operational acceptance:

1. The running hub uses the working tree while Dell reports the older served release; tests and
   code-parity checks are not wired into transfer admission. Do not release/update until root
   has integrated and tested both teams' complete tree.
2. Dell has no verified successor SSH route/operator binding in this report. The reported
   constrained node key is not a successor route. Dell disk/operator/staging state is unchecked.
3. `devices.<id>.brain_transfer_dir` and `storage.roots` are absent. The tunnel worker also
   found successor endpoint publication before tunnel activation and `prepare_successor` omits
   the signed link.
4. Return owner proof after Dell becomes MAIN is not established. `retire_interrupted` is not
   CLI-wired. Brain transfer still has an expected seam failure.
5. One adjacent participant test fails on duplicate fence acknowledgement. The test-only
   dispatch worker leaves offline-node and unticketed `send_file` paths unresolved; HTTP gateway
   dispatch still uses metadata relay, not transient forwarding, and has an 8192-byte cap.
6. The operational E2E tests intentionally retain two expected failures and the name-only
   `oracle` gateway-host blocker behavior. No live gateway-host proof comes from those fixtures.

Read-only baseline commands for the operator to run on the Zenbook and then independently on
the Dell (using that PC's own operator config):

```text
jarvis cluster roles
jarvis continuity --config <local-operator.json> status
```

Expected from the inspected Zenbook before cutover: `zenbook` main; gateway `LEGACY_ACTIVE`,
version 1, epoch 0, no lease/main/transition. If any values differ, stop and have root re-read
the live gateway and local state. Keep authority ids, key paths, and key material out of output.

The following is the intended protocol order only, **not safe to run today**. Root may use it
only after the code-composition, route, parity, and Dell-side checks above pass on one tested
release and a rollback/owner-proof review is complete. Use the latest status version at each
edge; run acknowledgements on both eligible PCs with each PC's own operator config:

```text
jarvis continuity --config <owner-operator.json> --yes mode PREPARING_V2 --expect-version <current>
jarvis continuity --config <owner-operator.json> --yes mode QUIESCED --expect-version <returned-version>
jarvis continuity --config <zenbook-operator.json> --yes ack --next-version
jarvis continuity --config <dell-operator.json> --yes ack --next-version
jarvis continuity --config <owner-operator.json> --yes mode V2_ONLY_NO_MAIN --expect-version <returned-version>
jarvis continuity --config <zenbook-operator.json> --yes ack
jarvis continuity --config <dell-operator.json> --yes ack
jarvis continuity --config <zenbook-operator.json> --yes recover --hub-config <zenbook-jarvis.config.json> --state <zenbook-.jarvis> --request-id initial-zenbook-main
jarvis cluster main dell
# After Dell status independently shows a ready lease for dell, from Dell:
jarvis cluster main zenbook
```

`V2_ACTIVE` is not an operator-settable mode; `ack` records a fence and does not drain; a ready
lease is required before each role publication. The final Dell→Zenbook command is especially
blocked until owner proof works after the Dell becomes main. Root should accept the live
roundtrip only after gateway status is `V2_ACTIVE` with the intended MAIN and valid lease, both
nodes report the same tested release fingerprint, one ordinary desktop/dispatch operation
after each move reaches the current main, and the old MAIN's fenced operation is refused. A
loopback or in-process result is not acceptance.

## Production composition implementation update

The earlier gap report is now partly closed in the working tree:

- `jarvis/continuity/production.py` builds the controller from the local operator
  config, enrolled PC installation pins, existing participant runtime and executor
  guard. It composes `RemoteRuntime`, `CandidatePort`, `SafeHandoff`, `ManualRecovery`,
  `RoleLifecycle`, `TransitionBundleTransport`, an installation-signed canary, and
  the production admission issuer. It rejects missing pins/routes and never copies
  another installation's private key.
- `Hub.start` gives startup the live Hub context before lease renewal. The successor
  can start its transition-scoped Hub after its own grant; local canary checks the
  running listener, runtime control readiness and open non-quarantined journal.
  Local signed canary calls execute against the real local participant rather than
  trying to treat the Hub itself as a Broker node.
- Authority-owner proof remains separate from installation credentials. A successor
  without the owner private key can bind and serve its promoted MAIN role, but recovery
  and another owner handoff refuse with `owner_proof_unavailable` there until a
  supported owner signer is provisioned; no owner key was provisioned to the Dell in
  the composed test.
- Source repoint now probes the signed successor endpoint before dropping the old
  tunnel, activates the signed link, and publishes the endpoint/note only after the
  new tunnel is ready. Retry phases preserve the active tunnel on a unit-start retry.
- Root's `hub_control.run_main` parity gate is complemented by the production
  controller's direct `_handoff` parity preflight, so direct bound handoff calls cannot
  bypass release parity. Root owns the shared Hub-control implementation.

Focused verification after these changes:

```text
JARVIS_NO_MODELS=1 .venv/bin/python -m unittest \
  tests.continuity.test_production_composition \
  tests.continuity.test_startup_binding \
  tests.continuity.test_repoint_lifecycle \
  tests.continuity.test_repoint_verify_cache \
  tests.continuity.test_bootstrap_adapters \
  tests.continuity.test_launch_hooks -v
```

Result: **65 tests passed in 35.227s**. `git diff --check` and `compileall` on the
changed production/test modules also passed. The process-boundary composition test uses
a temporary gateway and separate Dell `NodeWorker` process: the shipped Hub startup
binds the factory, owner recovery obtains a ready Zenbook lease with a signed live
listener canary, then a managed broker probe is served by the separate Dell process.
The test first creates and fences each fixture executor journal with its enrolled
installation key. Direct factory handoff refuses the fixture's missing tested release;
with only the fixture parity result stubbed true, the factory-bound `SafeHandoff` uses
the real Dell readiness RPC and stops at the gateway's `not_quiet` wait. It does not
wait out the lease or publish a new MAIN. These are fixture results only, not a Dell
or Oracle handoff.

## Remaining live prerequisites and command boundary

Read-only live inspection remains as previously reported: the running Zenbook still
has no managed marker/config binding, the gateway remains `LEGACY_ACTIVE` without a
lease, and the Dell is on the older served fingerprint. Dell-side operator/config and
staging data are unchecked. Therefore none of the mutating commands below is currently
safe to run.

The still-missing live material is concrete:

1. Install the tested, parity-matched release on Zenbook and Dell only after root's
   integration run. Both served fingerprints must match before a new transfer.
2. Provision each PC's own operator file with its installation private key and the
   reviewed enrollment pins. Keep the authority-owner private key only with its
   current owner. Dell needs a supported remote owner-signing path before Dell →
   Zenbook recovery/handoff can run; copying Zenbook's owner key to Dell is not an
   acceptable substitute.
3. Provision matching managed state through the supported enrollment process (never
   by hand), and a valid open executor journal already fenced at the current authority
   tuple on each PC. The running deployment must load the marker, operator path,
   `storage.roots`, and per-PC `brain_transfer_dir` only after parity is verified.
4. Review/provision both successor routes, including Dell's authenticated endpoint,
   TLS/forwarding listener, and signed link metadata. Confirm the transport from the
   Dell side with its own operator/config policy; this team did not inspect its disk.
5. Preserve `auto_recover`/`auto_failover` disabled during the owner cutover. Confirm
   return owner proof is available through the supported signer before making Dell
   MAIN.

Only after those gates, root's operational acceptance should run the protocol with
each PC's own operator file and read status at every edge. The command shapes are:

```text
jarvis continuity --config <zenbook-owner-operator.json> status
jarvis continuity --config <zenbook-owner-operator.json> --yes mode PREPARING_V2 --expect-version <status-version>
jarvis continuity --config <zenbook-owner-operator.json> --yes mode QUIESCED --expect-version <returned-version>
jarvis continuity --config <zenbook-operator.json> --yes ack --next-version
jarvis continuity --config <dell-operator.json> --yes ack --next-version
jarvis continuity --config <zenbook-owner-operator.json> --yes mode V2_ONLY_NO_MAIN --expect-version <returned-version>
jarvis continuity --config <zenbook-operator.json> --yes ack
jarvis continuity --config <dell-operator.json> --yes ack
jarvis continuity --config <zenbook-operator.json> --yes recover --hub-config <zenbook-jarvis.config.json> --state <zenbook-.jarvis> --request-id initial-zenbook-main
jarvis cluster main dell
# After Dell independently reports a ready MAIN lease; requires the supported owner signer there:
jarvis cluster main zenbook
```

After each role move, acceptance still requires a fresh gateway status showing the
intended ready MAIN, matching tested fingerprints, one ordinary desktop/dispatch job
arriving at that MAIN, and refusal of the old MAIN's fenced work. Neither this test nor
the prior loopback drills establish those live facts. No live role, process, release,
or service was changed by this implementation pass.

## Return path, pinned routes, and platform-stop hook

The shipped controller now has a scoped Dell-to-Zenbook return owner proxy. Dell's
operator metadata uses `owner_proxy_device: "zenbook"`; Zenbook's operator metadata
uses `owner_proxy_clients: ["dell"]`. The owner key remains on Zenbook. The signed
participant job can request only a `set_preferred_main` transition to the local
Zenbook candidate and owner steps for that same open Dell-to-Zenbook tuple; it cannot
set modes, enroll, remove participants, or supply gateway origins/key paths.

Each operator file's `successor_routes` map contains both PC records and describes
each PC's own route. The candidate resolves its endpoint using its local device id.
The endpoint's public HTTPS port may differ from its loopback `listen_port` only on
HTTPS routes, allowing the verified tailnet `:8443 -> 127.0.0.1:7781` path while
preserving existing `:443` services. A standby Hub advertises the exact pinned
endpoint as `cluster.main_url`; remote Host validation requires that hostname and
port together.

Platform/bootstrap integration hook for an exact local node process is:

```python
from jarvis.cluster import request_node_worker_stop
receipt = request_node_worker_stop(local_hub_config_path, local_state, timeout=35.0)
```

The worker registers `.jarvis/cluster-process.json` with its resolved config/state,
PID, and fresh local nonce. It consumes only the matching
`.jarvis/cluster-stop-request.json`, finishes its current poll/job, closes the
continuity renewal/guard, remote input sessions, and cameras, then writes a matching
`.jarvis/cluster-stop-ack.json`. The helper accepts timeouts up to 45 seconds. It
does not signal or kill a PID and does not use an owner credential. A mismatched
config/state, unsafe record, or timeout refuses; a timeout leaves the request for a
retry. Team A's Windows service lifecycle should call this local hook for a
registered `NodeWorker`, then verify the process/service exit and restore through
its platform helper. It should not use this node hook for a Hub-only process.

For cutover service timing, `Hub.defer_after_response` now queues local or
cross-thread shutdown actions against the active authenticated owner MAIN request.
`RoleLifecycle.switch_node` publishes and records tunnel state, writes the command
response first, then starts the old MAIN's node unit. `NodeMainServices.sync` uses
the same response boundary when a return transition drains its Hub. If no matching
owner request is active, shutdown follows the normal path.

Current implementation evidence is in `tests/continuity/test_production_composition`
and `tests/cluster/test_node_cooperative_stop`. Process fixtures do not establish
live Tailscale, Windows service, desktop/phone availability, or tested-release
parity; the two live Serve routes are configured but their Hub backends are still
inactive until root performs the separately authorized setup.

Latest targeted execution after the response-pivot changes:

```text
JARVIS_NO_MODELS=1 .venv/bin/python -m unittest \
  tests.continuity.test_production_composition.ProductionCompositionTests.test_factory_completes_one_way_handoff_with_real_broker_and_brain_transport -v
```

Result: **1 test passed in 35.219s**. This test performs both the initial
Zenbook-to-Dell move and the full Dell-to-Zenbook return in isolated processes.
The scoped negative checks (owner tuple/key refusal, unavailable owner proof,
exact pinned remote Host, external HTTPS port vs local listener, failed response
write) and cooperative-stop refusal/timeout checks passed separately: **10 tests
in 0.356s**. The standalone repoint lifecycle slice passed **27 tests in
107.748s**. `git diff --check` is clean. Root's `ProductionController.close`
method relocation was preserved on `ProductionController`.
