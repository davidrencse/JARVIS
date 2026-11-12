# Ingress implementation

Status: isolated prototype. This is not operational failover. Oracle is not a verified gateway host. Dell readiness and Luna +5 / Grok 4.7 are not verified. `legacy_phone_fenced=True` is a caller claim; `activate()` records `proved: false`. Nothing in the running application imports `jarvis.continuity`. `phone.py` and `phone.js` are unchanged.

`jarvis/continuity/ingress.py` is the phone origin client. It does not write the authority database, renew a lease, consume an admission, or choose MAIN. A successful metadata relay is not fenced work.

## What the gateway actually accepts

`ContinuityGateway` (not the three-argument sketch in OPERATIONAL-CONTRACT.md) is the peer. Ingress talks to it through `ContinuityGatewaySession`, which signs each call with the ingress installation key and does not listen.

| Session call | Real gateway call | Payload |
| --- | --- | --- |
| `status()` | `status(DevicePrincipal, {})` | Empty object. Purpose `status`. |
| `relay_metadata(operation_id, target, attempt_id, digest, capability)` | `relay(DevicePrincipal, payload)` | Exactly those five fields. Capability sent by ingress is `client`. |
| `transition_request(owner_proof, payload)` | `transition_request(OwnerProof, payload)` | Exact store fields below. The proof is `gateway.OwnerProof`, one use, bound to the payload digest. |

`relay` returns `effect_authorized: false` and posts metadata to the enrolled MAIN's `relay_origin`. `pin_relay` allows only `http://127.0.0.1` or `http://[::1]`. A tailnet URL is refused. Ingress `pin_target` is a separate allowlist of tailnet HTTPS names plus a pinned Tailscale address. Ingress never resolves DNS and never dials that address. An allowlisted Dell is not a substitute when status says the MAIN is the Zenbook.

A phone prompt, event, frame, cookie, CSRF token, passkey receipt, or URL is not an argument. `ContinuityGateway.relay` rejects those keys with `bad_request` and does not store them. Ingress does not open a second socket to deliver them.

Ingress does not call `renew`, `release`, `acknowledge`, `consume`, `admission`, `transition_readiness`, `transition_step`, `set_mode`, or `enroll`.

## Facade

`MobileIngress` keeps the contract methods, plus `read` because a GET must not be stored as an effect:

- `pair(pairing_proof, client_metadata) -> Session`
- `authenticate(cookie, csrf_token, request_origin) -> Principal`
- `status(principal) -> MobileStatus` (`relay` is false and `metadata_only` is true even when a MAIN is ready)
- `submit(principal, operation_id, request) -> OperationStatus`
- `operation(principal, operation_id, cursor=None) -> OperationStatus`
- `manage_main_preview(principal, request) -> TransitionPreview`
- `manage_main_confirm(principal, preview_id, passkey_proof) -> TransitionView`
- `read(principal, kind, query=None)` validates a read and then refuses it

`submit` has two shapes.

Phone kinds `ask`, `session_start`, `session_input`, `session_stop`, and `logout_end` are checked (operation id, approval words, session ownership, epoch) and then refused with `body_not_relayable` (503). They are not inserted and not relayed. The prompt is not hashed onto disk.

Metadata requests may contain only `attempt_id` and an optional `effect_class` identifier. `effect_class` is part of the local digest so two classes conflict. It is not a gateway field, so the MAIN cannot recover it. The relay target is the ready MAIN from `status()`, and only when mode is `V2_ACTIVE`, `main_ready` and `lease_valid` are true, the lease holder matches, and that device is in `allowed_mains`. The stored state `forwarded` means the metadata notice was accepted upstream. `effect_authorized` in the client view is false. The same operation id is not sent again. A different digest is `409`. A gateway `no_relay`, a crash, a non-dict result, `effect_authorized: true`, or a result that carries `path` / `url` / `reply` is stored `uncertain` and is not sent again. Ingress does not open a path in that result.

`manage_main_preview` does not call the gateway. It stores a hash of the challenge. `manage_main_confirm` checks the passkey locally, drops the receipt, and calls `transition_request` only with a fresh `gateway.OwnerProof` and this payload:

```text
client_key, action, candidate, expected_incarnation, expected_epoch,
expected_policy, profile
```

`client_key` is the preview id. There is no `caller`, `receipt`, or `reconcile` field. The store's caller is the owner-key id inside the proof, and the store is idempotent on that caller plus `client_key`. A lost answer mints a new proof for the same payload. A dict, a passkey string, or a missing minter does not call the gateway. A phone `manage_main` grant does not make the phone an owner.

Until `activate(expected_mode_version, legacy_phone_fenced=True)`, every phone route is `503 legacy_retained` and `status()` is not called. Activation is RAM-only. Reopening the sqlite file is inert. Mode `LEGACY_ACTIVE`, a version mismatch, quarantine, or an unreachable authority fails closed. `last_main` and `preferred` are not used as a route.

## Phone adapter

`PhoneProtocolAdapter` maps the current phone paths. It does not serve the shell (`404 shell_not_ingress`). `/phone/api/locate` is `404`.

The current `phone.js` sends `X-JARVIS-Phone: 1` and same-origin cookies. It does not send `X-CSRF-Token`. It does not send `operation_id` on session start or input. Ask uses `key` as the operation id. After a future pair response, that PWA still fails closed: a cookie without the CSRF header is `403`, not a bounce to Pair. A missing cookie is `401` with `pair: true`. `__Host-jarvis_phone` is origin-bound and does not migrate from the PC origin to a gateway origin.

Logout revokes the phone row locally and returns `ending: local_only`. It does not tell the PC. Session stop is `body_not_relayable`, so a held PC input is not released through this module.

`503` and `409` responses do not include a job id. `phone.js` retries those with the same ask key. The retry is refused again and does not create an effect.

## PC seam

`accept_gateway_phone(claim, kind, operation_id, body)` is not implemented. Do not add it to `phone.py` until a reviewed body channel exists. A reverse proxy onto `phone.py` is the wrong seam: the gateway hop's Tailscale whois is the gateway, and the `__Host-` cookie is a different origin. The future function must refuse a raw cookie and `X-Forwarded-For` from that hop, require `role == phone`, `manage_main == false`, and `opener == phone:{phone_id}`, and run `ExecutorGuard.accept` before any effect. Reads must not create work. Ingress does not call this function today, because the gateway relay cannot deliver `body`.

`jarvis.continuity.integration.MobileIngress` is a different class used by `IsolatedFleet`. It is not this module. Its `relay(principal, operation_id, bounded)` shape is not `ContinuityGateway.relay`.

## Stored data

The ingress sqlite file is separate from the authority database (WAL, FULL, mode `0600`, exclusive `.writer` lock). One process owns it. Columns are ids, hashes, pairing state, and the metadata digest. There is no prompt, reply, cookie, token, frame, or event column. `token_sha256` and `csrf_sha256` are hashes. A short code or prompt can still be guessed from an unsalted sha256; the plaintext is not written. Name, login, OS, and node are bounded identity fields, the same idea as `phone.py`.

## Incompatibilities

- No verified provisioned gateway. Oracle and Dell are not shown ready by this module.
- `ContinuityGateway.relay` cannot carry ask text, session events, frames, or overview JSON. Those routes fail closed.
- Gateway relay targets are loopback HTTP. Ingress tailnet pins are not dialed and are not gateway relay origins.
- There is no transition-read method. Reconcile is the same `client_key` with a new owner proof. If the owner key changes, the store treats it as a different caller.
- A passkey receipt is not an `OwnerProof`. Confirm without a minter that returns `gateway.OwnerProof` does not change MAIN.
- `integration.py` still says the peer modules are absent, and `tests.continuity.test_operational_e2e` fails on that report plus `IsolatedFleet` identities: the dict key `secret` is in `integration.PERSONAL`, so `acknowledge` raises `personal_field`. That suite does not construct this ingress.
- Current `phone.js` cannot complete a v2 phone session. Screen control is not implemented here.
- Two failures in one metadata send stay uncertain. The gateway's relay table is RAM and disappears on gateway restart. Ingress still does not send that operation id again.

## Tests

```text
.venv/bin/python -m unittest tests.continuity.test_ingress
```

16 tests, temp directories and a fake session plus one in-process `ContinuityGateway` (no `serve()`, no live config, no `.jarvis` write). They cover the pin refusals, inert activate, routing refusals, one metadata send, no second send after uncertainty or a crash, phone-body refusal with the prompt absent from disk, pairing/CSRF, passkey-is-not-owner-proof, and a signed real-gateway call that rejects a `prompt` field.

`discover -s tests/continuity` also runs the other continuity modules. On 2026-11-11 that discovery reported failures only in `tests.continuity.test_operational_e2e`, described above. `tests.continuity.test_ingress` passed.
