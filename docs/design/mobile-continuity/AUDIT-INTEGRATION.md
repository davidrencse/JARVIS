# Independent implementation audit — integration evidence

Status: ongoing; Grok archive/backend/usability audits pending. No production edits or restarts
performed as part of this audit.

## Full suite

`JARVIS_NO_MODELS=1 .venv/bin/python -m unittest discover -s tests -q`
ran3334 tests in243.004s: FAILED(errors=1,skipped=7,expected failures=1).
Log:`/tmp/jarvis-independent-audit-suite.log`.

Error: `web.test_ui_security.ConsoleSecurity.test_oversize_body_gets_413_not_reset`,
BrokenPipeError while client sends its upload. Isolated single run passed; repeated20-case
suite reproduced1error. Do not replace the failing fullsuite result with the isolated pass.

Root cause evidence: ui.MAX_AUDIO=2,880,000; test body=MAX_AUDIO+1000. Handler._reject_body
sends413 then Handler._drain consumes at mostDRAIN_CAP=1,048,576 beforeclosing. Unread incoming
bytes can break the client'ssend beforeit reads the413. Boundedrejection is deliberate;
predictable error delivery for uploads justabove the supported limit is currently unreliable.
Suggested correction: retain bounded time/bytes, drain the ordinary near-limit upload class
fully and explicitly test stalled/enormous declared bodies without allocatingthem. This is
unrelated to safely accepting oversized data; the input must remain rejected.

## Live read-only evidence

`/v1/status`,`/api/fleet`,`/api/fleet/vault` returned200 with the local ownerclient.
Zenbook MAIN and Dell online; MAIN transferblocked. Archive capture_complete true,pending0,
held0,errors0,statecurrent; GitHubremoteconfigured. Cluster andvoice servicesactive.
Summary:`/tmp/jarvis-independent-live-audit.json`. These healthchecks do not prove realnodejoin,
remoteinput/camera or MAINfailover. No nodecredentials changed and no realeffects triggered.


## Grok backend audit independently reproduced by parent

Parent reran `/tmp/jarvis-fleet-backend-audit.py`:11/11 expected bug/control outcomes
reproduced. These PASS labels mean the reproduction matched, not that the implementation is
correct. See AUDIT-FLEET-BACKEND.md for evidence and proposed regressions.

Confirmed open defects: shared tool-log concurrency can lose completed effects and permit
planner fallback replay; MAIN config can change during invite and still issue credentials;
late code-signing-key failure leaves pending credentials without returning a code; in-memory
request dedup is lost after restart and explicit replacement can repeat the same request ID;
cloud eligibility wording incorrectly calls enrollment gateway-only.

No production fix or restart performed during audit. Archive and UI audits still pending.


## Grok archive audit independently reproduced by parent

Parent reran `/tmp/jarvis-obsidian-audit.py`: all four defect scenarios and the control
scenario reproduced on temporary data. Existing live records were not altered. Confirmed:
pre-activation rows can remain unarchived despite current status; oversized held revisions
block later revisions for that record; merging two task ledgers fails strict restore on event
sequence collisions; filename/tombstone disagreement can validate green then fail restore.
Stale documentation also contradicts activated C2 behavior. See AUDIT-OBSIDIAN.md.

These are archive-completeness/recovery defects, not evidence of source SQLite data loss.
Do not discard original databases or claim current means full historical parity. Any repair
for oversized revisions must preserve the original pending payload or explicitly report an
intentional lossy policy; silently dropping it is not an acceptable lossless fix.
No code fixes, live archive migrations or service restarts have been performed in this audit.
