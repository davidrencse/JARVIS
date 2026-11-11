# Continuity: next implementation slice (H2 authority prototype)

Date: 2026-11-11. Status: **isolated prototype, not activated, not end-to-end.** Nothing in the
running JARVIS imports `jarvis/continuity/`. `cluster.main_device` remains the live (static,
unfenced) MAIN. No gateway is provisioned, no config/state/vault changed, no service restarted.

## Why sleeping Arch does not promote Dell today (confirmed gaps)

| Area | Today | Consequence |
| --- | --- | --- |
| Mobile ingress | `jarvis/phone.py` binds `127.0.0.1:7787` on Arch, published by Arch's `tailscale serve` under Arch's tailnet name | Arch asleep/off = phone URL dead. No other host can answer that name. |
| Pairing/session | Phone cookie + pairing records in Arch-local `.jarvis/phone.json`, bound to Arch's Host/whois | Dell cannot verify the phone even if reached; re-pairing at Dell's terminal is the only path. |
| Authority | `cluster.main_device` static config; `fleet_management.main_transfer` always `blocked` | No lease, epoch, fencing or transition. Editing config would create two MAINs once Arch wakes. |
| Executor fencing | Nodes accept any signed job from the configured main | Nothing makes a woken Arch's old work refused. |
| Recovery | None | No safe "no MAIN" state; no ambiguous-replay reconciliation. |

A stable gateway on a host that stays up (Oracle in the design) is a hard prerequisite: no
code change on Arch can make Arch's URL reach Dell while Arch is off.

## What this slice adds

- `jarvis/continuity/authority.py` — `AuthorityStore`: single SQLite writer (WAL, FULL sync,
  exclusive `<db>.writer` OS lock), one `BEGIN IMMEDIATE` CAS per mutation. Implements:
  - fleet mode machine (`LEGACY_ACTIVE → PREPARING_V2 → QUIESCED → V2_ONLY_NO_MAIN`, rollback via
    `ROLLBACK_QUIESCED`), entering V2/legacy only after every eligible participant acked;
  - lease held only in RAM on the authority's monotonic clock (15 s validity, 5 s renew); a lapsed
    lease is never revived; restart = new incarnation + epoch bump, all leases void, grant
    barrier of max-ever-validity + margin (acks cannot shortcut it), open transitions FAILED;
  - transitions `make_main_now | set_preferred_main | recover` (idempotent on caller+client_key,
    CAS on incarnation/epoch/policy): REQUESTED → VALIDATING (readiness booleans, `control`/`ai`
    profile) → DRAINING (renewals + admission stop) → FENCING (epoch+1 only when old MAIN and all
    eligible participants acked, or old validity + margin elapsed) → GRANTED_NOT_READY (canary,
    no admission) → SUCCEEDED. Failure/cancel after fence (or after any target acked) leaves
    **no MAIN** at a fresh epoch; never resurrects the old lease;
  - planned `release()` (pre-suspend hook) → `V2_ONLY_NO_MAIN`;
  - admission `consume()`: once per (target, operation, attempt) under the exact ready tuple;
    identical retry returns the original record even after handover; different digest/admission
    = conflict; expired ticket refused before lookup; old-incarnation tickets refused; target must
    have acked the current mode; `admission()` is read-only reconciliation;
  - metadata only: identifier/`sha256:` validation, no free-text or body columns; unknown schema
    or foreign `authority_id` → QUARANTINED.
- `jarvis/continuity/clock.py` — `CachedAuthority`: PC-side cached validity anchored at request
  start, Linux `CLOCK_BOOTTIME` only (Windows/macOS: no cache, always online), boot-id bound,
  invalidated on reboot/suspend/link loss/newer tuple; stale tuples ignored; not-ready grants
  admit nothing.
- Tests: `tests/continuity/` — 56 tests (43 authority, 13 clock) incl. the reported scenario
  (`SleepRecovery`: Arch stops renewing → Dell recovers only after validity drains; woken Arch's
  tuple refused for renew and admission).

Run: `.venv/bin/python -m unittest discover -s tests/continuity` (also found by `discover -s tests`).

## Integration contracts for parent (not done here)

1. **Gateway service** (new, Oracle): owns exactly one `AuthorityStore(path, fleet_id,
   authority_id)` per process; exposes authenticated HTTP for `status`, `renew`, `release`,
   `acknowledge`, `request/readiness/drain/fence/grant/complete/cancel`, `consume`, `admission`.
   Must authenticate device identity (enrolled installation keys) and owner `manage_main` proof
   *before* calling the store — the store trusts its caller. Map `AuthorityError.code` → HTTP
   (`stale/busy/key_reused/conflict`→409, `not_quiet/lease_live`→409 + `Retry-After=wait`,
   `bad_request/bad_ticket`→400, `quarantined`→503, `ticket_expired`→410).
2. **Phone ingress** moves to the gateway's stable HTTPS origin; pairing verifier and session
   cookies live there (metadata only); requests are relayed to whichever PC `status().main` names.
   `phone.py` stays the per-PC backend behind the relay.
3. **MAIN runtime** (`fleet_management.main_transfer`, `cluster.py` hub): replace
   `cluster.main_device` reads with authority status; renew every 5 s via `CachedAuthority`;
   stop new work when `remaining()==0`.
4. **Sleep hook** (`jarvis/rest.py:185 sleep()`, before suspend): call `release()` and have each
   participant `acknowledge()`; on resume call `CachedAuthority.invalidate("resumed")` and go online.
5. **Executor guard** (node job intake, local effect sinks): require a consumed admission under
   current tuple; persist highest acked `fenced_epoch` durably; refuse older epochs after reboot.
6. **Auto-recover policy** (H7): watcher opens `recover` for the preferred eligible PC after lease
   loss; shadow mode first.
7. Register `continuity` in `jarvis/sysview.py` stations when anything imports it (sysview only
   globs top-level `jarvis/*.py`, so the subpackage does not trip `test_every_module_belongs_to_a_station` now).

## Remaining deployment requirements (owner decisions / ops)

- Provision the stable gateway host (Oracle not assumed provisioned), TLS name, backup, and
  operator procedure that the old authority copy cannot run when restoring (single writer).
- Enrollment of device installation keys; passkey/owner proof for `manage_main`.
- Measured margin (`SAFETY_MARGIN=2.0` is a placeholder) and real suspend/resume drills on Arch.
- Dell (Windows) has no tested suspend-aware clock adapter: it must validate online per action.
- Legacy-path inventory and disable (MAIN-HANDOFF H0/H1) before V2 activation; then H3 cutover.
- State catch-up (personal data in Obsidian vault) for Dell as MAIN is separate (AI-ready profile).

Not claimed: failover, mobile continuity, or that Dell becomes MAIN when Arch sleeps. This slice
is the authority/fencing kernel those features need.
