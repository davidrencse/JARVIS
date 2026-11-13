# Continuity redundancy

*Pairs that look duplicated and must stay separate, and the few that already have one owner.*

Module behavior is [continuity-runtime.md](continuity-runtime.md). The effect-sink inventory is [continuity-contract.md](continuity-contract.md).

## Already one owner

`authority.TICKET_FIELDS` is the only ticket field tuple. Gateway, runtime, executor, and integration import it. Each still validates values itself. Do not copy the tuple back into four literals.

`MAX_SIGNATURE` is 1800 in `authority.py`. Every ticket-signature boundary uses that cap. `GatewayPort` refuses a longer signature before I/O.

Gateway `HTTP_STATUS` is the source for any code the gateway speaks. Ingress and integration copy from it. They do not invent a second meaning.

Phone fleet projection has one owner: `phone.py` redact, `project_step`, `request_usage`, and `Trace`.

Archive loading has one owner: `SecondBrain._load` and `_assemble`.

Config mutation has one owner: `config_lock.config_mutation` / `patch_config` / `save_delta`. Fleet and settings are adapters. See [fleet-api.md](fleet-api.md).

Presence vocabulary for cards is `connections.presence`. Do not schedule a second merge of phone liveness onto that function. `link_state()` stays the invite and enrolled guard on purpose.

The historical `LoopbackGateway`, `integration.MobileIngress`, `LocalExecutor`, and `PcRuntime` doubles are gone. `IsolatedFleet` uses the real classes. Do not describe those doubles as live, and do not reintroduce a shared-secret identity to make an old test pass.

## Do not merge

Three HTTP maps stay separate. Gateway `HTTP_STATUS` is the full gateway set. `integration.CONTRACT_HTTP` plus `EXTRA_HTTP` adds integration-only codes (`personal_field`, `body_not_relayable`, `bad_passkey`, `not_manage_main`, `legacy_unwired`, and the rest of that map). Ingress `_GATEWAY_HTTP` is a smaller allowlist plus `authority_unavailable`. Unknown gateway codes on the ingress become 503. Collapsing the maps would either leak integration-only codes onto the gateway or hide an ingress refusal inside a store code.

`GatewayPort`, `AuthenticatedPort`, and `SignedAuthority` are not interchangeable. `AuthenticatedPort.consume` requires a ticket signature. `ExecutorGuard.accept` still calls consume without one. The mismatch fails closed. Do not drop the signature to unify them.

`AuthorityStore`, `CachedAuthority`, and `MainRuntime` protect different jobs: the writer, the cache, and the lease holder. Merging them would let a cache grant a lease.

`ExecutorGuard` and `AuthorityStore.consume` protect different windows. The journal fences the local effect. The store consumes the admission. A crash between them is the reconcile table in [continuity-runtime.md](continuity-runtime.md), not a reason to use one record.

`MobileIngress` SQLite and the gateway RAM relay are different protocols. Metadata relay and transient body relay stay different. `_relay` remains a fail-closed trap. Ingress must not grow a second socket for prompts, frames, or cookies.

`integration.PERSONAL` and the gateway forbidden-key set differ on purpose. `PERSONAL` includes `secret`, `passkey`, `cookie_value`, `workflow`, `input`, and `payload`. The gateway set includes `url`, `host`, `password`, and `payload_body` and does not list every personal key. Do not remove `secret` from `PERSONAL`. Do not teach the gateway to accept a stand-in identity dict.

Three receipt authorities stay separate. The invite fence is `state/cluster-invite-receipts.json`. Admission consumption is `AuthorityStore.consume`. The local effect fence is the `ExecutorGuard` journal. `Broker.finish` is a job ack, not a receipt. An uncertain invite blocks that node id. An uncertain executor row blocks that attempt. They are not the same fence.

Launch-hook helpers are live and opt-in. The default is open. They are not the production fence. Do not delete them because production wiring is incomplete, and do not treat installing them as cutover.

Provider `Failover.ask` may repeat a pure model request only before a tool starts. That is not continuity replay. Continuity does not blind-replay an effect. Do not funnel one through the other.

`lan_mouse_status` and `_quick_status` are different cost and accuracy contracts. `start_blocked` and `pair_blocked` differ in strictness. Do not merge them into one probe.

`cluster.save_json` writes `cluster-node.json`. It is not `patch_config` and it is not the managed user config.

`Roles` describes a machine. It grants nothing. `Remote.call` uses `Roles.execution_target`. A descriptive role map is not an admission ticket.

Continuity modules are an active prototype. Incomplete production wiring does not make them orphans and does not make them the live MAIN. `cluster.main_device` remains the unfenced role until the gates in [main-handoff.md](main-handoff.md) close.

No production deletion of a live path was confirmed by the redundancy review. A "duplicate" that still has a caller stays until that caller is moved on purpose.
