State: done
Done:
- Senior SWE read required contracts and every WS status; all code/test edits delegated to Grok. Manual code/test edits (pre-rule or later): none. Only this status document manually edited.
- jarvis/cluster_setup.py join/leave: fresh disk role under managed config lock; refuse MAIN and malformed enabled identities; staged private key/note with ownership-aware rollback on config drift; preserve concurrent redemption. Cleanup deletes canonical owned credentials/pins/launchers, preserves unrelated state/symlink targets and permanent tunnel lock inode.
- Real subprocess cluster_tunnel.run tests: cooperative marker stop, second-worker lock exclusion, lock inode preservation, stop timeout retaining marker/status/credentials, join timeout refusal, stale heartbeat with held lock. Fake SSH subprocess only; no actual SSH host or services contacted.
- Real separate Hub/node processes: loopback HTTP + SQLite invite redemption, invite reuse rejected, revoke invalidates access, leave and fresh invite/rejoin succeeds. SSH probe/address/host-key/authorized-key location/launch boundaries simulated; ssh-keygen temp files only.
- jarvis/cluster_cli.py run configure: locked latest config, refuses MAIN change; legacy JSON bundle join/enroll: fresh-role checks and local credential rollback. No edits to WS1 main/handoff functions.
- jarvis/autostart.py install off: Windows/macOS/Linux entries removed even if supervisor cleanup fails; failures reported. Supervisor tests use fakes only.
- jarvis/platforms.py: membership OS helpers; exact Windows private-key DACL script with data argv; newline rejection; unique atomically published detached launchers; shared SSH-key file lock. Native Windows assertions modeled, not kernel-proven.
- jarvis/fleet/readiness.py + cluster_setup.doctor + cluster_cli doctor rendering: runtime/sqlite, credential presence (no bodies/paths), local vault/state readability, code fingerprint comparison. Uses canonical vault path with tilde expansion. main_readiness exposed in JSON and TTY; ready/ai_ready remain false, no provider calls and no authorization claim.
- Final wrap-up focused run (Sonnet 5.5): 218/218 OK in 42.3s, incl. test_ws2_leave_retry 4 and test_cluster_roles. Earlier parent run: 211/211 in 42.014s. Modules/counts: test_ws2_membership 22; test_ws2_doctor 4; test_cluster_setup 18; test_config_writers 26; test_ws2_doctor_cli 6; test_ws2_readiness 16; test_ws2_platforms 23; test_ws2_pairing_races 7; test_integrated_fleet_gaps 19; test_fleet_management 70.
- Luna cleared readiness, platform repairs, doctor TTY rendering and config-writer fixtures. Luna's four remaining membership blockers were repaired and parent-reviewed/tested; no fresh re-audit after David's wrap-up order. Legacy pairing latest batch also parent-reviewed/tested without a new Luna under that order.
Doing: nothing. Wrap-up by Sonnet 5.5 (Codex quota ran out); no new code edits, no Grok round needed.
Next: lead runs full suite on quiet tree; David runs `jarvis cluster doctor` per candidate. Open items under "Cannot be done here / Next".
Needs:
- WS1/WS5 retain fleet-manage.lock for role/config publication; WS1 owns authority disable_participant API and WS5 authenticated gateway removal wiring. WS2 Broker revoke is the legacy credential layer, not gateway revocation.
- WS1 may edit Fleet transfer/inventory projection functions only; membership edits preserved. WS3 brain transport uses existing deploy SSH adapter; forward-only delivery seam remains cross-WS work.
Decision: No new dependencies or config defaults. No deployment/cutover authorized.
David runs: Nothing now. After lead review/full suite, run `jarvis cluster doctor` locally on each candidate. Native service/SSH/Windows ACL checks require separate owner-directed acceptance. This work ran no service restart, cluster update, Dell/cloud/tailscale action, production config/vault write, commit or push.
Cannot be done here / Next:
- Leave-retry quick fix (cluster_cli leave reaches cluster_setup.leave after timeout-disabled cluster) is in tree and green (4 tests); no fresh audit (Luna unavailable, Codex quota; parent-read only, not re-audited via Sonnet per wrap-up order).
- Strict requirement that ALL OS branches live only in platforms.py/autostart.py is unfinished: cluster_setup retains compatibility wrappers and authorized-key mode branches; release.py retains process/nice branches; deploy.py retains remote restart OS mapping. Deferred by explicit wrap-up order; no new extraction batch started. No new fixed machine-role assumptions were added.
- Live Dell, Windows/macOS autostart, PowerShell ACL/OpenSSH/Explorer, actual SSH tunnel and provider credential validity/model readiness unavailable/prohibited. Local model tests do not prove those environments. Readiness checks presence/readability only, brain freshness/handoff remain unverified. A node compares authenticated hub status code; independent tested-release provenance is not established by that alone.
- Voluntary leave is local unlink/disable. It deliberately requires MAIN-side `jarvis cluster revoke NODE` to revoke the server credential; existing running node agent must be stopped separately. Authenticated self-revoke endpoint and managed agent shutdown are not implemented here (Hub belongs WS1; no live service actions authorized).
- Multi-file join rollback handles tested local exceptions, not crash-atomic distributed commit; compensator write failure/crash recovery still lacks a durable recovery journal. No global machine-agnostic operational or promotion readiness claim.
- Final repaired membership/legacy pairing code lacks a fresh Luna audit due explicit start-nothing-new wrap-up. Full suite belongs to lead on quiet shared tree.
Files changed by WS2:
- jarvis/autostart.py
- jarvis/cluster_cli.py (run configure/doctor/legacy bundle join/enroll/leave only; WS1 owns other diff)
- jarvis/cluster_setup.py
- jarvis/platforms.py
- jarvis/fleet/readiness.py (new)
- tests/cluster/test_config_writers.py
- tests/cluster/test_ws2_membership.py (new)
- tests/cluster/test_ws2_doctor.py (new)
- tests/cluster/test_ws2_doctor_cli.py (new)
- tests/cluster/test_ws2_readiness.py (new)
- tests/cluster/test_ws2_platforms.py (new)
- tests/cluster/test_ws2_pairing_races.py (new)
- tests/cluster/test_ws2_leave_retry.py (final quick-fix batch)
- docs/design/mobile-continuity/swarm/WS2.md
