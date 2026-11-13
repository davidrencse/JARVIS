# Continuity runtime

*What `jarvis/continuity/` gateway, ingress, executor, and runtime actually do. Isolated prototype. Not live failover.*

Contracts and the effect-sink inventory are [continuity-contract.md](continuity-contract.md). The plan is [continuity-design.md](continuity-design.md).

`authority.TICKET_FIELDS` is the one field tuple. `MAX_SIGNATURE` is 1800. Gateway, runtime, executor, and integration import both and each validates values itself. Modes are `LEGACY_ACTIVE`, `PREPARING_V2`, `QUIESCED`, `V2_ONLY_NO_MAIN`, `V2_ACTIVE`, `HANDOVER`, `ROLLBACK_QUIESCED`, `QUARANTINED`.

## Gateway

`ContinuityGateway` is a loopback HTTP store client. It verifies SSH installation signatures and a pinned owner key. It does not verify WebAuthn. `serve` listens only when called.

| Route | Who | Rule |
| --- | --- | --- |
| `/v1/authority/challenge` | request digest | Single-use owner challenge, TTL 60 seconds, max 32, burned after 5 bad signatures |
| `/v1/authority/status` | device `status` | Empty payload |
| `/v1/authority/renew` | device `lease` | Holder must be the signer |
| `/v1/authority/release` | device `lease` | Holder must be the signer |
| `/v1/authority/acknowledge` | device `fence` | |
| `/v1/authority/transitions` | owner | |
| `/v1/authority/transitions/readiness` | device `candidate` | Signer must be the candidate |
| `/v1/authority/transitions/step` | owner | `drain`, `fence`, `grant`, `complete`, `cancel` |
| `/v1/authority/admissions/consume` | device `consume` | Signer must be `target` |
| `/v1/authority/admissions/lookup` | device `reconcile` | Target or MAIN. Anyone else is `not_found` |
| `/v1/authority/relay` | device `client` | Metadata only |
| `/v1/authority/participants` | owner | Enroll |
| `/v1/authority/mode` | owner | One cutover edge |
| `/v1/authority/self/policy`, `/v1/authority/self/transitions` | enrolled device | Self-recovery only if that gateway config allows it. The store still decides |

Relay identity is `(digest, target, attempt_id, capability)` per `operation_id`, in RAM. The same identity returns the first result and is not sent again. A different identity is `conflict`. A failure after a network attempt is not resent. A refusal before any attempt does not consume the id. The upstream URL is the MAIN device's enrolled `relay_origin`, not a caller field. `pin_relay` allows loopback HTTP only. The path is `/v1/continuity/relay`. The body is authority-tuple metadata. `effect_authorized` is always false. A relay is not an admission. Nothing is spooled to disk.

`GatewayError` text is the stable code only. A positive finite store wait is returned as JSON `wait` and `Retry-After: ceil(wait)`. The client keeps `wait` only when both agree. A header alone, a body field alone, a boolean, zero, an HTTP-date, or a mismatch is no wait. A missing wait is not "the barrier elapsed". The wait is not written to the audit line. Audit lines are method, route, status, code, and `device_id` only.

Gateway `HTTP_STATUS` (source of truth for codes the gateway speaks):

| HTTP | Codes |
| --- | --- |
| 400 | `bad_request`, `bad_ticket`, `bad_origin`, `refused_bind` |
| 401 | `unauthorized`, `envelope_unsigned` |
| 403 | `forbidden`, `unknown_participant`, `revoked` |
| 404 | `not_found` |
| 410 | `ticket_expired` |
| 409 | `stale`, `busy`, `key_reused`, `conflict`, `not_quiet`, `lease_live`, `ahead`, `bad_edge`, `unacked`, `expired`, `draining`, `mode`, `ineligible`, `main_alive`, `no_main`, `noop`, `not_ready`, `bad_state`, `no_admission`, `target_unfenced`, `installation_mismatch`, `active_holder`, `transition_member` |
| 503 | `body_not_relayable`, `quarantined`, `writer_busy`, `unavailable`, `no_relay`, `authority_mismatch`, `redirect` |

Unknown store codes become 503 without their message. Exception text and database paths are not returned. Opening a store with the wrong `authority_id` quarantines that file. Status then reports the stored id. A client pinned to the foreign id gets `authority_mismatch`. Two gateway processes on one SQLite file get `writer_busy`.

Forbidden gateway keys (not the same set as `integration.PERSONAL`): `prompt`, `body`, `text`, `content`, `message`, `file`, `filename`, `path`, `url`, `upstream`, `host`, `authorization`, `cookie`, `secret`, `token`, `password`, `transcript`, `frame`, `image`, `payload_body`.

### AuthenticatedPort

Bound to one installation key. A different `device_id` or `installation_id` is `forbidden` and is not signed. `renew` / `release` require `holder` to be that device. Extra keys, including `prompt`, are `bad_request`. `transition_request` drops `caller`. The owner key is the caller. `managed-hub` is not proof. `transition_step` is owner-key only. Readiness booleans are not coerced. `checkpoint: null` is omitted.

`consume` requires `ticket_signature`. A missing signature is `unauthorized` and does not open a socket. A signature whose `target` is not this device is `forbidden`. `ExecutorGuard.accept` still calls `consume` without the signature. Wiring that guard here fails closed. Do not drop the signature.

A dead gateway surfaces as `AuthorityError("unavailable")`. The runtime treats that as a refusal, not as a cached lease.

## Ingress

`jarvis.continuity.ingress.MobileIngress` is the phone origin client. It is not `phone.py`. It does not write the authority database, renew a lease, consume an admission, or choose MAIN. It talks through `ContinuityGatewaySession`, which signs with the ingress installation key and does not listen.

Session calls: `status()` with an empty object; `relay_metadata` with exactly `operation_id`, `target`, `attempt_id`, `digest`, and capability `client`; `transition_request` with a one-use `OwnerProof` bound to the payload digest. A string or a passkey dict is not an `OwnerProof`.

Ingress does not call renew, release, acknowledge, consume, admission, readiness, step, enroll, or set_mode. Its `pin_target` allowlist is not dialed and is not a relay origin. An allowlisted Dell is not a substitute when status says the MAIN is the Zenbook.

Until `activate(expected_mode_version, legacy_phone_fenced=True)`, every phone route is `503 legacy_retained` and `status()` is not called. Activation is RAM-only. `legacy_phone_fenced=True` is a caller claim. `activate()` records `proved: false`. `last_main` and `preferred` are not routes.

Phone kinds `ask`, `session_start`, `session_input`, `session_stop`, and `logout_end` are checked and then refused `body_not_relayable` (503). They are not inserted and not relayed. The prompt is not hashed onto disk. Metadata requests may contain only `attempt_id` and an optional `effect_class` identifier. `effect_class` is in the local digest and is not a gateway field. Relay runs only when mode is `V2_ACTIVE`, `main_ready` and `lease_valid` are true, the lease holder matches, and that device is in `allowed_mains`. Stored `forwarded` means the metadata notice was accepted. `effect_authorized` stays false. The same operation id is not sent again. A different digest is 409. `no_relay`, a crash, a non-dict result, `effect_authorized: true`, or a result carrying `path`, `url`, or `reply` is stored `uncertain` and is not sent again.

`manage_main_preview` does not call the gateway. `manage_main_confirm` checks a passkey locally, drops the receipt, and calls `transition_request` only with a fresh `OwnerProof` and `client_key`, `action`, `candidate`, `expected_incarnation`, `expected_epoch`, `expected_policy`, `profile`. A phone `manage_main` grant does not make the phone an owner. Confirm without a minter that returns `OwnerProof` does not change MAIN.

`PhoneProtocolAdapter` does not serve the shell (`404 shell_not_ingress`). `/phone/api/locate` is 404. The current phone page sends `X-JARVIS-Phone: 1` and a same-origin cookie. It does not send `X-CSRF-Token`. After a gateway pair, a cookie without the CSRF header is 403. A missing cookie is 401 with `pair: true`. `__Host-jarvis_phone` does not migrate across origins. Logout revokes the local phone row and returns `ending: local_only`. It does not tell the PC. `503` and `409` do not include a job id.

`accept_gateway_phone` is not implemented. Do not add it to `phone.py` until a reviewed body channel exists. A reverse proxy onto `phone.py` is the wrong seam: the gateway hop's Tailscale whois is the gateway, and the cookie is a different origin. The future function must refuse a raw cookie and `X-Forwarded-For` from that hop, require `role == phone`, `manage_main == false`, and `opener == phone:{id}`, and run `ExecutorGuard.accept` before any effect.

Ingress SQLite is separate from the authority database (WAL, FULL, mode 0600, exclusive `.writer` lock). Columns are ids, hashes, pairing state, and the metadata digest. No prompt, reply, cookie, token, frame, or event column.

Ingress HTTP allowlist `_GATEWAY_HTTP` copies gateway statuses for `bad_request`, `forbidden`, `not_found`, `ticket_expired`, `stale`, `busy`, `key_reused`, `conflict`, `not_quiet`, `lease_live`, `quarantined`, `no_relay`, `unavailable`, `writer_busy`, plus its own `authority_unavailable` 503. A code outside that map, including an unauthorized gateway code, is 503.

`integration.CONTRACT_HTTP` / `EXTRA_HTTP` are a third map. Extras such as `personal_field`, `body_not_relayable`, `bad_passkey`, `not_manage_main`, and `legacy_unwired` exist only there. The three maps stay separate because the allowlists differ. Gateway status wins for a code the gateway speaks.

## Executor

`ExecutorGuard` is the target-side fence. It cannot authorize work by itself. Tests may inject `AuthorityStore` directly. That store trusts its caller. Production must inject an authenticated target client. A missing client is `no_authority` / `no_verifier`. There is no flag that skips the signature or the consume.

`accept(signed_operation, admission_ticket, sink)` calls the zero-argument sink only after the `ACCEPTED` receipt is fsynced. The sink receives no operation fields. Its return value is discarded. The caller does not run the effect again after `accept` returns.

Required operation keys: `authority_id`, `fleet_id`, `main`, `incarnation`, `epoch`, `mode_version`, `policy_version`, `target`, `operation_id`, `attempt_id`, `digest`, `effect_class`, `issued_at`, `admit_until`, `issuer_incarnation`, `admission_id`, `signature`. Optional `session_id` only for `effect_class="interactive"`. The ticket is exactly `TICKET_FIELDS` and every field must match the operation. Unknown keys are `bad_request` and the error does not name them. `effect_class` is `ordinary`, `pure_compute`, or `interactive`. The verifier must return exactly `True`. The signature is not stored. The journal keeps `token_hash`. Identifiers match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`. Digests are `sha256:` plus 64 hex digits. Lifetime must be inside `(0, 24h]`. Issuance more than 5 seconds ahead is `bad_ticket` and is not stored.

`effect` on the return is `completed`, `uncertain`, `released`, or `not_run`. `already` means this `(target, operation_id, attempt_id)` was already journaled. A resume that starts the sink for the first time still returns `already=True`. Callers read `state` and `effect`. They do not start a second sink.

Legacy sinks run while the journal is missing (unmanaged) or while the last durable mode is `LEGACY_ACTIVE` or `PREPARING_V2`. `QUIESCED` and every later mode close legacy inside `fence()`, and the close is written before `acknowledge`. A failed acknowledge does not roll the local fence back. The guard does not poll fleet mode. A status read must not close legacy. `fence(..., reopen_legacy=True)` applies only when authority `status()` is already `LEGACY_ACTIVE` at a strictly newer `mode_version`. Anything else is `bad_edge`.

`managed=True` and a missing file quarantine the process. The path is not created. First enrollment is an unmanaged `fence()` that creates the file.

Accept order: quarantine, parse, require a closed legacy fence plus client and verifier, verify the signature, then dedup. The same attempt with the same token returns the prior record. A different digest, token, issuance, expiry, or session is `conflict`. Another attempt is `in_progress` while a row is `PREPARED`, `CONSUMED`, `ACCEPTED`, or `UNCERTAIN`. After `COMPLETED`, a new attempt is allowed only when both rows are `pure_compute`. A tuple below the high-water mark, or at or below the fenced pair, is `stale` with no network call. A window the wall clock says is over is pinned `EXPIRED` and is not consumed. If `admission(id)` already exists and this journal has no row, insert `UNCERTAIN` and do not run. Otherwise insert `PREPARED`, read `status()` online, consume, fsync `ACCEPTED`, call the sink, then `COMPLETED`. A newer status tuple is persisted from `status()`, never from the ticket. Mode other than `V2_ACTIVE`, or `main_ready` / `lease_valid` false, is `no_admission` and leaves `PREPARED`. A sink that calls back into the guard raises `busy`.

| Durable state | Resume |
| --- | --- |
| No row | Nothing ran. A later valid ticket may consume. |
| `PREPARED` | Consume has not happened. Resume continues at status and consume. After a handover the resume is `stale` and the consume count stays 0. |
| `CONSUMED` | The effect has not started. Resume may call the sink once. |
| `ACCEPTED` or `UNCERTAIN` | The effect may have started. Never call the sink again. `reconcile` reports `uncertain`. |
| `COMPLETED` | Identical retry returns `already=True` and does not run. |
| `EXPIRED` / `REJECTED` / `RELEASED` | The original refusal stands. |

A sink exception becomes `uncertain` with a fixed message. The exception text is not attached. `reconcile` reads the local row only and works while Oracle is down. `stop_interactive` writes a session release. It does not consume, does not insert a receipt, and does not press keys. If the journal is quarantined, `stop_interactive` raises, and the OS sink still releases input.

The journal is a separate SQLite file (WAL, `synchronous=FULL`, exclusive `<path>.writer` lock, `BEGIN IMMEDIATE`, schema id `1`). `meta.authority_schema` must equal `authority.SCHEMA_VERSION`. Receipts are keyed by `(target, operation_id, attempt_id)` with `UNIQUE(admission_id)`. Hash mismatch, schema mismatch, a foreign installation pin, or an unknown receipt state quarantines the journal. Quarantine does not fall back to legacy.

`acknowledge` can send only the integer `fenced_epoch` (the store takes a MAX). The journal stores the `(incarnation, epoch)` pair and refuses `tuple <= fenced pair`. A later store that reset epoch below a previously acknowledged integer would disagree with this journal.

## Runtime

`MainRuntime` holds a lease. It is not operational failover and it never calls `make_main_now`. Failback requires at least 30 seconds of stability. `release_before_suspend` and `invalidate_after_resume` drop cached authority. Sleep does not promote the Dell: the phone listener on this fleet binds `127.0.0.1:7787` on the Arch PC. Renewal hooks renew only an already accepted, bound lease. They never request a lease, elect, recover, or promote.

`CachedAuthority` is a cache. It is not the store and not the runtime. Linux uses `CLOCK_BOOTTIME` for cached validity. Windows and macOS pass `clock=None` and must validate online. Windows `remaining()` is 0 on purpose.

## Do not weaken

- Do not drop the ticket signature to fit `ExecutorGuard` onto `AuthenticatedPort`.
- Do not remove `secret` from `integration.PERSONAL`.
- Do not collapse `grant` and `complete`.
- Do not treat a metadata relay and a transient body relay as one protocol. `_relay` stays a fail-closed trap.
- Do not share ingress SQLite with the gateway RAM relay.
- Gateway `not_found` stays 404. Ingress unknown codes stay 503.
