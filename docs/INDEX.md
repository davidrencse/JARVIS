# Documentation index

*Every markdown file the repo held before the 2026-11-13 consolidation (317), what it was, and where it went. Start here to find a topic; CLAUDE.md lists the order to read the architecture docs.*

## Current docs

| Path | What it is |
| --- | --- |
| `docs/INDEX.md` | This index: every markdown file the repo used to hold, a one-sentence summary, and where it went. |
| `docs/architecture-guide.md` | Running map of modules, flow, safety, and pipeline invariants. |
| `docs/reference.md` | Commands and setup for chat, console, vault, knowledge, and speech. |
| `docs/jarvis-os-architecture.md` | Design decisions with reasons, the intent registry, and what is still open. |
| `docs/migration.md` | How to move the main, including state that is not in git. |
| `docs/provider-limits.md` | How Claude and Codex plan windows are read and how an expired Claude session is renewed. |
| `docs/reference-skills.md` | Catalogue and restore instructions for the twelve third-party reference skills and four Three.js skills that were vendored under skills/ (only skills/sources.json remains). |
| `docs/design/continuity-contract.md` | Operational rules, effect-sink inventory, and fail-closed pins for the isolated continuity prototype. |
| `docs/design/continuity-design.md` | Draft plan for one leased PC MAIN, capability-granted nodes, and a tailnet-only Oracle gateway. |
| `docs/design/continuity-redundancy.md` | Pairs that must stay separate, and the few duplicates that already have one owner. |
| `docs/design/continuity-runtime.md` | Gateway, ingress, executor, and runtime behavior of jarvis/continuity, including the HTTP map and crash table. |
| `docs/design/design.json` | Structured design schemas kept because the prose plan does not enumerate entity fields. |
| `docs/design/fleet-api.md` | Owner-console and phone fleet contract for invite, revoke, capabilities, vault status, and main-transfer. |
| `docs/design/main-handoff.md` | Exclusive-authority handoff protocol, local bootstrap commands, and the live gates that still block cutover. |
| `docs/design/obsidian-storage.md` | Second-brain capture stages, revision and recall rules, and brainsync snapshot handoff. |
| `docs/design/parity-console-phone.md` | Console, phone, and cluster auth, route shapes, visual tokens, and the QA bar. |
| `docs/handbook/browser.md` | agent-browser source build and the ten Grokbot handoff checks. |
| `docs/handbook/cluster.md` | Main and nodes, hub auth, and how a dropped link is found again. |
| `docs/handbook/continuity.md` | Continuity gateway, authority modes, and the blocked MAIN cutover. |
| `docs/handbook/drag-drop-design.md` | Unbuilt native file-drag between Arch and Windows, and the gates that block it. |
| `docs/handbook/fleet-ui.md` | How the console and phone add a computer, and how archive differs from Git. |
| `docs/handbook/lan-mouse.md` | Shared keyboard and mouse, the apart guard, clipboard, and untested display extension. |
| `docs/handbook/operations.md` | Restarts, sleep, fail-safes, machine readings, and the hardware inventory. |
| `docs/handbook/perf.md` | Tuning rules that are still in the code. |
| `docs/handbook/phone.md` | Phone pairing, threat checks, Ask, camera, and the screen's design rules. |
| `docs/handbook/release.md` | Order for restarting the main, updating nodes, and the unversioned-bundle window. |
| `docs/handbook/remote-control.md` | Remote-view session rules and the requirement to release every held key. |
| `docs/handbook/voice.md` | Wake word, speaker thresholds, and when the listener learns a clip. |

## Other markdown kept as is

| Path | What it is |
| --- | --- |
| `CLAUDE.md` | Project instructions for Claude: hard rules, request flow, repo map, machines, working commands, safety notes. Protected. |
| `JARVIS.md` | JARVIS's own self-model, loaded at the start of every request. Protected. |
| `README.md` | Root project overview and doc entry points. Protected; its links to docs/handbook/index.md, docs/handbook/audit-2026-11.md and docs/feature-validation.md are now stale and need David's edit. |
| `jarvis-memory/Home.md` | Entry page of JARVIS's own memory notes, separate from David's vault. Protected. |
| `jarvis-memory/Rules.md` | David's standing rules for JARVIS. Protected. |
| `jarvis-memory/Machines.md` | The machines JARVIS runs on. Protected. |
| `jarvis-memory/Decisions.md` | Design decisions with their reasons. Protected. |
| `jarvis-memory/memory/*.md (8 files)` | Memory notes on the AppData trap, commit policy, David's machines, the project, the Python interpreter, the vault, no API credits and no cursor hacks, architecture-view style. Protected. |
| `.claude/agents/*.md (10 files)` | Subagent role definitions: architect, devops, frontend, implementer, prompt-engineer, researcher, reviewer, scout, security, tester. Protected. |
| `tests/README.md` | Map of the tests/ subsystem folders and how to run the suite. Kept: describes live code layout. |
| `prompts/prompt-engineer.md` | Reusable prompt-engineer subagent instruction used when refining a worker contract. Kept: referenced by jarvis/prompts.py and the orchestrator. |
| `.learnings/LEARNINGS.md` | Running log of resolved engineering learnings (e.g. Windows SendInput typing). Kept: an active log, not a status doc. |
| `jarvis/codex_base.md` | Base instructions file loaded by jarvis/chatgpt.py. Kept: code asset, not documentation. |
| `artifacts/main-handoff/README.md` | Untracked preparation notes for the MAIN handoff setup. Left untouched: another session's work product. |
| `.jarvis/**, jarvis.prev/codex_base.md` | Runtime state, release staging and a code snapshot (gitignored/untracked); their .md files are not docs and were not touched. |

## Where the old files went

Deleted means finished one-off material (audits, checkpoints, swarm logs, repair diaries, phase reports, vendored third-party text) already covered by code and git history; anything still true was copied into the new home first. Recover any of them with `git log --diff-filter=D -- <old path>`.

| Old path | What it was | New home |
| --- | --- | --- |
| `docs/agent-browser/README.md` | Source build and history-step patch for agent-browser 0.39.1. | `docs/handbook/browser.md` |
| `docs/architecture-guide.md` | Full reference for modules, request flow, config, and safety rules. | `docs/architecture-guide.md` |
| `docs/architecture.md` | Earlier pipeline, tier, memory, and voice-cost sketch from October 2026. | `docs/architecture-guide.md` |
| `docs/audit-evidence/backup-inventory-2026-11-12.md` | Audit/research evidence report: Backup inventory — 2026-11-12. | deleted (done/stale) |
| `docs/audit-evidence/fastlane-evidence-repair-2026-11-13.md` | Audit/research evidence report: FastLane evidence repair — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/findings-cluster-ssh.md` | Audit/research evidence report: Cluster SSH and hub audit. | deleted (done/stale) |
| `docs/audit-evidence/findings-console-remote.md` | Audit/research evidence report: Console and remote-view audit (2026-11-12). | deleted (done/stale) |
| `docs/audit-evidence/findings-phone-gateway.md` | Audit/research evidence report: Phone and continuity gateway session audit. | deleted (done/stale) |
| `docs/audit-evidence/findings-transfer-input.md` | Audit/research evidence report: Transfer, clipboard, drop zone, and Lan Mouse input audit. | deleted (done/stale) |
| `docs/audit-evidence/grok-2026-11-12-audit.md` | Audit/research evidence report: JARVIS cleanup audit — 2026-11-12 (in progress). | deleted (done/stale) |
| `docs/audit-evidence/grok-continuity-authority-2026-11-13.md` | Audit/research evidence report: Authority and clock audit (2026-11-13). | deleted (done/stale) |
| `docs/audit-evidence/grok-continuity-gateway-2026-11-13.md` | Audit/research evidence report: Gateway audit (Grok 4.7), 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-continuity-ingress-2026-11-13.md` | Audit/research evidence report: Ingress and console-gateway review — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-continuity-lifecycle-2026-11-13.md` | Audit/research evidence report: Continuity lifecycle review (Grok 4.7). | deleted (done/stale) |
| `docs/audit-evidence/grok-continuity-runtime-tests-2026-11-13.md` | Audit/research evidence report: Continuity runtime, executor, integration, and assigned tests. | deleted (done/stale) |
| `docs/audit-evidence/grok-fleet-security-hub_security-2026-11-13.md` | Audit/research evidence report: Hub credential boundary — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-fleet-security-receipts_enrollment-2026-11-13.md` | Audit/research evidence report: Fleet receipts and enrollment — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-fleet-security-role_mutations-2026-11-13.md` | Audit/research evidence report: Role mutations — fleet security review (2026-11-13). | deleted (done/stale) |
| `docs/audit-evidence/grok-fleet-security-settings_config-2026-11-13.md` | Audit/research evidence report: Settings and config-lock review — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-fleet-security-tests_redundancy-2026-11-13.md` | Audit/research evidence report: Fleet security tests — redundancy and coverage — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-frontend-mobile-console-assets-2026-11-13.md` | Audit/research evidence report: Console assets review — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-frontend-mobile-hardware-sharing-2026-11-13.md` | Audit/research evidence report: Hardware, meters, and sharing — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-frontend-mobile-http-backend-2026-11-13.md` | Audit/research evidence report: Phone and console HTTP backend — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-frontend-mobile-phone-worker-2026-11-13.md` | Audit/research evidence report: Phone service worker — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-frontend-mobile-quota-graph-2026-11-13.md` | Audit/research evidence report: Console quota ring and full-graph view. | deleted (done/stale) |
| `docs/audit-evidence/grok-reconnect-announce-2026-11-13.md` | Audit/research evidence report: Announce listener and role handoff — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-reconnect-cleanup-2026-11-13.md` | Audit/research evidence report: Reconnect artifact cleanup — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-reconnect-failback-2026-11-13.md` | Audit/research evidence report: Tunnel failback audit — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-reconnect-supervision-2026-11-13.md` | Audit/research evidence report: Reconnect supervision audit — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-reconnect-testcost-2026-11-13.md` | Audit/research evidence report: Link-test cost and redundancy — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-repair-directory-quarantine-store-2026-11-13.md` | Audit/research evidence report: Repair: directory collision, quarantine latch, store guards (2026-11-13). | deleted (done/stale) |
| `docs/audit-evidence/grok-runtime-tests-desktop_remote-2026-11-13.md` | Audit/research evidence report: Desktop and remote runtime/tests audit — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-runtime-tests-intelligence-2026-11-13.md` | Audit/research evidence report: Intelligence tests residual audit — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-runtime-tests-providers-2026-11-13.md` | Audit/research evidence report: Provider snapshot and replay audit — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-runtime-tests-runner-2026-11-13.md` | Audit/research evidence report: Runtime runner and CLI-test audit — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-runtime-tests-voice-2026-11-13.md` | Audit/research evidence report: Voice runtime test audit — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-storage-codec-2026-11-13.md` | Audit/research evidence report: Codec, validate, and restore — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-storage-export-2026-11-13.md` | Audit/research evidence report: Storage export / publish review — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-storage-files-2026-11-13.md` | Audit/research evidence report: Storage files review — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/grok-storage-queue-2026-11-13.md` | Audit/research evidence report: Queue review — outbox, capture, retention. | deleted (done/stale) |
| `docs/audit-evidence/grok-storage-runtime-2026-11-13.md` | Audit/research evidence report: Grok storage review — runtime and canonical recall — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/link-recovery-review-2026-11-13.md` | Audit/research evidence report: Link recovery reconnect review — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/research-remote-web-pwa-2026-11-12.md` | Audit/research evidence report: Remote, web console, and phone PWA — primary-source research. | deleted (done/stale) |
| `docs/audit-evidence/research-ssh-tailscale-2026-11-12.md` | Audit/research evidence report: SSH and Tailscale hardening research. | deleted (done/stale) |
| `docs/audit-evidence/security-hardening-2026-11-12.md` | Audit/research evidence report: JARVIS security hardening pass, 2026-11-12. | deleted (done/stale) |
| `docs/audit-evidence/sol-continuity-2026-11-13.md` | Audit/research evidence report: Continuity audit consolidation — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/sol-fleet-security-2026-11-13.md` | Audit/research evidence report: Fleet and security audit consolidation — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/sol-frontend-mobile-2026-11-13.md` | Audit/research evidence report: Frontend and mobile audit consolidation — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/sol-reconnect-2026-11-13.md` | Audit/research evidence report: Reconnect consolidation — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/sol-runtime-tests-2026-11-13.md` | Audit/research evidence report: Runtime and tests audit — 2026-11-13. | deleted (done/stale) |
| `docs/audit-evidence/sol-storage-2026-11-13.md` | Audit/research evidence report: Storage audit consolidation — 2026-11-13. | deleted (done/stale) |
| `docs/design/mobile-continuity/ACTION-PLAN.md` | Ordered P0–P10 work packages, release slices A–D, and the definition of done for the continuity draft. | `docs/design/continuity-design.md` |
| `docs/design/mobile-continuity/ARCHITECTURE.md` | Topology, concurrent lanes, and proposed limits for one MAIN and a tailnet gateway. | `docs/design/continuity-design.md` |
| `docs/design/mobile-continuity/AUDIT-CONSOLIDATION-BACKEND.md` | Backend consolidation audit diary whose still-true rules were already absorbed into later contracts. | deleted (done/stale) |
| `docs/design/mobile-continuity/AUDIT-CONSOLIDATION-SURFACES.md` | Surface consolidation audit diary superseded by the parity and fleet contracts. | deleted (done/stale) |
| `docs/design/mobile-continuity/AUDIT-FLEET-BACKEND.md` | Fleet backend audit whose durable invite, lock, and cloud rules landed in the fleet contract. | `docs/design/fleet-api.md` |
| `docs/design/mobile-continuity/AUDIT-FLEET-USABILITY.md` | Fleet usability audit whose surviving UI rules landed in the fleet contract. | `docs/design/fleet-api.md` |
| `docs/design/mobile-continuity/AUDIT-GROK-FINAL-VERIFICATION.md` | Final verification log of an audit pass, with no contract beyond the later repairs. | deleted (done/stale) |
| `docs/design/mobile-continuity/AUDIT-INTEGRATED-REPAIRS.md` | Integrated repair verification diary superseded by the fleet and surface contracts. | deleted (done/stale) |
| `docs/design/mobile-continuity/AUDIT-INTEGRATION.md` | Short integration verdict log with no remaining open contract. | deleted (done/stale) |
| `docs/design/mobile-continuity/AUDIT-OBSIDIAN.md` | Obsidian audit whose open items were later closed or restated as storage rules. | `docs/design/obsidian-storage.md` |
| `docs/design/mobile-continuity/AUDIT-SUMMARY.md` | Audit summary verdict with no durable rule past the merged contracts. | deleted (done/stale) |
| `docs/design/mobile-continuity/BACKEND-AUDIT-REPAIRS.md` | Backend repair diary whose fleet lock and receipt rules are in the fleet contract. | `docs/design/fleet-api.md` |
| `docs/design/mobile-continuity/BRAINSYNC.md` | Brainsync handoff, bundle, and git rules for moving a vault snapshot between PCs. | `docs/design/obsidian-storage.md` |
| `docs/design/mobile-continuity/CONCURRENCY.html` | Four proposed concurrency situations: Arch online, Dell promoted, phone loss, no MAIN. | `docs/design/continuity-design.md` |
| `docs/design/mobile-continuity/CONSOLE-LIVE-CHECKPOINT.md` | Console paint checkpoint and screenshot log with no contract past the parity doc. | deleted (done/stale) |
| `docs/design/mobile-continuity/CONTINUITY-CONSOLIDATION.md` | Rules for one ticket-field owner, signature cap, and HTTP map ownership. | `docs/design/continuity-runtime.md` |
| `docs/design/mobile-continuity/CONTINUITY-NEXT-IMPLEMENTATION.md` | Next-step notes for the isolated continuity modules, kept only as fail-closed rules. | `docs/design/continuity-runtime.md` |
| `docs/design/mobile-continuity/CONTINUITY-RUNTIME.md` | MainRuntime lease, recovery, and port behavior for the isolated prototype. | `docs/design/continuity-runtime.md` |
| `docs/design/mobile-continuity/DESIGN.md` | Canonical draft plan for one leased PC MAIN, nodes, and a tailnet-only Oracle gateway. | `docs/design/continuity-design.md` |
| `docs/design/mobile-continuity/EXECUTOR-IMPLEMENTATION.md` | ExecutorGuard accept order, journal, and crash resume table. | `docs/design/continuity-runtime.md` |
| `docs/design/mobile-continuity/FINAL-ARCHIVE-REPAIRS.md` | Archive repair notes whose surviving oversize, gap, and recall rules are storage rules. | `docs/design/obsidian-storage.md` |
| `docs/design/mobile-continuity/FINAL-ARCHIVE-SHARING-VERIFICATION-REPAIRS.md` | Archive sharing verification diary superseded by the storage rules. | `docs/design/obsidian-storage.md` |
| `docs/design/mobile-continuity/FINAL-FLEET-REPAIRS.md` | Fleet repair pass whose current main-transfer and receipt behavior is the fleet contract. | `docs/design/fleet-api.md` |
| `docs/design/mobile-continuity/FINAL-RECEIPT-VERIFICATION-REPAIRS.md` | Receipt verification diary whose fence and uncertain-receipt rules are in the fleet contract. | `docs/design/fleet-api.md` |
| `docs/design/mobile-continuity/FINAL-SURFACE-REPAIRS.md` | Surface repair pass for presence, capability titles, battery, and archive wording. | `docs/design/parity-console-phone.md` |
| `docs/design/mobile-continuity/FLEET-API.md` | Owner and phone fleet HTTP contract for invite, revoke, capabilities, and vault status. | `docs/design/fleet-api.md` |
| `docs/design/mobile-continuity/GATEWAY-IMPLEMENTATION.md` | Continuity gateway routes, relay idempotency, and HTTP status map. | `docs/design/continuity-runtime.md` |
| `docs/design/mobile-continuity/IMPLEMENTATION-BASELINE.md` | Harness baseline pins for dual config, unfenced sinks, and set_main refusal. | `docs/design/continuity-contract.md` |
| `docs/design/mobile-continuity/IMPLEMENTATION-PROGRESS.md` | Progress log of test counts and session status with no remaining contract. | deleted (done/stale) |
| `docs/design/mobile-continuity/INGRESS-IMPLEMENTATION.md` | Mobile ingress fail-closed session, relay, and phone-adapter rules. | `docs/design/continuity-runtime.md` |
| `docs/design/mobile-continuity/INTEGRATED-FLEET-GAP-REPAIRS.md` | Fleet gap repairs for the receipt fence, lock, and cloud-never-main rule. | `docs/design/fleet-api.md` |
| `docs/design/mobile-continuity/LIVE-UI-INTEGRATION.md` | Live UI notes for CSS order, compact Remote header, and the voice wake mismatch. | `docs/design/parity-console-phone.md` |
| `docs/design/mobile-continuity/LOCAL-BOOTSTRAP.md` | Per-PC continuity bootstrap preflight, prepare, configure, and cutover commands. | `docs/design/main-handoff.md` |
| `docs/design/mobile-continuity/LUNA-CONTINUITY-REDUNDANCY.md` | Continuity pairs that must not be merged by weakening a fence. | `docs/design/continuity-redundancy.md` |
| `docs/design/mobile-continuity/LUNA-PROGRAM-REDUNDANCY.md` | Program-wide do-not-merge rules for receipts, config writers, and phone projection. | `docs/design/continuity-redundancy.md` |
| `docs/design/mobile-continuity/MAIN-BOOTSTRAP-BLOCKERS.md` | Production bootstrap blockers: startup factory, successor route, and owner proof. | `docs/design/main-handoff.md` |
| `docs/design/mobile-continuity/MAIN-HANDOFF.md` | Exclusive-authority handoff protocol, tuple, cutover machine, and failure rules. | `docs/design/main-handoff.md` |
| `docs/design/mobile-continuity/MAIN-SESSION-ROOT.md` | Integration-coordinator session diary whose operator gates are in the handoff doc. | `docs/design/main-handoff.md` |
| `docs/design/mobile-continuity/MAIN-SESSION-TEAM-A.md` | Team A session diary of handoff worker outcomes with no extra protocol. | `docs/design/main-handoff.md` |
| `docs/design/mobile-continuity/MAIN-SESSION-TEAM-B.md` | Team B session diary whose safe command sequence and live gates are in the handoff doc. | `docs/design/main-handoff.md` |
| `docs/design/mobile-continuity/OBSIDIAN-CUTOVER.md` | C0–C4 cutover model and the rule that only C3 makes the vault primary. | `docs/design/obsidian-storage.md` |
| `docs/design/mobile-continuity/OBSIDIAN-RUNTIME.md` | Second-brain drainer, status words, and archive CLI. | `docs/design/obsidian-storage.md` |
| `docs/design/mobile-continuity/OBSIDIAN-STORAGE-IMPLEMENTATION.md` | Revision, tombstone, restore, oversize, and canonical-read rules for the archive. | `docs/design/obsidian-storage.md` |
| `docs/design/mobile-continuity/OBSIDIAN-WRITE-THROUGH.md` | Which stores write through, and the recall fallback limits. | `docs/design/obsidian-storage.md` |
| `docs/design/mobile-continuity/OPERATIONAL-CONTRACT.md` | Operational deployment decisions and facade obligations for the prototype. | `docs/design/continuity-contract.md` |
| `docs/design/mobile-continuity/OPERATIONAL-INTEGRATION.md` | Integration facades, personal-field set, and HTTP extras around the prototype. | `docs/design/continuity-contract.md` |
| `docs/design/mobile-continuity/P1-BOUNDARIES.md` | Effect-sink inventory and node content-route denials the harness test pins. | `docs/design/continuity-contract.md` |
| `docs/design/mobile-continuity/P1-IMPLEMENTATION.md` | P1 facade facts for topology, registry, and remote without moving state. | `docs/design/continuity-contract.md` |
| `docs/design/mobile-continuity/P1-ROLE-WIRING.md` | Role wiring notes: roles describe, they do not grant. | `docs/design/continuity-contract.md` |
| `docs/design/mobile-continuity/P2-PRIVACY-LAUNCH-FIX.md` | P2 privacy rules for node content routes and launch hooks that stay opt-in. | `docs/design/continuity-contract.md` |
| `docs/design/mobile-continuity/P2-REPLAY-FIX.md` | P2 rule that a started tool call blocks provider failover replay. | `docs/design/continuity-contract.md` |
| `docs/design/mobile-continuity/PARITY-API.md` | Console, phone, and cluster auth boundaries, error envelopes, and route shapes. | `docs/design/parity-console-phone.md` |
| `docs/design/mobile-continuity/PARITY-CONSOLE.md` | Console hashes, rail keys, and ask behavior a new screen must keep. | `docs/design/parity-console-phone.md` |
| `docs/design/mobile-continuity/PARITY-DESIGN.md` | Visual tokens, type, motion, and the CSS cascade order. | `docs/design/parity-console-phone.md` |
| `docs/design/mobile-continuity/PARITY-PHONE.md` | Phone hash routes, pairing, session, and ask contract. | `docs/design/parity-console-phone.md` |
| `docs/design/mobile-continuity/PARITY-QA.md` | QA acceptance bar for console and phone parity. | `docs/design/parity-console-phone.md` |
| `docs/design/mobile-continuity/PHONE-LIVE-CHECKPOINT.md` | Phone paint checkpoint with no contract past the parity doc. | deleted (done/stale) |
| `docs/design/mobile-continuity/REVIEW.md` | Structural review corrections and the owner residual-risk acceptance. | `docs/design/continuity-design.md` |
| `docs/design/mobile-continuity/SENIOR-SWE-RECONCILIATION.md` | Release-gate note that production composition was not ready. | `docs/design/continuity-design.md` |
| `docs/design/mobile-continuity/SONNET-INTEGRATION-HANDOFF.md` | Integration handoff of transient relay, peer report, and launch-hook boundaries. | `docs/design/main-handoff.md` |
| `docs/design/mobile-continuity/UPLOAD-USABILITY-REPAIRS.md` | Upload drain bounds, light-theme contrast tokens, and capability ack wording. | `docs/design/parity-console-phone.md` |
| `docs/design/mobile-continuity/design.json` | Structured twin of the design: entity schemas and machine-readable plan fields the prose does not enumerate. | `docs/design/design.json` |
| `docs/design/mobile-continuity/swarm/CHARTER.md` | Swarm ownership charter and process notes for a finished docs pass. | deleted (done/stale) |
| `docs/design/mobile-continuity/swarm/CLEANUP-PROPOSAL.md` | Superseded proposal to delete docs and backups. This consolidation replaces it. | deleted (done/stale) |
| `docs/design/mobile-continuity/swarm/LOG.md` | Session status diary with no durable contract. | deleted (done/stale) |
| `docs/design/mobile-continuity/swarm/WS1.md` | Workstream status diary for runtime bootstrap. Durable gates are in the handoff doc. | deleted (done/stale) |
| `docs/design/mobile-continuity/swarm/WS2.md` | Workstream status diary for cluster membership and doctor. | deleted (done/stale) |
| `docs/design/mobile-continuity/swarm/WS3.md` | Workstream status diary for brainsync staying off by default. | deleted (done/stale) |
| `docs/design/mobile-continuity/swarm/WS4.md` | Workstream status diary for phone admission and WebAuthn staying unwired. | deleted (done/stale) |
| `docs/design/mobile-continuity/swarm/WS5.md` | Workstream status diary for gateway TLS and the unwritten handbook runbook. | deleted (done/stale) |
| `docs/feature-validation.md` | One-off October 2026 coverage matrix and test totals. | `docs/handbook/operations.md` |
| `docs/grokbot-preflight.md` | The ten checks required before a Grokbot handoff. | `docs/handbook/browser.md` |
| `docs/handbook/audit-2026-11.md` | November 2026 overhaul changelog; durable rules kept, the report dropped. | `docs/handbook/cluster.md` |
| `docs/handbook/battery-health.md` | How charge, health, RAM, swap, disk, and CPU readings are computed. | `docs/handbook/operations.md` |
| `docs/handbook/cluster.md` | Main, nodes, hub, clipboard, and bundle rules. | `docs/handbook/cluster.md` |
| `docs/handbook/continuity.md` | Gateway install, mode walk, and blocked MAIN cutover. | `docs/handbook/continuity.md` |
| `docs/handbook/drag-drop-design.md` | Native cross-machine drag design; not implemented in code. | `docs/handbook/drag-drop-design.md` |
| `docs/handbook/fleet-ui.md` | Connections and phone fleet screens: invite, revoke, archive versus Git. | `docs/handbook/fleet-ui.md` |
| `docs/handbook/index.md` | Handbook table of contents and a duplicate request-flow diagram. | deleted (done/stale) |
| `docs/handbook/input-sharing.md` | Lan Mouse paths, apart guard, and commands. | `docs/handbook/lan-mouse.md` |
| `docs/handbook/lan-mouse-reboot-plan.md` | Why Lan Mouse stays off after reboot, and the guard contract. | `docs/handbook/lan-mouse.md` |
| `docs/handbook/link-recovery.md` | Tunnel supervisor, LAN scan, signed announce, and Run unattended. | `docs/handbook/cluster.md` |
| `docs/handbook/mobile-targeting-changes.md` | Phone Send-to, approvals on the main, and native Spotify routing. | `docs/handbook/phone.md` |
| `docs/handbook/operations.md` | Restarts, sleep, fail-safes, and troubleshooting. | `docs/handbook/operations.md` |
| `docs/handbook/perf-2026-11.md` | November 2026 performance and dead-code pass. | `docs/handbook/perf.md` |
| `docs/handbook/phone-design.md` | Phone visual tokens, signal rail, and component rules. | `docs/handbook/phone.md` |
| `docs/handbook/phone.md` | Phone PWA wiring, pairing, camera, and Ask limits. | `docs/handbook/phone.md` |
| `docs/handbook/remote-control.md` | Console remote-view lifecycle and session limits. | `docs/handbook/remote-control.md` |
| `docs/handbook/voice.md` | Wake word, speaker bands, and listener commands. | `docs/handbook/voice.md` |
| `docs/hardware.md` | October 2026 Dell and Zenbook inventory from live probes. | `docs/handbook/operations.md` |
| `docs/jarvis-os-architecture.md` | OS v2 design decisions, registry contract, and build status. | `docs/jarvis-os-architecture.md` |
| `docs/migration.md` | Procedure for moving the main to another machine. | `docs/migration.md` |
| `docs/pipeline-architecture-review.md` | October 2026 workflow review, measurements, and backlog. | `docs/architecture-guide.md` |
| `docs/pipeline-hardening-2026-10-26.md` | Owned-path checks and SSE feed recovery from 2026-10-26. | `docs/architecture-guide.md` |
| `docs/pipeline-hardening-phase2.md` | Plan lock, recovery, copy collisions, and mic cleanup report. | `docs/architecture-guide.md` |
| `docs/provider-limits.md` | Claude and Codex quota endpoints, cache, and session renewal. | `docs/provider-limits.md` |
| `docs/reference.md` | CLI, console, vault, knowledge, and setup commands. | `docs/reference.md` |
| `docs/unified-desktop.md` | Clipboard, Lan Mouse, and untested Sunshine/Moonlight plan. | `docs/handbook/lan-mouse.md` |
| `skills/README.md` | Vendored third-party reference skill file: Project-local reference skills. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/accessibility/SKILL.md` | Vendored third-party reference skill file: Audit and improve web accessibility following WCAG 2.2 guidelines. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/accessibility/references/A11Y-PATTERNS.md` | Vendored third-party reference skill file: Accessibility Code Patterns. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/accessibility/references/WCAG.md` | Vendored third-party reference skill file: WCAG 2.2 Quick Reference. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/architecture-patterns/SKILL.md` | Vendored third-party reference skill file: Implement proven backend architecture patterns including Clean Architecture, Hexagonal Architecture, and Domain-Driven Design. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/architecture-patterns/references/advanced-patterns.md` | Vendored third-party reference skill file: Advanced Architecture Patterns — Reference. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/architecture-patterns/references/details.md` | Vendored third-party reference skill file: architecture-patterns — detailed patterns and worked examples. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/AGENTS.md` | Vendored third-party reference skill file: React Composition Patterns. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/README.md` | Vendored third-party reference skill file: React Composition Patterns. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/SKILL.md` | Vendored third-party reference skill file: React composition patterns that scale. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/_sections.md` | Vendored third-party reference skill file: Sections. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/_template.md` | Vendored third-party reference skill file: Rule Title Here. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/architecture-avoid-boolean-props.md` | Vendored third-party reference skill file: Avoid Boolean Prop Proliferation. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/architecture-compound-components.md` | Vendored third-party reference skill file: Use Compound Components. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/patterns-children-over-render-props.md` | Vendored third-party reference skill file: Prefer Children Over Render Props. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/patterns-explicit-variants.md` | Vendored third-party reference skill file: Create Explicit Component Variants. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/react19-no-forwardref.md` | Vendored third-party reference skill file: React 19 API Changes. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/state-context-interface.md` | Vendored third-party reference skill file: Define Generic Context Interfaces for Dependency Injection. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/state-decouple-implementation.md` | Vendored third-party reference skill file: Decouple State Management from UI. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/composition-patterns/rules/state-lift-state.md` | Vendored third-party reference skill file: Lift State into Provider Components. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/emil-design-eng/SKILL.md` | Vendored third-party reference skill file: This skill encodes Emil Kowalski's philosophy on UI polish, component design, animation decisions, and the invisible details that make software feel g. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/kpi-dashboard-design/SKILL.md` | Vendored third-party reference skill file: Design effective KPI dashboards with metrics selection, visualization best practices, and real-time monitoring patterns. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/kpi-dashboard-design/references/details.md` | Vendored third-party reference skill file: kpi-dashboard-design — detailed worked examples. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/performance-optimization/SKILL.md` | Vendored third-party reference skill file: Optimizes application performance across frontend, backend, queries, and databases. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/performance-optimization/references/optimization-patterns.md` | Vendored third-party reference skill file: Optimization Patterns. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/AGENTS.md` | Vendored third-party reference skill file: React Best Practices. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/README.md` | Vendored third-party reference skill file: React Best Practices. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/SKILL.md` | Vendored third-party reference skill file: React and Next.js performance optimization guidelines from Vercel Engineering. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/_sections.md` | Vendored third-party reference skill file: Sections. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/_template.md` | Vendored third-party reference skill file: Rule Title Here. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/advanced-effect-event-deps.md` | Vendored third-party reference skill file: Do Not Put Effect Events in Dependency Arrays. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/advanced-event-handler-refs.md` | Vendored third-party reference skill file: Store Event Handlers in Refs. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/advanced-init-once.md` | Vendored third-party reference skill file: Initialize App Once, Not Per Mount. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/advanced-use-latest.md` | Vendored third-party reference skill file: useEffectEvent for Stable Callback Refs. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/async-api-routes.md` | Vendored third-party reference skill file: Prevent Waterfall Chains in API Routes. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/async-cheap-condition-before-await.md` | Vendored third-party reference skill file: Check Cheap Conditions Before Async Flags. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/async-defer-await.md` | Vendored third-party reference skill file: Defer Await Until Needed. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/async-dependencies.md` | Vendored third-party reference skill file: Dependency-Based Parallelization. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/async-parallel.md` | Vendored third-party reference skill file: Promise.all() for Independent Operations. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/async-suspense-boundaries.md` | Vendored third-party reference skill file: Strategic Suspense Boundaries. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/bundle-analyzable-paths.md` | Vendored third-party reference skill file: Prefer Statically Analyzable Paths. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/bundle-barrel-imports.md` | Vendored third-party reference skill file: Avoid Barrel File Imports. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/bundle-conditional.md` | Vendored third-party reference skill file: Conditional Module Loading. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/bundle-defer-third-party.md` | Vendored third-party reference skill file: Defer Non-Critical Third-Party Libraries. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/bundle-dynamic-imports.md` | Vendored third-party reference skill file: Dynamic Imports for Heavy Components. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/bundle-preload.md` | Vendored third-party reference skill file: Preload Based on User Intent. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/client-event-listeners.md` | Vendored third-party reference skill file: Deduplicate Global Event Listeners. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/client-localstorage-schema.md` | Vendored third-party reference skill file: Version and Minimize localStorage Data. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/client-passive-event-listeners.md` | Vendored third-party reference skill file: Use Passive Event Listeners for Scrolling Performance. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/client-swr-dedup.md` | Vendored third-party reference skill file: Use SWR for Automatic Deduplication. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-batch-dom-css.md` | Vendored third-party reference skill file: Avoid Layout Thrashing. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-cache-function-results.md` | Vendored third-party reference skill file: Cache Repeated Function Calls. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-cache-property-access.md` | Vendored third-party reference skill file: Cache Property Access in Loops. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-cache-storage.md` | Vendored third-party reference skill file: Cache Storage API Calls. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-combine-iterations.md` | Vendored third-party reference skill file: Combine Multiple Array Iterations. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-early-exit.md` | Vendored third-party reference skill file: Early Return from Functions. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-flatmap-filter.md` | Vendored third-party reference skill file: Use flatMap to Map and Filter in One Pass. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-hoist-regexp.md` | Vendored third-party reference skill file: Hoist RegExp Creation. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-index-maps.md` | Vendored third-party reference skill file: Build Index Maps for Repeated Lookups. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-length-check-first.md` | Vendored third-party reference skill file: Early Length Check for Array Comparisons. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-min-max-loop.md` | Vendored third-party reference skill file: Use Loop for Min/Max Instead of Sort. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-request-idle-callback.md` | Vendored third-party reference skill file: Defer Non-Critical Work with requestIdleCallback. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-set-map-lookups.md` | Vendored third-party reference skill file: Use Set/Map for O(1) Lookups. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/js-tosorted-immutable.md` | Vendored third-party reference skill file: Use toSorted() Instead of sort() for Immutability. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-activity.md` | Vendored third-party reference skill file: Use Activity Component for Show/Hide. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-animate-svg-wrapper.md` | Vendored third-party reference skill file: Animate SVG Wrapper Instead of SVG Element. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-conditional-render.md` | Vendored third-party reference skill file: Use Explicit Conditional Rendering. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-content-visibility.md` | Vendored third-party reference skill file: CSS content-visibility for Long Lists. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-hoist-jsx.md` | Vendored third-party reference skill file: Hoist Static JSX Elements. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-hydration-no-flicker.md` | Vendored third-party reference skill file: Prevent Hydration Mismatch Without Flickering. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-hydration-suppress-warning.md` | Vendored third-party reference skill file: Suppress Expected Hydration Mismatches. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-resource-hints.md` | Vendored third-party reference skill file: Use React DOM Resource Hints. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-script-defer-async.md` | Vendored third-party reference skill file: Use defer or async on Script Tags. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-svg-precision.md` | Vendored third-party reference skill file: Optimize SVG Precision. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rendering-usetransition-loading.md` | Vendored third-party reference skill file: Use useTransition Over Manual Loading States. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-defer-reads.md` | Vendored third-party reference skill file: Defer State Reads to Usage Point. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-dependencies.md` | Vendored third-party reference skill file: Narrow Effect Dependencies. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-derived-state-no-effect.md` | Vendored third-party reference skill file: Calculate Derived State During Rendering. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-derived-state.md` | Vendored third-party reference skill file: Subscribe to Derived State. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-functional-setstate.md` | Vendored third-party reference skill file: Use Functional setState Updates. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-lazy-state-init.md` | Vendored third-party reference skill file: Use Lazy State Initialization. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-memo-with-default-value.md` | Vendored third-party reference skill file: Extract Default Non-primitive Parameter Value from Memoized Component to Constant. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-memo.md` | Vendored third-party reference skill file: Extract to Memoized Components. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-move-effect-to-event.md` | Vendored third-party reference skill file: Put Interaction Logic in Event Handlers. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-no-inline-components.md` | Vendored third-party reference skill file: Don't Define Components Inside Components. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-simple-expression-in-memo.md` | Vendored third-party reference skill file: Do not wrap a simple expression with a primitive result type in useMemo. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-split-combined-hooks.md` | Vendored third-party reference skill file: Split Combined Hook Computations. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-transitions.md` | Vendored third-party reference skill file: Use Transitions for Non-Urgent Updates. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-use-deferred-value.md` | Vendored third-party reference skill file: Use useDeferredValue for Expensive Derived Renders. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/rerender-use-ref-transient-values.md` | Vendored third-party reference skill file: Use useRef for Transient Values. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-after-nonblocking.md` | Vendored third-party reference skill file: Use after() for Non-Blocking Operations. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-auth-actions.md` | Vendored third-party reference skill file: Authenticate Server Actions Like API Routes. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-cache-lru.md` | Vendored third-party reference skill file: Cross-Request LRU Caching. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-cache-react.md` | Vendored third-party reference skill file: Per-Request Deduplication with React.cache(). | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-dedup-props.md` | Vendored third-party reference skill file: Avoid Duplicate Serialization in RSC Props. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-hoist-static-io.md` | Vendored third-party reference skill file: Hoist Static I/O to Module Level. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-no-shared-module-state.md` | Vendored third-party reference skill file: Avoid Shared Module State for Request Data. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-parallel-fetching.md` | Vendored third-party reference skill file: Parallel Data Fetching with Component Composition. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-parallel-nested-fetching.md` | Vendored third-party reference skill file: Parallel Nested Data Fetching. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/react-best-practices/rules/server-serialization.md` | Vendored third-party reference skill file: Minimize Serialization at RSC Boundaries. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/refactoring-ui/SKILL.md` | Vendored third-party reference skill file: Audit and fix visual hierarchy, spacing, color, and depth in web UIs. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/refactoring-ui/references/accessibility-depth.md` | Vendored third-party reference skill file: Accessibility Deep Dive. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/refactoring-ui/references/advanced-patterns.md` | Vendored third-party reference skill file: Advanced Design Patterns. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/refactoring-ui/references/animation-microinteractions.md` | Vendored third-party reference skill file: Animation & Microinteractions. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/refactoring-ui/references/data-visualization.md` | Vendored third-party reference skill file: Data Visualization Design. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/refactoring-ui/references/theming-dark-mode.md` | Vendored third-party reference skill file: Theming & Dark Mode Design. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/SKILL.md` | Vendored third-party reference skill file: Manages shadcn components and projects — adding, searching, fixing, debugging, styling, and composing UI, including chat interfaces. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/cli.md` | Vendored third-party reference skill file: shadcn CLI Reference. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/customization.md` | Vendored third-party reference skill file: Customization & Theming. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/design-system-page.md` | Vendored third-party reference skill file: The Design System Page. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/design-system.md` | Vendored third-party reference skill file: Design Systems from DESIGN.md. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/mcp.md` | Vendored third-party reference skill file: shadcn MCP Server. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/registry.md` | Vendored third-party reference skill file: Registry Authoring and Addresses. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/rules/base-vs-radix.md` | Vendored third-party reference skill file: Base vs Radix. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/rules/chat.md` | Vendored third-party reference skill file: Chat & Messaging. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/rules/composition.md` | Vendored third-party reference skill file: Component Composition. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/rules/forms.md` | Vendored third-party reference skill file: Forms & Inputs. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/rules/icons.md` | Vendored third-party reference skill file: Icons. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/shadcn/rules/styling.md` | Vendored third-party reference skill file: Styling & Customization. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/system-design/SKILL.md` | Vendored third-party reference skill file: Design scalable distributed systems using structured approaches for load balancing, caching, database scaling, and message queues. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/system-design/references/building-blocks.md` | Vendored third-party reference skill file: Building Blocks of Scalable Systems. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/system-design/references/common-designs.md` | Vendored third-party reference skill file: Common System Designs. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/system-design/references/database-scaling.md` | Vendored third-party reference skill file: Database Design and Scaling. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/system-design/references/estimation-numbers.md` | Vendored third-party reference skill file: Back-of-the-Envelope Estimation. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/system-design/references/four-step-process.md` | Vendored third-party reference skill file: The Four-Step System Design Process. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/system-design/references/reliability-operations.md` | Vendored third-party reference skill file: Reliability and Operations. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/tailwind-design-system/SKILL.md` | Vendored third-party reference skill file: Build scalable design systems with Tailwind CSS v4, design tokens, component libraries, and responsive patterns. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/tailwind-design-system/references/advanced-patterns.md` | Vendored third-party reference skill file: Tailwind Design System: Advanced Patterns. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/tailwind-design-system/references/details.md` | Vendored third-party reference skill file: tailwind-design-system — detailed patterns and worked examples. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/threejs-fundamentals/SKILL.md` | Vendored third-party reference skill file: Three.js scene setup: scenes and fog, cameras, the renderer and tone mapping, the animation loop, object hierarchies, transforms, core math and cleanu. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/threejs-performance/SKILL.md` | Vendored third-party reference skill file: Measuring and optimizing three.js speed: render statistics, memory counters, fewer draw calls via merging, instancing, batching and LOD, fill rate, sh. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/threejs-react-three-fiber/SKILL.md` | Vendored third-party reference skill file: Building three.js scenes from React with React Three Fiber and drei: renderer setup, the scene graph as JSX, frame-loop, state and loading hooks, poin. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/threejs-shaders/SKILL.md` | Vendored third-party reference skill file: Custom GLSL vertex and fragment shaders: uniforms, varyings, shader chunks, extending built-in materials, instanced attributes, plus the same effects . | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
| `skills/webapp-testing/SKILL.md` | Vendored third-party reference skill file: Toolkit for interacting with and testing local web applications using Playwright. | deleted (vendored; recoverable from git history, catalogued in docs/reference-skills.md) |
