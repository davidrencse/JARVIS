# Integration review — distributed compute harness

Status: integrated draft; awaiting owner review. No implementation/deployment approved.

The owner-supplied specification replaces the shorter canonical DESIGN.md while preserving
its single-MAIN, cloud-NODE, PC-only personal state, mobile MAIN administration and no-mobile-location
boundaries. Resource discovery, deterministic scheduling, concurrent DAG jobs, reservations and
artifact movement are now explicit core requirements rather than optional fleet enrollment.
The prior local draft was preserved in /tmp/jarvis-design-before-compute-integration.md for this
session; the attachment remains the supplied source. Source SHA-256 is in design.json.

## Review changes

1. Phase4 can demonstrate typed-control continuity using a control-ready sole MAIN; it cannot
   claim AI continuity before Phase5B provider/state readiness. No second leader is allowed.
2. A database epoch change alone cannot revoke grants on a disconnected executor. Require fence
   acknowledgements or expiry plus a clock-error margin before activating new authority.
3. Resource reservation cleanup follows verified process state. Timeouts/lease expiry cannot
   free VRAM held by an uncertain process; quarantine and reconcile it.
4. Cloud metadata is not a private workflow backup. A verified PC checkpoint, worker receipts
   and artifact manifests are required for DAG recovery. Missing state restricts continuation.
5. Separate release gates: A continuity foundation; B actual two-PC distributed compute; C
   prepared AI/state continuity and authenticated mobile MAIN management. No calendar promises.

These corrections are in section16A of DESIGN.md and mirrored in design.json. The staged roadmap
remains phases0–8, with explicit5A compute and5B AI/state milestones. No assertions about deployed
features, CPU/GPU pooling, live migration or full implementation are made.

## Validation and limits

Check draft status, unique requirement/architecture/flow IDs, graph references, canonical roadmap,
source fingerprint and rendered-report diagnostics. This validates the planning artifacts only.
Independent protocol/security/resource-scheduler review, actual Oracle resources/costs, PC state
inventory, provider availability and all real failover/compute tests remain required before build
completion. A design approval does not itself grant credential access or deployment authority.

## Owner dispositions and follow-up — 2026-11-11

- Finding1 (revocation lost by stale backup): owner accepts this residual risk. The canonical
  plan no longer guarantees preservation of post-backup identity revocations. Old coordinator
  execution grants still must not revive; this is not acceptance of split brain.
- Finding2 (legacy/v2 authority cutover): agent research complete; the dedicated repository-grounded
  MAIN-HANDOFF.md subplan is integrated through canonical section16A.10.
- Finding3 (operation expiry): choose signed immutable original issuance/admission expiry;
  fresh execution grants cannot reset it. Default24h admission TTL is a proposal. GC does not
  turn an old ID back into a new operation.
- Finding4 (concurrent mutations): default per-attempt isolated worktrees/sandboxes, exclusive
  writer reservations where isolation is unsuitable, explicit reviewed merge and manifest
  publication. Uncertain writers retain reservations. Global logical-result commit belongs
  to the MAIN's PC workflow journal, not independent target-local CAS stores.
- Finding5 (schema ownership): rebuilt17 entity-specific schemas. Fields, primary/unique/index
  keys and foreign references validated; PC-private entities acknowledge PC-local durability.

These dispositions correct the draft only. Full architecture approval, implementation and
production deployment are distinct; no runtime changes were made as part of these revisions.

Handoff research produced H0–H8 implementation gates and explicitly scoped managed-runtime
authority guarantees, online admission, resume invalidation and controlled rollback.
This is not proof that those mechanisms exist or that the plan has been approved.


## Final independent structural audit — 2026-11-11

Verdict: coherent direction, suitable for staged implementation approval; not yet a complete
executable protocol. No remaining reproducible structural defect warranted a blocking finding
in the final amended draft. This is an audit conclusion, not proof of correctness or approval.

Earlier substantive findings are resolved in canonical sections16B–16E and corresponding
structured contracts: frozen-writer final workflow barrier with conservative abrupt-loss
reconciliation; stable logical identities separated from target-bound execution tickets;
and explicit provider execution classes with real tool mediation requirements.

The final reviewer also checked the partially consumed online-admission case, mobile
reconnection without input replay, MAIN exclusivity, and submission deduplication. Job client
keys remain required and cannot be rebound to another target, payload digest or attempt.

Remaining production gates are concrete implementation evidence: suspend-aware clock margins,
crash reconciliation transitions, real provider-tool denial, Windows process-tree containment,
resource isolation, and real-phone/real-PC fault drills. These are unverified guarantees, not
claims of deployed behavior. The accepted stale-backup identity-revocation risk remains accepted.

Recommended first authorization is P0 inventory/baseline and P1 behavior-preserving interfaces.
Authority prototypes remain isolated until exclusive cutover passes; workflow/provider
continuity and automation retain their later release gates. The design remains unapproved.
