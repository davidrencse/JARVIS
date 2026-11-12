State: done
Done: (wrap-up by Sonnet 5.5 after Codex quota stop; no code edits made in wrap-up, no Grok round needed: tree was green)
- jarvis/continuity/gateway.py: main/load_gateway_config/serve_gateway; loopback-only CLI; TLS serve (errors bind no listener, writer released); durable directory + nonce digests; offline backup/restore with fresh authority identity; writer-sidecar/symlink/destination-race guards; shutdown with incomplete POST.
- Luna audit findings (enrollment atomicity, restore destination race) repaired by Grok coders; covered by tests/continuity/test_drill_gateway_audit.py.
- Drill harness: tests/continuity/test_drill_process.py (real child processes on loopback), test_drill_gateway.py, test_drill_gateway_audit.py, test_drill_handoff.py. Seam rows are expectedFailure via SeamMixin/SeamGap, each names its owner.
- Focused run (2026-11-12): test_drill_gateway, test_drill_gateway_audit, test_drill_handoff, test_drill_process, test_production_fences, test_gateway, test_transient_relay = 77 tests, OK, expected failures=3, 74.8 s. test_drill_handoff re-run 3x: 4 tests OK (3 expected failures) each time, so the earlier network-loss proxy-close port-check failure did not reproduce (treat as a possible flake; watch it).
- No drill/gateway process left running afterwards (pgrep compared with pre-run list; remaining jarvis processes are the real clipboard/voice/dropzone/lan-mouse services plus other workstreams' temp brain-export children, untouched).
Doing: nothing (wrap-up order: no new work).
Next: see "Cannot be done here / Next".
Needs: WS1: AuthorityStore.disable_participant reachable through ContinuityGateway/AuthorityClient (authenticated revoke route; fail closed for active MAIN), then WS5 un-xfails test_participant_removal_is_a_ws1_seam. WS4: signed phone claim envelope + network-capable gateway session/transient relay (no cookie or X-Forwarded-For trust), then un-xfail test_phone_cookie_is_a_ws4_seam. WS3/WS1: MainRuntime.release_before_suspend / SafeHandoff must call brainsync.handoff with real receive/freeze and validate vault+memory on destination, then un-xfail test_brain_transfer_is_a_ws3_seam. WS1: use AuthorityClient + AuthenticatedPort for signed production bootstrap; gateway launch: python -m jarvis.continuity.gateway serve --config <dedicated gateway JSON>.
Decided 2026-11-12 (David): Oracle Always Free ARM VM on tailnet; runbook docs/handbook/continuity.md
David runs: nothing yet. Runbook was never written (see below); no live actions were performed.
Cannot be done here / Next:
- docs/handbook/continuity.md and deploy/ or scripts/continuity/ runbook scripts: not written. The deploy/docs task never launched (parent_not_active) and wrap-up forbids new work. Next: write the runbook (gateway JSON, systemd user unit, TLS cert, backup/restore commands) once the gateway host Decision is made.
- Automatic promotion: auto_failover=true fails closed before database open; no policy identity/controller configured. Needs authenticated readiness/canary/brain hook and explicit deployment acceptance from David.
- Live gateway host choice/provisioning, tailscale configuration, Dell and physical suspend/wake checks: need David; prohibited by charter.
- Drill rows blocked on other workstreams (see Needs): participant removal (WS1), same-phone-session cookie continuity (WS4), brain bytes following new MAIN (WS3/WS1).
- Full suite not run (lead runs it on a quiet tree).
- Luna audit of the final repair round not run (Codex quota); the Senior SWE read the repair diff and the combined run passed. Audit via Sonnet not launched under wrap-up order.

Drill pass/fail table (real multi-process, loopback, no live services):
| Drill | Result |
|---|---|
| A release → B promotion → stale A refused (signed status + transient relay) | PASS (test_release_recovery_phone_route_network_loss_and_restart) |
| …same phone session works after promotion | EXPECTED FAIL: WS4 cookie/session seam (test_phone_cookie_is_a_ws4_seam) |
| …brain storage follows B | EXPECTED FAIL: WS3/WS1 seam (test_brain_transfer_is_a_ws3_seam) |
| Gateway restart mid-session | PASS (same test; plus test_drill_gateway restart/old-nonce-refused, SIGTERM during incomplete POST) |
| A–gateway network loss | PASS (same test via proxy; 3 reruns green) |
| C join | PASS (test_participant_removal_is_a_ws1_seam, join half) |
| C removal | EXPECTED FAIL: WS1 AuthorityStore.disable_participant not reachable via gateway (same test) |
| Gateway backup/restore (fresh pin, no revived admissions/lease), TLS, writer-busy | PASS (test_drill_gateway, test_drill_gateway_audit) |

Review gates for pending batch: all six met by the passing tests above, except gate 6 rows (phone cookie, brain movement, membership removal), which stay separate expected-failure rows and are never inferred from signed status requests.
