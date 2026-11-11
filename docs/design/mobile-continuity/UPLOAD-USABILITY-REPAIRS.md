# Upload / usability repairs (2026-11-11)

Scope: three audit leftovers from `AUDIT-INTEGRATION.md` / `AUDIT-FLEET-USABILITY.md`.
Production changes are **not activated**: `jarvis/ui.py` needs a JARVIS restart (parent to approve);
`jarvis/web/*` is live on reload.

## 1. Flaky `test_oversize_body_gets_413_not_reset`

**Cause.** An over-limit POST (`MAX_AUDIO + 1000` bytes) got a 413, then the server drained only
1 MiB and closed. Unread bytes left in the socket made the kernel send RST, so the client sometimes
hit `BrokenPipe` before reading the 413 (~1 in 20 runs; 1/100 in a direct loop).

**Fix** (`jarvis/ui.py`, `Handler._reject_body` / `_refuse` / `_drain` / `_content_length`):
send the response, half-close the write side (`SHUT_WR`), then read and discard the rest of the body
within hard bounds, then close. Nothing read is kept.

| Bound | Value | Why |
|---|---|---|
| `REJECT_DRAIN_BYTES` | `MAX_AUDIO + 1 MiB` | an ordinary near-limit body is always fully drained, so the 413 is predictable |
| `REFUSE_DRAIN_BYTES` | 64 KiB | unauthenticated/forbidden bodies get far less |
| `REJECT_IDLE_S` | 2 s | no data for 2 s ends the drain (stalled client) |
| `REJECT_DEADLINE_S` | 8 s | hard wall-clock cap (slowloris dribble) |
| `DRAINS` | `BoundedSemaphore(8)` | at most 8 handler threads drain at once; others close at once |
| read block | 64 KiB | memory stays flat whatever `Content-Length` says |

Unchanged guarantees: oversized content is never accepted or passed to a handler; a declared length
far beyond the cap is rejected from the header alone with no allocation (client may see a reset,
that is the accepted trade-off for abusive sizes). `_content_length()` returns a bad value for
`Transfer-Encoding`, conflicting or non-digit lengths (400 "Bad Content-Length.", connection closed);
lengths over 18 digits clamp to 10**18 so they are refused cheaply.

**Tests** `tests/web/test_ui_body_rejection.py` (13): 40 repeated near-limit uploads all get 413 and
are fully drained; huge `Content-Length` (10**15, 6000 digits) with no body; stalled and dribbling
client bounded by the deadline; concurrent drains capped, semaphore released; bad/conflicting/chunked
lengths 400; unauthorized body 403 and no action executed; oversize JSON never reaches a handler;
bounds coherent (`REJECT_DRAIN_BYTES > MAX_AUDIO`). The original test in `tests/web/test_ui_security.py`
is unchanged.

**Repeated upload result** (100 near-limit uploads, fresh connection each, scratch loop):
old 1 MiB cap **1/100** failed; new cap **0/100**. The module ran 3× clean.

## 2. Light-theme `--ok` / `--warn` contrast

`jarvis/web/porcelain.css:38` (backup `porcelain.css.pre-contrast`). On `#e7e7e9`:

| Token | Old | Ratio | New | Ratio |
|---|---|---|---|---|
| `--ok` | `#2f7a45` | 4.26 | `#2a6e3e` | ≥4.5 |
| `--warn` | `#9a6400` | 4.05 | `#865700` | ≥4.5 |
| `--alert` | `#c0392f` | 4.40 | `#b1342b` | ≥4.5 (also failed, darkened too) |

Dark theme tokens are unchanged. Colour still means state only. Automated check:
`tests/web/test_theme_contrast.py` (7 tests) parses the real CSS cascade, computes WCAG ratios on
every actual surface (bg, card, card-strong, sunk, hover) and fails on the old values.

## 3. Capability acknowledgement wording

`jarvis/web/fleet.js` (backup `fleet.js.pre-ackcopy`): the sentence showed raw ids ("camera go beyond").
Now it uses the capability title, singular/plural agreement, 80-char cap, text via `textContent`
(hostile labels stay plain text): "I understand that Webcam view goes beyond the default permissions
and gives this computer more control…", "A and B go … give", "A, B and C go … give".
Gating and the ids sent to the API are unchanged. Tests: two new scenarios in
`tests/web/web_fleet_test.cjs` (titles/plurals/gating; hostile + long titles).

## Verification run

- `tests.web.test_ui_body_rejection`, `test_ui_security`, `test_web_fleet`, `test_theme_contrast`: 44 OK.
- `tests/mobile` discover: 191 OK.
- `tests/web` discover: 172 run, **1 fail** (see below, not mine).
- No full suite (per instructions), no visual screenshot batch run (DOM + computed-contrast tests cover it).

## Known unfixed / outside ownership

- `tests.web.test_sysview.test_every_module_belongs_to_a_station` fails: `['hostmeter']` is not in
  `jarvis/sysview.py` stations. New module from the inventory/stats owner; they must register it.
- Light `--ink-3` (`#66666e`) is 4.28 on sunk and 4.16 on hover surfaces (<4.5 for small text);
  not touched (outside ok/warn scope).
- `Expect: 100-continue` bodies are not rejected before the client sends them; the drain bounds still apply.
- A declared length beyond the drain cap can still produce a client-side reset by design.
- `jarvis/ui.py` has other agents' uncommitted edits; mine are limited to the body-rejection helpers.


Parent review: bounded half-close/drain implementation inspected; parent independently ran
UI body rejection, UI security, fleet DOM and computed theme contrast suites:44 tests OK
in3.152s. No service restart or production activation of Python fix yet. Full integration
awaits parallel archive/backend/battery changes; hostmeter system-map registration remains
a known integration issue until resolved by its owner. Oversized declared bodies beyond the
byte/concurrency/time bounds may still reset by deliberate bounded policy.
