# Operational integration

Status: **built and tested on loopback with the real components. Not deployed. Not verified on Oracle or the Dell. This is not live failover.** Every gate in `ContinuityIntegration.blockers()` is still false.

## What is real in the test fleet

`jarvis.continuity.integration.IsolatedFleet` builds a temp-directory fleet from the production classes only:

| Piece | Real class |
|---|---|
| authority | `AuthorityStore` behind `ContinuityGateway`, on a loopback socket, with `ssh-keygen` ed25519 device and owner keys |
| PC clients | `AuthorityClient` (signed device requests), wrapped by `GatewayPort` / `AuthenticatedPort` |
| MAIN admission | `MainRuntime.require_authority` (Arch: suspend-aware cache clock; Dell: `clock=None`, so every call validates online) |
| effect guard | `ExecutorGuard`, fed by `SignedAuthority` (real `TICKET_NS` signature checked against the enrolled MAIN key; `consume` sends that same signature to the gateway) |
| phone | `MobileIngress` + `ContinuityGatewaySession` + `PhoneProtocolAdapter` |

Stand-ins, and only these: a `Clock`, and a loopback HTTP "MAIN relay" sink per PC (`PcNode`) that does what the future MAIN-side relay endpoint must do: check `require_authority`, sign a ticket as MAIN, call `ExecutorGuard.accept`, then run a zero-arg sink. The old doubles (`LoopbackGateway`, `LocalExecutor`, `PcRuntime`, a second `MobileIngress`) are gone; `peer_report()` now probes behaviour, not names.

## Behaviour covered (tests/continuity)

- `test_operational_e2e.py`: Arch sleeps → Dell promoted after the lease wait + fence margin → same phone cookie keeps working → effect runs once on Dell (replay does not rerun) → stale Arch refused by runtime, relay and authority. Plus reboot/partition/gateway-restart without replay, canary failure leaving no MAIN and no effect, auth gaps not opening a transition or extending a lease, personal text absent from every persisted byte and the audit log, second writer / live-state refusal, default executor refusing with no verifier or a tampered signature, Dell online validation, and the 2 s placeholder margin.
- `test_transient_relay.py`: `ContinuityGateway.forward_transient` — effect once, stored repeat not re-posted, conflict on changed body/target/attempt, validation before any row, target must be the ready MAIN, overclaims/lost answers never resent, reads not remembered, text never stored.
- `test_launch_hooks.py`: the opt-in contract below.

## Phone relay (Ask, overview, frames)

`MobileIngress` → `ContinuityGatewaySession.relay_transient` → `ContinuityGateway.forward_transient` → allowlisted ready MAIN's `relay_origin`.

- Authenticated: a signed device request; the phone cookie/CSRF stays at the ingress.
- Bounded: envelope 1..256 KiB; effects keep a 1 KiB reply limit; reads ≤ ~4 MiB; at most `MAX_RELAYS` remembered effects, in RAM only.
- Transient: the gateway keeps digest + ids, never the text; audit rows are method/route/status/code/device only. The ingress stores digest and ids, not text.
- No retry of ambiguous effects: a lost, odd or over-claiming answer marks the operation uncertain/`no_relay` and a repeat returns that outcome without a second POST. The upstream may only say `{operation_id, accepted}`; the gateway reports `effect_authorized: false` itself.
- `relay` is advertised to the phone only when the ingress has a transient relay and a ready MAIN exists (`/phone/api/me` → `relay`, `metadata_only`).
- The test relay is plain loopback HTTP (`pin_relay` refuses anything else). A tailnet TLS relay is **not** built or verified.

## Opt-in launch contract

Nothing changes in the running legacy path. Each surface imports `jarvis.continuity.integration` lazily; if the import fails, legacy proceeds. Gates are RAM only, default open.

| Surface | Gate | After `close_unfenced(...)` |
|---|---|---|
| `jarvis cluster main` (`cluster_cli.set_main`) | `set_main` | `IntegrationError("legacy_retained")` before any write |
| `cluster.execute_local` | `cluster_local` | refused |
| `Hub.dispatch` | `hub_dispatch` | refused |
| `Remote.call` | `remote_call` | refused |
| `rest.sleep` | `sleep` | refused before `pause()` |
| phone listener (`PhoneHandler._handle`) | `set_phone_listener_open(False)` | 503 `legacy_retained` |
| node seen-file (`NodeWorker._untrusted`) | `set_node_seen_mode("after")` | records the job only after success |

A deployment must do, in this order, and only after every blocker below is cleared: quiesce legacy (`close_unfenced`), walk the store `PREPARING_V2 → QUIESCED → V2_ONLY_NO_MAIN` with every participant acknowledging (`ExecutorGuard.fence`), enrol devices, promote, start `MainRuntime`. `rest.sleep` still needs `MainRuntime.release_before_suspend()` wired in before automatic failover could be considered. Rolling back means `open_unfenced()` and `rollback_quiesced`; nothing here persists across a restart.

## Deployment prerequisites (none verified)

1. A real gateway host (Oracle) running `ContinuityGateway.open(...).serve()` behind a TLS terminator, with its own ssh-keygen device keys enrolled per PC / ingress, and the owner key off the gateway host. Today it is only exercised on 127.0.0.1.
2. A MAIN-side relay endpoint on each PC (the `PcNode.handle_relay` contract) reachable from the gateway at the enrolled `relay_origin`, over a transport `pin_relay` allows. The phone listener currently binds 127.0.0.1:7787 on whichever PC is awake.
3. A WebAuthn passkey verifier and owner verifier for `MobileIngress` (`passkey_unavailable` today).
4. The Dell's AI profile (Luna +5, Grok 4.7) verified as `ai_ready`, not just named.
5. A measured suspend margin. `SAFETY_MARGIN` is the 2 s placeholder; Arch wake-up behaviour has not been timed.
6. A tested Windows suspend-aware clock. Until then the Dell must run `clock=None` (online validation each call), which the tests assert.
7. Legacy paths fenced for real (`LEGACY_PATHS`): `cluster main`, hub dispatch, phone listener, `rest.sleep`, fleet main transfer (`fleet_management.py` is not hooked).
8. Automatic failover remains disabled; promotion is owner-driven.

Known authority gaps, unchanged: no `CATCHING_UP`/`VERIFYING` store states; the store trusts its caller (authentication lives in the gateway); `not_found` is 404 and `unknown_participant` is 403 in the HTTP map.

## Not done

No live config, state or vault change; no enrolment on real machines; no service restart or new server; no cutover. `jarvis cluster update` is not needed for the node surfaces because the hooks are default-off, but nodes compare code, so run it after the parent commits.
