# Releasing a code change

*How a tested tree reaches the running main and its nodes.*

Python loads on the next start of that process. Files under `jarvis/web/` load when the page is reloaded. Voice unit changes need the unit regenerated, then `systemctl --user restart jarvis-voice`.

Order:

1. Push a green commit to origin/main. The hub cuts a release on its own (`release.py`: full suite on a `git archive` of origin/main) and nodes pull it automatically. A failing test is re-run once by id; the release is blocked only if it fails again (`code-release-failed.json` holds both attempts).
2. Restart the main's process (console, `jarvis-cluster`, voice) so the hub is the new code.
3. `jarvis cluster update <node>` is optional: it only speeds step 1 up (cut early and wait). `--rollback` swaps back to the previous copy `deploy.py` kept.
4. Leave an offline node alone. Do not retry it: it pulls on reconnect. Update the Dell only when David says it is available.

`jarvis cluster doctor` shows who holds the release lock (pid, age, command; a dead pid clears itself) and any online node behind the release for over 30 min (`drift.py`; one notification per node and release). Offline nodes show as "offline, will pull".

A node whose fingerprint differs from the main refuses jobs. Do not enroll again because a DHCP address changed. Release staging skips nested git checkouts.

The hub always serves a versioned bundle (`version` plus `vsig`). A node that has already installed one refuses a bundle with no version. `LEGACY_UNVERSIONED_UNTIL` in `jarvis/deploy.py` is 2027-01-15 00:00:00 UTC (`1799971200`). Until then, a node that has never recorded a version still accepts an unversioned bundle, and the hub still sends the legacy `signature` field so that node can update. After that instant it refuses. On the node, look for `cluster-code-version.json` in that machine's `.jarvis/` (the main does not have this file). When every node has it, the legacy branch can be deleted. The step-up test is `tests/cluster/test_bundle_upgrade_path.py`.

The continuity gateway is a different ship. `python -m jarvis.continuity.gateway_deploy` copies the package to the gateway host and is not `cluster update`. Flags and the restart rule are in [continuity.md](continuity.md).

Restart commands for the Arch units are in [operations.md](operations.md).
