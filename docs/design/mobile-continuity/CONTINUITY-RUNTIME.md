# Continuity runtime — implementation contract

Date: 2026-11-11. Status: **isolated library plus tests. Not operational failover.**
Nothing in the running JARVIS imports `jarvis.continuity.runtime`. No gateway is
provisioned, Oracle and Dell readiness were not checked, and `cluster.main_device`
is untouched. Luna +5 / Grok 4.7 is a readiness boolean the hub must supply from a
local subscription check. This module never starts a provider and never spends a
paid API.

Peers `gateway.py`, `ingress.py`, `executor.py`, and `integration.py` are now in
the tree. They do not call this `MainRuntime`. `GatewayPort` adapts the
authority client method names onto the port below. It does not authenticate
and it does not weaken a fence. Mismatches are in sections 6 and 9. The
operational end-to-end test in `tests/continuity/test_operational_e2e.py` is
not this slice and is not green.

## 1. What this slice owns

| File | Role |
| --- | --- |
| `jarvis/continuity/runtime.py` | PC lifecycle (`MainRuntime`) and hub recovery driver (`RecoveryOrchestrator`) |
| `tests/continuity/test_runtime.py` | Regression against the real `AuthorityStore` and `CachedAuthority` |
| This document | Contract, codes, tests, limits |

It does not own cutover (`set_mode`), phone ingress, executor receipts, or gateway
authentication. `StoreAdapter` is an in-process test/supervisor mapping. It does
not authenticate. A public process must not expose it.

Default behavior is legacy. V2 admission fails closed. Automatic recover and
failback default off. Failback cannot be configured under 30 seconds and never
uses `make_main_now` or `recover`.

## 2. `MainRuntime` (the contract facade)

```text
start() -> RuntimeState
readiness(profile="control") -> Readiness
renew_once() -> LeaseView
require_authority(effect_class, target=None) -> AuthorityContext
begin_drain(transition_id) -> DrainReport
release_before_suspend() -> ReleaseReport
invalidate_after_resume() -> None
stop_new_admission(reason) -> None
```

Construct with `(port, device_id, installation_id, probe, *, clock="auto", boot=None)`.
`probe()` returns booleans only. There is no config object and no writer.

Also required, because `AuthorityStore.status()` does not return `lease_id`:

- `accept_lease(lease)` — remember the grant response in RAM. A restarted PC
  cannot renew until the lease lapses and recovery runs. The id is not written
  to disk.
- `acknowledge_current()` — ack the epoch `status` names, for this installation
  only. A live MAIN may ack only while `release_before_suspend` is in progress
  or local drain/hold is already set.
- `state()` — read-only metadata. After a partition this is the last snapshot
  with `stale: true` and `admission_open: false`.

`effect_class` is `control`, `ordinary`, or `ai`. Anything else, including a role
change, is `bad_request`. `require_authority` is not an admission ticket: it
always sets `consume_required: true` and never calls `consume`.

### Readiness

`ai_ready` is true only when `control_ready`, `luna`, `grok`, `quota_ok`,
`mediated_tools`, and `session_local` are the boolean `True`. A model string,
a file path, or `1` does not count. `session_local` means the subscription
session is on this PC. The runtime does not copy it and does not check it
itself. Control readiness does not imply AI readiness, and an `ai` transition
is never rewritten as `control`.

`checkpoint` is an identifier (`sha`-style or `cp-1`) or absent. Free text,
prompts, filenames, and the banned keys in section 5 raise `bad_request` or
`bad_checkpoint` before any port call.

### Admission latch

| Latch | Renew extends the lease? | Cleared by a later ready renew? |
| --- | --- | --- |
| `stop_new_admission` | no | no, for the life of this process |
| `suspend` (release) | no | no; `invalidate_after_resume` moves it to `resumed` |
| `resumed` | yes, if this PC is still the ready MAIN | yes |
| `draining` | no, while the authority transition is open | yes, after cancel removes it |
| `partition` / `stale` / `expired` | one attempt | yes, only if this PC is still ready MAIN |

`require_authority` always reads `status()` when the link is up. A warm
`CachedAuthority` is an extra veto, not a substitute:

- remaining `0` stops new work;
- link loss invalidates the cache and stops new work even if time remains;
- Linux re-anchors only from `renew` `valid_for`, at the request start, never
  from `status.lease_remaining`;
- no suspend-aware clock (Windows/macOS): nothing is cached; every
  `require_authority` uses the live status and fails if that read fails.

`begin_drain` stops this PC only. It does **not** call store `drain` (that call
is owner-authenticated; see the orchestrator). It also does not ack, because an
ack during `DRAINING` makes a later `cancel` leave no MAIN.

`release_before_suspend` stops local work, then best-effort `release`, then
acks only if the release actually cleared the lease (or this PC held none).
`busy` (a transition is open) does not ack and does not renew. A missed ack
does not elect a successor; the store fence still has to finish.

## 3. `RecoveryOrchestrator` (hub driver)

```text
step() -> Step
open_manual(action, candidate, client_key, profile=None) -> Step
submit_canary(transition_id, ok: bool) -> Step
cancel(transition_id) -> Step
note_discontinuity() -> None
```

`RuntimePolicy` fields: `preferred` (initial hint only), `recover_candidates`,
`auto_recover=False`, `auto_failback=False`, `failback_stability_s=30` (minimum
30), `readiness_ttl_s=10` (must be `0 < ttl <= 15`), `profile="control"`,
`caller="managed-hub"`. There is no force flag. Non-bool auto flags are refused.

`step` while a lease is valid does not recover. With auto-recover off and no
lease, the code is `manual`. With auto-recover on, the candidate order is:
stored `preferred`, then policy `preferred`, then `recover_candidates`. The
first fresh `pc` whose directory entry meets the profile wins. A preferred PC
that is not ready is skipped so a prepared successor can be chosen. No second
choice is tried after a transition has been opened. A server, a stale
`observed_at`, a non-bool, or banned text is not eligible. Oracle is not a
special case in this file; the store still rejects a server, and a directory
that lies about kind gets `ineligible` with no row left open.

Directory entries need `kind=="pc"`, `observed_at` from the same clock as
`step`, and the same booleans as the probe. Older than `readiness_ttl_s` is
not ready. Failback also needs a checkpoint id and `failback_stability_s` of
continuous fresh readiness. The target is `status.preferred` if set, else the
policy hint. A stored Dell preference beats a policy hint that names another
PC. `note_discontinuity` (and a failed link) resets the window. The action is
`set_preferred_main`, never `make_main_now` or `recover`.

`step` advances one stage: request, or readiness+drain, or fence+grant. Fence
waits are returned as `not_quiet` or `lease_live` with the store's `wait`
unchanged. Grant is returned only when `ready` is exactly `False`
(`canary_required`). `step` does not call `complete`. `submit_canary` accepts
only a real bool; `1` and `"yes"` never reach the store. `False` or a lapsed
grant leaves no MAIN.

The hub must pass `step()["lease"]` to that candidate's `accept_lease` before
dropping the orchestrator. The grant is kept in RAM for a retry of the same
object. It is not durable.

Auto client keys are `auto-{action}-{candidate}-{incarnation}-{epoch}-{profile}`
and are reused for that tuple. `open_manual` uses the caller's key so a retry
is the same transition and a different action is `key_reused`.

## 4. Authority port

`StoreAdapter` maps these names onto `AuthorityStore`. A gateway client should
implement the same names after it has authenticated. Identity arguments on
`acknowledge` exist because the in-process adapter has no authenticated
channel; the gateway must derive them from the verified installation and
reject a mismatch.

| Port | Store | Gateway facade |
| --- | --- | --- |
| `status()` | `status()` | `status(authenticated_device)` |
| `renew(lease_tuple)` | `renew(holder, lease_id, incarnation, epoch)` | `renew(device, lease_tuple)` |
| `release(lease_tuple)` | `release(...)` then `status()` | `release` returns status; the store method returns `None` |
| `acknowledge(device, installation, mode_version, fenced_epoch, boot_id=None)` | `acknowledge` | `acknowledge(device, mode_version, fenced_epoch)` |
| `transition_request(request)` | `request(...)` | `transition_request(owner_proof, request)` — proof stays in the gateway |
| `transition_readiness(id, {control_ready, ai_ready, checkpoint})` | `readiness` | `transition_readiness` |
| `transition_step(id, "drain"\|"fence"\|"grant"\|"complete"\|"cancel", canary_ok=None)` | those methods, uncollapsed | `transition_step` |
| `consume(ticket)` / `admission(id)` | `consume` / `admission` | same; executor owns receipts, not this runtime |

`GatewayPort` wraps a client with `AuthorityClient`'s names:
`status()`, `renew(holder, lease_id, incarnation, epoch)`,
`release(...)` returning status, `acknowledge(mode_version, fenced_epoch, boot_id=None)`,
`readiness(transition_id, control_ready, ai_ready, checkpoint=None)`,
`step(transition_id, step, canary_ok=None)`,
`transition_request(client_key, action, candidate, expected_incarnation, expected_epoch, expected_policy, profile)`,
`consume(ticket, ticket_signature)`, `admission(admission_id)`.
`caller` is not sent. A null checkpoint is omitted. Renew and acknowledge
run only when `client.device_id` and, for acknowledge, `client.installation_id`
match the arguments. Consume requires an SSH signature block and refuses the
executor's unsigned keyword form. Transport codes (`unavailable`,
`authority_mismatch`, `no_relay`, and the other link codes) become
`LinkError("partition")`. Other stable client codes become `AuthorityError`
and keep a finite `wait` only when the client object still has one.

`lease_tuple` keys: `holder`, `lease_id`, `incarnation`, `epoch`.
`request` keys: `caller`, `client_key`, `action`, `candidate`,
`expected_incarnation`, `expected_epoch`, `expected_policy`, `profile`.
`caller` is local metadata for `StoreAdapter`. `GatewayPort` drops it.

`LinkError(code="partition")` is transport failure. `AuthorityError` codes are
passed through. `RuntimeRefusal.code` is the runtime's own refusal. Neither
exception carries a body or a path.

### Result keys

`RuntimeState`: `phase`, `mode`, `mode_version`, `incarnation`, `epoch`, `main`,
`preferred`, `device_id`, `installation_id`, `admission_open`, `cache_remaining`,
`stale`, `reason`, `authority_id`, `policy_version`, `transition_id`. No
`lease_id`.

`LeaseView` on success: `code`, `holder`, `lease_id`, `incarnation`, `epoch`,
`valid_for`, `ready`, `remaining`, `cached`, `renew_after`, `wait`. On failure:
`code` and `wait` only.

`AuthorityContext`: `effect_class`, `target`, `holder`, `incarnation`, `epoch`,
`mode`, `mode_version`, `authority_id`, `policy_version`, `ready`, `online`,
`consume_required`.

`Step`: `ok`, `phase`, `code`, `wait`, `reason`, `stable_for`, `transition_id`,
`transition_state`, `action`, `candidate`, `lease`. `lease` is set only for
`canary_required` and includes `ready: false`.

`admission_decision(receipt)` returns `execute` or `reconcile`. `already: true`
is reconcile, including after a later handover. Unknown keys and banned keys
raise `bad_request` instead of being stripped. This does not start an effect.

## 5. Metadata

Banned keys, refused on the way in: `body`, `prompt`, `content`, `transcript`,
`secret`, `cookie`, `password`, `passkey`, `filename`, `frame`, `screenshot`.
Identifiers match the store rule: `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`.
The runtime does not log. Audit text belongs in the gateway and must stay on
this allow-list.

## 6. Incompatibilities for the other owners

1. **`status` has no `lease_id`.** Renew and release need the grant response.
   Do not invent one from `lease_remaining`. Do not persist it on gateway disk.
2. **Do not collapse `grant` and `complete`.** A not-ready grant admits nothing.
   `step` will not complete a canary.
3. **`begin_drain` is local.** The owner identity calls `transition_step(..., "drain")`.
   If the PC runtime called store `drain`, it would skip `manage_main`.
4. **`release` on the store returns `None`.** The adapter returns `status()`.
5. **Gateway auth is mandatory and absent here.** `StoreAdapter` trusts its caller.
6. **`integration` owns `set_mode`.** This runtime will not quiesce legacy or
   activate v2. In `LEGACY_ACTIVE`, `PREPARING_V2`, `QUIESCED`, and
   `ROLLBACK_QUIESCED` it refuses with `legacy`. A later `start()` after the
   store has left legacy will follow. It never writes the saved cluster role.
7. **`require_authority` is not `consume`.** The executor still has to persist
   its fence and call `consume` under the returned tuple. A context is not a
   ticket. Replay is `admission_decision`, then the executor reports the old
   receipt and does not run the effect again.
8. **Cache does not cover a partition.** Offline TTL admission is not
   implemented. The contract's "no new ordinary operation when Oracle is
   unreachable" wins over acting for the rest of a cached lease.
9. **Windows `remaining()` is always 0.** That is not a bug to paper over with
   `CLOCK_MONOTONIC`. Online status is the per-action check.
10. **No participant list exists on the store.** The hub passes
    `recover_candidates`. This driver will not scan or pick a device that was
    not named. An empty list and no preferred yields `NO_ELIGIBLE_MAIN`.
11. **Package docstring** in `jarvis/continuity/__init__.py` still says the
    prototype must not be described as failover. That remains true.
12. **`sysview` stations** do not see this subpackage. Do not import it from a
    top-level `jarvis/*.py` until a station owns it.

## 7. Tests

`.venv/bin/python -m unittest tests.continuity.test_runtime`

30 tests, real SQLite authority, fake monotonic/boottime clock. They do not
open a socket, rewrite config, or call a model. The gateway-port tests use a
local client with the authority client's method names, not a live gateway.

| Test | What it locks |
| --- | --- |
| `test_legacy_is_the_default_and_does_not_renew_or_promote` | Fresh store: no renew, no transition |
| `test_start_again_follows_a_later_cutover` | A long-lived PC follows v2 only after the store leaves legacy; context is not a ticket |
| `test_renew_anchors_at_request_start` | A 4s slow response caches ~11s, not a fresh 15s |
| `test_partition_stops_admission_and_does_not_use_the_cache` | Warm cache cannot admit or recover while the link is down; restore does not create a second MAIN |
| `test_windows_validates_online_and_does_not_cache` | No cache; expired online status admits nothing and renew does not revive it |
| `test_reboot_voids_the_cache_until_a_new_renew` | New boot id drops the cache; a new renew may re-anchor if this PC is still MAIN |
| `test_explicit_stop_is_sticky` | Operator hold blocks renew for this process |
| `test_provider_name_is_not_ai_readiness` | Model name and a session path are not AI-ready; real booleans are |
| `test_personal_readiness_text_is_refused_before_the_store` | Prompt and free-text checkpoint never reach the db |
| `test_resume_drops_cache_and_release_does_not_reclaim` | Resume invalidates; release then resume does not become MAIN again |
| `test_planned_release_then_manual_fence_without_skipping_the_canary` | Acks fence without waiting out the lease; grant stays not-ready until a bool canary; no `make_main_now` |
| `test_crash_sleep_waits_out_validity_and_refuses_the_old_tuple` | No promotion while the lease is valid; half the fence wait is not enough; old renew, admit, and consume stay `stale` |
| `test_canary_must_be_a_real_bool_and_failure_leaves_no_main` | `1` does not complete; `False` leaves `V2_ONLY_NO_MAIN` |
| `test_cancel_before_fence_lets_the_old_main_admit_again` | Local drain without an ack; cancel keeps the old MAIN |
| `test_release_during_a_transition_does_not_ack_or_extend` | `busy` does not ack; the lease still lapses |
| `test_recover_while_the_lease_is_valid_is_refused` | `main_alive`, no row opened |
| `test_auto_recover_is_off_until_asked_and_prefers_the_ready_preferred` | Default `manual`; ready preferred wins; a not-ready preferred is skipped for the successor with no extra 30s |
| `test_ai_profile_is_not_silently_downgraded` | Control-only candidate does not auto-open an `ai` recover; an explicit `profile="control"` is a separate owner request |
| `test_failback_waits_and_follows_the_stored_preference` | 29s does not open; 30s opens `set_preferred_main`; a stored Dell preference beats a policy hint naming the other PC |
| `test_failback_resets_when_readiness_blinks_or_auto_is_off` | Auto off never returns; a blink or `note_discontinuity` restarts the 30s |
| `test_oracle_cannot_be_nominated_and_a_lie_is_still_ineligible` | `kind=server` is skipped; a directory that says `pc` still gets the store's `ineligible` and no row |
| `test_stale_directory_and_personal_facts_do_not_promote` | Stale `observed_at`, banned prompt, and a diary checkpoint do not open a transition or land in the db |
| `test_restart_barrier_is_not_shortened` | New incarnation: old renew is `stale`; no request until `grant_barrier` elapses; the following grant is not ready |
| `test_replay_reconciles_and_conflict_is_not_a_second_effect` | Same ticket reconciles; a different digest conflicts; the body is not in the db |
| `test_request_keys_stay_idempotent` | Same key returns the same id; a different action is `key_reused` |
| `test_policy_rejects_instant_failback_and_sloppy_flags` | Stability under 30 and a string auto flag are refused; `open_manual` has no `force` |
| `test_module_does_not_name_a_config_writer_or_a_provider_client` | Source has no cluster-role writer, provider client, or hardcoded machine id; `GatewayPort` has no `set_mode`, `enroll`, or `relay` |
| `test_gateway_port_drives_a_real_fence_and_does_not_set_mode` | Recover through the client shape still drains, fences, and grants separately; `1` does not complete; a failed canary leaves no MAIN; the owner caller is not on the wire |
| `test_gateway_port_binds_the_client_and_requires_a_signature` | A client bound to another device cannot ack or submit that candidate's readiness; unsigned consume, a non-bool flag, and a free-text checkpoint never reach the client |
| `test_gateway_port_fail_closed_on_transport_and_auth` | A signed consume reconciles; a foreign holder is refused; `unavailable` stops admission; `unauthorized` is not turned into a context; a boolean wait is dropped and a real wait is kept |

The existing `tests.continuity.test_authority` sleep case is still the store's
own proof. These tests drive that store through the runtime instead of a fake
that always returns a MAIN.

## 8. Limits (do not claim past these)

- Not live failover, not a stable phone origin, not "Dell becomes MAIN when
  Arch sleeps" on the real machines. Oracle was not contacted. Dell's Luna/Grok
  session was not checked.
- The hub must renew about every 5 seconds. This object does not schedule.
- The hub must serialize calls on one `MainRuntime`. There is no internal lock.
- `SAFETY_MARGIN` is still the unmeasured 2s prototype. Waits are not shortened
  and not calibrated.
- A PC process restart loses `lease_id` and cannot renew that grant.
- Losing the orchestrator after `grant` and before `accept_lease` strands the
  lease id until expiry. Authority restart voids it anyway.
- Application fencing does not stop an old checkout or an unsandboxed desktop
  process. Executor receipts are not in this slice.
- Manual `open_manual(..., profile="control")` while policy is `ai` is an
  explicit owner request. The UI has to say the profile is reduced. The
  automatic path will not do that by itself.
- Gateway authentication, passkeys, signed tickets, and phone cookies are not
  implemented. A canary is a bool from the hub, not a probed device.
- No service was restarted and no config, vault, or live state was written.

## 9. Landed peers (do not treat as wired)

Checked against the modules on disk. This runtime was not changed to match
their private shapes, and their files were not edited from this slice.

| Peer | What it actually does | Why this runtime does not copy it |
| --- | --- | --- |
| `integration.PcRuntime` | `require_authority` returns `{ok, code, acted, source}` and can return `ok` from the cache while `remaining() > 0` | The contract facade raises `RuntimeRefusal` or returns an `AuthorityContext` with `consume_required` and no `ok` key. A warm cache is not admission while status cannot be read. |
| `integration.MobileIngress.submit` | Calls `runtime.require_authority(...)` and branches on `decision["ok"]` | That key is not on `AuthorityContext`. Wiring this `MainRuntime` in without a translator raises `KeyError` or `RuntimeRefusal`. Do not add `ok: false` to paper over it. |
| `integration.LoopbackGateway` | `renew(identity, lease)`, `acknowledge(identity, mode_version, fenced_epoch)`. `_pc` runs `reject_personal` on the identity. | `_identity` stores `secret`. `secret` is in that module's personal-key set, so `cutover` → `acknowledge` raises `personal_field` before any store write. `IsolatedFleet.boot_zenbook` stops there. |
| `integration.INCOMPATIBILITIES` | Still says `gateway.py`, `ingress.py`, `runtime.py`, and `executor.py` are absent | Those files exist. `peer_report()` now marks them present when the class and methods import. `tests/continuity/test_operational_e2e.py` still expects `present` false, and expects a blocker id `peer_gateway`. `_blockers` emits `peer_<name>` only when a facade is incompatible, plus `gateway_host_unverified`. That test file is red. The continuity package is not green. |
| `ContinuityGateway` | `status(principal, payload)`, `renew(principal, payload)`, `transition_step(owner_proof, payload)`, `consume(principal, payload)` with `ticket_signature`. One-shot proofs. | `GatewayPort` does not wrap this object. Wrapping it would need a fresh principal on every call. There is no signature bypass. |
| `AuthorityClient` | HTTP client. `step` is one of drain, fence, grant, complete, cancel. `_send` raises `GatewayError` from `{"error": code, "wait": seconds}` and keeps `wait` only when that number and `Retry-After` (`ceil(wait)` delay-seconds) agree. | A header alone, a body field alone, a boolean, zero, or a mismatch is dropped. `GatewayPort` does not invent a fence wait. A missing wait is not "the barrier elapsed". |
| `ingress.MobileIngress` | Requires `status()`, `relay_metadata(...)`, and `transition_request(proof, payload)` | `ContinuityGateway` has `relay(principal, payload)` and `status(principal, payload)`. No `relay_metadata`. Not connected to this runtime. |
| `ExecutorGuard` | `acknowledge(device_id, installation_id, mode_version, fenced_epoch, boot_id)` and `consume(**ticket fields)` with no signature | That matches `AuthorityStore` / `StoreAdapter`. `GatewayPort.consume` refuses the unsigned keyword form. An executor pointed at the port fails closed until it can pass `ticket_signature`. |
| Readiness owner | The hub driver submits the candidate's readiness through the port | The gateway accepts readiness only from the candidate's installation. A client bound to another device gets `forbidden`, and the driver stops before `drain`. It does not resubmit as the owner. |

`IsolatedFleet` is a temp loopback with shared-secret doubles. It is not a
provisioned Oracle gateway, not a Dell Luna/Grok check, and not failover.
`jarvis/continuity/__init__.py` still says this package must not be described
as failover. That remains the right description.
