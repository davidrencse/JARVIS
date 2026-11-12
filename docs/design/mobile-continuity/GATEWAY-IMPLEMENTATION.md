# Continuity gateway implementation

Date: 2026-11-11. Status: isolated authenticated adapter and tests. This is not a
deployment, not a cutover, and not operational failover. Oracle is not a verified
gateway host. Dell provider readiness was not checked. Importing
`jarvis.continuity.gateway` does not bind a socket, read live config, or change
`cluster.main_device`. `ContinuityGateway.serve` is the only listener, and only
when a caller invokes it.

The running application does not import this module. `jarvis/continuity/__init__.py`
still says no gateway is provisioned. That package docstring was left unchanged.

Contract source: `OPERATIONAL-CONTRACT.md` section 3, plus the stricter choices
below. `AuthorityStore` trusts its caller. This module authenticates before every
store call. There is no `auth=false` switch and no shared-secret enrollment.

## What is implemented

`ContinuityGateway` owns one `AuthorityStore`, a RAM device directory, and an
optional loopback HTTP server. `AuthorityClient` is the pinned-origin HTTP client.
`AuthenticatedPort` exposes the method names `MainRuntime` / `ExecutorGuard` already
call, and it still goes through `AuthorityClient`.

Device routes require an enrolled installation key (`ssh-ed25519`) in namespace
`jarvis-continuity-device-v1`. Owner routes require a fresh challenge signature by
an owner public key pinned at `ContinuityGateway.open`, namespace
`jarvis-continuity-owner-v1`. MAIN admission tickets are signed in
`jarvis-continuity-ticket-v1`. A signature made in `jarvis-code` does not verify
here. Verification is `ssh-keygen -Y sign` / `ssh-keygen -Y verify`. There is no
`cryptography` or `nacl` dependency and no WebAuthn verifier. A passkey field is
refused, not treated as owner proof.

The owner key is not enrollable over HTTP. `manage_main` is not a device capability.
`client` devices are directory entries only. They are not store participants, so a
phone cannot satisfy or block a fence. The directory is RAM-only: a process restart
fails closed until the owner enrolls again. Re-enrolling the same `installation_id`
keeps store acknowledgements. An identical admission replay after restart returns
the historical record and is not a new epoch grant.

`set_mode` can move along the store's legacy edges
(`PREPARING_V2`, `QUIESCED`, `V2_ONLY_NO_MAIN`, `ROLLBACK_QUIESCED`, back to
`LEGACY_ACTIVE`). It cannot jump to `V2_ACTIVE`, `HANDOVER`, or `QUARANTINED`.
A fresh store stays `LEGACY_ACTIVE`.

## Facade compared with the operational contract

| Contract signature | This module | Why it is stricter |
| --- | --- | --- |
| `status(authenticated_device)` | `status(principal, payload)` | Principal is single-use and bound to the payload digest. |
| `acknowledge(device, mode_version, fenced_epoch)` | `acknowledge(principal, payload)` | Device and installation come from the signature, not from the body. |
| `transition_request(owner_proof, request)` | same names, payload has no `caller` | Stored caller is `owner-` plus the SHA-256 prefix of the pinned public key. |
| `transition_step(owner_or_policy_identity, ...)` | `transition_step(owner_proof, payload)` | No unsigned policy identity. `grant` is owner-proof only. |
| `consume(authenticated_main, ticket)` | `consume(principal, payload)` | HTTP caller is the enrolled **target**, plus a MAIN-signed ticket. MAIN cannot pre-consume. |
| `relay(client, operation_id, bounded_request)` | `relay(principal, payload)` | Five metadata fields. `effect_authorized` is always false. |
| passkey `manage_main` | refused | No verifier exists. A boolean must not pass for one. |
| HTTPS on Oracle | plaintext HTTP, loopback unless `confirm_bind` on one non-wildcard IP | No verified TLS name. `0.0.0.0`, `::`, and hostnames are refused. |

`enroll` and `set_mode` are extra owner routes so other modules do not open the
SQLite file. Reads are POST so signatures stay out of URLs. GET and other methods
are 405. Query strings, non-`application/json` bodies, and `Transfer-Encoding` are
refused. Body cap is 8192 bytes. Response cap is 8192, relay response cap 1024.

Forbidden payload keys include `prompt`, `body`, `text`, `content`, `message`,
`file`, `filename`, `path`, `url`, `upstream`, `host`, `authorization`, `cookie`,
`secret`, `token`, `password`, `transcript`, `frame`, `image`, and `payload_body`.
`control_ready`, `ai_ready`, and `canary_ok` must be JSON booleans. The integer
`1` is rejected so `bool(1)` inside the store cannot mark a candidate ready.

## HTTP

All routes are POST.

| Path | Caller | Purpose |
| --- | --- | --- |
| `/v1/authority/challenge` | anyone with the request digest | single-use owner challenge, TTL 60s, max 32, burned after 5 bad signatures |
| `/v1/authority/status` | device `status` | empty payload |
| `/v1/authority/renew` | device `lease` | holder must be the signer |
| `/v1/authority/release` | device `lease` | holder must be the signer |
| `/v1/authority/acknowledge` | device `fence` | |
| `/v1/authority/transitions` | owner | |
| `/v1/authority/transitions/readiness` | device `candidate` | signer must be the transition's candidate |
| `/v1/authority/transitions/step` | owner | `drain`, `fence`, `grant`, `complete`, `cancel` |
| `/v1/authority/admissions/consume` | device `consume` | signer must be `target` |
| `/v1/authority/admissions/lookup` | device `reconcile` | target or MAIN only; anyone else gets `not_found` |
| `/v1/authority/relay` | device `client` | metadata only |
| `/v1/authority/participants` | owner | enroll |
| `/v1/authority/mode` | owner | one cutover edge |

`AuthorityClient` pins `origin` and `authority_id`. Methods take no URL. Redirects
raise `redirect`. Environment proxies are not used. HTTP origins must be loopback.
An `https` origin is accepted only as a pin for a future terminator; this process
does not speak TLS and does not relay to `https`. A response whose `authority_id`
differs from the pin is `authority_mismatch`.

Renew echoes the accepted `(holder, lease_id, incarnation, epoch)` plus the store's
`valid_for`, `renew_after`, and `ready`. It does not invent a longer TTL.

## Relay

Relay identity is `(digest, target, attempt_id, capability)` per `operation_id`,
kept in RAM. A repeated id with the same identity returns the first result and is
not sent again. A different identity is `conflict`. Failure after a network attempt
is not resent. Refusal before any attempt does not consume the id.

The upstream URL is the MAIN device's enrolled `relay_origin`, not a caller field.
`pin_relay` allows loopback HTTP only. The path is fixed: `/v1/continuity/relay`.
The body is authority tuple metadata. `effect_authorized` in the client result is
always false. A relay is not an admission. No spool is written to disk.

`_relay_status` failures are re-raised with the caller's `operation_id`.

## Errors

`GatewayError` string is the stable code only. Store codes pass through.
When the store supplies a positive finite wait, the error JSON includes that
number as `wait` and the response sets `Retry-After` to `ceil(wait)`
delay-seconds. `AuthorityClient` keeps `wait` only when those two agree.
A header alone, a body field alone, a boolean, zero, an HTTP-date, or a
mismatch becomes no wait. A missing wait is not "the barrier elapsed".
`AuthenticatedPort` and `runtime.GatewayPort` pass that client wait through.
The wait is not written to the audit line.

| Code | HTTP |
| --- | --- |
| `bad_request`, `bad_ticket`, `bad_origin`, `refused_bind` | 400 |
| `unauthorized` | 401 |
| `forbidden`, `unknown_participant` | 403 |
| `not_found` | 404 |
| `ticket_expired` | 410 |
| `stale`, `busy`, `key_reused`, `conflict`, `not_quiet`, `lease_live`, `ahead`, `bad_edge`, `unacked`, `expired`, `draining`, `mode`, `ineligible`, `main_alive`, `no_main`, `noop`, `not_ready`, `bad_state`, `no_admission`, `target_unfenced` | 409 |
| `quarantined`, `writer_busy`, `unavailable`, `no_relay`, `authority_mismatch`, `redirect` | 503 |

`integration.EXTRA_HTTP` maps `not_found` and `unknown_participant` to 409. This
gateway does not. A missing admission stays 404. Unknown store codes become 503
without their message. Exception text and database paths are not returned.

Opening a store with the wrong `authority_id` quarantines that file. Status then
reports the **stored** id. A client pinned to the foreign id gets
`authority_mismatch`. Do not point `open` at a live authority database.

Two gateway processes on one SQLite file get `writer_busy`.

Audit lines are method, route, status, code, and `device_id` only.

## AuthenticatedPort

`MainRuntime` and `StoreAdapter` call `status()`, `renew(lease_tuple)`,
`release(lease_tuple)`, `acknowledge(device_id, installation_id, mode_version,
fenced_epoch, boot_id=None)`, `transition_request(request)`,
`transition_readiness(transition_id, readiness)`, and
`transition_step(transition_id, step, canary_ok=None)`. `ExecutorGuard` calls
`status()`, `acknowledge(...)` with those same arguments, `admission(admission_id)`,
and `consume(**ticket_fields)` **without** a ticket signature.

`AuthenticatedPort` implements that store shape on top of `AuthorityClient`:

- The client is bound to one installation key. A different `device_id` or
  `installation_id` is `forbidden` and is not signed.
- `renew` / `release` require `holder` to be that device. Extra keys, including
  `prompt`, are `bad_request`.
- `transition_request` drops `caller`. The owner key is the caller. A client
  without the owner key gets `unauthorized` and the store is not asked.
  `managed-hub` is not proof.
- `transition_step` is owner-key only. There is no policy token.
- Readiness booleans are not coerced. `checkpoint: null` is omitted.
- `consume` requires `ticket_signature`. A missing signature is `unauthorized`
  and does not open a socket. A signature whose `target` is not this device is
  `forbidden`. This is the deliberate break with `ExecutorGuard.accept`, which
  still calls `consume` without the signature. Wiring that guard at this port
  fails closed. Do not drop the signature to make it pass.
- `GatewayError` becomes `AuthorityError` with the same code and wait. The
  message is the code only.

`runtime.py` still catches `LinkError` for partitions. A dead gateway surfaces as
`AuthorityError("unavailable")`, which the runtime treats as an authority refusal.
That is fail-closed. It is not a claim that the PC cached a lease.

## Ingress session

`jarvis.continuity.ingress.ContinuityGatewaySession` is the in-process client the
phone module already wrote. It calls `authenticate_device`, `status(principal, {})`,
`relay(principal, payload)`, and `transition_request(owner_proof, payload)`.
`relay_metadata` has exactly five fields. A string or passkey dict is not an
`OwnerProof`.

`MobileIngress` in that file expects `status()`, `relay_metadata(...)`, and
`transition_request(proof, payload)`. Pass the session, not `ContinuityGateway`
itself and not `integration.MobileIngress`. The ingress module imports
`_message`, `payload_digest`, `sign_message`, `OwnerProof`, `DEVICE_NS`,
`GatewayError`, and `CAPABILITIES`. Those names are part of this seam.

Ingress must not call renew, release, acknowledge, consume, admission, readiness,
step, enroll, or set_mode. Its own `GATEWAY_NOT_OURS` list says so.

## Integration stand-in (do not merge by weakening this module)

`peer_report()` imports `jarvis.continuity.{gateway,ingress,runtime,executor}` and
checks method **names**. On 2026-11-11 all four are present and name-compatible.
`integration.INCOMPATIBILITIES` still says those files are absent, that no MAIN
signature verifier exists, and that no WebAuthn verifier exists. The first two
are stale for this gateway. The passkey line is still true. `_blockers()` still
adds `signature_verifier_absent` and `passkey_verifier_absent` unconditionally,
because the integration module does not call this verifier.

`tests/continuity/test_operational_e2e.py` still asserts every peer is absent and
that `peer_gateway`, `peer_ingress`, `peer_runtime`, and `peer_executor` are
blockers. Those assertions fail because the modules exist. Production
`ContinuityIntegration` still refuses cutover for the other gates (no Oracle
device, loopback hub, unverified Dell provider, unmeasured margin, unfenced
legacy paths, no passkey, no automatic failover).

`IsolatedFleet` does not construct `ContinuityGateway`. It uses
`integration.LoopbackGateway` and `integration.MobileIngress`, which are different
classes from the ones in `gateway.py` and `ingress.py`.

| | `LoopbackGateway` | This gateway |
| --- | --- | --- |
| Auth | shared `secret` compared in-process | SSH installation key and pinned owner key |
| `acknowledge` | `(identity, mode_version, fenced_epoch)` | principal plus payload; identity is the signer |
| Identity dict | includes `secret` | `secret` is a forbidden key |
| `consume` | caller device must be the MAIN | caller must be the target, with a MAIN signature |
| `transition_step` | candidate may `grant`; owner is a shared secret | owner proof for every step, including `grant` |
| Readiness | `bool(...)` coerces `1` to ready | JSON booleans only |
| Relay | returns `ok` and `effect_class` when `main_ready` | metadata POST, `effect_authorized` false |
| Owner proof | `mint_owner_proof()` random string | one-use SSH challenge |

`LoopbackGateway._pc` calls `reject_personal` on the identity dict. That dict
contains `secret`, and `secret` is in `PERSONAL`, so `acknowledge` raises
`personal_field` before the store. `IsolatedFleet.cutover` does that for every
PC, which is why `LoopbackCase` in `test_operational_e2e.py` errors. Fixing it
by removing `secret` from the personal-field set, or by teaching
`ContinuityGateway` to accept that dict, would weaken the boundary. Left for the
integration owner.

`integration.PcRuntime.renew(identity, lease)` and
`integration.MobileIngress.status(gateway.status(identity))` also use the
stand-in shape. `AuthenticatedPort.renew` takes only the lease tuple because the
installation was pinned at client construction.

## Tests

```text
.venv/bin/python -W error::ResourceWarning -m unittest tests.continuity.test_gateway
```

That file is the gateway regression. It covers pins, bind refusal, legacy
fail-closed, SSH replay, capability and holder binding, owner single-use,
target-only consume, restart barrier, relay idempotency, foreign `authority_id`
quarantine, the runtime port, and the ingress session.

`python -m unittest discover -s tests/continuity` also runs fellow-owner tests.
Gateway tests passing does not make that discovery green. The operational e2e
file fails for the peer-report and `personal_field` reasons above. Those files
were not edited.

## Limits

- Not operational failover. Automatic promotion is not implemented here.
- No verified Oracle host, TLS name, Tailscale route, supervisor, or backup.
- Dell Luna / Grok session readiness was not verified. A provider name is not
  `ai_ready`. No paid API is called.
- Listener is not TLS. `confirm_bind` is an explicit non-loopback IP bind for a
  lab, not a public gateway.
- `SAFETY_MARGIN` remains the store's unmeasured 2 second placeholder.
- Linux `CLOCK_BOOTTIME` is the only cache clock. Windows and macOS must not cache.
- Relay does not authorize an effect and does not carry a body.
- Passkey proof is not implemented.
- RAM directory and RAM relay state die with the process.
- `ssh-keygen` must be on `PATH`.
- Legacy `phone.py`, `cluster.main_device`, and the hub are untouched.
- Wrong `authority_id` on `open` quarantines the file it opens.
