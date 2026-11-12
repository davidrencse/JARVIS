# Sonnet integration handoff

Parent delegates the implementation. This session did not edit continuity code, did not run the suite again, and did not activate anything. The tree below is the code on disk when this note was written.

Baseline log: `/tmp/jarvis-continuity-resumed.log`

```
Ran 164 tests in 13.697s
FAILED (failures=2, errors=6)
```

All eight names are in `tests/continuity/test_operational_e2e.py`. The older full-suite log `/tmp/jarvis-final-five-hole-integration.log` is superseded. Do not chase failures that are not in the 164-test log.

Python is `.venv/bin/python`. No commit, no push, no CI, no live config or vault write, no `rest.sleep`, no `pause()`, no phone or hub process, no v2 activation, no further agents.

## Changed files (this session)

Only this document. No `search_replace` or code write landed.

Files Sonnet is expected to change, and no others unless a test that already imports a deleted symbol forces a same-change update:

- `jarvis/continuity/gateway.py` — add `forward_transient`. Do not change `relay()` or `AuthorityClient._send` wait parsing.
- `jarvis/continuity/ingress.py` — optional transient branch, `/phone/api/me` pass-through. `GATEWAY_METHODS` stays the exact 3-tuple.
- `jarvis/continuity/executor.py` — signature length cap `512` → `1800` at the check around line 751. Nothing else.
- `jarvis/continuity/integration.py` — delete the doubles, compose the real modules, default-off helpers.
- `jarvis/continuity/__init__.py` — one honesty sentence only if a launch file imports the package. Do not export `ContinuityGateway`. `tests/continuity/test_gateway.py` line 126 asserts `hasattr(package, "ContinuityGateway")` is false.
- `jarvis/cluster_cli.py`, `jarvis/cluster.py`, `jarvis/remote.py`, `jarvis/rest.py`, `jarvis/phone.py` — lazy default-off hooks only.
- `tests/continuity/test_operational_e2e.py` — rewrite onto the real API. Do not weaken fences to go green.
- `tests/continuity/test_gateway.py` — drop `LoopbackGateway` and the `peers_absent` assertion in the same change that deletes them.
- `docs/design/mobile-continuity/OPERATIONAL-INTEGRATION.md` — new activation contract. Every gate still false. No live-failover claim.

Do not edit `jarvis/continuity/authority.py`, `jarvis/continuity/clock.py`, `docs/design/mobile-continuity/OPERATIONAL-CONTRACT.md`, or `jarvis/fleet_management.py` (`main_transfer` stays 409). Historical design docs that mention `LoopbackGateway` stay as history. Point at `OPERATIONAL-INTEGRATION.md` from the new integration docstring. Do not copy SSH host keys, IPs, or `identity_file` into docs or code. `ssh-keygen` only inside a temp test directory.

`jarvis/continuity/*.py` does not import `integration`. `cluster.py`, `phone.py`, `rest.py`, `remote.py`, and `cluster_cli.py` have no continuity import yet. Keep it that way at module level: lazy import inside the hooked function. `integration.py` must not import `cluster`, `phone`, `rest`, or `remote` at import time.

## Unresolved failures

Six errors, one traceback. `LoopbackGateway.acknowledge` → `_pc` → `reject_personal(identity)` → `IntegrationError("personal_field")` because the identity dict key is `secret` and `PERSONAL` includes `secret`. First site is `integration.py` around lines 371, 419, 969, 1016, 175.

- `test_arch_sleep_promotes_dell_and_phone_session_survives`
- `test_auth_gaps_do_not_open_a_transition_or_extend_a_lease`
- `test_canary_failure_ai_profile_and_rollback_leave_one_or_none`
- `test_dell_validates_online_and_safety_margin_is_the_placeholder`
- `test_personal_fields_never_reach_the_store`
- `test_reboot_partition_and_restart_do_not_replay`

Two failures:

- `test_default_mode_refuses_cutover` expects `peer_gateway` in blockers. Actual set: `luna_grok_unverified`, `safety_margin_unmeasured`, `automatic_failover_disabled`, `signature_verifier_absent`, `windows_clock_untested`, `gateway_host_unverified`, `passkey_verifier_absent`, `phone_origin_arch_loopback`, `hub_loopback`, `legacy_paths_unfenced`, `dell_provider_unverified`. `peer_*` is absent because the modules exist and the name check passes. Do not delete the peer modules. Assert those real gates. `signature_verifier_absent` and `passkey_verifier_absent` stay: a temp fleet verifier does not clear the production blocker. Live CLI, hub, phone, `Remote.call`, and `NodeWorker` still have no verifier.
- `test_peers_missing_and_repo_has_no_oracle_gateway` expects `peers[name]["present"]` false. Actual is true. Presence is not compatibility. `peer_report` must probe behavior. Oracle device stays absent (`gateway_host_unverified`, `deployment_facts()["oracle_device"]` is false). `zenbook` and `dell` are in `device_ids`. `phone_bind` is `127.0.0.1:7787`.

`test_store_codes_fail_closed` passed. Keep fail-closed. Its allowed HTTP tuple is `(400, 401, 403, 409, 410, 503)` and does **not** include 404. If `not_found` moves to 404, add 404 to that tuple in the same change.

`test_second_writer_and_live_state_are_refused` does not call `boot_zenbook`. It must keep passing: a second `AuthorityStore` on the fleet path is `writer_busy`, and `IsolatedFleet(repo/.jarvis)` raises `refuses_live_state` before bind and before mkdir, even if `.jarvis` does not exist.

## Current step

Contract read is done. Implementation has not started. `integration.py` and `test_operational_e2e.py` are still the shared-secret doubles. `LoopbackGateway` begins at `integration.py:332`, `LocalExecutor` at 523, `PcRuntime` at 643, `IsolatedFleet` at 906. `boot_zenbook` is at 1015.

`peer_report` (`integration.py:184`) checks class and method names only. `INCOMPATIBILITIES` still contains `peers_absent` and `inprocess_auth_double`. `EXTRA_HTTP["not_found"]` is 409 and `EXTRA_HTTP["unknown_participant"]` is 409. The gateway map is 404 and 403 (`gateway.py` `HTTP_STATUS`). `test_gateway.py:191` comments that integration maps `not_found` to 409. `test_gateway.py:195-233` imports `LoopbackGateway`, asserts `peers_absent`, and asserts the stand-in acknowledge signature `(self, identity, mode_version, fenced_epoch)`.

Ingress unit tests that use a fake `Gateway` with no `relay_transient` assert `body_not_relayable` and must stay green. The real `ContinuityGatewaySession` path must forward. `tests/continuity/test_ingress.py` `GATEWAY_METHODS` assertion is the exact tuple `("status", "relay_metadata", "transition_request")` (around line 898). Uncertain-confirm expects code `unavailable` (around line 852). Do not rename a real gateway `unavailable` to `authority_unavailable`. `unauthorized` on the ingress gateway mapping is remapped to `authority_unavailable` so a cookie is not blamed. Leave that.

## API contracts to implement

### Gateway `forward_transient(principal, metadata, envelope)`

In-process. Same device principal purpose `"relay"` as metadata `relay()`. The signed payload is the metadata dict only. The envelope is bytes beside it. Do not put the envelope in `canonical()`, sqlite, audit, or the stored relay result. `canonical()` refuses payloads larger than `MAX_BODY` (8192), so a frame cannot ride inside the signed JSON.

Metadata keys, exact: `operation_id`, `target`, `attempt_id`, `digest`, `capability`, `kind`. Extra keys, including every `_FORBIDDEN_KEYS` entry, are `bad_request` via `_exact`.

- `capability` must be `"client"`.
- `kind` is one of ingress `EFFECT_KINDS` (`ask`, `session_start`, `session_input`, `session_stop`, `logout_end`) or `READ_KINDS` (`overview`, `fleet`, `ask_poll`, `session_frame`, `effect_status`). Duplicate those frozensets in `gateway.py`. Do not import ingress.
- `envelope` is `bytes`, length 1 through 256 KiB. Digest must equal `"sha256:" + sha256(envelope).hexdigest()`. Ingress `_sha256` hashes `str(text)`, not raw bytes. Do not use it for this digest.
- `target` must equal the current ready MAIN from `_relay_status` (mode `V2_ACTIVE`, `main_ready`, `lease_valid`, `main`). Otherwise `no_relay`.
- Enrolled `relay_origin` is `pin_relay` (loopback HTTP only). Missing origin, or origin equal to this gateway's listening origin, is `no_relay`.
- Validate digest, kind, and target **before** inserting the idempotency row.
- Effects use a **new** RAM table, not `_relays`. Identity is `(digest, target, attempt_id, capability, kind)`. Same identity returns the stored metadata result and does not post again and does not need the envelope. Different identity is `conflict`. State `error` raises `no_relay` and is not resent. Cap at `MAX_RELAYS`.
- Effect upstream JSON must be exactly `{"operation_id", "accepted": bool}`. Any other key (`reply`, `path`, `text`, `url`, `effect_authorized`, or anything else) is overclaim: `no_relay`, state `error`, body discarded, not stored.
- Client result for an effect is exactly `{"operation_id", "relayed": True, "effect_authorized": False, "upstream_accepted": bool}`. `effect_authorized` is always false.
- Reads are not effects. Do not store the answer. One gateway attempt per call. No automatic retry. A repeated read id with the same identity must not resend and must not replay `answer_b64`. A different identity conflicts. The first return may include `answer_b64` for that caller only.
- Read upstream may be `operation_id`, `accepted`, and optional `answer_b64`. Decoded answer length at most `2_500_000`. Base64 inflates about 4/3, so the POST response limit is about 4 MiB. Effects keep `MAX_RELAY_RESPONSE` (1024).
- Build the upstream body with `json.dumps`, not `canonical`. POST to `relay_origin + RELAY_PATH` (`/v1/continuity/relay`). `_post_json` does not log. Reuse it with the larger read limit.
- Audit, if any, is method, route, status, code, and `device_id` only (`_remember`). Never the envelope. `forward_transient` does not go through `_read_json`.
- Do not change `relay()`: five metadata fields, RAM `_relays`, result always `effect_authorized` false, prompt key is `bad_request`.

`ContinuityGatewaySession.relay_transient(...)` signs that metadata dict with purpose `"relay"`, then calls `forward_transient(principal, payload, envelope)`. Extend `_device` with an optional `after` callback. Do not put the envelope in the signed payload. Fake gateway tests never call this method.

`relay()` and `forward_transient` share `RELAY_PATH`. The temp MAIN sink must accept both shapes: no `envelope_b64` means metadata-only, return `{operation_id, accepted}` and do not run a sink; with `envelope_b64`, verify the digest, drop the body after the handler, and for effects call the guard only after `MainRuntime.require_authority`.

### Ingress optional forward

`MobileIngress.__init__` still requires only `GATEWAY_METHODS`. If `relay_transient` is missing, Ask and reads stay `body_not_relayable`, are not inserted, and are not relayed. That keeps `tests/continuity/test_ingress.py` phone-effect, overclaim, and contract tests green.

If `relay_transient` is callable:

- `submit` of `EFFECT_KINDS` validates with the existing `_effect_body`, then forwards. Ask envelope is `json.dumps({"text": text})` only. Digest is sha256 of those bytes. Store the digest and ids, never the text.
- Same operation id and digest returns the stored outcome and does not send again. Different digest is 409 `conflict`. Lost or overclaim outcome is `uncertain` and is not resent. `effect_authorized` true, or `path` / `url` / `reply` / a non-dict, is `relay_overclaim`.
- `read` of `READ_KINDS` forwards one new operation id per call. Overview and fleet answers are returned to the caller as `body`. A frame answer is returned as raw bytes for `PhoneProtocolAdapter._frame`. Do not write the answer to sqlite or the log hook.
- `status()` reports `relay` true only when `relay_transient` is callable **and** `main_ready`. `metadata_only` is false when the method exists. The authority view is the dict `_authority_view` already returns (`main_ready` is on that dict, not under `["authority"]`). Fake gateways lack the method, so unit tests that expect `metadata_only` true keep passing.
- `PhoneProtocolAdapter` GET `/phone/api/me` (around lines 1557–1563) hardcodes `"relay": False` and `"metadata_only": True`. Pass through `view["relay"]` and `view["metadata_only"]`. Changing only `MobileIngress.status` leaves the phone HTTP path metadata-only.
- Leave `MobileIngress._relay` raising `body_not_relayable`. The new path is separate.
- `logout()` stays a local revoke. Do not invent a passkey success. `passkey_verifier is None` means `manage_main_confirm` raises `passkey_unavailable` and opens no transition. A phone without `manage_main` gets `forbidden` (403), not the double's `not_manage_main`.

Real-gateway ingress test (`test_ingress.py` `RealGateway`, around 949) must keep: prompt smuggled into `relay()` is `bad_request`, `SECRET` absent from `authority.sqlite`, forged `OwnerProof` is `unauthorized`. Add a transient ask proof beside that. Do not put the prompt in the five-field metadata payload.

### Executor cap

```python
if not isinstance(signature, str) or not 1 <= len(signature) <= 1800:
```

Gateway `_signature` allows `MAX_SIGNATURE` 1800 and requires `BEGIN`/`END SSH SIGNATURE`. `GatewayPort.consume` requires that armor and a length up to 4096, and raises `RuntimeRefusal("bad_request")` otherwise. **Do not copy the armor check into `ExecutorGuard`.** Executor unit tests use short fake signatures (`lambda signature == SIGN`). No test asserts 512. A 10000-character signature must still fail. Verifier rejection stays `AuthorityError("signature")`, not `unauthorized`.

`ExecutorGuard.accept` calls `authority.consume(**ticket fields)` with **no** `ticket_signature`. The numbers are already `_num` floats (`executor.py` around 537–541). `_lookup_admission` re-raises `AuthorityError`; the store's `admission()` returns `None`, the gateway raises `not_found`. The adapter must map `not_found` to `None`.

`_require_v2` order: quarantined, then `legacy` if the journal is not fenced, then `no_authority`, then `no_verifier`. A fresh guard with `verifier=None` raises `legacy`, not `no_verifier`. `no_verifier` happens only after `fence()` has closed legacy, and it happens before the sink and before consume. The code is `AuthorityError("no_verifier")`, not `IntegrationError("signature_unverified")`. The old e2e that constructs `LocalExecutor` must move to `ExecutorGuard`.

`_reject_local_stale` is `tup < high` OR `tup <= fenced`. Fencing epoch−1 does **not** make the working-epoch ticket locally stale. An online old ticket after promotion is store-stale (sink stays 0) even when it is not locally stale. Offline local stale: while the link is up, fence the guard at the epoch being left (requested epoch ≤ issued epoch, so not `ahead`), then drop the link, then `accept`. The local check runs before consume.

Do not ack the sleeping zenbook during the lease-wait fence. Ack only awake dell and oracle at the epoch being left. After lapse the wait is `validity_end + margin - now` on the exception. Use that `wait`. Do not assume `SAFETY_MARGIN` if the tick landed past expiry. Tick exactly the client wait. A second fence before that tick still fails. `AuthorityClient._send` keeps `wait` only when the JSON `wait` and `Retry-After` ceil(wait) agree. A missing wait is not "the barrier elapsed".

### Signed adapter (integration only)

`GatewayPort` is the `MainRuntime` port. Do not use it as the executor's consume path: it refuses signatures that lack SSH armor, and the guard does not pass a signature.

`AuthenticatedPort.consume(ticket=None, **fields)` requires a string `ticket_signature`, checks the target is this device, and does not require armor. `GatewayError` becomes `AuthorityError(code, code, wait=exc.wait)`.

`SignedAuthority` wraps one `AuthorityClient` through `AuthenticatedPort`:

- `verify(signed)` is the guard verifier. It checks `verify_message(TICKET_NS, _message(TICKET_NS, ticket fields), signature, keys[signed["main"]])`. On true, store the signature and the **original** ticket field dict (ints). Return true only after a real verify. The constant `SIGN` double is not a production verifier.
- `consume(**fields)` refuses when no signature was captured, or when a numeric field differs from the captured ints by more than `1e-6` (same tolerance as the guard). Otherwise `AuthenticatedPort.consume(stored_ints, ticket_signature=signature)`. Do not sign the float form. Do not drop the signature.
- `admission`: `AuthorityError("not_found")` → `None`. Other codes re-raise.
- `acknowledge(device, installation, mode_version, fenced_epoch, boot_id)` refuses when the ids are not the bound client, then calls `AuthorityClient.acknowledge(mode_version, fenced_epoch, boot_id)`. Do not drop `mode_version`. An unissued epoch is `ahead` and must not move the watermark.
- `status` raises `AuthorityError("unavailable")` when the client is down. Do not return `{ok: true}`.

A `Reachable` wrapper around the client: non-callables (`device_id`, `installation_id`, `origin`) pass through; callables raise `GatewayError("unavailable")` when the link is down. Count renew calls. Do not kill the gateway thread to simulate partition.

Completed local receipt: `_verify` still runs (local public key), consume is not called, sink is not called. `failpoint="after_accept"` raises `AuthorityError("crash")` before the sink. Replay of that receipt is `uncertain` and does not rerun. `UNCERTAIN` is not restarted.

### Effect map

`MainRuntime.require_authority` runs before a ticket is minted. A sleeping Arch raises `RuntimeRefusal("asleep")` and no ticket is minted. Sink stays 0. Return accepted false. That is a clean refusal, not a retry.

- `control` → executor `ordinary`. `control` plus pure is `bad_request`.
- `ordinary` → `ordinary`.
- `ai` when not `ai_ready` refuses. Runtime reason is `profile` (control true, ai false). Do not downgrade to `ordinary`. Do not invent `ai_not_ready`.
- `ai` ready without an explicit pure flag refuses. Do not downgrade.
- `ai` ready and pure → `pure_compute`.
- `interactive` stays `interactive`. It is not a synonym for control or ai.

Phone ask is control/ordinary. The AI refusal is a separate fleet call through this map.

Runtime lanes that must all be true for `ai_ready`: `luna`, `grok`, `quota_ok`, `mediated_tools`, `session_local`. A provider name is not `ai_ready`. Probe facts are the only source.

### Runtime facts the e2e must match

`require_authority` returns an authority context with `consume_required` and no `ok` key, or raises `RuntimeRefusal`. Do not treat `{ok: true}` as success.

`clock=None` (Dell): `CachedAuthority` does not cache. `require_authority` calls `status()` every time and does **not** call `renew`. Do not assert `renew_calls + 2`. Assert `SAFETY_MARGIN == 2.0`, remaining 0, renew count unchanged by `require_authority`.

Arch with a warm cache: a second `require_authority` does not increase renew calls.

`stop_new_admission(reason)` is sticky. It invalidates the cache, so remaining becomes 0 and `require_authority` raises that reason (`"asleep"` is a valid ident). `invalidate_after_resume` does not clear the hold. A real wake is a **new** `MainRuntime` (new boot id). `accept_lease(old)` on that runtime raises `RuntimeRefusal("stale")`.

`begin_drain` does not call `store.drain`. `renew_once` returns `{code: ok|partition|stale|...}` and does not raise `AuthorityError`. `LinkError` / transport `unavailable` becomes `RuntimeRefusal("partition")` through `GatewayPort`.

`GatewayPort` has no `set_mode`, `enroll`, or `relay`. Do not add `relay` to it. Runtime tests scan the source.

Grant return (`authority.py` grant): `holder`, `lease_id`, `incarnation`, `epoch`, `valid_for`, `ready` false. Status `main` is the holder only when `lease_valid`. `main_ready` is valid AND `lease_ready`. `grant_barrier` is seconds until the barrier, or `None` if inf.

First recover from `V2_ONLY_NO_MAIN`: epoch starts at 0, cutover acks `fenced_epoch` 0, fence increments epoch to 1. Guard fences epoch−1 (0) so legacy closes and the store `fenced_epoch` stays below the working epoch. Accept before `complete(canary=True)` is `no_admission`, sink 0. Then complete, `accept_lease`, `renew_once`, ordinary accept runs the sink once, second accept is `already` and the sink stays 1.

Store steps are `drain`, `fence`, `grant`, `complete`, `cancel`. `catching_up` and `verifying` are `bad_request` before the store call. There are no `CATCHING_UP` or `VERIFYING` states.

`recover` while `lease_remaining > 0` is `main_alive` and opens no row. `make_main_now` / handover while the lease is valid is the other action. Oracle `kind=server` as a candidate is `ineligible`.

`MODE_EDGES`: `V2_ACTIVE` and `HANDOVER` are not keys, so `set_mode("ROLLBACK_QUIESCED")` from either is `bad_edge`. `V2_ONLY_NO_MAIN` → `ROLLBACK_QUIESCED` is a legal edge and then may raise `not_quiet` or `unacked`. It must **not** be asserted as `bad_edge`. A successful temp-store rollback is not production legacy restored. `ContinuityIntegration.rollback_quiesced` stays `applied` false and `mode()` stays `LEGACY_ACTIVE`.

Restart: `gateway.close()`, `ContinuityGateway.open` on the same path, `serve()` (new port), re-enroll the same public keys (the device directory is RAM), assign the new origin onto existing `AuthorityClient` objects (`origin` is a public attribute), rebind `ContinuityGatewaySession._gateway`. Do not open a second store while the first holds the writer lock. Status `main` is `None`, `grant_barrier > 0`, incarnation increased. Phone sqlite is separate. The cookie still authenticates. Submit is `no_main`, not the double's `no_admission`. A **new** operation id with the old `issuer_incarnation` is `stale`. Reusing the consumed id returns `already=True` before the incarnation check. Do not use that as the stale proof.

Exact retry of the same ticket, including after handover and after restart when every ticket field including `issuer_incarnation` matches, returns `already=True` before the incarnation check. Expired tickets are refused before lookup. `consume` creates the admission. There is no separate issue API.

`IsolatedFleet(repo/.jarvis)` compares resolved paths and raises `refuses_live_state` before mkdir and before bind. `__init__` must close partial resources if a later step raises. The second-writer test needs the fleet store already open, so `__init__` opens the gateway store. `close()` is idempotent and closes the non-daemon gateway thread and the relay threads.

Production `ContinuityIntegration` is a separate object. It never opens an authority database. `activate_v2` / `prepare_v2` / `quiesce_legacy` / `rollback_quiesced` return `applied` false. Promoting Dell in the temp fleet must not flip `ContinuityIntegration.mode()`. Legacy dispatch and v2 dispatch are never both true. `legacy_request` still `reject_personal` and does not call cluster. `v2_request` stays inactive.

Fleet clocks are one `tests.continuity._fleet.Clock`. Inject it as gateway monotonic and wall, `AuthorityClient.wall`, Arch `MainRuntime` clock (`clock.mono`), Dell clock `None`, `ExecutorGuard` wall, and ingress clock. `issued_at = int(clock.wall())`. Ticket lifetime `+3600` so sleep ticks do not expire it. `SKEW_SECONDS` is 30, so int wall is fine. Jarivs code must not import `tests`.

Keys: `ssh-keygen -q -t ed25519 -N "" -C jarvis-continuity-test -f <temp>`. Same pattern as `test_gateway._keys`. Enroll through an owner proof. PC caps: `status`, `lease`, `fence`, `candidate`, `consume`, `reconcile`. Server: `status`, `fence`, `reconcile`. Client (ingress session): `status`, `client`. Never `manage_main`.

Phone pair follows the ingress test, not `pairing_code` on the double: `issue_code(owner)` → POST `/phone/pair` → `decide_pairing(owner, code_id, True)` → POST `/phone/pair/status` → cookie. `authenticate` needs `identify(headers)` first (`PeerProof`). `owner_from` material `"owner-token"` is the test stand-in. `peer_from` lives in the test, not production. `allowed_mains` are `pin_target` tailnet pins and are not dialed. `blocked_stable_ids` includes the PC stable ids. `activate` only after the authority is already v2. Session survival means the same cookie still passes `identify` + `authenticate` after the MAIN change, **and** a new ask is forwarded to the new MAIN's real sink exactly once. The peer verifier is only the whois stand-in. It is not the proof of forwarding.

Personal ask text may cross the RAM forward. `SECRET` and `POINTER` bytes must be absent from the temp tree and from `audit_events()`. Do not scan for the substring `b"prompt"` or the word `secret` (schema text and private-key files). A frame marker must be in the returned bytes and absent from the temp tree and the audit. Production `legacy_request({"prompt": ...})` is still `personal_field`.

HTTP: align integration `not_found` to 404 and `unknown_participant` to 403. Unknown codes stay 503. `RETRY_AFTER` stays `{not_quiet, lease_live}` and only when `wait > 0`. Update the e2e allowed tuple to include 404. Update the `test_gateway.py` comment.

### `peer_report`

Name checks stay. `compatible` also requires behavior, and `test_gateway.py:198` requires `compatible` true and `missing` `[]`. Keep the probes fast and on temp files that are closed:

- `hasattr(ContinuityGateway, "forward_transient")` and `hasattr(ContinuityGatewaySession, "relay_transient")`. Add these only in the same change that implements them, or `compatible` becomes false and `peer_gateway` returns as a blocker.
- Fresh `ExecutorGuard` is `legacy` before it is fenced. After a temp fence against a fake status port (mode `V2_ACTIVE`, issued epoch 1, acknowledge succeeds, boot id set), `verifier=None` then `accept` raises `AuthorityError("no_verifier")` and the sink is not called.
- `MainRuntime` on a port whose `status()["mode"]` is `LEGACY_ACTIVE`: `start()`, then `require_authority("control")` raises `RuntimeRefusal("legacy")`. Use `clock=None`.
- `GatewayPort.consume` without a signature raises `RuntimeRefusal` and does not call `client.consume`. The client stub must still expose the methods `GatewayPort.__init__` requires.

`INCOMPATIBILITIES`: remove `peers_absent` and `inprocess_auth_double`. Keep `no_passkey`. Keep id `no_signatures` but say the live CLI/hub/phone/Remote/NodeWorker verifier is absent and the temp fleet verifier is not activation. Keep `store_states` (no `CATCHING_UP`). Update unfenced-path rows to say hooks exist and are inactive. Note that transient forward, once landed, is still loopback HTTP to a temp relay, not a tailnet TLS route and not a live phone origin. `test_gateway.py:203` must stop requiring `peers_absent` and must keep `no_passkey` and `no_signatures`.

`LEGACY_PATHS` `v2_aware` stays false. A `hooked: true` / `activated: false` field is fine.

Production blockers that remain even when peers import: `gateway_host_unverified`, `hub_loopback`, `phone_origin_arch_loopback`, `dell_provider_unverified`, `luna_grok_unverified`, `safety_margin_unmeasured`, `windows_clock_untested`, `legacy_paths_unfenced`, `passkey_verifier_absent`, `signature_verifier_absent`, `automatic_failover_disabled`.

Replace the `LoopbackGateway` secret-identity test with: a real gateway refuses a payload that contains a `prompt` or `secret` key (`bad_request`), and the store mode stays `LEGACY_ACTIVE` with `main` none. Do not keep the stand-in so the old test stays green. Delete `LoopbackGateway`, `LocalExecutor`, `PcRuntime`, and the integration `MobileIngress` class. Keep exports the other tests import: `PERSONAL`, `CONTRACT_HTTP`, `EXTRA_HTTP`, `HTTP_STATUS`, `RETRY_AFTER`, `STEPS`, `ABSENT_STEPS`, `PEER_FACADES`, `LEGACY_PATHS`, `INCOMPATIBILITIES`, `IntegrationError`, `http_status`, `reject_personal`, `operation_digest`, `peer_report`, `deployment_facts`, `ContinuityIntegration`, `IsolatedFleet`, `ORIGIN`, `FLEET_ID`, `AUTHORITY_ID`.

One-sentence docstring updates where `gateway.py` line 15 and `AuthenticatedPort` around line 1179 name `LoopbackGateway`. Say the port does not accept a shared-secret identity. Do not rewrite `GATEWAY-IMPLEMENTATION.md`.

`deployment_facts` already copies only identity fields. Leave that. Do not dump device bodies.

### Launch hooks (default off, no activation)

Helpers on `integration.py`: `refuse_unfenced(surface)`, `close_unfenced`, `open_unfenced`, `node_seen_mode`, `set_node_seen_mode`, `apply_node_seen`, `phone_listener_open`, `set_phone_listener_open`.

Defaults: closed-set empty, seen mode `"before"`, phone listener open true. `refuse_unfenced` returns immediately when the surface is not closed. When a test closes it, raise `IntegrationError` (code `legacy_retained`). If the continuity import itself fails, the call site proceeds with today's legacy behavior. If `refuse_unfenced` raises `IntegrationError`, reraise it. Do not swallow a closed gate. Do not take down sleep because the import failed.

Call sites, lazy import inside the function:

- `cluster_cli.set_main` (`cluster_cli.py:68`) before `patch_config`. Test with a temp config file, gate closed, file bytes unchanged. Do not touch the user's `jarvis.config.json`.
- `cluster.execute_local` (`cluster.py:437`) and `Hub.dispatch` (`cluster.py:609`) before `validate_action` / execute. Gate closed must raise before inventory or desktop runs.
- `NodeWorker._untrusted` seen-file block (`cluster.py:1429-1439`) and the executor result path (`cluster.py:1394-1408`). Mode `"before"` is today's write-before-effect. Mode `"after"` writes only after the effect returns success, and drops the id on `AuthorityError` and on uncertain / ACCEPTED-without-completed. The guard still prevents a second effect if the id is dropped. Do not store payloads. `apply_node_seen` writes only the job id and a timestamp. Also prove with `inspect.getsource` that `_untrusted` consults `node_seen_mode`. Do not run a live `NodeWorker` against the user's hub. Default mode must stay `"before"` so existing cluster tests pass.
- `remote.Remote.call` (`remote.py:211`) as the first statement, before the unknown-action return and before `run_here` / dispatch. Test with an empty config and the gate closed so no SSH happens. `Remote.__init__` only stores config, state, and tools.
- `rest.sleep` (`rest.py:185`) **before** `pause()` at line 189. `pause()` (`rest.py:105-129`) stops systemd user units and writes a record. Do not call `rest.sleep()` or `pause()` in a test. Prove the hook with the gate closed via `inspect`/`ast` showing the gate is the first call, and a direct `refuse_unfenced("rest.sleep")` test. Never invoke an open-gate sleep.
- `phone.PhoneHandler._handle` (`phone.py:1428`). `do_GET` / `do_POST` only call `_handle`. Default `phone_listener_open()` is true, so current admission is unchanged. When a test closes it, return 503 code `legacy_retained` and close the connection. Do not start the live phone. Prove with the flag plus `inspect` of `_handle`.

`fleet_management.main_transfer` stays 409. It is not a continuity import target.

Importing continuity from those modules was previously forbidden. The parent ordered the hooks. They stay default-off and fail-closed. They do not flip production to v2.

### Temp MAIN sink

`ThreadingHTTPServer(("127.0.0.1", 0), ...)`, `daemon_threads = True`, thread `daemon=False`, joined on close. Enroll that origin as the PC's `relay_origin`. It must not equal the gateway listening origin. Handler caps the body, does not log, returns JSON.

Effects: `require_authority` then `ExecutorGuard.accept`. Sink is zero-arg and must not capture prompt text. Capture an operation-id counter only. If `require_authority` or `accept` raises, the HTTP result must not say `accepted: true`. Metadata-only posts do not run the sink.

### E2E properties to preserve (new API, same security)

Rewrite `tests/continuity/test_operational_e2e.py`. Do not keep `run_count`, `hold`, `note_input`, `signature_verified`, `LocalExecutor`, or `require_authority()["ok"]` as the success path.

Sleep / promote / phone:

- Boot zenbook through real cutover. Not-ready consume is `no_admission`, sink 0. After complete, main is zenbook and `main_ready`. Production mode stays `LEGACY_ACTIVE`. Both dispatch flags never true together.
- A normal phone ask is forwarded once to the zenbook sink. `effect_authorized` is false. Text is not in sqlite or audit.
- During sleep, `stop_new_admission("asleep")`. `require_authority` raises `RuntimeRefusal("asleep")`. The ask does not run the sink. Remaining is 0.
- `recover` while the lease is valid is `main_alive` and opens no row.
- After lapse, ack dell and oracle only. Fence is `not_quiet` with the client's wait. Tick that wait minus a hair and the fence still fails. Tick the rest. Fence, grant, not-ready accept, `complete(True)`.
- The new ask runs on Dell exactly once. The same operation id is not sent again. The old zenbook tuple (`accept_lease` of the old grant, `renew`, `guard.accept`) is stale and sink 0.
- AI without `ai_ready` does not run and is not ordinary. Reason `profile`.
- Cookie still authenticates after the MAIN change. `manage_main` stays false.
- `SECRET` and `POINTER` absent from persisted bytes. Production `v2_request` and `activate_v2` stay false.
- Overview and frame after promotion return the new MAIN and the frame marker, and neither is on disk.

Reboot / partition:

- `failpoint="after_accept"` is uncertain. Replay does not run the sink.
- Reboot is a new guard (new boot id, same journal). A completed op replays without consume while the client is down. Consume count unchanged.
- A new current-epoch ticket is `AuthorityError("unavailable")`, not `authority_unavailable`.
- The old epoch, fenced while up and then the link dropped, is local stale. Consume is not called.
- A failed MAIN relay stays uncertain and is not resent. Ingress can still answer status if the gateway itself is up.
- Restart as above. Phone cookie still authenticates. Submit is `no_main`. Sink counts stay put.
- A new operation id with the old `issuer_incarnation` is `stale`.

Canary:

- AI readiness false, drain `not_ready`, cancel keeps zenbook.
- Control handover fence succeeds. `complete(False)` leaves `main` none and mode `V2_ONLY_NO_MAIN`.
- `set_mode(ROLLBACK_QUIESCED)` from `HANDOVER` or `V2_ACTIVE` is `bad_edge`. From `V2_ONLY_NO_MAIN` expect the real quiet/ack result, and that result is not production `LEGACY_ACTIVE`.
- Production rollback stays `applied` false. Both dispatch flags false.

Auth:

- A client whose public key is not the enrolled key gets `unauthorized` before renew. `lease_remaining` unchanged. Renew counter unchanged.
- Wrong CSRF is `IngressError` code `csrf`.
- Phone without `manage_main` cannot confirm (`forbidden`). No transition row.
- With `manage_main` but `passkey_verifier is None`, confirm is `passkey_unavailable` and does not change MAIN. Do not keep a fake passkey success.
- `catching_up` is `bad_request` and does not change epoch.
- Oracle as MAIN candidate is `ineligible`.

Personal:

- A signed operation or metadata relay with an extra `prompt` key is `bad_request` before the journal write.
- An ask whose text is `SECRET` may be forwarded in memory and must be absent from the temp tree and `audit_events()`.
- `POINTER` as an ask during sleep is not written. A digest may be stored. Plaintext must not.

Also: verifier none on a fenced `ExecutorGuard` is `AuthorityError("no_verifier")` and the sink is not called. Dell online assertions as in the runtime section. Launch-hook tests as in the hook section. Node seen drops a refused id when the test sets mode `"after"`, and the default mode is `"before"`.

### Activation document

Write `docs/design/mobile-continuity/OPERATIONAL-INTEGRATION.md`. Every item stays false:

- Verified Oracle host (a device id in a temp test is not one).
- Measured `SAFETY_MARGIN` (still 2.0 placeholder).
- Dell suspend-aware clock tested (`windows_clock_tested` false).
- Luna +5 and Grok 4.7 readiness from local subscription credentials, all five lanes. Not a model name. No paid API.
- Passkey verifier.
- Live signature verifier on CLI, hub, phone, `Remote.call`, and `NodeWorker`.
- Legacy paths fenced (hooks present, inactive).
- Hub and phone not Arch loopback.
- Automatic failover still disabled.
- Owner-directed cutover with canary. Not this object.

State that the transient body channel is loopback HTTP to a temp relay. It is not a tailnet TLS route and not a live phone origin. Do not claim live operational failover.

`__init__.py`: if the hooks land, replace the "nothing imports this" sentence with one sentence that launch hooks import a default-off gate and production is not failover. Do not export gateway symbols. Do not rewrite the package.

## Commands and results

This session ran no tests. The baseline above is the last continuity-only result. After the edits, run:

```
.venv/bin/python -m unittest tests.continuity.test_operational_e2e tests.continuity.test_gateway tests.continuity.test_ingress tests.continuity.test_executor tests.continuity.test_runtime
```

Then:

```
.venv/bin/python -m unittest discover -s tests/continuity
```

Do not claim the whole JARVIS suite. Do not start `rest.sleep`, `pause`, the live phone, or the live hub. Gateway `serve` threads are non-daemon. Tests must `close()` in `finally`.

Known ways to break a suite that is already green:

- Requiring `relay_transient` on `MobileIngress.__init__`, or adding it to `GATEWAY_METHODS`, breaks the ingress fakes.
- Requiring SSH armor in `ExecutorGuard` breaks the executor fakes.
- Deleting `LoopbackGateway` or `peers_absent` without editing `test_gateway.py` breaks the gateway suite.
- Raising the signature cap is safe. Lowering it, or requiring armor, is not.
- Calling `rest.sleep` or `pause()` stops the user's systemd units.

## Next concrete implementation

Do these in order so the tree is never left importing a deleted symbol:

1. `gateway.forward_transient` and the separate RAM effect table. Leave `relay()` and `_send` alone.
2. `ContinuityGatewaySession.relay_transient` and the optional ingress submit/read/status branch. Pass `/phone/api/me` through. Do not touch `GATEWAY_METHODS`.
3. Executor cap 1800 only.
4. Replace integration doubles with `SignedAuthority`, the temp fleet, behavioral `peer_report`, aligned HTTP codes, and default-off helpers. Update `test_gateway.py` in the same edit.
5. Rewrite `test_operational_e2e.py` onto the real types.
6. Lazy hooks in the six call sites. Default behavior unchanged.
7. `OPERATIONAL-INTEGRATION.md` and the one-sentence `__init__.py` update.
8. Run the two unittest commands above. Fix owned failures. Report any failure outside `jarvis/continuity`, `tests/continuity`, and those six hook sites. Do not edit it.

## Boundaries

- Not alone: do not revert other dirty files. The worktree was already dirty.
- No paid API. Luna +5 / Grok 4.7 is routing intent, not a provider call.
- No commit, push, CI, restart, or live activation.
- Do not weaken auth, fencing, signing, or personal-key rejection for a green test.
- Do not pretend metadata relay delivered a prompt. `effect_authorized` stays false.
- Default legacy until an explicit cutover that this task does not perform. Production `ContinuityIntegration` stays `LEGACY_ACTIVE` and refuses `activate_v2`.
- Fail closed in v2. No split-brain, no replay, no personal content on gateway disk or logs.
- Other-module failures outside the owned surface are reported, not edited.
- Parent is waiting on completion of this handoff, then will delegate. Do not poll, sleep, loop, watch, or schedule.
