# Provider quota verification and implementation

## Automatic Claude session renewal (October 22, 2026)

Claude Code access tokens expire routinely. JARVIS no longer stops at "sign-in expired":

- When the saved token has expired, or the usage endpoint rejects an unexpired token with HTTP 401, `jarvis/claude_auth.py` starts the native `claude` CLI once with only an SDK `initialize` control request, then closes stdin. Claude Code renews its own session during startup. JARVIS sends no prompt, starts no model turn, runs no login command, and never writes credentials itself.
- The child runs in `--safe-mode` with no tools, no MCP servers, no Chrome, no session persistence, an empty temporary working directory, a 20-second timeout, and alternate auth environment variables (API keys, base URLs, Bedrock/Vertex switches) removed so only the existing subscription sign-in is used.
- After the CLI exits, JARVIS rereads the credentials file and requires a new unexpired access token. A clean exit alone is not trusted. A 401 gets at most one renewal; a token that is unchanged afterwards is reported as a rejected sign-in instead of retrying in a loop.
- Concurrent checks share one CLI start, and each config directory gets a 60-second cooldown, so a revoked sign-in cannot spawn a process on every dashboard refresh.
- Renewal failures (timeout, missing CLI, revoked sign-in, missing refresh token) are sanitized and shown in the Credits panel with "run claude auth login". The local token tally remains visible.
- The quota cache is invalidated when the credentials file changes (for example, a sign-in in another window) or when a cached window's reset time passes. The panel shows when the provider data was actually fetched, not when the cache was read.
- Fixed on October 22: when the CLI rewrote the credentials file but left them unusable, JARVIS mistook its own rewrite for a concurrent sign-in. That hid the real "run claude auth login" message and retried every refresh. The CLI's rewrite is now tracked whether renewal succeeds or fails.

What still needs a person: a revoked or logged-out account, or one whose refresh token has expired, needs `claude auth login`. JARVIS reports this clearly; it cannot sign in for you.

Validation: `tests/runtime/test_quota_renewal.py` (10 scenarios through the real collector with a fake CLI renewal) and `tests/security/test_claude_auth.py` (7 scenarios with fake child processes covering protocol, timeout reaping, cooldown, concurrency and redaction). The full suite passed **293 of 293** tests, all 34 Python modules parsed, and all 8 browser JavaScript files passed `node --check`. A live read-only check at **15:30 UTC (11:30 AM EDT)** returned `source=live` for both providers: Claude 27% five-hour / 75% weekly remaining, Codex 0% five-hour / 31% weekly remaining.

## Earlier checks (October 21)

**Claude successfully renewed and verified October 22, 2026, 02:32:15 UTC (October 21, 10:32:15 PM EDT).** [Sanitized Claude snapshot](audit-evidence/claude-quota-renewed.json).

- **Claude Pro:** 6% used / **94% remaining** in the five-hour window; 14% used / **86% remaining** weekly. Extra usage is **disabled**. Five-hour reset: October 22 at **3:19:59 AM EDT**. Weekly reset: October 25 at **12:59:59 PM EDT**.
- Opening the installed CLI with `claude --safe-mode --ax-screen-reader "/usage"` allowed Claude Code to renew its own existing session. Its usage screen and the JARVIS collector then agreed. The CLI reported zero input/output tokens and $0 session cost. No new browser login, purchase, or inference was needed. At that time the JARVIS collector did not renew sessions; it now does (see above).
- This resolves the expired-token result below. Merely running `claude auth status` reported logged in but did not restore quota access; `/usage` was the useful verification step. If renewal fails in a future session, use the CLI's normal sign-in flow. See [Claude authentication documentation](https://code.claude.com/docs/en/authentication).

The preceding combined check started **October 22, 2026, 02:28:39 UTC (October 21, 10:28:39 PM EDT)**. [Sanitized snapshot](audit-evidence/provider-quota-snapshot.json).

- **Codex Plus:** 11% used / **89% remaining** in the five-hour window; 51% used / **49% remaining** weekly; **0 provider credits**. The five-hour reset is October 22 at **3:24:36 AM EDT**; weekly reset is October 27 at **2:32:48 AM EDT**.
- **Claude at that earlier check:** the existing CLI access token had expired, so the collector made no authenticated usage request and showed the local token tally instead. That check alone could not establish current quota. The subsequent CLI renewal above resolved this.

These readings supersede the midday figures below. The earlier Claude reading remains historical evidence only; its five-hour reset has already passed and cannot establish current availability.

## Earlier successful account snapshot

Both providers were verified on October 21, 2026, at **16:07:03 UTC (12:07:03 PM EDT)**. These are point-in-time account readings, not promised future availability.

| Provider / plan | Window | Used | Remaining | Reported reset (UTC) | Reset in New York (EDT) |
| --- | --- | ---: | ---: | --- | --- |
| Claude Pro | Five hours | 97% | **3%** | Oct 21, 17:59:59 | Oct 21, 1:59:59 PM |
| Claude Pro | Weekly | 13% | **87%** | Oct 25, 16:59:59 | Oct 25, 12:59:59 PM |
| Codex Plus | Five hours | 100% | **0%** | Oct 21, 18:12:59 | Oct 21, 2:12:59 PM |
| Codex Plus | Weekly | 48% | **52%** | Oct 27, 06:32:48 | Oct 27, 2:32:48 AM |

Claude reported **extra usage disabled**. Codex reported a credit balance of **101.7489250000 provider credits** during that midday check. An earlier check around 16:04 UTC returned 252.5924250000, so the balance was changing during active work. Neither number is a dollar balance, and exhausting the included five-hour window does not by itself establish that every paid-credit request is blocked.

## What the figures mean

Claude subscription percentages cover the account's plan allowance across Claude surfaces. Local Claude Code token totals cannot establish the remaining subscription allowance, and context length is another separate limit. Official help describes five-hour and weekly usage windows and factors that influence usage. [Claude usage and length limits](https://support.claude.com/en/articles/11647753-how-do-usage-and-length-limits-work)

Claude Code's `/usage` command and [Settings > Usage](https://claude.ai/settings/usage) are the user-facing places to check current plan bars. Local dollar estimates are not authoritative billing figures. Usage credits, subscription windows, and API billing should remain distinct. [Claude Code cost and usage documentation](https://code.claude.com/docs/en/costs)

OpenAI documents `account/rateLimits/read` in the Codex app-server protocol, including usage percentages, window duration and reset timestamps. That is the supported protocol for future app-server integration. [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server)

## Changes

`jarvis/credits.py` now reads actual quotas before falling back to local logs:

- Claude: authenticated GET to `https://api.anthropic.com/api/oauth/usage`, using the existing Claude Code sign-in. This first-party endpoint and its OAuth header were confirmed in the installed Claude executable and successfully exercised on this account.
- Codex: authenticated GET to `https://chatgpt.com/backend-api/wham/usage`, using the existing Codex ChatGPT sign-in. This endpoint was confirmed in the installed Codex executable and successfully exercised on this account.
- Both direct HTTP endpoints are **internal compatibility surfaces**, not a promise of a stable public API. No inference request, login, purchase, or configuration change is performed. Expired Claude sessions are renewed only through the CLI (see above); JARVIS never writes credentials. An unavailable endpoint leaves the existing local/manual fallback usable.
- Fixed HTTPS destinations, refused redirects, an eight-second request timeout, bounded response reads, sanitized errors, and a shared lock with 60-second success/failure caching protect credentials and prevent repeated concurrent usage requests. The existing panel cache remains 120 seconds by default. A forced panel refresh still respects the shorter endpoint cache.
- The existing dashboard data contract is preserved: live source, numeric percentage meters, reset timestamps, plan facts and a remaining-percentage headline. Unknown provider window names are not assigned guessed meanings. Extra-usage spending is not presented as a prepaid balance.
- Expired Codex log windows are no longer invented as 100% available. Their previous percentage becomes a historical fact with current usage marked unknown; no misleading zero-percent meter is emitted.
- The Claude hourly token chart only reads local logs. Removed/aged log cache entries are discarded; future-dated records and malformed numeric usage rows are excluded.

Only allowlisted quota fields were printed in the investigation. Credentials remain in their original files and in transient request memory, with no copies in the report or application state. The implementation currently supports the usual Windows CLI credential-file locations. `CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honored. Accounts using OS keychains fall back to local data and the provider dashboard; their credentials are not extracted by this change.

## Validation

- `python -m unittest discover -s tests -p test_provider_limits.py -v`: **12 passed**, offline with synthetic credentials and mocked provider responses.
- `python -m unittest discover -s tests -p test_ui.py -v`: **16 passed**, including the corrected stale-reset expectation.
- Direct live smoke check of the implemented Claude and Codex collectors returned `source=live`, `status=ok` and the sanitized readings above.

Tests cover authentic quota normalization, credits versus dollars, expired sign-ins without refresh, cached failures, local fallback, no quota request from the hourly chart, invalid percentages, malformed window shapes, redirect refusal, error redaction, stale log handling and cache pruning.

No fixed token/message ceiling was inferred for either plan. The providers returned percentages and reset windows; those are the account-specific limits actually observable here.
