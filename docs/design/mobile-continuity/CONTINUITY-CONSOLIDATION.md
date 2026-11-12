# Continuity consolidation

Scope: the confirmed duplicates in `LUNA-CONTINUITY-REDUNDANCY.md`, plus the signature-cap mismatch. `LUNA-PROGRAM-REDUNDANCY.md` found no safe production deletion; its one candidate is dispositioned below. Nothing was committed, restarted or written live.

## Deleted / consolidated

| Was | Now |
|---|---|
| `_TICKET_FIELDS` copied in `gateway.py`, `runtime.py`, `executor.py`, plus `_CONSUME` in `integration.py` (four identical 11-tuples) | One `TICKET_FIELDS` in `authority.py`; the four modules import it. Order is unchanged (it is what MAIN signs). Each boundary keeps its own value validation and error type. |
| `MAX_SIGNATURE = 1800` in `gateway.py`, literal `1800` in `executor.py`, literal `4096` in `runtime.GatewayPort.consume` | One `MAX_SIGNATURE` in `authority.py`, used by all three. |
| `ingress._AUTH_STATUS`, `ingress._GATEWAY_HTTP`, `integration.CONTRACT_HTTP` and the gateway-shared half of `integration.EXTRA_HTTP`: literal status numbers repeating `gateway.HTTP_STATUS` | Built from `gateway.HTTP_STATUS` through per-boundary code allowlists. Verified identical to the old literals (every entry of all five maps compared before/after). |

## Bug fixed: signature cap mismatch

`GatewayPort.consume` accepted signatures up to 4096 characters and non-ASCII text; the gateway refuses anything over 1800 or non-ASCII (`_signature`). A 1801–4096 character signature went to the wire and failed late. The adapter now refuses it as `RuntimeRefusal("bad_request")` before any client I/O. `ExecutorGuard` already capped at 1800 (it does not require armor, so short test signatures still work).

## Deliberately kept

- `ingress._from_gateway` collapses unknown gateway codes and `unauthorized` to `authority_unavailable` 503: the phone must not learn gateway-internal codes. `authority_unavailable` stays local to the ingress.
- Integration-only codes (`personal_field`, `unauthenticated`, `bad_passkey`, `not_manage_main`, `not_owner`, `not_candidate`, `signature_unverified`, `admission_stopped`, `ai_not_ready`, `v2_inactive`, `legacy_unwired`, `refuses_live_state`) stay literal; the gateway does not speak them.
- Runtime vs executor vs `AuthenticatedPort` adapters and their error types (`RuntimeRefusal` vs `AuthorityError`), executor `_OP_REQUIRED`/`_TOKEN_FIELDS`, the three forbidden/personal key sets (differences not audited, so not merged), gateway metadata relay vs `forward_transient`, `MobileIngress._relay` fail-closed trap, and all launch-hook helpers (live callers and tests).

## Phone liveness (`phone.py` vs `connections.presence`): not changed

`phone.py` derives `online` from the broker row (`cluster_store.py:234`, which uses `time.time()`) and calls `presence(seen, busy, self.clock())` only for the not-online case, with an injectable clock. Replacing it with `presence()` would move the online decision from wall time to the injected clock, so equality is not demonstrable. The no-broker `shown` fallback and `presence=None` meters rule would also need to survive. No focused test could prove preservation, so nothing was changed.

## Tests

New `tests/continuity/test_contract_consolidation.py` (11 tests): tuples are the same object and equal the store's `consume` parameters and the signed order; every boundary's status matches the gateway map, key values and integration-only codes are pinned; ingress maps gateway refusals to gateway statuses and unknown codes to 503; the cap has one owner; the gateway edge (1800 ok, 1801 / non-ASCII / no armor refused); the real runtime port refuses over-cap and non-ASCII signatures with no call recorded on the real client, and forwards one at the cap; the executor refuses 1801 and empty with no sink and no consume, and lets one at the cap reach verification. The port test fails with the old 4096 check (confirmed by temporarily restoring it).

Results: `tests.continuity` discovery 192 tests OK (includes the 11 new).

## Parent suite evidence (`/tmp/jarvis-sonnet-composed-integration.log`)

3717 tests, 1 failure, 7 skipped, 1 expected failure. Not all green. The one failure, `tests.remote.test_remote_kinds.HubRouteTests.test_target_caps` (line 500: expected `(True, None)`, got "Runs older JARVIS code. Run: jarvis cluster update node"), is a race with this cleanup, not a regression. `Hub.target_caps` compares the node's heartbeat fingerprint with `release.served_code(state)`, which is `code_fingerprint()` of the live `jarvis/` tree when no release exists. The test heartbeats, then asserts; my edits to `jarvis/continuity/*.py` landed between 23:15:42 and 23:17:03, inside the suite window (it ran 320 s and ended 23:17:33), so the fingerprint changed mid-test. Focused reruns on a quiet tree: the test alone 4/4 OK, and the whole `test_remote_kinds` module (33) OK. The fixture and the code guard are unchanged and correct. Any file edited under `jarvis/` while a suite runs can do this; the parent suite should be rerun on a quiet tree to get the full-scope result.

## Files edited after the parent suite started (about 23:12)

`jarvis/continuity/authority.py`, `gateway.py`, `executor.py`, `runtime.py`, `ingress.py`, `integration.py` (the consolidation; `runtime.py` was also briefly reverted and restored as a proof step), `tests/continuity/test_contract_consolidation.py`, this document.
