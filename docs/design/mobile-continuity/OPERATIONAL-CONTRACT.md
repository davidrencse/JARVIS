# Mobile continuity operational contract

Date: 2026-11-11. Status: implementation contract and deployment decision record; not a
deployment or cutover authorization. This translates `DESIGN.md`,
`CONTINUITY-NEXT-IMPLEMENTATION.md`, `MAIN-HANDOFF.md`, and the existing isolated authority
prototype into concrete boundaries for an operational MAIN/mobile path. The requested
provider profile is **Luna +5 with Grok 4.7**; model names are routing intent only and do not
authorize paid API access. No provider readiness, quota, or endpoint was verified here.

## 1. Current operational truth

- Legacy mode remains active. `jarvis.config.json` metadata identifies `zenbook` as
  `cluster.main_device`, uses the `forward` tunnel, and binds the hub to loopback port 7780.
  The phone service is currently a per-PC service (`jarvis/phone.py`), and the implementation
  notes describe the Arch-hosted phone path as unavailable when Arch is asleep.
- `jarvis/continuity/authority.py` and `clock.py` are an H2 prototype. The running application
  does not import them. No gateway API, authority caller authentication, target receipt,
  executor fencing, or safe role cutover is present as a complete path.
- Existing repository notes identify Oracle as a proposed gateway host, not a verified host.
  Oracle capacity, network/tailnet reachability, installed supervisor, disk/backup, TLS name,
  and active connection are **unknown**. Dell provider-session readiness is also unknown.
  This inspection did not query hosts or read credentials.
- The staged design explicitly retains `LEGACY_ACTIVE` until every old dispatch path is
  quiesced and participants acknowledge the mode. This is a required safety property, not a
  configurable convenience.

Consequently no honest operator instruction can currently say “sleep Arch and Dell becomes
MAIN” or “the phone remains operational.” The code and infrastructure gates below must be
closed before either statement is true.

## 2. Deployment decisions required by the design

These are the concrete defaults to implement and verify; unresolved external facts remain
deployment blockers.

1. **Authority/gateway:** one supervised process and one local SQLite writer on a stable,
   tailnet-reachable Oracle host. The initial design uses one deployable for HTTPS ingress and
   authority. Do not put SQLite on a network filesystem or add a second writer. If the Oracle
   host cannot be verified and provisioned by the owner, stop at isolated testing; do not
   substitute Arch/Dell as a supposedly stable public gateway.
2. **Origin and exposure:** stable HTTPS name and certificate, tailnet-only routing in the
   initial deployment, no Funnel/public listener. Device authentication is mutual installation
   identity; browser/mobile management additionally requires an explicit `manage_main` grant
   and fresh passkey proof. Phone pairing/session metadata may live at the gateway; personal
   conversation, prompt, file, provider, camera, and task bodies may not.
3. **Durability:** authority DB on local durable disk, WAL + FULL sync, restrictive service
   account and filesystem permissions, single-writer lock. Backups are encrypted metadata
   backups, with restore treated as a new authority incarnation and mandatory lease barrier.
   Backup/restore must never revive a lease or automatically re-enable revoked dispatch.
4. **Authority timing:** retain prototype targets of 5 s renewal and 15 s lease only as
   provisional values. `SAFETY_MARGIN=2 s` is explicitly unmeasured. Set production margins
   only after measuring request latency, clock behavior, suspend/resume, process pauses, and
   participant fence completion on all supported PCs. Until Windows has a tested
   suspend-aware clock adapter, Dell validates online for every action; no cached authority.
5. **Recovery:** manual handover/recovery first. No automatic failover/failback at initial
   cutover. A prepared Dell may receive `control` readiness before `ai`; that is a reduced
   profile and UI must say so. The `ai` profile requires provider and private-state readiness.
   If Oracle cannot be reached, no new ordinary command, role grant, or admission is allowed.
6. **Provider selection:** on an AI-ready MAIN, route eligible assistant work through the
   configured Luna +5 lane and Grok 4.7 only for the supported Grok judgement/app lane. Provider
   sessions stay on each PC. Require a local authenticated subscription/session and a
   quota/permission preflight; if unavailable, fail closed or offer an explicitly allowed
   alternate. Never silently invoke a paid token API. No provider subprocess with unmediated
   fleet-effect tools may continue after demotion.
7. **Rollback:** only the `ROLLBACK_QUIESCED` protocol may return to legacy. First stop v2
   admission/renewal, reconcile accepted work, wait out permits, fence and acknowledge all
   reachable participants, and quarantine offline participants. Then authorize one legacy
   MAIN under a new mode version. Never run legacy and v2 authorities in parallel.

## 3. Module/API contracts

The following five modules are the minimum integration boundary under `jarvis/continuity/`.
Names below are proposed Python interfaces; adapt names to repository conventions while
preserving semantics. No module may reach into another module's private DB or bypass the
authority tuple. The gateway and client layers exchange bounded JSON metadata; they do not
accept/store personal bodies in authority tables or logs.

### `gateway.py` — authenticated transport/API boundary

**Owns:** HTTPS routes, device identity verification, owner challenge verification, request
schema validation, rate/size limits, error mapping, and transient relay to current MAIN.
Constructs exactly one `AuthorityStore` at process startup and closes it during orderly stop.

**Required facade:**

```python
class ContinuityGateway:
    def status(self, authenticated_device) -> AuthorityStatus: ...
    def renew(self, authenticated_device, lease_tuple) -> LeaseView: ...
    def release(self, authenticated_device, lease_tuple) -> AuthorityStatus: ...
    def acknowledge(self, authenticated_device, mode_version, fenced_epoch) -> AckView: ...
    def transition_request(self, owner_proof, request) -> TransitionView: ...
    def transition_readiness(self, authenticated_candidate, transition_id, readiness) -> TransitionView: ...
    def transition_step(self, owner_or_policy_identity, transition_id, step) -> TransitionView: ...
    def consume(self, authenticated_main, admission_ticket) -> AdmissionReceipt: ...
    def admission(self, authenticated_device, admission_id) -> AdmissionView: ...
    def relay(self, authenticated_client, operation_id, bounded_request) -> RelayResult: ...
```

**Rules:** authenticate before calling the store; bind every identity to an enrolled
installation key and role. Only a fresh owner proof can request/promote a MAIN. The
`AuthorityStore` is not an authentication system. Bound relays to the design's provisional
metadata/body-free policy and short in-memory lifetime; never spool request bodies, prompts,
provider output, screenshots, or camera frames to disk. Redact bodies and auth material from
access/error logs. Do not relay an ordinary operation if status is no-MAIN, stale, unready, or
authority is unavailable. Preserve caller operation IDs and return status lookup after lost
replies; do not synthesize a retry with a new ID.

**Errors:** preserve stable `AuthorityError.code` values. Map malformed request/ticket to 400,
expired ticket to 410, state conflict/stale/busy/duplicate mismatch to 409, authority
quarantine/unavailability to 503. Include `Retry-After` only when the store supplies a finite
wait. Never return internal exception text or DB paths.

### `ingress.py` — mobile session, pairing, and MAIN relay

**Owns:** stable phone HTTPS origin, pairing/session cookies and CSRF/origin protections,
mobile API compatibility, and request/status/progress relay. It is a client of `gateway.py`,
not a second authority or command scheduler.

**Required facade:**

```python
class MobileIngress:
    def pair(self, pairing_proof, client_metadata) -> Session: ...
    def authenticate(self, cookie, csrf_token, request_origin) -> Principal: ...
    def status(self, principal) -> MobileStatus: ...
    def submit(self, principal, operation_id, request) -> OperationStatus: ...
    def operation(self, principal, operation_id, cursor=None) -> OperationStatus: ...
    def manage_main_preview(self, principal, request) -> TransitionPreview: ...
    def manage_main_confirm(self, principal, preview_id, passkey_proof) -> TransitionView: ...
```

Pairing grants the phone a client role, not `manage_main`, PC approval, or executor authority.
Management requires a distinct grant plus fresh passkey proof, preview bound to current
incarnation/epoch/policy, and immutable transition ID. Lost response means query that ID.
No offline executable outbox: retain unsent text only as a client draft. On reconnect, query
the original operation/transition and resume progress by cursor; never replay input or create a
replacement operation automatically. Keep no location endpoint/UI/data. `phone.py` remains a
PC-side backend behind this stable ingress and must not independently admit work.

### `runtime.py` — MAIN lifecycle and provider readiness

**Owns:** acquiring/renewing/releasing the MAIN lease, readiness reports, admission stop, local
MAIN service lifecycle, and cutover-mode enforcement. Reads effective role from gateway
authority, never from a mutable `cluster.main_device` value while in v2.

**Required facade:**

```python
class MainRuntime:
    start(self) -> RuntimeState: ...
    readiness(self, profile="control") -> Readiness: ...
    renew_once(self) -> LeaseView: ...
    require_authority(self, effect_class, target=None) -> AuthorityContext: ...
    begin_drain(self, transition_id) -> DrainReport: ...
    release_before_suspend(self) -> ReleaseReport: ...
    invalidate_after_resume(self) -> None: ...
    stop_new_admission(self, reason) -> None: ...
```

Use `CachedAuthority` only when its clock adapter, boot identity, request-start anchoring, and
invalidation contract are valid. A zero/expired `remaining()` stops all new admissions.
Suspend, resume, process restart, link loss, new tuple, demotion, or authority uncertainty
invalidates cached state. On Windows, do online validation each action. Shutdown and sleep
release are best effort followed by local admission stop; lack of release acknowledgement does
not justify another MAIN acting until the authority fence has completed. Legacy runtime remains
the only path until atomic cutover is explicitly completed.

For the requested Luna +5 / Grok 4.7 profile, `ai_ready` is true only when the local permitted
subscription-backed session is available, its model/lane mapping is explicit, quota is
sufficient under current policy, and any tool calls are mediated through the v2 broker. A
provider name alone is not readiness. Do not copy session files between PCs or through Oracle.

### `executor.py` — target-side fencing and effect admission

**Owns:** target verification of MAIN authority, one-time admission consumption, durable local
fence/receipt records, work/effect classification, and safe uncertain-outcome reporting.
Apply the same guard to the MAIN's local executor and every remote target. Guard actual effect
sinks, not only Hub dispatch or a Python wrapper.

**Required facade:**

```python
class ExecutorGuard:
    accept(self, signed_operation, admission_ticket) -> LocalReceipt: ...
    fence(self, incarnation, epoch, mode_version) -> FenceAck: ...
    reconcile(self, operation_id, attempt_id) -> ReceiptStatus: ...
    stop_interactive(self, session_id, reason) -> StopAck: ...
```

Before effect: validate enrolled MAIN signature, exact `(authority_id, incarnation, epoch,
mode_version)`, target, operation/attempt IDs, digest, ticket validity, and successful online
consume at authority. Persist ticket/receipt identity and highest fenced tuple before starting an
effect. A duplicate identical ID returns prior status; conflicting digest/target/attempt is
rejected. Fencing is durable across reboot and applies to local shell, UI, voice, provider tools,
remote jobs, and other managed effect paths. Effects already started are not replayed: report
`uncertain` until reconciled. Pure compute retry is allowed only after target completion/resource
reconciliation and receives a new attempt ticket. Interactive input is ordered, bounded, and
released on loss of owner presence; never replay buffered input.

### `integration.py` — legacy adapters and controlled cutover

**Owns:** the only bridge between existing cluster/phone/executor entry points and continuity
interfaces; compatibility mode detection; participant inventory/ack collection; startup refusal
for mixed or downgraded modes; and the reversible cutover state machine.

**Required facade:**

```python
class ContinuityIntegration:
    mode(self) -> ModeView: ...
    legacy_request(self, request) -> LegacyResult: ...
    v2_request(self, principal, operation_id, request) -> OperationStatus: ...
    participants(self) -> ParticipantInventory: ...
    prepare_v2(self, expected_mode_version) -> CutoverReport: ...
    quiesce_legacy(self, expected_mode_version) -> CutoverReport: ...
    activate_v2(self, authority_tuple, canary) -> CutoverReport: ...
    rollback_quiesced(self, reason, expected_mode_version) -> CutoverReport: ...
```

Default installed behavior is unchanged legacy. V2 starts in isolated test fleet only. Refuse
activation if any old managed path can independently issue effects, any eligible participant
has not acknowledged the new mode/fence, any running job/provider tool is unmediated, or the
candidate state/profile is incomplete. During cutover the state machine is one-way across
explicit quiesce/fence gates; it must never infer authority from config edits, cached UI, a
bookmark, or an old credential. Rollback is a fresh fenced transition, never a process toggle
that could make both paths live.

## 4. End-to-end invariants and observable outcomes

| Event | Required result |
| --- | --- |
| Arch sleeps with a valid lease | Arch stops admissions/releases if possible; Dell cannot act until old validity plus margin or complete fence acknowledgements. |
| Dell obtains a recovery grant | New epoch, not-ready lease; only a successful canary enables admissions. |
| Arch wakes with its old tuple | Renewal, ticket consumption, and every effect sink reject it; no local config fallback. |
| Oracle unavailable | Existing UI may report cached/read-only state while valid; no new role, ordinary operation, or authority consume. Uncertain actions are reconciled by original ID. |
| Phone disconnects during action | No new action on reconnect. Query original ID and progress cursor; interactive input stops/releases. |
| Gateway restarts/restores | New incarnation/epoch, old grants void, max-validity barrier, no MAIN until fresh recovery. |
| Provider call began before demotion | Never blindly rerun; tool/effect status reconciled or remains uncertain. AI readiness can be false while control readiness is true. |
| Offline participant misses fence | It is excluded/quarantined; no global “all fenced” claim and no stale participant re-admission until mode sync. |

Audit events may include installation IDs, tuple, transition/operation IDs, timestamps, result
codes, and digests. They must not include prompt/input bodies, file names/content, provider
transcripts, frames, credentials, cookies, passkey material, or private workflow data.

## 5. Deployment gates and limits

1. **Inventory gate:** owner verifies Oracle identity, OS/resources, Tailscale reachability,
   durable local disk, supervisor, DNS/TLS ownership, firewall, backup destination, and recovery
   access without exporting secrets. Verify Dell OS/service startup and local Luna/Grok session
   readiness locally. No facts in this repository substitute for this check.
2. **Protocol gate:** implement gateway authentication and target-side receipt/fence before
   integrating any producer. Verify two gateway processes cannot own one authority file;
   restart/restore always advances incarnation and holds the barrier. Exercise clock, sleep,
   partitions, retries, conflicts, old tuples, and no-MAIN states with isolated fixtures.
3. **Legacy gate:** inventory CLI, UI, phone, voice, hub, direct local tools, provider
   subprocesses, startup tasks, and old checkouts/credentials. Provide durable managed startup
   refusal/fencing and acknowledgements from every effect-bearing participant. Until complete,
   keep `LEGACY_ACTIVE` and no v2 dispatch.
4. **Readiness gate:** prove control readiness independent of AI; prove Luna +5 and Grok 4.7
   readiness using local subscription credentials only, no paid API. Verify mediated tools and
   no session/state copied to gateway. A failed AI check yields control-only MAIN or no MAIN per
   requested profile; it never silently downgrades provider policy.
5. **Cutover gate:** after isolated tests, perform owner-directed maintenance cutover with
   explicit canary, Arch→Dell and Dell→Arch manual recovery, wake-old-MAIN rejection, phone
   cellular reconnect, and rollback drill. Keep automatic failover disabled until measured
   recovery and all failure drills pass.

**Known limits:** the prototype's SQLite writer lock protects only one local file/process
domain; it is not distributed consensus. RAM-only lease timing is safe only if the single
gateway is truly exclusive and its restart/restore incarnation barrier is trusted. The current
clock adapter supports tested Linux `CLOCK_BOOTTIME` shape only; Windows/macOS do not cache
validity. Existing legacy signed jobs are not fenced by the prototype. A valid MAIN lease does
not prove personal state, provider sessions, workflow journals, or approvals are ready. Oracle
loss intentionally stops new work. These limits prevent claiming operational failover today.

## 6. Repository evidence inspected

- `CLAUDE.md`: no paid-token spending, no commits/push/CI, no new daemons beyond approved
  existing runtime, and restart/input safeguards.
- `DESIGN.md`, `ARCHITECTURE.md`, `MAIN-HANDOFF.md`,
  `CONTINUITY-NEXT-IMPLEMENTATION.md`, `IMPLEMENTATION-BASELINE.md`, and existing audits.
- `jarvis/continuity/{authority,clock}.py` and package status docstring; `jarvis.config.json`
  cluster metadata only (no secrets copied into this report).
- Current git worktree has extensive pre-existing edits, additions, and deletions. This report
  was added without reverting or rewriting them. No host connection, credentials, provider,
  service, live state, or production configuration was accessed.

## 7. Gateway alignment note (2026-11-11)

Added after sections 1–6. It does not change those decisions and it is not a
cutover authorization. The implementation record is
`docs/design/mobile-continuity/GATEWAY-IMPLEMENTATION.md`.

`jarvis/continuity/gateway.py` now implements the section 3 method names
(`status`, `renew`, `release`, `acknowledge`, `transition_request`,
`transition_readiness`, `transition_step`, `consume`, `admission`, `relay`) plus
owner-only `enroll` and `set_mode`. Authentication is an SSH installation key
and a pinned owner key. There is still no WebAuthn verifier, no provisioned
Oracle gateway, and no live listener on import.

Incompatibilities left strict on purpose:

- `consume` is called by the enrolled target and requires a MAIN ticket
  signature. The contract's `authenticated_main` name is not the HTTP caller.
  `integration.LoopbackGateway.consume` instead requires the caller to be MAIN
  and does not check an installation-key signature.
- `transition_step` is owner-proof only. There is no unsigned policy identity.
  The loopback stand-in lets the candidate `grant` with a shared secret.
- `AuthenticatedPort` matches the `MainRuntime` / `ExecutorGuard` argument
  lists, but `consume` without `ticket_signature` does not reach the store.
  `ExecutorGuard.accept` still omits that field, so that wiring fails closed.
- `peer_report()` is name-compatible for gateway, ingress, runtime, and
  executor. `INCOMPATIBILITIES` still says those modules are absent.
  `test_operational_e2e.py` still asserts absence. Production
  `ContinuityIntegration` remains `LEGACY_ACTIVE`.
- `IsolatedFleet` uses `LoopbackGateway`, not `ContinuityGateway`. Its identity
  dict includes `secret`, and `reject_personal` rejects that key, so stand-in
  `acknowledge` raises `personal_field` before any mode change. Do not delete
  `secret` from the personal-field set to make that drill pass.
- Gateway `not_found` is HTTP 404. The integration extra map uses 409.
- `not_quiet` and `lease_live` HTTP errors include the store's `wait` and a
  matching `Retry-After`. The client keeps that wait only when both agree.
  Phone ask, overview, and frame bodies are still not relayed. Metadata-only
  authority storage does not by itself forbid a later transient forward, and
  this gateway does not store or retry a personal body.

Nothing in this note authorizes sleeping Arch, promoting Dell, or serving the
phone from Oracle.
