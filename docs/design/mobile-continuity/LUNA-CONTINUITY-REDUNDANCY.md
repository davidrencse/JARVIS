# Luna audit: continuity redundancy and duplicate logic

Status: read-only audit of the current worktree. No production code was changed. The source line numbers below refer to the inspected snapshot and may shift as Sonnet continues editing.

## Findings

### 1. HTTP refusal maps have three authorities

- `jarvis/continuity/gateway.py:68-83` owns `HTTP_STATUS` for gateway HTTP errors.
- `jarvis/continuity/integration.py:60-105,180-184` independently defines `CONTRACT_HTTP`, `EXTRA_HTTP`, and a merged `HTTP_STATUS` consumed by `IntegrationError` / `http_status`.
- `jarvis/continuity/ingress.py:126 onward, around 675-681` owns `_GATEWAY_HTTP`, which maps gateway errors into phone ingress responses; `_submit_effect` / metadata submit also read it directly (`1127`, `1206`).

This is a confirmed maintenance duplication: same stable refusal codes are mapped at three layers, with distinct fallbacks and response types. The current integration map has extras such as `personal_field` and `refuses_live_state`; gateway HTTP intentionally does not need all of those. The phone adapter also has ingress-only codes and user-facing bodies, so it should not blindly alias the entire table.

**Recommendation:** make the gateway map the single source for gateway refusal status, and have ingress import/use that mapping for codes originating at the gateway while keeping ingress-owned HTTP codes local. Keep `IntegrationError`'s integration-only codes in a separate map or remove the compatibility mapper if no supported caller remains. Define the fallback at each boundary explicitly (gateway unknown code → 503; phone exception retains its ingress status). Do not merge semantically different code sets just to reduce line count.

**Acceptance:** table-driven checks show every forwarded `GatewayError` uses the gateway's status in ingress, including `not_found` 404, `unknown_participant` 403, `no_relay` 503, and an unknown code 503; integration-only `legacy_unwired` / `refuses_live_state` behavior remains stable if `http_status` stays public.

### 2. Authority ticket field lists are copied across four layers

- Canonical store ticket fields are declared in `jarvis/continuity/authority.py` (ticket validation/consume path; inspect `_int` / `_num` callers and `consume`).
- `jarvis/continuity/gateway.py:93-97` defines `_TICKET_FIELDS`, then `ContinuityGateway._ticket_fields` at `917-926` validates and projects them.
- `jarvis/continuity/runtime.py:47-50` defines `_TICKET_FIELDS` for `GatewayPort.consume`.
- `jarvis/continuity/executor.py:25-31` defines `_TICKET_FIELDS` and related signed operation/receipt tuples for the local journal.
- `jarvis/continuity/integration.py:164-165,405-407` defines `_CONSUME` and projects it in `_ticket_fields`; `SignedAuthority` uses it at `443-473`.

Some copying is necessary at trust boundaries: gateway JSON must be exact, executor receipts persist a distinct record, and the MAIN signature excludes authority metadata by design. But the repeated ticket *field-name tuple* is a drift hazard, especially because the runtime, gateway and integration adapter must agree on the exact same signed consume payload.

**Recommendation:** consolidate only the shared immutable consume/ticket field-name tuple in a small contract module (or export a narrowly named constant from `authority.py` if import layering permits). Keep each boundary's independent shape/type validation and keep executor receipt fields separate. Do not share parsed dict objects or let a helper normalize signed numeric values: gateway signature verification must preserve original JSON number representation, while executor admission uses normalized numeric validation.

**Acceptance:** one source owns the shared ticket field order/set; gateway, runtime adapter, SignedAuthority and store tests prove missing/extra keys fail closed and signatures still verify the original numeric representation. Executor's larger receipt shape remains explicitly separate.

### 3. Port overlap is real, but the two adapters are not interchangeable

- `jarvis/continuity/runtime.py:288-415` `GatewayPort` translates `AuthorityClient` errors to runtime exceptions, binds renew/acknowledge to the client identity, validates recovery requests, and requires an SSH-armored signature before forwarding consume (`374-395`).
- `jarvis/continuity/gateway.py:1272 onward` `AuthenticatedPort` provides store-shaped methods for `ExecutorGuard`, validates a bound device client, requires the captured ticket signature, and maps gateway errors to `AuthorityError`.
- `jarvis/continuity/integration.py:427-488` `SignedAuthority` composes `AuthenticatedPort` with MAIN-key verification because `ExecutorGuard.accept` calls `verify` and later calls `consume` without passing the signature.

There is overlap in lease/status/acknowledge/transition forwarding, but consolidation into one generic adapter would blur different exception and signature contracts. A concrete drift exists in consume signature length: gateway `MAX_SIGNATURE` is 1800 (`gateway.py:48,242-245`) while `GatewayPort` accepts armor up to 4096 (`runtime.py:386-389`), so it can pass a signature known to be too long for the receiver and surface a later gateway refusal.

**Recommendation:** retain the separate adapter roles and `SignedAuthority`; align the preflight length cap in `GatewayPort` to gateway `MAX_SIGNATURE` (or make the mismatch explicit in a shared protocol constant). Do not move ticket verification into `GatewayPort` or make `ExecutorGuard` require armor; executor tests and verifier abstraction intentionally permit short fixture signatures.

**Acceptance:** over-limit signatures are rejected before client I/O at both the adapter and gateway; valid armor proceeds; short test signatures remain supported by `ExecutorGuard` when a test verifier accepts them; error types stay `RuntimeRefusal` for runtime and `AuthorityError` for executor paths.

### 4. Gateway metadata relay and transient relay are separate protocols, not duplicate code

- Metadata relay: `ContinuityGateway.relay` at `gateway.py:716-772`; `ContinuityGatewaySession.relay_metadata` at `ingress.py:461-470`; `MobileIngress._submit_metadata` (around `1200`) sends only five metadata fields.
- Transient path: `forward_transient` at `gateway.py:774 onward`; `ContinuityGatewaySession.relay_transient` at `ingress.py:472-483`; `MobileIngress.read` / `_submit_effect` at `986-1019` and `1086-1152`.
- Legacy `MobileIngress._relay` at `ingress.py:1489-1492` always refuses a body relay.

The methods share a route and some operation identity handling, but their guarantees differ. Metadata relay accepts no envelope and returns `effect_authorized: false`; transient relay sends a digest-bound body in RAM, remembers effect outcomes only, never retries an ambiguous effect, and leaves reads unremembered. `_relay` is an intentional fail-closed compatibility trap, not an alternate path.

**Recommendation:** keep the two gateway operations distinct, and retain `_relay` while any callers can reach it. Consider extracting only private common relay-state/result helpers if it reduces duplicated invariants without making the metadata path accept an envelope. Do not unify identity semantics: metadata idempotency includes capability; transient identity also includes kind.

**Acceptance:** tests independently demonstrate metadata cannot carry an envelope; transient digest mismatch fails before forwarding; effects are never reposted after uncertainty; reads are not stored/replayed; `effect_authorized` remains false; `_relay` still fails closed.

### 5. No confirmed unused launch-hook compatibility helper

The helpers in `integration.py:807-854` are all live: `refuse_unfenced` is called from `cluster_cli.py:78`, `cluster.py:440,618`, `remote.py:216`, and `rest.py:190`; phone listener state is used by `phone.py:1431`; node seen-mode helpers are used in `cluster.py:1410,1457`. `tests/continuity/test_launch_hooks.py` covers these hooks. The flags are RAM-only and default open, so they are not themselves a continuity authority or persistent fence.

**Recommendation:** do not delete these helpers as dead compatibility code. If the cutover design is later removed, remove each caller and its tests together. Until then, the document and status should continue to call them inactive opt-in hooks, not active fencing.

### 6. Removed integration doubles are absent; historical mentions are not executable duplicates

The current `jarvis/continuity/integration.py` contains `SignedAuthority`, `PcNode`, and `IsolatedFleet` built on the real `AuthorityStore`, `ContinuityGateway`, `MainRuntime`, `ExecutorGuard`, `ContinuityGatewaySession`, and `MobileIngress`. There are no current definitions of `LoopbackGateway`, `LocalExecutor`, `PcRuntime`, or an integration-local `MobileIngress` double. `OPERATIONAL-INTEGRATION.md` says these were removed. Older `GATEWAY-IMPLEMENTATION.md`, `OPERATIONAL-CONTRACT.md`, `EXECUTOR-IMPLEMENTATION.md`, `INGRESS-IMPLEMENTATION.md`, and the older portions of `SONNET-INTEGRATION-HANDOFF.md` still describe them as historical states.

**Recommendation:** no code deletion remains for those names. Avoid rewriting the explicitly preserved historical implementation notes as part of this audit. Where a current-status doc still uses present tense for removed doubles, correct only that narrow status statement.

## Deliberate distinctions to preserve

- `AuthorityStore` is fleet-wide durable authority; `CachedAuthority` (`clock.py:43-117`) is conservative per-process lease belief; `MainRuntime` (`runtime.py:417 onward`) owns local readiness and an admission latch. Similar tuple/lease checks serve different lifetimes and should not be merged into one state machine.
- `ExecutorGuard` (`executor.py:112 onward`) owns the target-side durable fence and effect receipt; `AuthorityStore.consume` owns fleet-side admission. Both have idempotency checks because they protect different failure windows.
- `MobileIngress` local SQLite operation/session state is phone ownership, CSRF, and retry bookkeeping; gateway `_effects` / `_relays` are RAM-only forwarding idempotency. They must not share persistence or body retention.
- The several forbidden/personal-key sets in gateway, ingress, and runtime are boundary-specific allow/deny lists with some intentionally different keys (`events`, `reply`, `raw`, readiness fields). Consolidation is safe only after a set-difference audit and boundary-specific tests; current overlap alone does not prove duplicate policy.

## Audit limits

This pass did not run tests or edit production modules. It inspected the current worktree and the named docs/source paths. Exact lines can drift during Sonnet's implementation; candidates above include caller lists and behavior-level acceptance so they can be rechecked against the final diff.
