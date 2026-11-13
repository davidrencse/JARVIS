# Lan Mouse reboot recovery

Implementation investigation, 2026-11-13. Code evidence, not a diagnosis from the Dell:
no live node, scheduled task, service, cursor, or Lan Mouse config was changed. Line
numbers refer to the working tree and may move with concurrent work.

## Ranked causes and paths

1. **Startup is conditional and delayed, rather than a persistent Windows service.**
   `jarvis/cluster.py:1625` waits 60 seconds before the first guard reading; two together
   readings are required. `_poll_loop` only creates that thread with `off_when_apart`
   enabled. `sharedinput.py:1231` additionally requires enabled + autostart and a readable
   boot. Login, not merely a powered-on Dell, starts the node via `autostart.py`'s Startup
   VBS. Missing/stale node startup, failure to log in, disabled settings, an unavailable
   main/network or unknown readings all leave it off. These are distinct from deletion.
   No reboot/guard path disables or deletes the elevated tasks or changes enabled=false.
   The code's explicit task deletion is `unelevate` (`sharedinput.py:606`), also called by
   elevate's unsafe-installation cleanup; neither is invoked by the guard.
2. **Windows launcher accepted, process absent or short-lived.** `sharedinput.py:359`:
   successful schtasks /Run only means accepted; missing/disabled tasks, a stale admin-only
   binary copy, a task error, startup delay or an early daemon crash can leave it off.
   The old precedence chose Startup VBS through wscript before start-now through Explorer;
   shell/job ownership could kill that child. Fix: prefer the one-shot Explorer launch,
   use Explorer for the legacy VBS too, verify the process with tasklist, and make at
   most three launch attempts. A failed guard start retains recovery debt and retries on
   the next reading. A missing task still permits the existing unelevated fallback, with
   the existing UIPI limitation; it does not grant elevation or install tasks silently.
3. **Main reboot/sleep stops the node as designed.** Two apart readings stop it;
   two together readings bring it back only when stopped_by_guard is retained
   (`sharedinput.py:811`). A manual kill after an automatic restart, a crash after the
   successful start has cleared debt, autostart disabled, or unreadable/unknown network
   readings can leave it off indefinitely. An arbitrary dead process is deliberately
   not restarted indefinitely: that would undo manual stops. If off_when_apart is false,
   the node guard isn't launched at all. Sleep restoration in `jarvis/rest.py` can also
   restore only the units it actually stopped; a missing/failed IP watcher means no main
   guard. Neither watcher nor node lifecycle was changed in this patch.
4. **Boot state contamination and manual-stop ambiguity.** Previously the first guard
   tick could rearm a manual stop made before that tick (the expectedFailure in
   `tests/remote/test_lanmouse_fuzz.py:239`). There was no explicit persisted manual-stop
   intent. Streaks, settled apart and override also survived a boot. Fix: reset streaks,
   settled proximity and stale override on a recognized boot; record explicit stop intent
   before acting; clear only after a verified explicit start or a later boot. A daemon
   already running at the first tick remains a manual override. A failed stop does not
   erase the intent. External Task Manager kills before the very first guard tick cannot
   be distinguished from 'not started yet'; use `jarvis lanmouse stop` for durable intent.
   A further crash window existed after launch and before clearing recovery debt: the
   next tick could mistake our running process for a manual override. A durable
   pending_start intent now attributes that launch correctly. A stop returning stopped=false
   now becomes stop_failed and retries, rather than turning into a manual override after
   GUARD_GRACE. Both have focused regressions.
5. **Config rewrite can really damage pairing, but reboot does not invoke write_pairing.**
   `sharedinput.py:1270` formerly wrote directly to config.toml (crash/truncation risk),
   accepted empty IPs, and dated backups collided within a second. Fix: validate full TOML,
   peer, fingerprint and nonempty non-Tailscale IPs; lock; unique backup; atomic private
   replacement. Explicit pairing still replaces the requested single-peer pairing.
   Existing unreadable TOML is not silently overwritten. The installed main watcher,
   `/home/jdoe/.local/bin/lan-mouse-peer-ips:43`, independently uses sed -i, has no backup,
   and replaces every matching IP line. It can race a pairing writer and write an
   unsuitable endpoint. The repo replacement `scripts/lan-mouse-peer-ips` delegates to
   `jarvis/lanmouse_peer_ips.py`, which validates before/after, requires exactly the
   expected peer, preserves authorization, rejects empty/VPN addresses, shares the config
   lock and atomically replaces with backup. The installed script was READ ONLY here.
6. **Stale remote pause.** Both remote markers normally expire after 60s, so an ordinary
   crash cannot block forever (`sharedinput.py:907`, `:921`). Previously future mtimes
   stayed live until wall time caught up. Now markers provably from a previous boot are ignored; current-boot markers continue
   blocking starts. Future timestamps remain conservative (blocking), because immediately
   ignoring one after a backward clock adjustment could start during a live session.
   An impossible future marker requires manual investigation; automatic expiry without a
   reliable monotonic session lease would weaken remote-control safety. Files are not unlinked by the guard (avoids racing a live session refresh).
   ControllerPause already adopted only this boot's marker; its session-end restore now
   also honors explicit manual stops and a live inbound remote session. Failed restoration
   retains guard debt. An unreadable marker's existing fail-open behavior is unchanged.
7. **Boot source unavailable / jitter.** `platforms.py:202-237`: Windows already prefers
   stable console logon time (handles Fast Startup), falling back to 64-bit uptime.
   Both failing yields None, so safe per-boot autostart cannot be established. BOOT_JITTER
   deliberately merges times within 120s; an extremely rapid reboot, changing boot sources
   or a large wall-clock correction can confuse this heuristic. No fake 'process start is
   a boot' fallback was added: that would violate manual-stop-sticks on agent restarts.
   Status now identifies boot_unavailable. A native stable login/boot identity is a future
   improvement; the existing platform sources and jitter tests remain in place.

## Invariants and implementation contract

- No automatic start while settled apart, during either remote-control marker, with
  enabled=false, or after an explicit stop in this boot. Manual starts retain the existing
  override semantics until together again; this intentional exception is kept separate
  from automatic-start invariants in the randomized oracle.
- Two consecutive together/apart readings; unknown breaks streaks and makes no action.
- Autostart recovery within two fair readings after a recognized boot, conditional on
  enabled+autostart, working launch, no explicit stop, and no remote session. Permanent
  launch failures or an offline main cannot have unconditional liveness guarantees.
- Preserve fingerprints, same-network routing, release shortcut, Tailscale bans, and
  administrative installation boundaries. No scheduled-task disable/delete in recovery.
- Guard decisions and failure reasons persist and appear under `guard.reason` in
  `jarvis lanmouse nearby` and `jarvis lanmouse status`.
- Pairing and watcher updates validate before replacement and retain a unique backup.

## Tests and limitations

`tests/remote/test_lanmouse_reboot.py` runs 100,000 seeded scenarios against production
_guard with in-memory persistence and fake status/start/stop/remote boundaries. Events
include both boots, main sleep, three reading states, explicit manual actions, failed
starts/crashes, remote markers, settings toggles and validated/rejected config updates.
Each tick checks automatic apart safety, manual-stop safety, no remote start, and config
integrity; a fair suffix checks two-reading boot recovery. The pure watcher rewrite is
exercised in the sweep. Separate regressions exercise real temporary-file persistence,
previous-boot/future markers, atomic-write interruption, preserved backups/authorization,
empty/VPN IP rejection, task retries and manual-stop recording before a failed action.
The older guard/network/XML sweep remains and its manual-stop expectedFailure becomes a
passing regression using explicit intent. Arbitrary external kills, native Win32 APIs,
actual task elevation and Rust behavior require the Dell; no fake test proves those.

Full verification command:

```sh
env $(env | grep -o '^CODEX_[A-Z_]*' | sed 's/^/-u /') JARVIS_NO_MODELS=1 .venv/bin/python -m unittest discover -s tests -t .
```

## David's rollout, after review

No restart or rollout was performed. Python changes need new JARVIS processes to take
effect; the code mismatch policy means the node needs `jarvis cluster update <Dell-id>`
from the main first. Use the actual id from `jarvis cluster roles`; don't keep retrying
an offline node.

On Arch, install the reviewed watcher from the repo (fish syntax):

```fish
cp ~/.local/bin/lan-mouse-peer-ips ~/.local/bin/lan-mouse-peer-ips.pre-reboot-reform
install -m 755 scripts/lan-mouse-peer-ips ~/.local/bin/lan-mouse-peer-ips
```

The existing watcher process must subsequently be restarted by David to use it. Do not
install the watcher alone before the matching Python module has reached that machine.

On the Dell, after login, use a terminal in the JARVIS repo:

```powershell
.venv\Scripts\python.exe -m jarvis lanmouse nearby
.venv\Scripts\python.exe -m jarvis lanmouse status
schtasks /Query /TN "JARVIS Lan Mouse" /V /FO LIST
schtasks /Query /TN "JARVIS Lan Mouse Stop" /V /FO LIST
```

If either task is missing, disabled, points at an old build, or cannot launch, open an
**Administrator** terminal in that repo and run:

```powershell
.venv\Scripts\python.exe -m jarvis lanmouse elevate
```

That operation installs the existing admin-only Program Files copy and may restart Lan
Mouse; David must run it himself. Never repoint the elevated task at LOCALAPPDATA. Read
`%LOCALAPPDATA%\lan-mouse\daemon.log` and the task Last Run Result if three attempts fail.
Check together/apart, manual stop through a full guard interval, reboot, and a Remote
view connection/disconnection on the real computers. If the daemon exists but fails to
exchange input, verify both certificate fingerprints and that both sides run the patched
Rust build; this patch makes no Rust change. UAC secure desktop remains inaccessible.

## Verified results

Final full command above: **4,194 tests in 622.937 seconds; OK, 7 skipped,
2 expected failures**. The former manual-stop expectedFailure now passes normally.
New seeded boot/state/config fuzz: **100,000 scenarios in 32.682 seconds** in that
full run. Runtime includes contention from other agents' suites in the shared workspace.
The four no-real-autostart security checks also passed independently, together with the
system-view module registration check (5 tests). Shell syntax validation and diff whitespace
checks passed. Earlier full-run failures in concurrently changing continuity code and a
then-missing module registry entry are absent from the final successful full run.

No commits, pushes, service restarts, node updates, task installation, live config writes,
or live input actions were performed. The replacement watcher is staged in the repository;
David must install it for the live watcher to gain the safe writer.
